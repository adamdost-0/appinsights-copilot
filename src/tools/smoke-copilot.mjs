import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, lstat, realpath, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';

const repository = fileURLToPath(new URL('../../', import.meta.url));
const privateRoot = path.join(repository, '.local');
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;

const secretVariables = [
  'COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
  'OTEL_EXPORTER_OTLP_HEADERS', 'OTEL_EXPORTER_OTLP_TRACES_HEADERS',
  'OTEL_EXPORTER_OTLP_METRICS_HEADERS', 'OTEL_EXPORTER_OTLP_LOGS_HEADERS',
];

async function optionalStat(file) {
  try {
    return await lstat(file);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    return undefined;
  }
}

async function rejectGitAncestors(directory) {
  for (let current = await realpath(directory);; current = path.dirname(current)) {
    if (await optionalStat(path.join(current, '.git')) ||
        (await optionalStat(path.join(current, 'HEAD')) &&
         await optionalStat(path.join(current, 'objects')) &&
         await optionalStat(path.join(current, 'refs')))) {
      throw new Error('Isolation directory must not have a Git ancestor');
    }
    if (current === path.dirname(current)) break;
  }
}

export async function createIsolationDirectory() {
  const root = await realpath(tmpdir());
  await rejectGitAncestors(root);
  const directory = await mkdtemp(path.join(root, 'copilot-relay-'));
  try {
    await rejectGitAncestors(directory);
    return directory;
  } catch (error) {
    await rm(directory, { recursive: true });
    throw error;
  }
}

function validateTransport(transport) {
  if (!['function-relay', 'apim-gateway'].includes(transport)) {
    throw new Error('transport must be function-relay or apim-gateway');
  }
}

