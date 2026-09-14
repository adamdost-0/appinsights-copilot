import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomUUID } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { mkdir, readFile, readdir, lstat, rm, symlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gunzipSync } from 'node:zlib';
import { JsonLogsSerializer, ProtobufLogsSerializer } from '@opentelemetry/otlp-transformer';
import { createLogRecord, generateFixture } from '../tools/generate-logs-fixture.js';

const moduleUrl = new URL('../tools/probe-apim.mjs', import.meta.url);
const probe = await import(moduleUrl).catch(error => {
  if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('probe-apim.mjs')) throw error;
  return {};
});
const repository = fileURLToPath(new URL('../../', import.meta.url));
const privateRoot = path.join(repository, '.local');
const endpoint = 'https://gateway.example.test/otlp';
const env = {
  APIM_SUBSCRIPTION_KEY: 'TEST_VALID_HEADER_SECRET',
  APIM_NEGATIVE_KEY: 'TEST_OTHER_HEADER_SECRET',
  APIM_RETIRED_KEY: 'TEST_RETIRED_HEADER_SECRET',
};
const statuses = {
  'valid-before': [200, 204],
  'no-key': [401, 403],
  'random-key': [401, 403],
  'wrong-subscription': [401, 403],
  'retired-key': [401, 403],
  'wrong-method': [404, 405],
  'wrong-path': [404, 405],
  'json-content-type': [415],
  gzip: [415],
  'query-key': [400, 403],
  'boundary-acceptance': [200, 204],
  'overlimit-length': [413],
  'unsupported-chunked': [400, 411],
  empty: [400],
  'traces-no-key': [401, 403],
  'traces-random-key': [401, 403],
  'metrics-no-key': [401, 403],
  'metrics-random-key': [401, 403],
  'valid-after': [200, 204],
};

async function destination(t) {
  await mkdir(privateRoot, { recursive: true, mode: 0o700 });
  const output = path.join(privateRoot, `apim-test-${randomUUID()}`);
  t.after(() => rm(output, { recursive: true, force: true }));
  return output;
}

function success(request) {
  return {
    status: statuses[request.id][0],
    headers: { 'content-type': 'application/x-protobuf' },
    body: Buffer.alloc(0),
  };
}

test('requires exact HTTPS /otlp endpoint and fresh output directly under worktree .local', async t => {
  assert.equal(typeof probe.validateOptions, 'function');
  const output = await destination(t);
  assert.deepEqual(probe.validateOptions({ endpoint, output }), {
    endpoint, output, includeOtherRoutes: false, includeBoundary: false,
  });
  for (const bad of [
    'http://gateway.example.test/otlp', `${endpoint}/`, `${endpoint}?key=secret`,
    `${endpoint}#fragment`, 'https://user:secret@gateway.example.test/otlp',
    'https://gateway.example.test:8443/otlp', 'https://gateway.example.test/OTLP',
    'https://gateway.example.test/x/../otlp', 'https://gateway.example.test/%6ftlp',
    ' https://gateway.example.test/otlp', 'https://gateway.example.test/otlp\n',
    'https://gateway.example.test\\otlp', 'https://gateway.example.test/otlp?',
  ]) assert.throws(() => probe.validateOptions({ endpoint: bad, output }));
  for (const bad of [privateRoot, repository, '../outside', `${output}/nested`, '']) {
    assert.throws(() => probe.validateOptions({ endpoint, output: bad }));
  }
});

