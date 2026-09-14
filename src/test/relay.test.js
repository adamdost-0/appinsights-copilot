import assert from 'node:assert/strict';
import { test } from 'node:test';
import { gzipSync } from 'node:zlib';
import functions from '@azure/functions';

const relay = await import('../relay.js').catch((error) => {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
  return {};
});
const { createRelay, validateEndpoints } = relay;
const dcr = `dcr-${'a'.repeat(32)}`;
const streams = { traces: 'Microsoft-OTLP-Traces', logs: 'Microsoft-OTLP-Logs', metrics: 'Custom-Metrics-Otel' };
const endpoints = Object.fromEntries(Object.entries(streams).map(([signal, stream]) => [
  `OTLP_${signal.toUpperCase()}_ENDPOINT`,
  `https://example.eastus-1.ingest.monitor.azure.com/datacollectionRules/${dcr}/streams/${stream}/otlp/v1/${signal}`,
]));
const bytes = Buffer.from([0, 10, 255, 128, 1]);
function request({ signal = 'logs', method = 'POST', body = bytes, headers = {} } = {}) {
  return {
    method, params: { signal },
    url: 'https://relay.invalid/v1/logs?url=https://attacker.invalid/',
    headers: new Headers({ 'content-type': 'application/x-protobuf', ...headers }),
    body: body instanceof ReadableStream ? body : new Response(body).body,
  };
}
function setup({ environment = endpoints, token, send } = {}) {
  assert.equal(typeof createRelay, 'function', 'dependency-injectable relay must exist');
  const calls = [];
  const logs = [];
  const credentials = [];
  const handler = createRelay({
    environment,
    credential: {
      async getToken(scope, options) {
        credentials.push({ scope, options });
        if (token) return token(scope, options);
        return { token: 'server-secret', expiresOnTimestamp: Date.now() + 3600000 };
      },
    },
    async send(options) {
      calls.push(options);
      if (send) return send(options);
      return { status: 200, headers: new Headers({ 'content-type': 'application/x-protobuf' }), body: new Response(bytes).body };
    },
  });
  return { handler: (req) => handler(req, { error: (...args) => logs.push(args) }), calls, logs, credentials };
}

test('exports immutable request and timeout policy', () => {
  assert.equal(relay.MAX_BODY_BYTES, 4 * 1024 * 1024);
  assert.equal(relay.TIMEOUT_MS, 30000);
});

for (const signal of Object.keys(streams)) {
  test(`forwards ${signal} unchanged using only server endpoint and identity`, async () => {
    const env = { ...endpoints };
    const s = setup({ environment: env });
    env.OTLP_LOGS_ENDPOINT = 'https://attacker.invalid';
    const response = await s.handler(request({ signal, headers: {
      authorization: 'Bearer client-secret', cookie: 'secret-cookie',
      'x-api-key': 'client-key', 'x-forwarded-host': 'attacker.invalid',
    } }));
    assert.equal(response.status, 200);
    assert.deepEqual(Buffer.from(response.body), bytes);
    assert.equal(response.headers['content-type'], 'application/x-protobuf');
    assert.equal(s.calls.length, 1);
    assert.equal(s.calls[0].url, endpoints[`OTLP_${signal.toUpperCase()}_ENDPOINT`]);
    assert.deepEqual(s.calls[0].body, bytes);
    assert.deepEqual(s.calls[0].headers, {
      authorization: 'Bearer server-secret',
      'content-type': 'application/x-protobuf',
      'content-length': String(bytes.length),
      'accept-encoding': 'identity',
    });
    assert.equal(s.credentials[0].scope, 'https://monitor.azure.com/.default');
    assert.ok(s.credentials[0].options.abortSignal instanceof AbortSignal);
    assert.deepEqual(s.logs, []);
  });
}

