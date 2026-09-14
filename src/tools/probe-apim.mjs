import https from 'node:https';
import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { lstat, mkdir, open, realpath } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';
import { JsonLogsSerializer, ProtobufLogsSerializer } from '@opentelemetry/otlp-transformer';
import { createLogRecord, generateFixture } from './generate-logs-fixture.js';

const repository = fileURLToPath(new URL('../../', import.meta.url));
const privateRoot = path.join(repository, '.local');
const timeoutMs = 30000;
const maxResponseBytes = 65536;
const maxDiagnosticBytes = 4096;
const boundaryBytes = 1048576;
const overlimitBytes = boundaryBytes + 1;
const batchLogRecords = 128;
const maxLogPaddingBytes = 8192;
const keyHeader = 'X-Copilot-Telemetry-Key';
const protobufType = 'application/x-protobuf';
const transportErrors = new Set(['timeout', 'response_too_large', 'network_error']);
const usage = 'Usage: node src/tools/probe-apim.mjs --endpoint https://<host>/otlp ' +
  '--output .local/<new-directory> [--include-other-routes] [--include-boundary]\n' +
  'Keys: APIM_SUBSCRIPTION_KEY (required), APIM_NEGATIVE_KEY and APIM_RETIRED_KEY (optional), environment only.\n' +
  'Sends 12-18 sequential synthetic requests, plus one with --include-boundary; query denial uses a fake key with a valid header.\n' +
  '--include-boundary opts into one exactly 1048576-byte protobuf batch acceptance request (128 synthetic logs).\n' +
  'This is the observed native logs HTTP boundary, not proof of a metrics/traces limit or persistence.\n' +
  'Declared 1048577-byte requests require 413; one small chunked request requires 400 (visible Transfer-Encoding) or 411 (normalized missing length).\n' +
  'Chunked requests are unsupported: neither outcome proves a streamed size bound.\n' +
  'Only application/x-protobuf is permitted; application/json requires 415. Empty bodies require 400 before backend forwarding.\n' +
  'No real URL keys, quota/load tests, or retries.\n' +
  'HTTP results and empty-body admission diagnostics never establish authenticated admission or backend ingestion.';

export function validateOptions({ endpoint, output, includeOtherRoutes = false, includeBoundary = false }) {
  if (typeof endpoint !== 'string' ||
      !/^https:\/\/[a-zA-Z0-9.-]+(?::443)?\/otlp$/.test(endpoint)) {
    throw new Error('Endpoint must be exactly https://<host>/otlp on port 443');
  }
  const url = new URL(endpoint);
  if (!url.hostname || url.username || url.password || url.pathname !== '/otlp' ||
      url.search || url.hash || url.protocol !== 'https:') {
    throw new Error('Endpoint must be exactly https://<host>/otlp on port 443');
  }
  if (typeof output !== 'string' || !output || typeof includeOtherRoutes !== 'boolean' ||
      typeof includeBoundary !== 'boolean') {
    throw new Error('A new private output directory and boolean probe options are required');
  }
  const destination = path.resolve(repository, output);
  if (path.dirname(destination) !== privateRoot ||
      !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/.test(path.basename(destination))) {
    throw new Error('Output must be a new direct child directory of worktree .local/');
  }
  return { endpoint: `${url.origin}/otlp`, output: destination, includeOtherRoutes, includeBoundary };
}

function readKeys(env) {
  const keys = {
    valid: env.APIM_SUBSCRIPTION_KEY,
    negative: env.APIM_NEGATIVE_KEY,
    retired: env.APIM_RETIRED_KEY,
  };
  const supplied = Object.values(keys).filter(value => value !== undefined);
  if (!keys.valid || supplied.some(value =>
    typeof value !== 'string' || !/^[\x21-\x7e]{1,4096}$/.test(value)) ||
    new Set(supplied).size !== supplied.length) {
    throw new Error('Invalid APIM key configuration');
  }
  return keys;
}

async function assertPrivateDirectory(directory) {
  const stat = await lstat(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o700 ||
      (process.getuid && stat.uid !== process.getuid()) || await realpath(directory) !== directory) {
    throw new Error('Unsafe private output directory');
  }
}