test('fixed matrix uses private official fixtures and never persists keys or untrusted responses', async t => {
  assert.equal(typeof probe.runProbes, 'function');
  const output = await destination(t);
  const requests = [];
  const result = await probe.runProbes({ endpoint, output, includeOtherRoutes: true }, {
    env,
    transport: async request => {
      requests.push(request);
      return { ...success(request), headers: {
        'content-type': 'application/x-protobuf',
        'set-cookie': env.APIM_SUBSCRIPTION_KEY, location: `${endpoint}?${env.APIM_NEGATIVE_KEY}`,
        'x-untrusted': 'x'.repeat(100000),
      }, ...(request.id.startsWith('valid-') ? {} : { body: Buffer.from(Object.values(env).join(' ')) }) };
    },
  });
  assert.equal(result.exitCode, 0);
  const manifest = JSON.parse(await readFile(path.join(output, 'manifest.json'), 'utf8'));
  assert.deepEqual(result.manifest, manifest);
  assert.equal(manifest.status, 'awaiting_backend_verification');
  assert.equal(manifest.http_gates, 'passed');
  assert.equal(manifest.azure_ingestion_proven, false);
  assert.equal(manifest.backend_verification, 'pending');
  assert.equal(manifest.quota_verification, 'not_run_operator_approval_required');
  assert.equal(requests.length, 18);
  assert.equal(requests[0].id, 'valid-before');
  assert.equal(requests.at(-1).id, 'valid-after');
  assert.equal(new Set(manifest.cases.map(c => c.run_id)).size, 18);
  assert.equal((await lstat(output)).mode & 0o777, 0o700);
  const files = await readdir(output);
  assert.equal(files.length, 19);
  for (const file of files) assert.equal((await lstat(path.join(output, file))).mode & 0o777, 0o600);
  for (const c of manifest.cases) {
    const request = requests.find(r => r.id === c.id);
    assert.deepEqual(c.expected_statuses, statuses[c.id]);
    assert.equal(c.result, 'passed');
    assert.equal(c.marker, `SYNTHETIC_RELAY_${c.run_id}`);
    assert.match(c.run_id, /^[0-9a-f-]{36}$/);
    assert.ok(Date.parse(c.started_at) <= Date.parse(c.finished_at));
    assert.equal(c.request_bytes, request.body.length);
    assert.equal(c.marker_in_request, c.id !== 'empty');
    assert.equal(c.expected_backend, c.id === 'empty' ? 'not_applicable' :
      c.id.startsWith('valid-') ? 'present' : 'absent');
    const bytes = await readFile(path.join(output, c.fixture));
    assert.ok(bytes.includes(Buffer.from(c.marker)));
    assert.ok(bytes.includes(Buffer.from('github-copilot')));
    if (!c.id.startsWith('overlimit-')) {
      assert.deepEqual(bytes, generateFixture(c.run_id, Date.parse(c.fixture_time)));
    }
    assert.equal(request.url.origin, 'https://gateway.example.test');
    assert.equal(request.url.username, '');
    for (const key of Object.values(env)) assert.ok(!request.url.href.includes(key));
    assert.equal(request.timeoutMs, 30000);
    assert.equal(request.maxResponseBytes, 65536);
  }
  const query = requests.find(r => r.id === 'query-key');
  assert.equal(query.headers['X-Copilot-Telemetry-Key'], env.APIM_SUBSCRIPTION_KEY);
  assert.match(query.url.searchParams.get('subscription-key'), /^synthetic-invalid-/);
  assert.equal(requests.find(r => r.id === 'no-key').headers['X-Copilot-Telemetry-Key'], undefined);
  assert.equal(requests.find(r => r.id === 'wrong-subscription').headers['X-Copilot-Telemetry-Key'], env.APIM_NEGATIVE_KEY);
  assert.equal(requests.find(r => r.id === 'retired-key').headers['X-Copilot-Telemetry-Key'], env.APIM_RETIRED_KEY);
  const oversized = requests.find(r => r.id === 'overlimit-length');
  assert.equal(oversized.body.length, 1048577);
  assert.equal(oversized.headers['Content-Length'], '1048577');
  assert.equal(oversized.chunked, false);
  const chunked = requests.find(r => r.id === 'unsupported-chunked');
  assert.ok(chunked.body.length < 4096);
  assert.equal(chunked.headers['Content-Length'], undefined);
  assert.equal(chunked.chunked, true);
  const gzip = requests.find(r => r.id === 'gzip');
  const gzipCase = manifest.cases.find(c => c.id === 'gzip');
  assert.deepEqual(gunzipSync(gzip.body), generateFixture(gzipCase.run_id, Date.parse(gzipCase.fixture_time)));
  const json = requests.find(r => r.id === 'json-content-type');
  const jsonCase = manifest.cases.find(c => c.id === json.id);
  assert.equal(json.headers['Content-Type'], 'application/json');
  assert.deepEqual(json.body, Buffer.from(JsonLogsSerializer.serializeRequest([
    createLogRecord(jsonCase.run_id, Date.parse(jsonCase.fixture_time)),
  ])));
  assert.equal(json.headers['Content-Length'], String(json.body.length));
  assert.equal(jsonCase.wire_encoding, 'json');
  const saved = await readFile(path.join(output, 'manifest.json'), 'utf8');
  for (const key of [...Object.values(env), 'set-cookie', 'x-untrusted', 'subscription-key=', endpoint,
    query.url.searchParams.get('subscription-key')]) {
    assert.ok(!saved.includes(key));
  }
});

test('approved protobuf-only contract sends valid JSON but accepts only its exact 415 denial', async t => {
  for (const status of [415, 400, 401, 403, 200, 204]) {
    const output = await destination(t);
    let jsonRequest;
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r => {
      if (r.id !== 'json-content-type') return success(r);
      jsonRequest = r;
      return { ...success(r), status };
    } });
    assert.ok(jsonRequest, 'required JSON negative must not be replaced by another media type');
    const entry = result.manifest.cases.find(c => c.id === 'json-content-type');
    const payload = JSON.parse(jsonRequest.body.toString('utf8'));
    const log = payload.resourceLogs[0].scopeLogs[0].logRecords[0];
    assert.equal(log.body.stringValue, entry.marker);
    assert.ok(log.attributes.some(a => a.key === 'run.id' && a.value.stringValue === entry.run_id));
    assert.ok(log.attributes.some(a => a.key === 'synthetic' && a.value.boolValue === true));
    assert.deepEqual(entry.expected_statuses, [415]);
    assert.equal(result.exitCode, status === 415 ? 0 : 1);
    assert.equal(entry.result, status === 415 ? 'passed' : 'failed');
    assert.deepEqual(result.manifest.cases.find(c => c.id === 'empty').expected_statuses, [400]);
    assert.deepEqual(result.manifest.cases.find(c => c.id === 'unsupported-chunked').expected_statuses, [400, 411]);
    assert.equal(result.manifest.cases.at(-1).result, 'passed');
  }
});