test('accepts only exact native per-signal immutable DCR endpoint URLs', () => {
  assert.equal(typeof validateEndpoints, 'function');
  assert.ok(Object.isFrozen(validateEndpoints(endpoints)));
  const base = endpoints.OTLP_LOGS_ENDPOINT;
  const invalid = [
    undefined, '', base.replace('https:', 'http:'),
    base.replace('example.eastus-1.ingest.monitor.azure.com', 'ingest.monitor.azure.com'),
    base.replace('.azure.com', '.azure.com.attacker.invalid'),
    base.replace('https://', 'https://user:pass@'),
    base.replace('.com/', '.com:8443/'), `${base}?url=x`, `${base}#x`,
    base.replace('/logs', '/traces'), base.replace('Microsoft-OTLP-Logs', 'Custom-Logs'),
    base.replace(dcr, 'dcr-123'), base.replace('/streams/', '/other/../streams/'),
    base.replace('/streams/', '/%73treams/'), ` ${base}`, `${base}\n`,
    base.replace('https://', 'https:\\\\'),
    base.replace(dcr, `dcr-${'b'.repeat(32)}`),
  ];
  for (const endpoint of invalid) {
    assert.throws(() => validateEndpoints({ ...endpoints, OTLP_LOGS_ENDPOINT: endpoint }), /Invalid relay endpoint configuration/);
  }
});

for (const [name, options, status] of [
  ['method', { method: 'GET' }, 405],
  ['unknown signal', { signal: 'events' }, 404],
  ['prototype key', { signal: 'toString' }, 404],
  ['JSON content', { headers: { 'content-type': 'application/json' } }, 415],
  ['empty content type', { headers: { 'content-type': '' } }, 415],
  ['encoding', { headers: { 'content-encoding': 'br' } }, 415],
  ['stacked encoding', { headers: { 'content-encoding': 'gzip, gzip' } }, 415],
  ['invalid gzip', { headers: { 'content-encoding': 'gzip' } }, 400],
  ['empty payload', { body: Buffer.alloc(0) }, 400],
  ['invalid length', { headers: { 'content-length': '-1' } }, 400],
  ['false length', { headers: { 'content-length': '1' } }, 400],
  ['oversize declared length', { headers: { 'content-length': '4194305' } }, 413],
  ['oversize body', { body: Buffer.alloc(4194305) }, 413],
]) {
  test(`rejects ${name} before Azure authentication`, async () => {
    const s = setup();
    const response = await s.handler(request(options));
    assert.equal(response.status, status);
    assert.equal(s.calls.length, 0);
    assert.equal(s.credentials.length, 0);
    assert.equal(s.logs.length, 1);
    assert.ok(!JSON.stringify(s.logs).includes('client-secret'));
  });
}

test('allows exactly 4 MiB, regardless of dangerous environment overrides', async () => {
  const s = setup({ environment: { ...endpoints, MAX_BODY_BYTES: '99999999', TIMEOUT_MS: '0' } });
  assert.equal((await s.handler(request({ body: Buffer.alloc(4194304, 1) }))).status, 200);
  assert.equal(s.calls[0].body.length, 4194304);
});

test('bounds streamed bodies even without content-length and cancels excess input', async () => {
  let cancelled = false;
  const body = new ReadableStream({
    pull(controller) { controller.enqueue(Buffer.alloc(1024 * 1024)); },
    cancel() { cancelled = true; },
  });

  const s = setup();
  assert.equal((await s.handler(request({ body }))).status, 413);
  assert.equal(cancelled, true);
  assert.equal(s.calls.length, 0);
});

test('cancels unread input after header rejection without waiting for the body', async () => {
  let cancelled = false;
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  const s = setup();
  assert.equal((await s.handler(request({ body, headers: { 'content-type': 'text/plain' } }))).status, 415);
  assert.equal(cancelled, true);
  assert.equal(s.credentials.length, 0);
});

test('actual Azure SDK HttpRequest exposes original gzip bytes unchanged', async () => {
  const compressed = gzipSync(bytes);
  const s = setup();
  const req = new functions.HttpRequest({
    method: 'POST', url: 'https://relay.invalid/v1/logs', params: { signal: 'logs' },
    headers: { 'content-type': 'application/x-protobuf', 'content-encoding': 'gzip' },
    body: { bytes: compressed },
  });
  assert.equal((await s.handler(req)).status, 200);
  assert.deepEqual(s.calls[0].body, compressed);
  assert.equal(s.calls[0].headers['content-encoding'], 'gzip');
});

test('valid gzip is forwarded byte-for-byte with original encoding', async () => {
  const compressed = gzipSync(bytes);
  const s = setup();
  assert.equal((await s.handler(request({ body: compressed, headers: { 'content-encoding': 'gzip' } }))).status, 200);
  assert.deepEqual(s.calls[0].body, compressed);
  assert.equal(s.calls[0].headers['content-encoding'], 'gzip');
  assert.equal(s.calls[0].headers['content-length'], String(compressed.length));
});

