import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, lstat, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const repository = fileURLToPath(new URL('../../', import.meta.url));
const privateRoot = path.join(repository, '.local');
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;

export function createIsolationDirectory() {
  return mkdtemp(path.join(tmpdir(), 'copilot-relay-'));
}

export function validateOptions({ endpoint, output, runId = randomUUID(), hostName, userId }) {
  const url = new URL(endpoint);
  if (url.protocol !== 'https:' || url.username || url.password || url.search ||
      url.hash || url.pathname !== '/' || (url.port && url.port !== '443')) {
    throw new Error('Endpoint must be an HTTPS base URL on port 443 without credentials, path, or query');
  }
  if (!uuid.test(runId)) throw new Error('run-id must be a lowercase UUID');
  for (const [name, value] of [['host-name', hostName], ['user-id', userId]]) {
    if (value !== undefined && (typeof value !== 'string' || !value ||
        value.length > 255 || value.trim() !== value || /[\x00-\x1f\x7f]/.test(value))) {
      throw new Error(`${name} must be a nonempty unpadded string of at most 255 characters without controls`);
    }
  }
  if (!output) throw new Error('A new private output directory is required');
  const destination = path.resolve(repository, output);
  if (!destination.startsWith(`${privateRoot}${path.sep}`)) {
    throw new Error('Output must be a new directory underneath repository .local/');
  }
  return { endpoint: url.origin, output: destination, runId, hostName, userId };
}

export function buildEnvironment(home, runId, endpoint, token, inherited = process.env, hostName, userId) {
  const env = Object.fromEntries(['PATH', 'LANG', 'LC_ALL']
    .filter(key => inherited[key]).map(key => [key, inherited[key]]));
  return {
    ...env,
    HOME: home,
    COPILOT_HOME: path.join(home, '.copilot'),
    XDG_CONFIG_HOME: path.join(home, '.config'),
    XDG_CACHE_HOME: path.join(home, '.cache'),
    XDG_DATA_HOME: path.join(home, '.local/share'),
    XDG_STATE_HOME: path.join(home, '.local/state'),
    COPILOT_GITHUB_TOKEN: token,
    COPILOT_AUTO_UPDATE: 'false',
    COPILOT_OTEL_ENABLED: 'true',
    COPILOT_OTEL_EXPORTER_TYPE: 'otlp-http',
    OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
    OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: 'http/protobuf',
    OTEL_EXPORTER_OTLP_METRICS_PROTOCOL: 'http/protobuf',
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: `${endpoint}/v1/traces`,
    OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: `${endpoint}/v1/metrics`,
    OTEL_SERVICE_NAME: 'github-copilot',
    OTEL_RESOURCE_ATTRIBUTES: `copilot.run.id=${runId},copilot.audit.scenario=metadata-only` +
      (hostName === undefined ? '' : `,host.name=${encodeURIComponent(hostName)}`) +
      (userId === undefined ? '' : `,user.id=${encodeURIComponent(userId)}`),
    OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: 'false',
    NO_COLOR: '1',
  };
}

export function cliArguments(runId) {
  return [
    '--no-auto-update', '--no-custom-instructions', '--disable-builtin-mcps',
    '--no-ask-user', '--disallow-temp-dir', '--deny-tool=shell', '--deny-tool=write',
    '--deny-tool=url', '--secret-env-vars=COPILOT_GITHUB_TOKEN', '--available-tools',
    '-p', `Reply exactly SYNTHETIC_RELAY_${runId}. Do not use any tools.`,
  ];
}

