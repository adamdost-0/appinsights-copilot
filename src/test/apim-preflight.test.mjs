import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { test } from 'node:test';

const helper = new URL('../tools/apim-preflight.mjs', import.meta.url);
const subscription = '00000000-0000-4000-8000-000000000001';
const marker = '00000000-0000-4000-8000-000000000002';
const nativeMarker = '00000000-0000-4000-8000-000000000003';
const prefix = `/subscriptions/${subscription}/resourceGroups/rg-copilot-otel-v1/providers`;
const immutable = `dcr-${'a'.repeat(32)}`;
const logsHost = 'https://synthetic-logs.eastus-1.ingest.monitor.azure.com';
const metricsHost = 'https://synthetic-metrics.eastus-1.ingest.monitor.azure.com';
const logStreams = ['Microsoft-OTel-Logs'];
const traceStreams = [
  'Microsoft-OTel-Traces-Spans', 'Microsoft-OTel-Traces-Events', 'Microsoft-OTel-Traces-Resources',
];
const metricStreams = ['Custom-Metrics-Otel'];

function fixture() {
  const nativeState = {
    subscription_id: subscription, resource_group: 'rg-copilot-otel-v1',
    location: 'eastus', ownership_marker: nativeMarker,
    dcr_resource_id: `${prefix}/Microsoft.Insights/dataCollectionRules/synthetic-dcr`,
    dce_resource_id: `${prefix}/Microsoft.Insights/dataCollectionEndpoints/synthetic-dce`,
    workspace_resource_id: `${prefix}/Microsoft.OperationalInsights/workspaces/synthetic-law`,
    azure_monitor_workspace_resource_id: `${prefix}/Microsoft.Monitor/accounts/synthetic-amw`,
    workspace_customer_id: '00000000-0000-4000-8000-000000000004',
    dcr_immutable_id: immutable,
    logs_endpoint: `${logsHost}/datacollectionRules/${immutable}/streams/Microsoft-OTLP-Logs/otlp/v1/logs`,
    traces_endpoint: `${logsHost}/datacollectionRules/${immutable}/streams/Microsoft-OTLP-Traces/otlp/v1/traces`,
    metrics_endpoint: `${metricsHost}/datacollectionRules/${immutable}/streams/Custom-Metrics-Otel/otlp/v1/metrics`,
  };
  const tags = { solution: 'copilot-otel-v1', 'ownership-marker': nativeMarker };
  return {
    subscriptionId: subscription, ownershipMarker: marker, location: 'eastus',
    publisherEmail: 'synthetic@example.invalid', publisherName: 'Synthetic evaluation',
    dcrResourceId: nativeState.dcr_resource_id, dceResourceId: nativeState.dce_resource_id,
    nativeState,
    dcrReadback: {
      id: nativeState.dcr_resource_id, location: 'eastus', tags,
      properties: {
        immutableId: immutable, dataCollectionEndpointId: nativeState.dce_resource_id,
        directDataSources: {
          otelLogs: [{ name: 'otelLogsDirect', streams: [...logStreams], enrichWithResourceAttributes: ['*'] }],
          otelTraces: [{ name: 'otelTracesDirect', streams: [...traceStreams], enrichWithResourceAttributes: ['*'] }],
          otelMetrics: [{ name: 'otelMetricsDirect', streams: [...metricStreams], enrichWithResourceAttributes: ['*'] }],
        },
        destinations: {
          logAnalytics: [{ name: 'nativeLogs', workspaceResourceId: nativeState.workspace_resource_id }],
          monitoringAccounts: [{ name: 'nativeMetrics', accountResourceId: nativeState.azure_monitor_workspace_resource_id }],
        },
        dataFlows: [
          { streams: [...metricStreams], destinations: ['nativeMetrics'] },
          { streams: [...logStreams, ...traceStreams], destinations: ['nativeLogs'] },
        ],
      },
    },
    dceReadback: {
      id: nativeState.dce_resource_id, location: 'eastus', tags,
      properties: { logsIngestion: { endpoint: logsHost }, metricsIngestion: { endpoint: metricsHost } },
    },
  };
}

async function load() {
  assert.ok(existsSync(helper), 'APIM offline preflight helper is missing');
  return import(helper.href);
}

