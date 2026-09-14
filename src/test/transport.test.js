import assert from 'node:assert/strict';
import { test } from 'node:test';
import https from 'node:https';
import { EventEmitter } from 'node:events';
import { Readable } from 'node:stream';
import { gzipSync } from 'node:zlib';

const transport = await import('../transport.js').catch((error) => {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
  return {};
});

test('native HTTPS transport sends exactly one POST and does not decode or follow redirects', async (t) => {
  assert.equal(typeof transport.sendHttps, 'function');
  const bytes = gzipSync(Buffer.from('upstream protobuf bytes'));
  const controller = new AbortController();
  const headers = { authorization: 'Bearer server-token' };
  let calls = 0;
  t.mock.method(https, 'request', (url, options, callback) => {
    calls++;
    assert.equal(url, 'https://example.ingest.monitor.azure.com/fixed');
    assert.equal(options.method, 'POST');
    assert.equal(options.signal, controller.signal);
    assert.equal(options.headers, headers);
    const outgoing = new EventEmitter();
    outgoing.end = (body) => {
      assert.deepEqual(body, Buffer.from([1, 2, 3]));
      const response = Readable.from([bytes]);
      response.statusCode = 307;
      response.headers = {
        'content-type': 'application/x-protobuf', 'content-encoding': 'gzip',
        location: 'https://attacker.invalid/', 'retry-after': '30',
      };
      callback(response);
    };
    return outgoing;
  });
  const response = await transport.sendHttps({
    url: 'https://example.ingest.monitor.azure.com/fixed',
    headers, body: Buffer.from([1, 2, 3]), signal: controller.signal,
  });
  assert.equal(response.status, 307);
  assert.deepEqual(Buffer.from(await new Response(response.body).arrayBuffer()), bytes);
  assert.equal(response.headers.get('retry-after'), '30');
  assert.equal(response.headers.get('content-encoding'), 'gzip');
  assert.equal(calls, 1);
});

test('HTTPS transport rejects connection errors without retrying', async (t) => {
  assert.equal(typeof transport.sendHttps, 'function');
  let calls = 0;
  t.mock.method(https, 'request', () => {
    calls++;
    const outgoing = new EventEmitter();
    outgoing.end = () => queueMicrotask(() => outgoing.emit('error', new Error('network failure')));
    return outgoing;
  });
  await assert.rejects(transport.sendHttps({
    url: 'https://example.ingest.monitor.azure.com/fixed',
    headers: {}, body: Buffer.alloc(0), signal: new AbortController().signal,
  }), /network failure/);
  assert.equal(calls, 1);
});
