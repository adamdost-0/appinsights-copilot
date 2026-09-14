import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, mkdir, mkdtemp, readFile, readdir, rm, stat, symlink, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
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

const repository = fileURLToPath(new URL('../../', import.meta.url));
const smoke = fileURLToPath(new URL('../tools/smoke-copilot.mjs', import.meta.url));
const subscriptionKey = 'synthetic-apim+credential/42';
const githubToken = 'synthetic-github+credential/42';
const options = { endpoint: 'https://gateway.example.com/otlp', output: '.local/run', runId,
  transport: 'apim-gateway' };

test('APIM options normalize only an exact HTTPS /otlp prefix and retain the transport', () => {
  for (const endpoint of ['https://gateway.example.com/otlp', 'https://gateway.example.com:443/otlp/']) {
    assert.equal(validateOptions({ ...options, endpoint }).endpoint, options.endpoint);
    assert.equal(validateOptions({ ...options, endpoint }).transport, 'apim-gateway');
  }
  assert.equal(validateOptions({ ...options, endpoint: 'https://relay.example.com',
    transport: undefined }).transport, 'function-relay');
});

test('transport and endpoint validation reject normalization tricks and extra URL components', () => {
  for (const endpoint of ['http://gateway.example.com/otlp', 'https://user:pass@gateway.example.com/otlp',
    'https://gateway.example.com/otlp?', 'https://gateway.example.com/otlp#',
    'https://gateway.example.com:8443/otlp', 'https://gateway.example.com/', 'https://gateway.example.com/OTLP',
    'https://gateway.example.com/otlp/v1/traces', 'https://gateway.example.com/otlp//',
    'https://gateway.example.com/a/../otlp', 'https://gateway.example.com/%6ftlp',
    'https://gateway.example.com\\otlp', ' https://gateway.example.com/otlp',
    'https://gate\nway.example.com/otlp', 'https://gateway.example.com/otlp\n',
    'https://gateway.example.com/otlp\r']) {
    assert.throws(() => validateOptions({ ...options, endpoint }), /Endpoint/);
  }
  assert.throws(() => validateOptions({ ...options, transport: 'unknown' }), /transport/);
  for (const endpoint of ['https://relay.example.com?', 'https://relay.example.com#',
    'https://relay.example.com/a/..', 'https://relay.example.com/otlp']) {
    assert.throws(() => validateOptions({ ...options, endpoint, transport: 'function-relay' }), /Endpoint/);
  }
});

test('APIM environment uses only an encoded telemetry header and excludes parent credentials', () => {
  const env = buildEnvironment('/tmp/isolated/home', runId, options.endpoint, githubToken, {
    PATH: '/usr/bin', APIM_SUBSCRIPTION_KEY: subscriptionKey, AZURE_CONFIG_DIR: '/private/azure',
    AZURE_CLIENT_ID: 'synthetic-id', AZURE_CLIENT_SECRET: 'synthetic-secret',
    GH_TOKEN: 'unused-synthetic-token', GITHUB_TOKEN: 'unused-synthetic-other-token',
    OTEL_EXPORTER_OTLP_TRACES_HEADERS: 'Authorization=synthetic-old',
    OTEL_EXPORTER_OTLP_HEADERS: 'Authorization=synthetic-old',
  }, undefined, undefined, 'apim-gateway');
  assert.equal(env.OTEL_EXPORTER_OTLP_HEADERS,
    `X-Copilot-Telemetry-Key=${encodeURIComponent(subscriptionKey)}`);
  assert.equal(env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, `${options.endpoint}/v1/traces`);
  assert.equal(env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT, `${options.endpoint}/v1/metrics`);
  for (const name of ['APIM_SUBSCRIPTION_KEY', 'AZURE_CONFIG_DIR', 'AZURE_CLIENT_ID',
    'AZURE_CLIENT_SECRET', 'GH_TOKEN', 'GITHUB_TOKEN', 'OTEL_EXPORTER_OTLP_TRACES_HEADERS']) {
    assert.equal(env[name], undefined);
  }
  assert.ok(cliArguments(runId, env).includes(
    '--secret-env-vars=COPILOT_GITHUB_TOKEN,OTEL_EXPORTER_OTLP_HEADERS'));
});