test('accepts real native internal streams while preserving distinct public OTLP endpoint routes', async () => {
  const { buildParameters } = await load();
  const input = fixture();
  let result;
  assert.doesNotThrow(() => { result = buildParameters(input); });
  assert.equal(result.parameters.logsEndpoint.value, input.nativeState.logs_endpoint);
  assert.equal(result.parameters.metricsEndpoint.value, input.nativeState.metrics_endpoint);
  assert.equal(result.parameters.dcrName.value, 'synthetic-dcr');
  assert.equal(result.parameters.ownershipMarker.value, marker);
  assert.equal(result.parameters.skuName.value, 'Developer');
  assert.equal(result.parameters.activateGateway?.value, false);
  assert.equal(result.parameters.publisherEmail.value, 'synthetic@example.invalid');
  assert.ok(!JSON.stringify(result).match(/primaryKey|secondaryKey|nativeState|Readback/));
});

for (const [name, mutate] of [
  ['subscription mismatch', x => { x.subscriptionId = marker; }],
  ['malformed UUID', x => { x.ownershipMarker = 'x'.repeat(36); }],
  ['reused native marker', x => { x.ownershipMarker = nativeMarker; }],
  ['unexpected DCR', x => { x.dcrResourceId += '-other'; }],
  ['unexpected DCE', x => { x.dceResourceId += '-other'; }],
  ['foreign LAW', x => { x.nativeState.workspace_resource_id = x.nativeState.workspace_resource_id.replace(subscription, marker); }],
  ['wrong LAW customer ID', x => { x.nativeState.workspace_customer_id = 'not-a-uuid'; }],
  ['DCR immutable drift', x => { x.dcrReadback.properties.immutableId = `dcr-${'b'.repeat(32)}`; }],
  ['DCR DCE binding drift', x => { x.dcrReadback.properties.dataCollectionEndpointId += '-other'; }],
  ['native marker drift', x => { x.dceReadback.tags = {}; }],
  ['unproven host', x => { x.nativeState.logs_endpoint = x.nativeState.logs_endpoint.replace('synthetic-logs', 'foreign'); }],
  ['stream not in DCR', x => { x.dcrReadback.properties.dataFlows[0].streams.pop(); }],
  ['missing direct datasource', x => { delete x.dcrReadback.properties.directDataSources.otelTraces; }],
  ['missing internal trace resource stream', x => { x.dcrReadback.properties.directDataSources.otelTraces[0].streams.pop(); }],
  ['public route used as internal datasource stream', x => { x.dcrReadback.properties.directDataSources.otelLogs[0].streams = ['Microsoft-OTLP-Logs']; }],
  ['foreign LAW destination', x => { x.dcrReadback.properties.destinations.logAnalytics[0].workspaceResourceId += '-foreign'; }],
  ['foreign AMW destination', x => { x.dcrReadback.properties.destinations.monitoringAccounts[0].accountResourceId += '-foreign'; }],
  ['metrics flow sent to LAW', x => { x.dcrReadback.properties.dataFlows[0].destinations = ['nativeLogs']; }],
  ['logs flow sent to AMW', x => { x.dcrReadback.properties.dataFlows[1].destinations = ['nativeMetrics']; }],
  ['unresolved flow destination', x => { x.dcrReadback.properties.dataFlows[0].destinations = ['unknown']; }],
  ['duplicate destination identity', x => { x.dcrReadback.properties.destinations.logAnalytics.push({ name: 'nativeLogs', workspaceResourceId: 'foreign' }); }],
  ['extra foreign flow destination', x => {
    x.dcrReadback.properties.destinations.logAnalytics.push({ name: 'foreign', workspaceResourceId: 'foreign' });
    x.dcrReadback.properties.dataFlows[1].destinations.push('foreign');
  }],
  ['internal stream substituted in public URL', x => { x.nativeState.logs_endpoint = x.nativeState.logs_endpoint.replace('Microsoft-OTLP-Logs', 'Microsoft-OTel-Logs'); }],
  ['data flow rewrites output stream', x => { x.dcrReadback.properties.dataFlows[1].outputStream = 'Custom-Foreign'; }],
  ['query', x => { x.nativeState.metrics_endpoint += '?subscription-key=synthetic-secret'; }],
  ['URL fragment', x => { x.nativeState.logs_endpoint += '#ignored'; }],
  ['encoded path', x => { x.nativeState.logs_endpoint = x.nativeState.logs_endpoint.replace('/streams/', '/%73treams/'); }],
  ['userinfo', x => { x.nativeState.logs_endpoint = x.nativeState.logs_endpoint.replace('https://', 'https://user:password@'); }],
  ['wrong metrics immutable ID', x => { x.nativeState.metrics_endpoint = x.nativeState.metrics_endpoint.replace(immutable, `dcr-${'b'.repeat(32)}`); }],
  ['incompatible SKU', x => { x.skuName = 'Consumption'; }],
  ['ambiguous activation', x => { x.activateGateway = 'true'; }],
  ['policy injection in publisher name', x => { x.publisherName = 'bad\nvalue'; }],
]) {
  test(`fails closed for ${name} without reflecting input`, async () => {
    const { buildParameters } = await load();
    assert.doesNotThrow(() => buildParameters(fixture()), 'Unmodified native fixture must pass first');
    const input = fixture();
    mutate(input);
    assert.throws(() => buildParameters(input), error => {
      assert.match(error.message, /^APIM preflight:/);
      assert.ok(!error.message.includes('synthetic-secret'));
      return true;
    });
  });
}

