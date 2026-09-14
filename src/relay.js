import { gunzip } from 'node:zlib';
import { promisify } from 'node:util';

export const MAX_BODY_BYTES = 4 * 1024 * 1024;
export const TIMEOUT_MS = 30_000;
const unzip = promisify(gunzip);
const SIGNALS = Object.freeze(['traces', 'logs', 'metrics']);
const STREAMS = Object.freeze({
  traces: 'Microsoft-OTLP-Traces',
  logs: 'Microsoft-OTLP-Logs',
  metrics: 'Custom-Metrics-[A-Za-z][A-Za-z0-9_-]*',
});

class RelayError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

export function validateEndpoints(environment) {
  const endpoints = {};
  let immutableId;
  for (const signal of SIGNALS) {
    const value = environment[`OTLP_${signal.toUpperCase()}_ENDPOINT`];
    const pattern = new RegExp(
      '^https://(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\\.)+ingest\\.monitor\\.azure\\.com' +
      '(?::443)?/datacollectionRules/(dcr-[a-f0-9]{32})/streams/' +
      STREAMS[signal] + '/otlp/v1/' + signal + '$',
    );
    const match = typeof value === 'string' && !/\s/.test(value) && pattern.exec(value);
    if (!match || (immutableId && immutableId !== match[1])) {
      throw new Error('Invalid relay endpoint configuration');
    }
    immutableId = match[1];
    endpoints[signal] = value;
  }
  return Object.freeze(endpoints);
}

async function readBounded(stream, signal, limitStatus, log) {
  if (!stream) return Buffer.alloc(0);
  const reader = stream.getReader();
  let complete = false;
  const cancel = () => {
    void reader.cancel().catch(() => log('stream_cancel_failed'));
  };
  signal.addEventListener('abort', cancel, { once: true });
  const chunks = [];
  let size = 0;
  try {
    signal.throwIfAborted();
    while (true) {
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      if (done) {
        complete = true;
        return Buffer.concat(chunks, size);
      }
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) throw new RelayError(limitStatus, 'body_too_large');
      chunks.push(Buffer.from(value));
    }
  } finally {
    signal.removeEventListener('abort', cancel);
    if (!complete) cancel();
    reader.releaseLock();
  }
}

// The SDK wrapper owns identity and transport; this module never reads process.env.
export function createRelay({ environment, credential, send }) {
  let endpoints;
  try {
    endpoints = validateEndpoints(environment);
  } catch {
    // Fail closed at invocation time so configuration failures get a redacted log.
    endpoints = null;
  }
  return async function relay(request, context) {
    const log = (code, status) => context.error('otlp_relay', { code, ...(status ? { status } : {}) });
    const controller = new AbortController();
    let timeout;
    let stage = 'request';
    const deadline = new Promise((_, reject) => {
      timeout = setTimeout(() => {
        controller.abort();
        reject(new RelayError(504, 'deadline_exceeded'));
      }, TIMEOUT_MS);
    });
    const operation = async () => {
      if (request.method !== 'POST') throw new RelayError(405, 'method_not_allowed');
      const signal = request.params?.signal;
      if (!SIGNALS.includes(signal)) throw new RelayError(404, 'unknown_signal');
      if (!endpoints) throw new RelayError(503, 'invalid_configuration');
      const contentType = request.headers.get('content-type')?.trim().toLowerCase();
      if (contentType !== 'application/x-protobuf') throw new RelayError(415, 'unsupported_content_type');
      const encoding = request.headers.get('content-encoding')?.trim().toLowerCase();
      if (encoding !== undefined && encoding !== 'identity' && encoding !== 'gzip') {
        throw new RelayError(415, 'unsupported_content_encoding');
      }
      const length = request.headers.get('content-length');
      if (length !== null && !/^(0|[1-9][0-9]*)$/.test(length)) throw new RelayError(400, 'invalid_content_length');
      if (length !== null && Number(length) > MAX_BODY_BYTES) throw new RelayError(413, 'body_too_large');
      const body = await readBounded(request.body, controller.signal, 413, log);
      if (body.length === 0) throw new RelayError(400, 'empty_body');
      if (length !== null && Number(length) !== body.length) throw new RelayError(400, 'content_length_mismatch');
      if (encoding === 'gzip') {
        try {
          // Validate compression and its expanded size, but send the original bytes.
          const expanded = await unzip(body, { maxOutputLength: MAX_BODY_BYTES });
          if (expanded.length === 0) throw new RelayError(400, 'empty_body');
        } catch (error) {
          if (error instanceof RelayError) throw error;
          throw new RelayError(error.code === 'ERR_BUFFER_TOO_LARGE' ? 413 : 400, 'invalid_gzip');
        }
      }
      controller.signal.throwIfAborted();
      stage = 'token';
      const token = await credential.getToken('https://monitor.azure.com/.default', { abortSignal: controller.signal });
      controller.signal.throwIfAborted();
      if (!token || typeof token.token !== 'string' || !token.token || /\s/.test(token.token) ||
          !Number.isFinite(token.expiresOnTimestamp) || token.expiresOnTimestamp <= Date.now()) {
        throw new RelayError(502, 'invalid_identity_token');
      }
      const headers = {
        authorization: `Bearer ${token.token}`,
        'content-type': 'application/x-protobuf',
        'content-length': String(body.length),
        'accept-encoding': 'identity',
      };
      if (encoding === 'gzip') headers['content-encoding'] = 'gzip';
      stage = 'upstream';
      const upstream = await send({ url: endpoints[signal], headers, body, signal: controller.signal });
      controller.signal.throwIfAborted();
      if (!Number.isInteger(upstream.status) || upstream.status < 200 || upstream.status > 599) {
        throw new RelayError(502, 'invalid_upstream_response');
      }
      const responseBody = await readBounded(upstream.body, controller.signal, 502, log);
      const responseHeaders = {};
      for (const name of ['content-type', 'retry-after', 'content-encoding']) {
        const value = upstream.headers.get(name);
        if (value !== null) responseHeaders[name] = value;
      }
      if (upstream.status >= 300) log('upstream_rejected', upstream.status);
      return { status: upstream.status, headers: responseHeaders, body: responseBody.length ? responseBody : null };
    };
    try {
      return await Promise.race([operation(), deadline]);
    } catch (error) {
      const failure = controller.signal.aborted
        ? new RelayError(504, 'deadline_exceeded')
        : error instanceof RelayError ? error : new RelayError(stage === 'request' ? 400 : 502, `${stage}_failed`);
      log(failure.code, failure.status);
      controller.abort();
      return {
        status: failure.status,
        headers: {
          'content-type': 'text/plain; charset=utf-8',
          ...(failure.status === 405 ? { allow: 'POST' } : {}),
        },
        body: Buffer.from(`OTLP relay: ${failure.code}\n`),
      };
    } finally {
      clearTimeout(timeout);
      if (request.body && !request.body.locked) {
        void request.body.cancel().catch(() => log('request_cancel_failed'));
      }
    }
  };
}
