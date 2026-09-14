import test from 'node:test';
import assert from 'node:assert/strict';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { buildEnvironment, cliArguments, validateOptions, redact, createIsolationDirectory } from '../tools/smoke-copilot.mjs';

const runId = '3b5eaa43-e40a-44d7-9641-1309d9d742f4';

test('synthetic work directory is outside the repository to avoid ancestor project context', async () => {
  const directory = await createIsolationDirectory();
  try {
    assert.equal(path.dirname(directory), tmpdir());
    assert.match(path.basename(directory), /^copilot-relay-/);
  } finally {
    await rm(directory, { recursive: true });
  }
});

test('isolated CLI environment excludes Azure auth, personal settings, and inherited OTLP configuration', () => {
  const env = buildEnvironment('/tmp/isolated/home', runId, 'https://relay.example.com', 'test-github-token', {
    PATH: '/usr/bin', HOME: '/personal', AZURE_CLIENT_SECRET: 'secret',
    OTEL_EXPORTER_OTLP_HEADERS: 'Authorization=secret',
    OTEL_EXPORTER_OTLP_TRACES_HEADERS: 'Authorization=secret',
    COPILOT_CUSTOM_INSTRUCTIONS_DIRS: '/personal/instructions',
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: 'true',
  });
  assert.equal(env.HOME, '/tmp/isolated/home');
  assert.equal(env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, 'https://relay.example.com/v1/traces');
  assert.equal(env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT, 'https://relay.example.com/v1/metrics');
  assert.equal(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT, 'false');
  assert.equal(env.COPILOT_GITHUB_TOKEN, 'test-github-token');
  assert.equal(env.OTEL_EXPORTER_OTLP_PROTOCOL, 'http/protobuf');
  assert.equal(env.OTEL_RESOURCE_ATTRIBUTES, `copilot.run.id=${runId},copilot.audit.scenario=metadata-only`);
  for (const name of ['AZURE_CLIENT_SECRET', 'OTEL_EXPORTER_OTLP_HEADERS',
    'OTEL_EXPORTER_OTLP_TRACES_HEADERS', 'COPILOT_CUSTOM_INSTRUCTIONS_DIRS']) {
    assert.equal(env[name], undefined);
  }
});

test('smoke accepts only HTTPS base URLs and UUID correlation IDs', () => {
  assert.equal(validateOptions({ endpoint: 'https://relay.example.com/', output: '.local/run', runId }).endpoint,
    'https://relay.example.com');
  for (const endpoint of ['http://relay.example.com', 'https://user:password@relay.example.com',
    'https://relay.example.com?key=secret', 'https://relay.example.com/v1/logs']) {
    assert.throws(() => validateOptions({ endpoint, output: '.local/run', runId }));
  }
  assert.throws(() => validateOptions({ endpoint: 'https://relay.example.com', output: '.local/run', runId: "bad'uuid" }));
  assert.throws(() => validateOptions({ endpoint: 'https://relay.example.com', runId }));
});

test('opt-in host.name survives validation and is encoded as one resource attribute', () => {
  const hostName = 'test-host,other=value';
  const options = validateOptions({
    endpoint: 'https://relay.example.com', output: '.local/run', runId, hostName,
  });
  assert.equal(options.hostName, hostName);
  const env = buildEnvironment('/tmp/isolated/home', runId, options.endpoint, 'test-token', {}, hostName);
  assert.equal(env.OTEL_RESOURCE_ATTRIBUTES,
    `copilot.run.id=${runId},copilot.audit.scenario=metadata-only,host.name=test-host%2Cother%3Dvalue`);
  assert.equal(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT, 'false');
  assert.equal(env.OTEL_EXPORTER_OTLP_HEADERS, undefined);
});

test('hostname opt-in rejects empty, padded, control-character and oversized values', () => {
  for (const hostName of ['', ' host', 'host ', 'host\nname', 'host\u0000name', 'x'.repeat(256), 42]) {
    assert.throws(() => validateOptions({
      endpoint: 'https://relay.example.com', output: '.local/run', runId, hostName,
    }), /host-name/);
  }
});

test('opt-in user.id is encoded alongside host.name without accepting injected attributes', () => {
  const userId = 'DOMAIN\\test.user@example.com,role=admin';
  const options = validateOptions({
    endpoint: 'https://relay.example.com', output: '.local/run', runId,
    hostName: 'test-host', userId,
  });
  assert.equal(options.userId, userId);
  const env = buildEnvironment('/tmp/isolated/home', runId, options.endpoint, 'test-token',
    { USER: 'wrong-user', OTEL_RESOURCE_ATTRIBUTES: 'user.id=wrong-user' }, options.hostName, options.userId);
  assert.equal(env.OTEL_RESOURCE_ATTRIBUTES,
    `copilot.run.id=${runId},copilot.audit.scenario=metadata-only,host.name=test-host,` +
    'user.id=DOMAIN%5Ctest.user%40example.com%2Crole%3Dadmin');
  assert.equal(env.USER, undefined);
  assert.equal(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT, 'false');
  assert.equal(env.OTEL_EXPORTER_OTLP_HEADERS, undefined);
});

test('user.id can be supplied without host.name and is validated explicitly', () => {
  const env = buildEnvironment('/tmp/isolated/home', runId, 'https://relay.example.com',
    'test-token', {}, undefined, 'test-user');
  assert.equal(env.OTEL_RESOURCE_ATTRIBUTES,
    `copilot.run.id=${runId},copilot.audit.scenario=metadata-only,user.id=test-user`);
  for (const userId of ['', ' user', 'user ', 'user\nname', 'user\u0000name', 'x'.repeat(256), 42]) {
    assert.throws(() => validateOptions({
      endpoint: 'https://relay.example.com', output: '.local/run', runId, userId,
    }), /user-id/);
  }
});

test('synthetic smoke disables tools, instructions, and auto update', () => {
  const args = cliArguments(runId);
  for (const flag of ['--no-custom-instructions', '--no-auto-update', '--disable-builtin-mcps',
    '--no-ask-user', '--available-tools', '--deny-tool=shell', '--deny-tool=write', '--deny-tool=url']) {
    assert.ok(args.includes(flag));
  }
  assert.match(args.at(-1), new RegExp(runId));
  assert.ok(args.includes('--secret-env-vars=COPILOT_GITHUB_TOKEN'));
});

test('private diagnostic output redacts GitHub token and bearer credentials', () => {
  const output = redact('test-token Authorization: Bearer other-value', 'test-token');
  assert.ok(!output.includes('test-token'));
  assert.ok(!output.includes('other-value'));
});