test('activation requires an explicit boolean after separate policy review', async () => {
  const { buildParameters } = await load();
  const input = fixture();
  input.activateGateway = true;
  assert.equal(buildParameters(input).parameters.activateGateway?.value, true);
});

test('CLI is bounded, offline, creates private parameters and never overwrites', async () => {
  const { runCli } = await load();
  const { mkdir, mkdtemp, writeFile, stat, readFile, rm } = await import('node:fs/promises');
  const root = new URL('../../.local/', import.meta.url);
  await mkdir(root, { recursive: true, mode: 0o700 });
  const dir = await mkdtemp(new URL('apim-preflight-test-', root).pathname);
  try {
    const input = `${dir}/input.json`;
    const output = `${dir}/parameters.json`;
    await writeFile(input, JSON.stringify(fixture()), { mode: 0o600 });
    await runCli(['--input', input, '--output', output]);
    assert.equal((await stat(output)).mode & 0o777, 0o600);
    assert.equal(JSON.parse(await readFile(output, 'utf8')).parameters.skuName.value, 'Developer');
    await assert.rejects(runCli(['--input', input, '--output', output]), /APIM preflight:/);
    await writeFile(input, ' '.repeat(1024 * 1024 + 1));
    await assert.rejects(runCli(['--input', input, '--output', `${dir}/oversize.json`]), /APIM preflight:/);
    assert.ok(!existsSync(`${dir}/oversize.json`));
    await assert.rejects(runCli(['--input', input, '--output', `${dir}/bad.json`, '--execute']), /APIM preflight:/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

for (const scenario of ['input symlink', 'input directory symlink', 'output symlink',
  'output directory symlink', 'readable input', 'readable output directory']) {
  test(`CLI refuses ${scenario} without creating parameters`, async () => {
    const { runCli } = await load();
    const { mkdir, mkdtemp, writeFile, chmod, symlink, rm } = await import('node:fs/promises');
    const root = new URL('../../.local/', import.meta.url);
    await mkdir(root, { recursive: true, mode: 0o700 });
    const dir = await mkdtemp(new URL('apim-private-path-test-', root).pathname);
    try {
      let input = `${dir}/input.json`;
      let output = `${dir}/parameters.json`;
      await writeFile(input, JSON.stringify(fixture()), { mode: 0o600 });
      await runCli(['--input', input, '--output', `${dir}/baseline.json`]);
      if (scenario === 'input symlink') {
        await symlink(input, `${dir}/input-link.json`);
        input = `${dir}/input-link.json`;
      } else if (scenario === 'input directory symlink') {
        await symlink(dir, `${dir}/input-link`);
        input = `${dir}/input-link/input.json`;
      } else if (scenario === 'output symlink') {
        await symlink(`${dir}/unexpected.json`, output);
      } else if (scenario === 'output directory symlink') {
        await symlink(dir, `${dir}/output-link`);
        output = `${dir}/output-link/parameters.json`;
      } else if (scenario === 'readable input') {
        await chmod(input, 0o644);
      } else {
        await mkdir(`${dir}/public`, { mode: 0o755 });
        await chmod(`${dir}/public`, 0o755);
        output = `${dir}/public/parameters.json`;
      }
      await assert.rejects(runCli(['--input', input, '--output', output]), /APIM preflight:/);
      assert.ok(!existsSync(output), 'No output or symlink destination should be written');
      assert.ok(!existsSync(`${dir}/unexpected.json`));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
}