test('gzip decompressed size also has the 4 MiB bound', async () => {
  const s = setup();
  const compressed = gzipSync(Buffer.alloc(4194305));
  assert.equal((await s.handler(request({ body: compressed, headers: { 'content-encoding': 'gzip' } }))).status, 413);
  assert.equal(s.credentials.length, 0);
});

test('gzip with exactly 4 MiB expanded data is accepted unchanged', async () => {
  const s = setup();
  const compressed = gzipSync(Buffer.alloc(4194304, 1));
  assert.equal((await s.handler(request({ body: compressed, headers: { 'content-encoding': 'gzip' } }))).status, 200);
  assert.deepEqual(s.calls[0].body, compressed);
});

test('upstream response over 4 MiB fails without success or body logging', async () => {
  const s = setup({ send: async () => ({
    status: 200, headers: new Headers(), body: new Response(Buffer.alloc(4194305)).body,
  }) });
  assert.equal((await s.handler(request())).status, 502);
  assert.equal(s.logs.length, 1);
});

test('upstream gzip response bytes and encoding are preserved', async () => {
  const compressed = gzipSync(bytes);
  const s = setup({ send: async () => ({
    status: 200, headers: new Headers({ 'content-encoding': 'gzip', 'content-type': 'application/x-protobuf' }),
    body: new Response(compressed).body,
  }) });
  const response = await s.handler(request());
  assert.equal(response.status, 200);
  assert.deepEqual(response.body, compressed);
  assert.equal(response.headers['content-encoding'], 'gzip');
});

for (const status of [200, 204, 205, 301, 304, 307, 400, 401, 403, 429, 500, 503]) {
  test(`preserves upstream ${status}, bytes, content type and retry-after without redirect/retry`, async () => {
    const s = setup({ send: async () => ({
      status, body: new Response([204, 205, 304].includes(status) ? null : bytes).body,
      headers: new Headers({
        'content-type': 'application/x-protobuf', 'retry-after': '15',
        location: 'https://attacker.invalid', 'set-cookie': 'secret',
      }),
    }) });
    const response = await s.handler(request());
    assert.equal(response.status, status);
    assert.deepEqual(Buffer.from(response.body ?? []), [204, 205, 304].includes(status) ? Buffer.alloc(0) : bytes);
    assert.equal(new functions.HttpResponse(response).status, status);
    assert.deepEqual(response.headers, { 'content-type': 'application/x-protobuf', 'retry-after': '15' });
    assert.equal(s.calls.length, 1);
    assert.equal(s.logs.length, status >= 300 ? 1 : 0);
  });
}

for (const stage of ['token', 'send']) {
  test(`${stage} failure is a redacted 502, never success`, async () => {
    const s = setup({ [stage]: async () => { throw new Error('secret-token private-body https://private.example'); } });
    const result = await s.handler(request());
    assert.equal(result.status, 502);
    assert.equal(s.logs.length, 1);
    const output = JSON.stringify([s.logs, result]);
    assert.doesNotMatch(output, /secret-token|private-body|private\.example/);
  });
}

test('missing token fails explicitly without upstream transmission', async () => {
  const s = setup({ token: async () => null });
  assert.equal((await s.handler(request())).status, 502);
  assert.equal(s.calls.length, 0);
});

test('invalid configuration fails closed with redacted logging', async () => {
  const s = setup({ environment: { ...endpoints, OTLP_LOGS_ENDPOINT: 'https://secret.example' } });
  assert.equal((await s.handler(request())).status, 503);
  assert.equal(s.credentials.length, 0);
  assert.equal(s.logs.length, 1);
  assert.doesNotMatch(JSON.stringify(s.logs), /secret\.example/);
});

for (const stage of ['request', 'token', 'send', 'response']) {
  test(`30-second deadline bounds ${stage} and aborts outstanding work`, async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const pending = () => new Promise(() => {});
    const s = setup({
      token: stage === 'token' ? pending : undefined,
      send: stage === 'send' ? pending : stage === 'response' ? async () => ({
        status: 200, headers: new Headers(), body: new ReadableStream(),
      }) : undefined,
    });
    const result = s.handler(request(stage === 'request' ? { body: new ReadableStream() } : {}));
    await new Promise((resolve) => setImmediate(resolve));
    t.mock.timers.tick(30000);
    assert.equal((await result).status, 504);
    assert.equal(s.logs.length, 1);
    if (s.credentials.length) assert.equal(s.credentials[0].options.abortSignal.aborted, true);
  });
}