test('APIM credentials fail closed for absent, empty, non-ASCII, whitespace and header separators', () => {
  for (const key of [undefined, '', ' ', 'synthetic\nkey', 'synthetic\rkey', 'synthetic\tkey',
    'synthetic-key\n', 'synthetic-key\r', 'synthetic-key\r\n',
    'synthetic\u0000key', 'synthetic\u007fkey', 'synthetic\u00e9key', 'synthetic,key',
    'synthetic;key', 'synthetic:key', 'synthetic=key', 'synthetic"key', 'synthetic\\key']) {
    assert.throws(() => buildEnvironment('/tmp/home', runId, options.endpoint, githubToken,
      { APIM_SUBSCRIPTION_KEY: key }, undefined, undefined, 'apim-gateway'), /APIM_SUBSCRIPTION_KEY/);
  }
});

test('redaction removes exact credentials and reversible encodings, not generic keywords', () => {
  for (const key of [subscriptionKey, githubToken, 'synthetic\ninvalid"credential']) {
    const forms = [key, encodeURIComponent(key), encodeURIComponent(key).replace(/%[A-F0-9]{2}/g,
      value => value.toLowerCase()), JSON.stringify(key).slice(1, -1),
    Buffer.from(key).toString('base64'), Buffer.from(key).toString('base64url')];
    for (const form of forms) assert.equal(redact(`before ${form} after`, [key]), 'before [REDACTED] after');
  }
  for (const text of ['X-Copilot-Telemetry-Key=unlisted-synthetic-value',
    'x-copilot-telemetry-key: unlisted-synthetic-value',
    '"X-Copilot-Telemetry-Key":"unlisted-synthetic-value"',
    'X-Copilot-Telemetry-Key%3Dunlisted-synthetic-value']) {
    assert.ok(!redact(text, []).includes('unlisted-synthetic-value'));
  }
  assert.equal(redact('token counts and key metadata are useful', []), 'token counts and key metadata are useful');
});

test('redaction matches mixed-case percent escapes without ignoring credential letter case', () => {
  for (const encoded of ['synthetic-apim%2Bcredential%2f42', 'synthetic-apim%2bcredential%2F42']) {
    assert.equal(redact(`before ${encoded} after`, subscriptionKey), 'before [REDACTED] after');
    const differentKey = encoded.replace('synthetic', 'Synthetic');
    assert.equal(redact(differentKey, subscriptionKey), differentKey);
  }
  assert.equal(redact('synthetic-%c3%Af', 'synthetic-\u00ef'), '[REDACTED]');
});

test('redaction matches partially percent-encoded unreserved characters with exact byte case', () => {
  assert.equal(redact('%73ynthetic-apim%2Bcredential%2f42', subscriptionKey), '[REDACTED]');
  const differentKey = '%53ynthetic-apim%2Bcredential%2f42';
  assert.equal(redact(differentKey, subscriptionKey), differentKey);
  assert.equal(redact('synthetic%2ekey%2Bvalue', 'synthetic.key+value'), '[REDACTED]');
  assert.equal(redact('syntheticXkey%2Bvalue', 'synthetic.key+value'), 'syntheticXkey%2Bvalue');
});