async function prepareOutput(output) {
  try {
    await mkdir(privateRoot, { mode: 0o700 });
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }
  await assertPrivateDirectory(privateRoot);
  await mkdir(output, { mode: 0o700 });
  await assertPrivateDirectory(output);
}

async function openPrivate(output, filename) {
  await assertPrivateDirectory(privateRoot);
  await assertPrivateDirectory(output);
  return open(path.join(output, filename),
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
}

function sizedBatchFixture(runId, now, targetBytes) {
  const template = createLogRecord(runId, now);
  const records = Array.from({ length: batchLogRecords }, () => ({
    ...template,
    attributes: { ...template.attributes, 'synthetic.padding': 'x'.repeat(maxLogPaddingBytes) },
  }));
  for (let attempt = 0; attempt < 4; attempt++) {
    const bytes = Buffer.from(ProtobufLogsSerializer.serializeRequest(records));
    if (bytes.length === targetBytes) return bytes;
    const difference = targetBytes - bytes.length;
    const adjustment = Math.trunc(difference / records.length);
    const remainder = difference - adjustment * records.length;
    for (const [index, record] of records.entries()) {
      const size = record.attributes['synthetic.padding'].length + adjustment +
        (index < Math.abs(remainder) ? Math.sign(remainder) : 0);
      if (size < 0 || size > maxLogPaddingBytes) throw new Error('Synthetic batch padding exceeds bounds');
      record.attributes['synthetic.padding'] = 'x'.repeat(size);
    }
  }
  throw new Error('Cannot generate exact-sized synthetic protobuf batch');
}

function casesFor(keys, includeOtherRoutes, includeBoundary) {
  const cases = [
    { id: 'valid-before', expected: [200, 204], positive: true },
    { id: 'no-key', expected: [401, 403], credential: 'none' },
    { id: 'random-key', expected: [401, 403], credential: 'random' },
    ...(keys.negative ? [{ id: 'wrong-subscription', expected: [401, 403], credential: 'negative' }] : []),
    ...(keys.retired ? [{ id: 'retired-key', expected: [401, 403], credential: 'retired' }] : []),
    { id: 'wrong-method', expected: [404, 405], method: 'GET' },
    { id: 'wrong-path', expected: [404, 405], route: '/v1/invalid' },
    { id: 'json-content-type', expected: [415], encoding: 'json' },
    { id: 'gzip', expected: [415], encoding: 'gzip' },
    { id: 'query-key', expected: [400, 403], query: true },
    ...(includeBoundary ? [{ id: 'boundary-acceptance', expected: [200, 204], positive: true, boundary: true }] : []),
    { id: 'overlimit-length', expected: [413], oversized: true, httpGate: 'request_size_limit' },
    // Managed framing normalization may hide Transfer-Encoding, leaving missing Content-Length.
    { id: 'unsupported-chunked', expected: [400, 411], chunked: true, httpGate: 'unsupported_framing' },
    { id: 'empty', expected: [400], empty: true },
  ];
  if (includeOtherRoutes) {
    for (const signal of ['traces', 'metrics']) {
      for (const credential of ['none', 'random']) {
        cases.push({
          id: `${signal}-${credential === 'none' ? 'no-key' : 'random-key'}`,
          expected: [401, 403], route: `/v1/${signal}`, credential,
        });
      }
    }
  }
  cases.push({ id: 'valid-after', expected: [200, 204], positive: true });
  return cases;
}

function requestFor(spec, endpoint, keys, runId, now, fixture) {
  const url = new URL(`${endpoint}${spec.route ?? '/v1/logs'}`);
  const headers = { 'Content-Type': protobufType, Accept: protobufType };
  const fakeKey = `synthetic-invalid-${randomUUID()}`;
  if (spec.credential !== 'none') {
    headers[keyHeader] = spec.credential === 'random' ? fakeKey : keys[spec.credential ?? 'valid'];
  }
  if (spec.query) url.searchParams.set('subscription-key', fakeKey);
  let body = fixture;
  if (spec.encoding === 'json') {
    headers['Content-Type'] = 'application/json';
    body = Buffer.from(JsonLogsSerializer.serializeRequest([createLogRecord(runId, now)]));
  } else if (spec.encoding === 'gzip') {
    headers['Content-Encoding'] = 'gzip';
    body = gzipSync(fixture);
  } else if (spec.empty) {
    body = Buffer.alloc(0);
  }
  if (!spec.chunked) headers['Content-Length'] = String(body.length);
  return {
    id: spec.id, url, method: spec.method ?? 'POST', headers, body,
    chunked: spec.chunked === true, timeoutMs, maxResponseBytes,
  };
}

function probeError(code) {
  return Object.assign(new Error(code), { code });
}

// Native HTTPS neither follows Location nor decompresses untrusted response bodies.
export function sendHttpsProbe(probe, {
  request = https.request, setTimer = setTimeout, clearTimer = clearTimeout,
} = {}) {
  return new Promise((resolve, reject) => {
    let req;
    let response;
    let settled = false;
    let timer;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimer(timer);
      if (error) {
        response?.destroy();
        req?.destroy();
        reject(probeError(error));
      } else {
        resolve(value);
      }
    };
    timer = setTimer(() => finish('timeout'), Math.min(probe.timeoutMs ?? timeoutMs, timeoutMs));
    try {
      const headers = { ...probe.headers };
      // Derive framing from actual bytes, not a caller's declared Content-Length.
      for (const name of Object.keys(headers)) {
        if (['content-length', 'transfer-encoding'].includes(name.toLowerCase())) delete headers[name];
      }
      if (probe.chunked) headers['Transfer-Encoding'] = 'chunked';
      else headers['Content-Length'] = String(probe.body.length);
      req = request(probe.url, {
        method: probe.method, headers, agent: false, rejectUnauthorized: true, maxHeaderSize: 16384,
      }, incoming => {
        response = incoming;
        const chunks = [];
        let received = 0;
        incoming.on('error', () => finish('network_error'));
        incoming.on('aborted', () => finish('network_error'));
        incoming.on('data', chunk => {
          if (settled) return;
          received += chunk.length;
          if (received > Math.min(probe.maxResponseBytes ?? maxResponseBytes, maxResponseBytes)) {
            finish('response_too_large');
          } else {
            chunks.push(chunk);
          }
        });
        incoming.on('end', () => finish(null, {
          status: incoming.statusCode, headers: incoming.headers, body: Buffer.concat(chunks),
        }));
      });
      req.on('error', () => finish('network_error'));
      if (probe.chunked) {
        req.write(probe.body);
        req.end();
      } else {
        req.end(probe.body);
      }
    } catch {
      finish('network_error');
    }
  });
}