test('CLI help states approved protobuf-only and pre-backend empty-body rejection contracts', async () => {
  const lines = [];
  assert.equal(await probe.main(['--help'], { stdout: text => lines.push(text) }), 0);
  assert.match(lines[0], /Only application\/x-protobuf is permitted; application\/json requires 415/);
  assert.match(lines[0], /Empty bodies require 400 before backend forwarding/);
  assert.match(lines[0], /1048576-byte protobuf batch acceptance request \(128 synthetic logs\)/);
  assert.match(lines[0], /Declared 1048577-byte requests require 413/);
  assert.match(lines[0], /observed native logs HTTP boundary/);
  assert.doesNotMatch(lines[0], /JSON is permitted|application\/octet-stream/);
});

test('default matrix omits unavailable real negative keys and optional routes explicitly', async t => {
  assert.equal(typeof probe.runProbes, 'function');
  const output = await destination(t);
  const result = await probe.runProbes({ endpoint, output }, {
    env: { APIM_SUBSCRIPTION_KEY: env.APIM_SUBSCRIPTION_KEY }, transport: async r => success(r),
  });
  assert.equal(result.manifest.cases.length, 12);
  assert.deepEqual(result.manifest.skipped, {
    wrong_subscription: 'APIM_NEGATIVE_KEY_not_set',
    retired_key: 'APIM_RETIRED_KEY_not_set',
    other_routes: 'not_requested',
    exact_boundary: 'not_requested',
    real_query_key: 'never_sent',
  });
});

// Decode the fixture's OTLP schema independently of the request serializer.
function decodeMessage(bytes, schema) {
  let offset = 0;
  const result = {};
  const varint = () => {
    let value = 0n;
    for (let index = 0; index < 10; index++) {
      assert.ok(offset < bytes.length, 'truncated varint');
      const byte = bytes[offset++];
      assert.ok(index !== 9 || byte <= 1, 'oversized varint');
      value |= BigInt(byte & 127) << BigInt(index * 7);
      if (!(byte & 128)) return value;
    }
    assert.fail('unterminated varint');
  };
  while (offset < bytes.length) {
    const tag = Number(varint());
    const field = schema[tag >>> 3];
    assert.ok(field, `unknown field ${tag >>> 3}`);
    const [name, type, repeated = false] = field;
    let value;
    if (type === 'uint' || type === 'bool') {
      assert.equal(tag & 7, 0);
      const integer = varint();
      if (type === 'bool') assert.ok(integer === 0n || integer === 1n);
      value = type === 'bool' ? integer === 1n : integer;
    } else if (type === 'fixed64') {
      assert.equal(tag & 7, 1);
      assert.ok(offset + 8 <= bytes.length);
      value = bytes.readBigUInt64LE(offset);
      offset += 8;
    } else {
      assert.equal(tag & 7, 2);
      const size = Number(varint());
      assert.ok(Number.isSafeInteger(size) && offset + size <= bytes.length, 'truncated field');
      const payload = bytes.subarray(offset, offset + size);
      offset += size;
      value = type === 'string' ? new TextDecoder('utf-8', { fatal: true }).decode(payload) :
        decodeMessage(payload, type);
    }
    if (repeated) (result[name] ??= []).push(value);
    else {
      assert.ok(!(name in result), `duplicate field ${name}`);
      result[name] = value;
    }
  }
  return result;
}

const anyValue = { 1: ['stringValue', 'string'], 2: ['boolValue', 'bool'] };
const keyValue = { 1: ['key', 'string'], 2: ['value', anyValue] };
const logSchema = {
  1: ['time', 'fixed64'], 2: ['severity', 'uint'], 3: ['severityText', 'string'],
  5: ['body', anyValue], 6: ['attributes', keyValue, true], 7: ['dropped', 'uint'],
  11: ['observedTime', 'fixed64'],
};
const requestSchema = { 1: ['resources', {
  1: ['resource', { 1: ['attributes', keyValue, true], 2: ['dropped', 'uint'] }],
  2: ['scopes', {
    1: ['scope', { 1: ['name', 'string'], 2: ['version', 'string'],
      3: ['attributes', keyValue, true], 4: ['dropped', 'uint'] }],
    2: ['logs', logSchema, true],
  }, true],
}, true] };

