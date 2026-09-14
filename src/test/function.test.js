import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import functions from '@azure/functions';
import { ManagedIdentityCredential } from '@azure/identity';
const { app, HttpRequest } = functions;

test('SDK registers anonymous /v1/{signal}, enables streaming, and rejects methods in handler', async (t) => {
  let definition;
  let setup;
  t.mock.getter(app, 'setup', () => (options) => { setup = options; });
  t.mock.method(app, 'http', (name, options) => {
    assert.equal(name, 'otlp');
    definition = options;
  });
  t.mock.method(ManagedIdentityCredential.prototype, 'getToken', () => {
    assert.fail('rejected input must never authenticate');
  });
  await import('../functions/otlp.js').catch((error) => {
    if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
  });
  assert.ok(definition, 'Function entrypoint must register its SDK trigger');
  assert.deepEqual(setup, { enableHttpStream: true });
  assert.equal(definition.route, 'v1/{signal}');
  assert.equal(definition.authLevel, 'anonymous');
  assert.equal(definition.methods, undefined, 'handler rejects all non-POST methods explicitly');
  const logs = [];
  const result = await definition.handler(new HttpRequest({
    method: 'GET', url: 'https://relay.invalid/v1/logs', params: { signal: 'logs' },
  }), { error: (...args) => logs.push(args) });
  assert.equal(result.status, 405);
  assert.equal(logs.length, 1);
});

test('host disables request/dependency telemetry and uses empty route prefix', () => {
  let host;
  try { host = JSON.parse(readFileSync(new URL('../host.json', import.meta.url))); } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  assert.ok(host, 'host.json must exist');
  assert.equal(host.extensions.http.routePrefix, '');
  assert.equal(host.logging.logLevel.default, 'None');
  assert.equal(host.logging.logLevel['Host.Results'], 'None');
  assert.equal(host.logging.logLevel['Function.otlp.User'], 'Error');
  assert.equal(host.logging.applicationInsights.enableDependencyTracking, false);
  assert.equal(host.logging.applicationInsights.enableLiveMetrics, false);
  assert.equal(host.logging.applicationInsights.httpAutoCollectionOptions.enableHttpTriggerExtendedInfoCollection, false);
});