// The pinned decoder skips some malformed fields and overwrites duplicates.
// Accept only the current ExportLogsServiceResponse schema before decoding it.
function validateResponseWire(bytes, partial = false) {
  let offset = 0;
  const seen = new Set();
  const varint = () => {
    let value = 0n;
    for (let index = 0; index < 10 && offset < bytes.length; index++) {
      const byte = bytes[offset++];
      if (index === 9 && byte > 1) throw probeError('invalid_otlp_response');
      value |= BigInt(byte & 0x7f) << BigInt(index * 7);
      if (!(byte & 0x80)) return value;
    }
    throw probeError('invalid_otlp_response');
  };
  while (offset < bytes.length) {
    const tag = varint();
    if (seen.has(tag) || (partial ? tag !== 8n && tag !== 18n : tag !== 10n)) {
      throw probeError('invalid_otlp_response');
    }
    seen.add(tag);
    if (partial && tag === 8n) {
      varint();
      continue;
    }
    const length = varint();
    if (length > BigInt(bytes.length - offset)) throw probeError('invalid_otlp_response');
    const value = bytes.subarray(offset, offset + Number(length));
    offset += Number(length);
    if (partial) new TextDecoder('utf-8', { fatal: true }).decode(value);
    else validateResponseWire(value, true);
  }
}