export function redact(text, token) {
  return (token ? text.replaceAll(token, '[REDACTED]') : text)
    .replace(/Bearer(?:%20|\s+)[^\s"',;]+/gi, 'Bearer [REDACTED]');
}

async function execute(options) {
  const { endpoint, output, runId, hostName, userId } = validateOptions(options);
  let token = process.env.COPILOT_GITHUB_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
  if (!token) {
    const auth = spawnSync('gh', ['auth', 'token', '--hostname', 'github.com'], {
      encoding: 'utf8', timeout: 15000, maxBuffer: 65536,
    });
    if (auth.error || auth.status !== 0) throw new Error('GitHub authentication unavailable; use gh auth login');
    token = auth.stdout.trim();
  }
  if (!token || /\s/.test(token)) throw new Error('GitHub authentication returned an invalid token');
  await mkdir(privateRoot, { recursive: true, mode: 0o700 });
  await mkdir(output, { mode: 0o700 });
  const temporary = await createIsolationDirectory();
  const home = path.join(temporary, 'home');
  const work = path.join(temporary, 'work');
  const manifest = {
    run_id: runId, transport: 'function-relay', scenario: 'metadata-only',
    capture_content: false, client_azure_auth: false, endpoint,
    ...(hostName === undefined ? {} : { host_name: hostName }),
    ...(userId === undefined ? {} : { user_id: userId }),
    started_at: new Date().toISOString(), status: 'running', azure_ingestion_proven: false,
  };
  const save = (name, content) => writeFile(path.join(output, name), redact(content, token), { mode: 0o600 });
  let failed;
  try {
    await mkdir(home, { mode: 0o700 });
    await mkdir(work, { mode: 0o700 });
    const env = buildEnvironment(home, runId, endpoint, token, process.env, hostName, userId);
    const versionEnv = Object.fromEntries(Object.entries(env).filter(([key]) => !key.startsWith('OTEL_')));
    versionEnv.COPILOT_OTEL_ENABLED = 'false';
    const version = spawnSync('copilot', ['--version'], {
      env: versionEnv, cwd: work, encoding: 'utf8', timeout: 30000,
    });
    if (version.error || version.status !== 0) throw new Error('Cannot obtain Copilot CLI version');
    manifest.cli_version = version.stdout.trim().split('\n')[0];
    const result = spawnSync('copilot', cliArguments(runId), {
      env, cwd: work, encoding: 'utf8', timeout: 180000,
      killSignal: 'SIGKILL', maxBuffer: 8 * 1024 * 1024,
    });
    await save('cli-stdout.txt', result.stdout ?? '');
    await save('cli-stderr.txt', result.stderr ?? '');
    manifest.exit_code = result.status;
    if (result.error) throw new Error(`Synthetic CLI execution failed: ${result.error.code}`);
    if (result.status !== 0) throw new Error(`Synthetic CLI exited ${result.status}; inspect private diagnostics`);
    if (!result.stdout.includes(`SYNTHETIC_RELAY_${runId}`)) {
      throw new Error('Synthetic CLI response did not include the expected marker');
    }
    manifest.status = 'awaiting_backend_verification';
  } catch (error) {
    manifest.status = 'failed';
    manifest.error = redact(error.message, token);
    failed = error;
  } finally {
    try {
      const logs = path.join(home, '.copilot', 'logs');
      let entries;
      try {
        entries = await readdir(logs);
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
        entries = [];
      }
      for (const name of entries.filter(name => name.endsWith('.log'))) {
        const file = path.join(logs, name);
        const stat = await lstat(file);
        if (!stat.isFile() || stat.size > 8 * 1024 * 1024) {
          throw new Error('Unsafe or oversized synthetic CLI diagnostic file');
        }
        await save(`diagnostic-${name}`, await readFile(file, 'utf8'));
      }
    } catch (error) {
      manifest.status = 'failed';
      manifest.error = redact(error.message, token);
      failed = error;
    } finally {
      manifest.finished_at = new Date().toISOString();
      await save('manifest.json', `${JSON.stringify(manifest, null, 2)}\n`);
      await rm(temporary, { recursive: true });
    }
  }
  if (failed) throw failed;
  console.log(JSON.stringify({ run_id: runId, status: manifest.status, output }));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const { values } = parseArgs({
      options: {
        endpoint: { type: 'string' }, output: { type: 'string' },
        'run-id': { type: 'string' }, 'host-name': { type: 'string' }, 'user-id': { type: 'string' },
      },
    });
    await execute({
      endpoint: values.endpoint, output: values.output,
      runId: values['run-id'], hostName: values['host-name'], userId: values['user-id'],
    });
  } catch (error) {
    console.error(`Relay smoke failed: ${error.message}`);
    process.exitCode = 1;
  }
}