test('opt-in boundary and overlimit fixtures decode as exact-sized batches of bounded synthetic logs', async t => {
  const output = await destination(t);
  const requests = [];
  assert.equal(await probe.main([
    '--endpoint', endpoint, '--output', output, '--include-boundary', '--include-other-routes',
  ], { env, transport: async r => { requests.push(r); return success(r); }, stdout: () => {}, stderr: () => {} }), 0);
  const manifest = JSON.parse(await readFile(path.join(output, 'manifest.json'), 'utf8'));
  assert.equal(requests.length, 19);
  assert.equal(requests[0].id, 'valid-before');
  assert.equal(requests.at(-1).id, 'valid-after');
  assert.equal(new Set(manifest.cases.map(c => c.run_id)).size, 19);
  assert.equal(manifest.limits.boundary_bytes, 1048576);
  assert.equal(manifest.limits.overlimit_bytes, 1048577);
  assert.equal(manifest.limits.boundary_signal, 'logs');
  assert.equal(manifest.skipped.exact_boundary, undefined);
  assert.equal(manifest.status, 'awaiting_backend_verification');
  for (const [id, size] of [
    ['boundary-acceptance', 1048576], ['overlimit-length', 1048577],
  ]) {
    const request = requests.find(r => r.id === id);
    const entry = manifest.cases.find(c => c.id === id);
    const bytes = await readFile(path.join(output, entry.fixture));
    assert.deepEqual(request.body, bytes);
    assert.equal(bytes.length, size);
    assert.equal(entry.fixture_bytes, size);
    assert.equal(entry.request_bytes, size);
    assert.equal(request.headers['Content-Length'], id.endsWith('chunked') ? undefined : String(size));
    assert.deepEqual(entry.expected_statuses, statuses[id]);
    assert.equal(entry.expected_backend, id === 'boundary-acceptance' ? 'present' : 'absent');
    const decoded = decodeMessage(bytes, requestSchema);
    assert.equal(decoded.resources.length, 1);
    const resource = decoded.resources[0];
    assert.deepEqual(resource.resource.attributes, [{ key: 'service.name', value: { stringValue: 'github-copilot' } }]);
    assert.equal(resource.scopes.length, 1);
    const logs = resource.scopes[0].logs;
    assert.equal(logs.length, 128);
    assert.equal(entry.fixture_log_records, logs.length);
    const template = createLogRecord(entry.run_id, Date.parse(entry.fixture_time));
    const records = logs.map(log => {
      assert.equal(log.body.stringValue, entry.marker);
      const attributes = Object.fromEntries(log.attributes.map(a =>
        [a.key, 'stringValue' in a.value ? a.value.stringValue : a.value.boolValue]));
      assert.equal(attributes['run.id'], entry.run_id);
      assert.equal(attributes.synthetic, true);
      assert.ok(attributes['synthetic.padding'].length <= 8192);
      assert.match(attributes['synthetic.padding'], /^x*$/);
      assert.equal(log.time, BigInt(Date.parse(entry.fixture_time)) * 1000000n);
      assert.equal(log.observedTime, log.time);
      assert.equal(log.severity, 9n);
      assert.equal(log.dropped, 0n);
      return { ...template, attributes };
    });
    assert.deepEqual(Buffer.from(ProtobufLogsSerializer.serializeRequest(records)), bytes);
  }
});

test('observed native logs HTTP boundary accepts exactly 1 MiB and rejects exactly one byte over', async t => {
  const output = await destination(t);
  const observedSizes = [];
  const result = await probe.runProbes({ endpoint, output, includeBoundary: true }, {
    env,
    transport: async request => {
      if (!['boundary-acceptance', 'overlimit-length'].includes(request.id)) return success(request);
      observedSizes.push(request.body.length);
      assert.equal(request.headers['Content-Length'], String(request.body.length));
      return {
        status: request.body.length <= 1048576 ? 204 : 413,
        headers: {},
        body: request.body.length <= 1048576 ? Buffer.alloc(0) :
          Buffer.from('Telemetry backend rejected the request.'),
      };
    },
  });
  assert.deepEqual(observedSizes, [1048576, 1048577]);
  assert.equal(result.exitCode, 0);
  const boundary = result.manifest.cases.find(c => c.id === 'boundary-acceptance');
  const overlimit = result.manifest.cases.find(c => c.id === 'overlimit-length');
  assert.equal(boundary.status, 204);
  assert.deepEqual(boundary.expected_statuses, [200, 204]);
  assert.equal(overlimit.status, 413);
  assert.deepEqual(overlimit.expected_statuses, [413]);
  assert.equal(overlimit.http_gate, 'request_size_limit');
  assert.equal(result.manifest.status, 'awaiting_backend_verification');
  assert.equal(result.manifest.azure_ingestion_proven, false);
});

test('boundary option is boolean and strict acceptance failures do not skip final control', async t => {
  const output = await destination(t);
  assert.throws(() => probe.validateOptions({ endpoint, output, includeBoundary: 'true' }));
  for (const response of [
    { status: 413, headers: {}, body: Buffer.alloc(0) },
    { status: 400, headers: {}, body: Buffer.alloc(0) },
    { status: 200, headers: { 'content-type': 'application/x-protobuf' },
      body: Buffer.from([0x0a, 0x02, 0x08, 0x01]) },
  ]) {
    const runOutput = await destination(t);
    const result = await probe.runProbes({ endpoint, output: runOutput, includeBoundary: true }, {
      env, transport: async r => r.id === 'boundary-acceptance' ? response : success(r),
    });
    assert.equal(result.exitCode, 1);
    assert.equal(result.manifest.cases.find(c => c.id === 'boundary-acceptance').result, 'failed');
    assert.equal(result.manifest.cases.at(-1).result, 'passed');
    assert.equal(result.manifest.azure_ingestion_proven, false);
  }
});