async function fixture(t, { key = subscriptionKey, transport = 'apim-gateway', fail = false,
  diagnosticLink = false, fallback = false, versionFailure = false } = {}) {
  const root = await mkdtemp(path.join(tmpdir(), 'copilot-auth-test-'));
  const output = path.join(repository, '.local', `auth-test-${randomUUID()}`);
  t.after(async () => {
    await rm(root, { recursive: true, force: true });
    await rm(output, { recursive: true, force: true });
  });
  const bin = path.join(root, 'bin');
  await mkdir(bin, { mode: 0o700 });
  // A local executable exercises the real runner without inference or real credentials.
  await writeFile(path.join(bin, 'copilot'), `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const env = process.env;
fs.appendFileSync(${JSON.stringify(path.join(root, 'spawned'))}, 'copilot\\n');
assert.equal(env.APIM_SUBSCRIPTION_KEY, undefined);
assert.equal(env.AZURE_CONFIG_DIR, undefined);
assert.equal(env.AZURE_CLIENT_SECRET, undefined);
assert.equal(env.GH_TOKEN, undefined);
assert.equal(env.GITHUB_TOKEN, undefined);
if (process.argv.includes('--version')) {
  assert.equal(env.OTEL_EXPORTER_OTLP_HEADERS, undefined);
  assert.ok(process.argv.includes('--secret-env-vars=COPILOT_GITHUB_TOKEN'));
  console.log('test-cli ' + env.COPILOT_GITHUB_TOKEN);
  process.exitCode = ${versionFailure ? 3 : 0};
} else {
  const apim = ${JSON.stringify(transport)} === 'apim-gateway';
  assert.equal(env.OTEL_EXPORTER_OTLP_HEADERS, apim ?
    'X-Copilot-Telemetry-Key=' + encodeURIComponent(${JSON.stringify(key)}) : undefined);
  assert.equal(env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, apim ?
    'https://gateway.example.com/otlp/v1/traces' : 'https://relay.example.com/v1/traces');
  assert.equal(env.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT, apim ?
    'https://gateway.example.com/otlp/v1/metrics' : 'https://relay.example.com/v1/metrics');
  assert.equal(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT, 'false');
  assert.ok(process.argv.includes('--secret-env-vars=COPILOT_GITHUB_TOKEN' +
    (apim ? ',OTEL_EXPORTER_OTLP_HEADERS' : '')));
  for (let current = process.cwd();; current = path.dirname(current)) {
    assert.ok(!fs.existsSync(path.join(current, '.git')));
    if (current === path.dirname(current)) break;
  }
  const keys = [${JSON.stringify(key)}, env.COPILOT_GITHUB_TOKEN,
    'synthetic-unused-gh-token', 'synthetic-unused-github-token'];
  const text = keys.filter(Boolean).flatMap(key => [key, encodeURIComponent(key),
    Buffer.from(key).toString('base64'), Buffer.from(key).toString('base64url')]).join('\\n') +
    '\\nX-Copilot-Telemetry-Key: synthetic-header-value';
  const logs = path.join(env.COPILOT_HOME, 'logs');
  if (${diagnosticLink}) {
    fs.mkdirSync(env.COPILOT_HOME, { recursive: true, mode: 0o700 });
    fs.symlinkSync(${JSON.stringify(path.join(root, 'external-logs'))}, logs);
  } else {
    fs.mkdirSync(logs, { recursive: true, mode: 0o700 });
  }
  fs.writeFileSync(path.join(logs, 'synthetic.log'), text, { mode: 0o600 });
  console.log('SYNTHETIC_RELAY_${runId}\\n' + text);
  console.error(text);
  process.exitCode = ${fail ? 7 : 0};
}
`, { mode: 0o700 });
  await writeFile(path.join(bin, 'gh'), `#!${process.execPath}
const assert = require('node:assert/strict');
for (const name of ['APIM_SUBSCRIPTION_KEY', 'AZURE_CLIENT_SECRET', 'AZURE_CONFIG_DIR',
  'OTEL_EXPORTER_OTLP_HEADERS']) assert.equal(process.env[name], undefined);
require('node:fs').appendFileSync(${JSON.stringify(path.join(root, 'spawned'))}, 'gh\\n');
console.log(${JSON.stringify(githubToken)});
`, { mode: 0o700 });
  if (diagnosticLink) await mkdir(path.join(root, 'external-logs'), { mode: 0o700 });
  const env = {
    PATH: bin, TMPDIR: tmpdir(), COPILOT_GITHUB_TOKEN: githubToken,
    GH_TOKEN: 'synthetic-unused-gh-token', GITHUB_TOKEN: 'synthetic-unused-github-token',
    APIM_SUBSCRIPTION_KEY: key, AZURE_CLIENT_SECRET: 'synthetic-azure-secret',
    AZURE_CONFIG_DIR: '/synthetic/azure',
  };
  const args = [smoke, '--endpoint', transport === 'apim-gateway' ? options.endpoint : 'https://relay.example.com',
    '--output', output, '--run-id', runId, ...(transport === 'function-relay' ? [] : ['--transport', transport])];
  if (fallback) {
    delete env.COPILOT_GITHUB_TOKEN;
    delete env.GH_TOKEN;
    delete env.GITHUB_TOKEN;
  }
  return { root, output, env, args, run: () => spawnSync(process.execPath, args,
    { env, encoding: 'utf8', timeout: 15000 }) };
}