function evaluateResponse(spec, response) {
  if (!Number.isInteger(response?.status) || response.status < 100 || response.status > 599 ||
      !Buffer.isBuffer(response.body)) throw probeError('network_error');
  if (response.body.length > maxResponseBytes) throw probeError('response_too_large');
  const result = {
    status: response.status, response_bytes: response.body.length,
    result: spec.expected.includes(response.status) ? 'passed' : 'failed',
  };
  if (spec.empty) result.response_body_kind = classifyDiagnosticBody(response);
  if (result.result === 'failed') return { ...result, error: 'unexpected_status' };
  if (!spec.positive) return result;
  const type = response.headers?.['content-type'];
  const isProtobuf = typeof type === 'string' && type.length < 128 &&
    type.split(';', 1)[0].trim().toLowerCase() === protobufType;
  const encoding = response.headers?.['content-encoding'];
  result.otlp_result = 'invalid';
  if ((response.status === 204 && response.body.length !== 0) ||
      (response.status === 200 && !isProtobuf) ||
      (encoding !== undefined && (typeof encoding !== 'string' || encoding.toLowerCase() !== 'identity'))) {
    return { ...result, result: 'failed', error: 'invalid_otlp_response' };
  }
  try {
    validateResponseWire(response.body);
    const decoded = ProtobufLogsSerializer.deserializeResponse(response.body);
    const rejected = decoded.partialSuccess?.rejectedLogRecords ?? 0;
    if (rejected !== 0) {
      return { ...result, result: 'failed', otlp_result: 'partial_success', error: 'otlp_partial_success' };
    }
    // Error text is untrusted and may reflect headers. Record only the warning's presence.
    result.otlp_result = decoded.partialSuccess?.errorMessage ? 'accepted_with_warning' : 'accepted';
  } catch {
    return { ...result, result: 'failed', error: 'invalid_otlp_response' };
  }
  return result;
}

function classifyDiagnosticBody(response) {
  if (response.body.length > maxDiagnosticBytes) return 'diagnostic_limit_exceeded';
  if (response.body.length === 0) return 'empty';
  const knownMessages = new Map([
    ['Telemetry backend rejected the request.', 'backend_rejection_message'],
    ['Telemetry gateway failure.', 'gateway_failure_message'],
    ['API-scoped subscription required.', 'subscription_scope_message'],
    ['Fixed-length requests are required.', 'unsupported_framing_message'],
    ['Content-Length is required.', 'length_required_message'],
  ]);
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(response.body);
    if (knownMessages.has(text)) return knownMessages.get(text);
    const value = JSON.parse(text);
    if (value && typeof value === 'object' && !Array.isArray(value) &&
        ((value.statusCode === response.status && typeof value.message === 'string') ||
         (value.error && typeof value.error === 'object' && !Array.isArray(value.error) &&
          typeof value.error.code === 'string' && typeof value.error.message === 'string'))) {
      return 'json_error_envelope';
    }
  } catch {
    return 'unrecognized';
  }
  return 'unrecognized';
}

function admissionDiagnostic(entry) {
  const status = entry.status ?? null;
  const observed = {
    400: 'empty_body_rejection_observed',
    401: 'credential_or_scope_denial_observed',
    403: 'credential_or_scope_denial_observed',
    404: 'route_or_method_denial_observed',
    405: 'route_or_method_denial_observed',
    429: 'throttling_observed_no_retry',
  };
  return {
    probe: 'empty', status,
    diagnostic: status === null ? 'no_usable_http_response' : observed[status] ?? 'unexpected_empty_body_status',
    authenticated_admission_proven: false, backend_verification: 'pending',
    response_body_kind: entry.response_body_kind ?? 'unavailable',
  };
}