test('admission diagnostic classifies empty-body HTTP outcomes without inferring authenticated admission', async t => {
  for (const [status, diagnostic] of [
    [400, 'empty_body_rejection_observed'],
    [401, 'credential_or_scope_denial_observed'],
    [403, 'credential_or_scope_denial_observed'],
    [404, 'route_or_method_denial_observed'],
    [429, 'throttling_observed_no_retry'],
    [200, 'unexpected_empty_body_status'],
    [204, 'unexpected_empty_body_status'],
    [undefined, 'no_usable_http_response'],
  ]) {
    const output = await destination(t);
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r => {
      if (r.id !== 'empty') return success(r);
      if (status === undefined) throw new Error(env.APIM_SUBSCRIPTION_KEY);
      return { ...success(r), status };
    } });
    assert.equal(result.exitCode, status === 400 ? 0 : 1);
    assert.deepEqual(result.manifest.data_plane_admission, {
      probe: 'empty', status: status ?? null, diagnostic,
      authenticated_admission_proven: false, backend_verification: 'pending',
      response_body_kind: status === undefined ? 'unavailable' : 'empty',
    });
    assert.equal(result.manifest.cases.at(-1).result, 'passed');
  }
});

test('unsupported chunked framing accepts only 400 or normalized 411 and never proves a streamed size bound', async t => {
  for (const status of [400, 411, 413, 401, 403, 415, 200]) {
    const output = await destination(t);
    let framedRequest;
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r => {
      if (r.id === 'unsupported-chunked') {
        framedRequest = r;
        return { ...success(r), status };
      }
      return success(r);
    } });
    const entry = result.manifest.cases.find(c => c.id === 'unsupported-chunked');
    assert.ok(entry, 'required unsupported-framing probe');
    assert.deepEqual(entry.expected_statuses, [400, 411]);
    assert.equal(entry.status, status);
    assert.equal(entry.result, [400, 411].includes(status) ? 'passed' : 'failed');
    assert.equal(result.exitCode, [400, 411].includes(status) ? 0 : 1);
    assert.equal(entry.http_gate, 'unsupported_framing');
    assert.equal(entry.fixture_log_records, 1);
    assert.ok(framedRequest.body.length < 4096);
    assert.equal(result.manifest.streamed_size_verification, 'not_applicable_chunked_unsupported');
    assert.equal(result.manifest.cases.find(c => c.id === 'overlimit-length').http_gate, 'request_size_limit');
    assert.equal(result.manifest.cases.at(-1).result, 'passed');
  }
});

test('bounded empty-body response classification never records text, keys or changes strict status outcomes', async t => {
  for (const [body, kind, status = 400] of [
    ['', 'empty'],
    ['Telemetry backend rejected the request.', 'backend_rejection_message'],
    ['Telemetry gateway failure.', 'gateway_failure_message'],
    ['API-scoped subscription required.', 'subscription_scope_message', 403],
    ['Fixed-length requests are required.', 'unsupported_framing_message'],
    ['Content-Length is required.', 'length_required_message', 411],
    [JSON.stringify({ statusCode: 400, message: env.APIM_SUBSCRIPTION_KEY }), 'json_error_envelope'],
    [JSON.stringify({ error: { code: 'BadRequest', message: env.APIM_SUBSCRIPTION_KEY } }), 'json_error_envelope'],
    ['{"error":', 'unrecognized'],
    [env.APIM_SUBSCRIPTION_KEY, 'unrecognized'],
    ['x'.repeat(4097), 'diagnostic_limit_exceeded'],
  ]) {
    const output = await destination(t);
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r =>
      r.id === 'empty' ? { status, headers: {}, body: Buffer.from(body) } : success(r),
    });
    assert.equal(result.manifest.data_plane_admission.response_body_kind, kind);
    assert.equal(result.exitCode, status === 400 ? 0 : 1);
    assert.equal(result.manifest.data_plane_admission.authenticated_admission_proven, false);
    assert.ok(!JSON.stringify(result).includes(env.APIM_SUBSCRIPTION_KEY));
    assert.equal(result.manifest.cases.at(-1).result, 'passed');
  }
});

test('wrong statuses and secret-bearing network errors fail without stopping later controls', async t => {
  assert.equal(typeof probe.runProbes, 'function');
  const output = await destination(t);
  const seen = [];
  const result = await probe.runProbes({ endpoint, output }, { env, transport: async r => {
    seen.push(r.id);
    if (r.id === 'no-key') return { ...success(r), status: 404 };
    if (r.id === 'random-key') throw new Error(`https://secret/${env.APIM_SUBSCRIPTION_KEY}`);
    if (r.id === 'wrong-path') return { ...success(r), status: 401 };
    if (r.id === 'gzip') return { ...success(r), status: 302 };
    return success(r);
  } });
  assert.equal(result.exitCode, 1);
  assert.equal(result.manifest.status, 'http_gates_failed');
  assert.equal(result.manifest.backend_verification, 'pending');
  assert.equal(seen.at(-1), 'valid-after');
  assert.equal(result.manifest.cases.filter(c => c.result === 'failed').length, 4);
  assert.equal(result.manifest.cases.find(c => c.id === 'random-key').error, 'network_error');
  assert.ok(!JSON.stringify(result).includes(env.APIM_SUBSCRIPTION_KEY));
});