for (const transport of ['function-relay', 'apim-gateway']) {
  for (const fail of [false, true]) {
    test(`executable ${transport} ${fail ? 'failure' : 'success'} preserves private redacted evidence`, async t => {
      const f = await fixture(t, { transport, fail });
      const result = f.run();
      assert.equal(result.status, fail ? 1 : 0, result.stderr);
      const manifest = JSON.parse(await readFile(path.join(f.output, 'manifest.json'), 'utf8'));
      assert.equal(manifest.transport, transport);
      assert.equal(manifest.client_azure_auth, false);
      assert.equal(manifest.client_auth, transport === 'apim-gateway' ? 'apim-subscription-key' : undefined);
      assert.equal(manifest.status, fail ? 'failed' : 'awaiting_backend_verification');
      assert.equal(manifest.capture_content, false);
      assert.equal(manifest.azure_ingestion_proven, false);
      assert.equal(manifest.exit_code, fail ? 7 : 0);
      const contents = [result.stdout, result.stderr];
      for (const name of await readdir(f.output)) {
        contents.push(name, await readFile(path.join(f.output, name), 'utf8'));
        assert.equal((await stat(path.join(f.output, name))).mode & 0o777, 0o600);
      }
      for (const key of [subscriptionKey, githubToken, 'synthetic-unused-gh-token',
        'synthetic-unused-github-token', 'synthetic-header-value']) {
        for (const form of [key, encodeURIComponent(key), Buffer.from(key).toString('base64'),
          Buffer.from(key).toString('base64url')]) assert.ok(!contents.join('\n').includes(form), form);
      }
      assert.equal((await stat(f.output)).mode & 0o777, 0o700);
    });
  }
}

test('executable APIM rejects bad keys before any subprocess or output directory', async t => {
  const f = await fixture(t);
  for (const key of [undefined, '', 'synthetic\nkey', 'synthetic-key\n', 'synthetic-key\r', 'synthetic;key']) {
    f.env.APIM_SUBSCRIPTION_KEY = key;
    delete f.env.COPILOT_GITHUB_TOKEN;
    delete f.env.GH_TOKEN;
    delete f.env.GITHUB_TOKEN;
    const result = f.run();
    assert.equal(result.status, 1);
    assert.match(result.stderr, /APIM_SUBSCRIPTION_KEY/);
    if (key) assert.ok(!result.stderr.includes(key));
    await assert.rejects(stat(path.join(f.root, 'spawned')), { code: 'ENOENT' });
    await assert.rejects(stat(f.output), { code: 'ENOENT' });
  }
});

test('executable parse and validation errors redact active keys before execution', async t => {
  const f = await fixture(t);
  for (const key of [subscriptionKey, githubToken, 'synthetic-unused-gh-token']) {
    const result = spawnSync(process.execPath, [smoke, `--invalid-${encodeURIComponent(key)}`],
      { env: f.env, encoding: 'utf8' });
    assert.equal(result.status, 1);
    assert.ok(!result.stderr.includes(key));
    assert.ok(!result.stderr.includes(encodeURIComponent(key)));
  }
  f.args.push('--apim-subscription-key', subscriptionKey);
  assert.equal(f.run().status, 1);
  await assert.rejects(stat(path.join(f.root, 'spawned')), { code: 'ENOENT' });
});

test('executable parse errors redact mixed-case percent escapes before any subprocess', async t => {
  const f = await fixture(t);
  for (const encoded of ['synthetic-apim%2Bcredential%2f42', '%73ynthetic-apim%2Bcredential%2f42']) {
    const result = spawnSync(process.execPath, [smoke, `--invalid-${encoded}`],
      { env: f.env, encoding: 'utf8' });
    assert.equal(result.status, 1);
    assert.ok(!result.stderr.includes(encoded), result.stderr);
    assert.ok(!result.stdout.includes(encoded));
    assert.match(result.stderr, /--invalid-\[REDACTED\]/);
    await assert.rejects(stat(path.join(f.root, 'spawned')), { code: 'ENOENT' });
    await assert.rejects(stat(f.output), { code: 'ENOENT' });
  }
});