function normalizeEndpoint(endpoint, transport) {
  validateTransport(transport);
  const invalid = () => new Error('Endpoint must be an HTTPS URL on port 443 without credentials, query, or fragment; ' +
    (transport === 'apim-gateway' ? 'APIM requires the exact /otlp base prefix' : 'Function requires a base origin'));
  const match = typeof endpoint === 'string' && endpoint.match(/^https:\/\/([^/?#\\\s]+)(\/[^?#\\\s]*)?$/i);
  const paths = transport === 'apim-gateway' ? ['/otlp', '/otlp/'] : ['', '/'];
  if (!match || !paths.includes(match[2] ?? '')) throw invalid();
  let url;
  try {
    url = new URL(endpoint);
  } catch {
    throw invalid();
  }
  if (url.protocol !== 'https:' || url.username || url.password || match[1].includes('@') ||
      match[1].includes('%') || (url.port && url.port !== '443')) throw invalid();
  return `${url.origin}${transport === 'apim-gateway' ? '/otlp' : ''}`;
}

function clientHeaders(transport, inherited) {
  validateTransport(transport);
  if (transport === 'function-relay') return {};
  const key = inherited.APIM_SUBSCRIPTION_KEY;
  // Allow slash for opaque credentials; encode the value for OTEL key-value syntax.
  if (typeof key !== 'string' || !/^[!#$%&'*+\-.^_`|~0-9A-Za-z/]+$/.test(key)) {
    throw new Error('APIM_SUBSCRIPTION_KEY must be nonempty ASCII without whitespace, controls, or header separators');
  }
  return { OTEL_EXPORTER_OTLP_HEADERS: `X-Copilot-Telemetry-Key=${encodeURIComponent(key)}` };
}

export function validateOptions({ endpoint, output, runId = randomUUID(), hostName, userId,
  transport = 'function-relay' }) {
  const normalized = normalizeEndpoint(endpoint, transport);
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
  return { endpoint: normalized, output: destination, runId, hostName, userId, transport };
}

export function buildEnvironment(home, runId, endpoint, token, inherited = process.env, hostName, userId,
  transport = 'function-relay') {
  endpoint = normalizeEndpoint(endpoint, transport);
  const headers = clientHeaders(transport, inherited);
  const env = Object.fromEntries(['PATH', 'LANG', 'LC_ALL']
    .filter(key => inherited[key]).map(key => [key, inherited[key]]));
  return {
    ...env,
    ...headers,
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

function secretArgument(env) {
  return `--secret-env-vars=${secretVariables.filter(key => env[key]).join(',')}`;
}

export function cliArguments(runId, env = { COPILOT_GITHUB_TOKEN: true }) {
  return [
    '--no-auto-update', '--no-custom-instructions', '--disable-builtin-mcps',
    '--no-ask-user', '--disallow-temp-dir', '--deny-tool=shell', '--deny-tool=write',
    '--deny-tool=url', secretArgument(env), '--available-tools',
    '-p', `Reply exactly SYNTHETIC_RELAY_${runId}. Do not use any tools.`,
  ];
}

export function redact(text, token) {
  const variants = new Set();
  const encodedPatterns = new Set();
  for (const key of (Array.isArray(token) ? token : [token])) {
    if (typeof key !== 'string' || !key) continue;
    encodedPatterns.add(Array.from(key.toWellFormed(), character => {
      const escaped = [...Buffer.from(character)].map(byte => `%${byte.toString(16).padStart(2, '0')
        .replace(/[a-f]/g, digit => `[${digit}${digit.toUpperCase()}]`)}`).join('');
      // Only escape hex digits are case-insensitive; credential bytes remain exact.
      return encodeURIComponent(character) === character
        ? `(?:${character.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}|${escaped})`
        : escaped;
    }).join(''));
    for (const value of [key,
      JSON.stringify(key).slice(1, -1), Buffer.from(key).toString('base64'), Buffer.from(key).toString('base64url')]) {
      variants.add(value);
    }
  }
  for (const value of [...variants].sort((a, b) => b.length - a.length)) {
    text = text.replaceAll(value, '[REDACTED]');
  }
  for (const pattern of [...encodedPatterns].sort((a, b) => b.length - a.length)) {
    text = text.replace(new RegExp(pattern, 'g'), '[REDACTED]');
  }
  return text
    .replace(/(X-Copilot-Telemetry-Key["']?(?:\s*[:=]\s*|%3[ad])["']?)[^\s"',;]+/gi, '$1[REDACTED]')
    .replace(/Bearer(?:%20|\s+)[^\s"',;]+/gi, 'Bearer [REDACTED]');
}

function activeSecrets(env) {
  return [...secretVariables.map(key => env[key]), env.APIM_SUBSCRIPTION_KEY];
}

async function createPrivateOutput(output) {
  const parts = path.relative(privateRoot, output).split(path.sep);
  let directory = privateRoot;
  for (let index = -1; index < parts.length; index++) {
    if (index >= 0) directory = path.join(directory, parts[index]);
    try {
      await mkdir(directory, { mode: 0o700 });
    } catch (error) {
      if (error.code !== 'EEXIST' || index === parts.length - 1) throw error;
    }
    const stat = await lstat(directory);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) ||
        (process.getuid && stat.uid !== process.getuid())) {
      throw new Error('Output requires owned private directories without symlink ancestors');
    }
  }
}

async function execute(options, secrets) {
  const { endpoint, output, runId, hostName, userId, transport } = validateOptions(options);
  clientHeaders(transport, process.env);
  const temporary = await createIsolationDirectory();
  try {
    let token = process.env.COPILOT_GITHUB_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
    if (!token) {
      const auth = spawnSync('gh', ['auth', 'token', '--hostname', 'github.com'], {
        env: Object.fromEntries(['PATH', 'LANG', 'LC_ALL', 'HOME', 'XDG_CONFIG_HOME', 'GH_CONFIG_DIR']
          .filter(key => process.env[key]).map(key => [key, process.env[key]])),
        encoding: 'utf8', timeout: 15000, maxBuffer: 65536,
      });
      if (auth.error || auth.status !== 0) throw new Error('GitHub authentication unavailable; use gh auth login');
      token = auth.stdout.trim();
    }
    secrets.push(token);
    if (!token || /\s/.test(token)) throw new Error('GitHub authentication returned an invalid token');
    await createPrivateOutput(output);
    await runCli({ endpoint, output, runId, hostName, userId, transport }, temporary, token, secrets);
  } finally {
    await rm(temporary, { recursive: true });
  }
}

async function runCli({ endpoint, output, runId, hostName, userId, transport }, temporary, token, secrets) {
  const home = path.join(temporary, 'home');
  const work = path.join(temporary, 'work');
  const manifest = {
    run_id: runId, transport, scenario: 'metadata-only',
    ...(transport === 'apim-gateway' ? { client_auth: 'apim-subscription-key' } : {}),
    capture_content: false, client_azure_auth: false, endpoint,
    ...(hostName === undefined ? {} : { host_name: hostName }),
    ...(userId === undefined ? {} : { user_id: userId }),
    started_at: new Date().toISOString(), status: 'running', azure_ingestion_proven: false,
  };
  const save = (name, content) => writeFile(path.join(output, redact(name, secrets)), redact(content, secrets),
    { mode: 0o600, flag: 'wx' });
  let failed;
  try {
    await mkdir(home, { mode: 0o700 });
    await mkdir(work, { mode: 0o700 });
    const env = buildEnvironment(home, runId, endpoint, token, process.env, hostName, userId, transport);
    const versionEnv = Object.fromEntries(Object.entries(env).filter(([key]) => !key.startsWith('OTEL_')));
    versionEnv.COPILOT_OTEL_ENABLED = 'false';
    const version = spawnSync('copilot', ['--version', secretArgument(versionEnv)], {
      env: versionEnv, cwd: work, encoding: 'utf8', timeout: 30000,
    });
    if (version.error || version.status !== 0) throw new Error('Cannot obtain Copilot CLI version');
    manifest.cli_version = version.stdout.trim().split('\n')[0];
    const result = spawnSync('copilot', cliArguments(runId, env), {
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
    manifest.error = redact(error.message, secrets);
    failed = error;
  } finally {
    try {
      const logs = path.join(home, '.copilot', 'logs');
      for (const directory of [home, path.join(home, '.copilot'), logs]) {
        const stat = await optionalStat(directory);
        if (stat && (!stat.isDirectory() || stat.isSymbolicLink())) {
          throw new Error('Unsafe synthetic CLI diagnostic directory');
        }
      }
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
      manifest.error = redact(error.message, secrets);
      failed = error;
    } finally {
      manifest.finished_at = new Date().toISOString();
      await save('manifest.json', `${JSON.stringify(manifest,
        (_key, value) => typeof value === 'string' ? redact(value, secrets) : value, 2)}\n`);
    }
  }
  if (failed) throw failed;
  console.log(redact(JSON.stringify({ run_id: runId, status: manifest.status, output }), secrets));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const secrets = activeSecrets(process.env);
  try {
    const { values } = parseArgs({
      options: {
        endpoint: { type: 'string' }, output: { type: 'string' },
        transport: { type: 'string', default: 'function-relay' },
        'run-id': { type: 'string' }, 'host-name': { type: 'string' }, 'user-id': { type: 'string' },
      },
    });
    await execute({
      endpoint: values.endpoint, output: values.output,
      runId: values['run-id'], hostName: values['host-name'], userId: values['user-id'], transport: values.transport,
    }, secrets);
  } catch (error) {
    console.error(`Relay smoke failed: ${redact(error.message, secrets)}`);
    process.exitCode = 1;
  }
}