test('positive OTLP responses reject partial success and malformed or non-OTLP bodies', async t => {
  assert.equal(typeof probe.runProbes, 'function');
  for (const response of [
    { status: 200, body: Buffer.from([0x0a, 0x02, 0x08, 0x01]), expected: 'otlp_partial_success' },
    { status: 200, body: Buffer.from([0x0a, 0xff]), expected: 'invalid_otlp_response' },
    { status: 200, body: Buffer.from('not OTLP'), type: 'text/html', expected: 'invalid_otlp_response' },
    { status: 204, body: Buffer.from('unexpected'), expected: 'invalid_otlp_response' },
  ]) {
    const output = await destination(t);
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r =>
      r.id === 'valid-before' ? { ...response, headers: {
        'content-type': response.type ?? 'application/x-protobuf',
      } } : success(r),
    });
    assert.equal(result.exitCode, 1);
    assert.equal(result.manifest.cases[0].error, response.expected);
    assert.equal(result.manifest.cases.at(-1).result, 'passed');
  }
  assert.deepEqual(ProtobufLogsSerializer.deserializeResponse(Buffer.from([0x0a, 0x02, 0x08, 0x01])),
    { partialSuccess: { rejectedLogRecords: 1 } });
});

test('positive OTLP wire validation catches permissive decoder skips and hidden rejections', async t => {
  for (const bytes of [
    [0x09], // The pinned decoder skips past a missing fixed64 field.
    [0x00, 0x00],
    [0x0a, 0x01, 0x09],
    [0x0a, 0x02, 0x08, 0x01, 0x0a, 0x02, 0x08, 0x00],
    [0x0a, 0x04, 0x08, 0x01, 0x08, 0x00],
    [0x0a, 0x0d, 0x08, ...Array(11).fill(0x80), 0x00],
  ]) {
    const output = await destination(t);
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r =>
      r.id === 'valid-before' ? { ...success(r), body: Buffer.from(bytes) } : success(r),
    });
    assert.equal(result.exitCode, 1);
    assert.equal(result.manifest.cases[0].error, 'invalid_otlp_response');
  }
});

test('accepts explicit 204, zero-rejection protobuf and bounded sanitized OTLP warnings', async t => {
  const warning = Buffer.from(env.APIM_SUBSCRIPTION_KEY);
  for (const response of [
    { status: 204, headers: {}, body: Buffer.alloc(0) },
    { status: 200, headers: { 'content-type': 'application/x-protobuf; charset=binary' },
      body: Buffer.from([0x0a, 0x02, 0x08, 0x00]) },
    { status: 200, headers: { 'content-type': 'application/x-protobuf' },
      body: Buffer.concat([Buffer.from([0x0a, warning.length + 2, 0x12, warning.length]), warning]) },
  ]) {
    const output = await destination(t);
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r => {
      if (r.id.startsWith('valid-')) return response;
      return { ...success(r), status: statuses[r.id].at(-1) };
    } });
    assert.equal(result.exitCode, 0);
    assert.equal(result.manifest.status, 'awaiting_backend_verification');
    assert.ok(!JSON.stringify(result).includes(env.APIM_SUBSCRIPTION_KEY));
    assert.equal(result.manifest.cases[0].otlp_result,
      response.body.includes(warning) ? 'accepted_with_warning' : 'accepted');
  }
});

test('encoded and oversized responses cannot become successful HTTP gates', async t => {
  for (const [response, expected] of [
    [{ status: 200, headers: { 'content-type': 'application/x-protobuf', 'content-encoding': 'gzip' },
      body: Buffer.alloc(0) }, 'invalid_otlp_response'],
    [{ status: 200, headers: { 'content-type': 'x'.repeat(100000) }, body: Buffer.alloc(0) },
      'invalid_otlp_response'],
    [{ status: 401, headers: {}, body: Buffer.alloc(65537) }, 'response_too_large'],
  ]) {
    const output = await destination(t);
    const result = await probe.runProbes({ endpoint, output }, { env, transport: async r =>
      r.id === 'valid-before' ? response : success(r),
    });
    assert.equal(result.exitCode, 1);
    assert.equal(result.manifest.cases[0].error, expected);
    assert.equal(result.manifest.cases.at(-1).result, 'passed');
  }
});

test('missing, duplicate or malformed keys fail before output or requests', async t => {
  assert.equal(typeof probe.runProbes, 'function');
  for (const invalid of [
    {}, { APIM_SUBSCRIPTION_KEY: 'bad\r\nheader' }, { APIM_SUBSCRIPTION_KEY: 'x'.repeat(4097) },
    { ...env, APIM_NEGATIVE_KEY: env.APIM_SUBSCRIPTION_KEY },
    { ...env, APIM_RETIRED_KEY: env.APIM_SUBSCRIPTION_KEY },
    { ...env, APIM_NEGATIVE_KEY: '' },
  ]) {
    const output = await destination(t);
    await assert.rejects(probe.runProbes({ endpoint, output }, {
      env: invalid, transport: () => assert.fail('must not send'),
    }), /Invalid APIM key configuration/);
    await assert.rejects(lstat(output), { code: 'ENOENT' });
  }
});