export async function runProbes(options, { env = process.env, transport = sendHttpsProbe } = {}) {
  const { endpoint, output, includeOtherRoutes, includeBoundary } = validateOptions(options);
  const keys = readKeys(env);
  await prepareOutput(output);
  const manifest = {
    schema_version: 1,
    suite_id: randomUUID(),
    transport: 'apim-subscription-key',
    started_at: new Date().toISOString(),
    status: 'running',
    http_gates: 'running',
    backend_verification: 'pending',
    azure_ingestion_proven: false,
    quota_verification: 'not_run_operator_approval_required',
    streamed_size_verification: 'not_applicable_chunked_unsupported',
    limits: {
      timeout_ms: timeoutMs, max_response_bytes: maxResponseBytes,
      max_diagnostic_bytes: maxDiagnosticBytes,
      boundary_bytes: boundaryBytes, overlimit_bytes: overlimitBytes, boundary_signal: 'logs',
    },
    skipped: {
      ...(!keys.negative ? { wrong_subscription: 'APIM_NEGATIVE_KEY_not_set' } : {}),
      ...(!keys.retired ? { retired_key: 'APIM_RETIRED_KEY_not_set' } : {}),
      ...(!includeOtherRoutes ? { other_routes: 'not_requested' } : {}),
      ...(!includeBoundary ? { exact_boundary: 'not_requested' } : {}),
      real_query_key: 'never_sent',
    },
    cases: [],
  };
  const journal = await openPrivate(output, 'manifest.json');
  const save = async () => {
    const bytes = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`);
    await journal.truncate(0);
    let offset = 0;
    while (offset < bytes.length) {
      const { bytesWritten } = await journal.write(bytes, offset, bytes.length - offset, offset);
      if (bytesWritten === 0) throw new Error('Cannot write private manifest');
      offset += bytesWritten;
    }
    await journal.sync();
  };
  try {
    await save();
    for (const spec of casesFor(keys, includeOtherRoutes, includeBoundary)) {
      const runId = randomUUID();
      const now = Date.now();
      const batch = spec.oversized || spec.boundary;
      const fixture = batch ? sizedBatchFixture(runId, now, spec.boundary ? boundaryBytes : overlimitBytes) :
        generateFixture(runId, now);
      const request = requestFor(spec, endpoint, keys, runId, now, fixture);
      const entry = {
        id: spec.id, run_id: runId, marker: `SYNTHETIC_RELAY_${runId}`,
        fixture: `${spec.id}.pb`, fixture_time: new Date(now).toISOString(),
        fixture_bytes: fixture.length, request_bytes: request.body.length,
        fixture_log_records: batch ? batchLogRecords : 1,
        marker_in_request: !spec.empty, wire_encoding: spec.empty ? 'empty' : spec.encoding ?? 'protobuf',
        framing: spec.chunked ? 'chunked' : 'content-length',
        expected_statuses: spec.expected,
        ...(spec.httpGate ? { http_gate: spec.httpGate } : {}),
        expected_backend: spec.empty ? 'not_applicable' : spec.positive ? 'present' : 'absent',
        started_at: new Date().toISOString(),
      };
      const file = await openPrivate(output, entry.fixture);
      try {
        await file.writeFile(fixture);
      } finally {
        await file.close();
      }
      try {
        Object.assign(entry, evaluateResponse(spec, await transport(request)));
      } catch (error) {
        Object.assign(entry, {
          result: 'failed', error: transportErrors.has(error?.code) ? error.code : 'network_error',
        });
      }
      entry.finished_at = new Date().toISOString();
      manifest.cases.push(entry);
      if (spec.empty) manifest.data_plane_admission = admissionDiagnostic(entry);
      await save();
    }
    const failed = manifest.cases.some(entry => entry.result === 'failed');
    manifest.http_gates = failed ? 'failed' : 'passed';
    manifest.status = failed ? 'http_gates_failed' : 'awaiting_backend_verification';
    manifest.finished_at = new Date().toISOString();
    await save();
    return { manifest, exitCode: failed ? 1 : 0 };
  } finally {
    await journal.close();
  }
}

export async function main(args, {
  env = process.env, transport = sendHttpsProbe,
  stdout = text => console.log(text), stderr = text => console.error(text),
} = {}) {
  try {
    if (args.length === 1 && args[0] === '--help') {
      stdout(usage);
      return 0;
    }
    const options = {};
    const seen = new Set();
    for (let index = 0; index < args.length; index++) {
      const name = args[index];
      if (seen.has(name)) throw new Error('Duplicate option');
      seen.add(name);
      if (name === '--include-other-routes') options.includeOtherRoutes = true;
      else if (name === '--include-boundary') options.includeBoundary = true;
      else if (name === '--endpoint' || name === '--output') {
        const value = args[++index];
        if (!value || value.startsWith('--')) throw new Error('Missing option value');
        options[name.slice(2)] = value;
      } else {
        throw new Error('Unknown option');
      }
    }
    const { manifest, exitCode } = await runProbes(options, { env, transport });
    stdout(JSON.stringify({
      suite_id: manifest.suite_id, status: manifest.status,
      http_gates: manifest.http_gates, backend_verification: manifest.backend_verification,
    }));
    return exitCode;
  } catch {
    stderr('APIM probe failed: check options (--help), environment keys, and new private .local output. No error content is recorded.');
    return 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main(process.argv.slice(2));
}