test('isolation refuses a Git ancestor even through a symlinked TMPDIR', async t => {
  const f = await fixture(t, { transport: 'function-relay' });
  await writeFile(path.join(f.root, '.git'), 'gitdir: /synthetic/not-used\n');
  const nested = path.join(f.root, 'nested');
  await mkdir(nested);
  const link = path.join(f.root, 'linked');
  await symlink(nested, link);
  f.env.TMPDIR = link;
  const result = f.run();
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Git ancestor/);
  await assert.rejects(stat(path.join(f.root, 'spawned')), { code: 'ENOENT' });
});

test('private output rejects symlink ancestors without writing through them', async t => {
  const f = await fixture(t, { transport: 'function-relay' });
  await mkdir(path.dirname(f.output), { recursive: true, mode: 0o700 });
  await symlink(f.root, f.output);
  f.args[f.args.indexOf('--output') + 1] = path.join(f.output, 'escaped');
  const result = f.run();
  assert.equal(result.status, 1);
  assert.match(result.stderr, /private|symlink/i);
  await assert.rejects(stat(path.join(f.root, 'escaped')), { code: 'ENOENT' });
  await assert.rejects(stat(path.join(f.root, 'spawned')), { code: 'ENOENT' });
});

test('GitHub auth fallback excludes Azure and APIM credentials and redacts the acquired token', async t => {
  const f = await fixture(t, { fallback: true, fail: true });
  const result = f.run();
  assert.equal(result.status, 1);
  assert.equal(await readFile(path.join(f.root, 'spawned'), 'utf8'), 'gh\ncopilot\ncopilot\n');
  const manifest = JSON.parse(await readFile(path.join(f.output, 'manifest.json'), 'utf8'));
  assert.equal(manifest.exit_code, 7);
  for (const text of [result.stderr, await readFile(path.join(f.output, 'cli-stderr.txt'), 'utf8'),
    JSON.stringify(manifest)]) {
    assert.ok(!text.includes(githubToken));
    assert.ok(!text.includes(encodeURIComponent(githubToken)));
  }
});

test('diagnostic directory symlinks fail closed instead of copying outside logs', async t => {
  const f = await fixture(t, { diagnosticLink: true });
  const result = f.run();
  assert.equal(result.status, 1);
  const manifest = JSON.parse(await readFile(path.join(f.output, 'manifest.json'), 'utf8'));
  assert.equal(manifest.status, 'failed');
  assert.match(manifest.error, /diagnostic/i);
  assert.ok(!(await readdir(f.output)).some(name => name.startsWith('diagnostic-')));
});

test('version failure persists a failed manifest without exposing credentials', async t => {
  const f = await fixture(t, { versionFailure: true });
  const result = f.run();
  assert.equal(result.status, 1);
  const manifest = JSON.parse(await readFile(path.join(f.output, 'manifest.json'), 'utf8'));
  assert.equal(manifest.status, 'failed');
  assert.match(manifest.error, /version/);
  assert.equal(await readFile(path.join(f.root, 'spawned'), 'utf8'), 'copilot\n');
  assert.ok(!JSON.stringify(manifest).includes(githubToken));
});

test('output refuses public ancestors and an already existing destination', async t => {
  const f = await fixture(t);
  await mkdir(f.output, { mode: 0o700 });
  const existing = f.run();
  assert.equal(existing.status, 1);
  assert.match(existing.stderr, /EEXIST/);
  await chmod(f.output, 0o755);
  f.args[f.args.indexOf('--output') + 1] = path.join(f.output, 'nested');
  const publicAncestor = f.run();
  assert.equal(publicAncestor.status, 1);
  assert.match(publicAncestor.stderr, /private/);
  await assert.rejects(stat(path.join(f.root, 'spawned')), { code: 'ENOENT' });
});

test('isolation rejects bare Git ancestors', async t => {
  const f = await fixture(t);
  await writeFile(path.join(f.root, 'HEAD'), 'ref: refs/heads/main\n');
  await mkdir(path.join(f.root, 'objects'));
  await mkdir(path.join(f.root, 'refs'));
  f.env.TMPDIR = f.root;
  const result = f.run();
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Git ancestor/);
  await assert.rejects(stat(path.join(f.root, 'spawned')), { code: 'ENOENT' });
});