test('refuses existing files, directories and symlinks without writes or traffic', async t => {
  assert.equal(typeof probe.runProbes, 'function');
  const target = await destination(t);
  await mkdir(target, { mode: 0o700 });
  await writeFile(path.join(target, 'unchanged'), 'private', { mode: 0o600 });
  for (const kind of ['directory', 'file', 'symlink']) {
    const output = kind === 'directory' ? target : await destination(t);
    if (kind === 'file') await writeFile(output, 'existing', { mode: 0o600 });
    if (kind === 'symlink') await symlink(target, output);
    await assert.rejects(probe.runProbes({ endpoint, output }, {
      env, transport: () => assert.fail('must not send'),
    }));
    assert.equal(await readFile(path.join(target, 'unchanged'), 'utf8'), 'private');
    assert.deepEqual(await readdir(target), ['unchanged']);
  }
});

test('CLI parses a closed option set and never echoes secret argv or exceptions', async t => {
  assert.equal(typeof probe.main, 'function');
  const output = await destination(t);
  for (const args of [
    ['--key', env.APIM_SUBSCRIPTION_KEY],
    ['--endpoint', endpoint, '--endpoint', endpoint, '--output', output],
    ['--endpoint', `${endpoint}?subscription-key=${env.APIM_SUBSCRIPTION_KEY}`, '--output', output],
    ['--endpoint', endpoint, '--output', output, '--real-query-key', env.APIM_SUBSCRIPTION_KEY],
  ]) {
    const lines = [];
    const result = await probe.main(args, {
      env, transport: () => assert.fail('must not send'), stdout: s => lines.push(s), stderr: s => lines.push(s),
    });
    assert.equal(result, 1);
    assert.equal(lines.length, 1);
    for (const key of Object.values(env)) assert.ok(!lines[0].includes(key));
  }
  const lines = [];
  assert.equal(await probe.main(['--endpoint', endpoint, '--output', output, '--include-other-routes'], {
    env, transport: async r => success(r), stdout: s => lines.push(s), stderr: s => lines.push(s),
  }), 0);
  assert.equal(JSON.parse(lines[0]).status, 'awaiting_backend_verification');
});

test('CLI failure exit and manifest retain every case when all HTTP requests fail', async t => {
  const output = await destination(t);
  const lines = [];
  const exitCode = await probe.main(['--endpoint', endpoint, '--output', output], {
    env, transport: async () => { throw Object.assign(new Error(env.APIM_SUBSCRIPTION_KEY), { code: 'timeout' }); },
    stdout: s => lines.push(s), stderr: s => lines.push(s),
  });
  assert.equal(exitCode, 1);
  assert.equal(JSON.parse(lines[0]).status, 'http_gates_failed');
  const manifest = JSON.parse(await readFile(path.join(output, 'manifest.json'), 'utf8'));
  assert.equal(manifest.cases.length, 14);
  assert.ok(manifest.cases.every(c => c.error === 'timeout'));
  assert.equal(manifest.cases.at(-1).id, 'valid-after');
});

function fakeHttps({ status = 204, headers = {}, chunks = [], hang = false, fail, aborted = false } = {}) {
  const calls = [];
  const request = (url, options, callback) => {
    const req = new EventEmitter();
    const response = new EventEmitter();
    response.statusCode = status;
    response.headers = headers;
    response.destroy = () => { response.destroyed = true; };
    req.destroy = () => { req.destroyed = true; };
    const sent = [];
    req.write = body => { sent.push(body); return true; };
    req.end = body => {
      if (body) sent.push(body);
      queueMicrotask(() => {
        if (fail) return req.emit('error', fail);
        if (hang) return;
        callback(response);
        for (const chunk of chunks) response.emit('data', chunk);
        if (aborted) response.emit('aborted');
        else response.emit('end');
      });
    };
    calls.push({ url, options, req, response, sent });
    return req;
  };
  return { request, calls };
}

test('actual HTTPS request headers match the checked-in APIM subscription contract for every credential case', async t => {
  const baseline = await readFile(new URL('../../infra/apim-baseline.bicep', import.meta.url), 'utf8');
  const contracts = [...baseline.matchAll(/^\s*subscriptionKeyParameterNames\s*:\s*\{([^{}]*)\}/gm)];
  assert.equal(contracts.length, 1, 'require one unambiguous baseline subscription-key contract');
  const headers = [...contracts[0][1].matchAll(/^\s*header\s*:\s*'([^']+)'\s*$/gm)];
  assert.equal(headers.length, 1, 'require an explicit literal header in the baseline contract');
  const contractHeader = headers[0][1];
  assert.equal(contractHeader, 'X-Copilot-Telemetry-Key', 'baseline must preserve the approved custom header');

  const output = await destination(t);
  const outbound = new Map();
  const result = await probe.runProbes({ endpoint, output, includeOtherRoutes: true }, {
    env,
    transport: request => probe.sendHttpsProbe(request, {
      request: (url, options, callback) => {
        outbound.set(request.id, { url, options });
        const expected = success(request);
        const fake = fakeHttps({
          status: options.headers[contractHeader] ? expected.status : 401,
          headers: expected.headers,
        });
        return fake.request(url, options, callback);
      },
    }),
  });
  assert.equal(outbound.size, 18);
  for (const [id, { url, options }] of outbound) {
    assert.ok(!Object.keys(options.headers).some(name => name.toLowerCase() === 'ocp-apim-subscription-key'),
      `${id} must never send the wrong default APIM header`);
    const credentialHeaders = Object.keys(options.headers)
      .filter(name => name.toLowerCase() === contractHeader.toLowerCase());
    if (id === 'no-key' || id.endsWith('-no-key')) {
      assert.deepEqual(credentialHeaders, []);
    } else {
      assert.deepEqual(credentialHeaders, [contractHeader], `${id} must use the exact baseline header`);
      const value = options.headers[contractHeader];
      if (id === 'wrong-subscription') assert.equal(value, env.APIM_NEGATIVE_KEY);
      else if (id === 'retired-key') assert.equal(value, env.APIM_RETIRED_KEY);
      else if (id === 'random-key' || id.endsWith('-random-key')) {
        assert.match(value, /^synthetic-invalid-/);
        assert.ok(!Object.values(env).includes(value));
      } else {
        assert.equal(value, env.APIM_SUBSCRIPTION_KEY);
      }
    }
    for (const key of Object.values(env)) assert.ok(!url.href.includes(key));
  }
  for (const id of ['valid-before', 'wrong-subscription', 'retired-key', 'valid-after']) {
    assert.ok(outbound.has(id), `required credential case ${id}`);
    assert.equal(result.manifest.cases.find(c => c.id === id).result, 'passed');
  }
  assert.equal(result.exitCode, 0);
  const saved = await readFile(path.join(output, 'manifest.json'), 'utf8');
  for (const key of Object.values(env)) assert.ok(!saved.includes(key));
});

test('native transport uses actual bytes, explicit chunking, TLS and never follows redirects', async () => {
  assert.equal(typeof probe.sendHttpsProbe, 'function');
  for (const chunked of [false, true]) {
    const fake = fakeHttps({ status: 302, headers: { location: 'https://untrusted.test/' } });
    const body = Buffer.alloc(4194305, 0x61);
    const result = await probe.sendHttpsProbe({
      url: new URL(`${endpoint}/v1/logs`), method: 'POST',
      headers: { 'X-Copilot-Telemetry-Key': env.APIM_SUBSCRIPTION_KEY },
      body, chunked, timeoutMs: 30000, maxResponseBytes: 65536,
    }, { request: fake.request });
    assert.equal(result.status, 302);
    assert.equal(fake.calls.length, 1);
    const { options, sent } = fake.calls[0];
    assert.equal(options.headers['X-Copilot-Telemetry-Key'], env.APIM_SUBSCRIPTION_KEY);
    assert.ok(!Object.keys(options.headers).some(name => name.toLowerCase() === 'ocp-apim-subscription-key'));
    assert.equal(options.rejectUnauthorized, true);
    assert.equal(options.agent, false);
    assert.equal(options.maxHeaderSize, 16384);
    assert.equal(options.headers['Content-Length'], chunked ? undefined : '4194305');
    assert.equal(options.headers['Transfer-Encoding'], chunked ? 'chunked' : undefined);
    assert.equal(Buffer.concat(sent).length, 4194305);
  }
});

test('native transport bounds responses, wall-clock duration, and handles aborted/error responses', async () => {
  assert.equal(typeof probe.sendHttpsProbe, 'function');
  const options = {
    url: new URL(`${endpoint}/v1/logs`), method: 'POST', headers: {},
    body: Buffer.alloc(0), timeoutMs: 30000, maxResponseBytes: 65536,
  };
  for (const [config, code] of [
    [{ chunks: [Buffer.alloc(65536), Buffer.alloc(1)] }, 'response_too_large'],
    [{ aborted: true }, 'network_error'],
    [{ fail: new Error(env.APIM_SUBSCRIPTION_KEY) }, 'network_error'],
  ]) {
    const fake = fakeHttps(config);
    await assert.rejects(probe.sendHttpsProbe(options, { request: fake.request }), { code });
    assert.equal(fake.calls[0].req.destroyed, true);
  }
  const fake = fakeHttps({ hang: true });
  let fire;
  let cleared = false;
  const pending = probe.sendHttpsProbe(options, {
    request: fake.request,
    setTimer: (callback, ms) => { assert.equal(ms, 30000); fire = callback; return 123; },
    clearTimer: timer => { assert.equal(timer, 123); cleared = true; },
  });
  fire();
  await assert.rejects(pending, { code: 'timeout' });
  assert.equal(cleared, true);
  assert.equal(fake.calls[0].req.destroyed, true);
});
