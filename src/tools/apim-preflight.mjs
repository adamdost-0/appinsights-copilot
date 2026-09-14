import { constants } from 'node:fs';
import { lstat, open, realpath } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const NAME = /^[A-Za-z0-9_.()-]{1,90}$/;
const MAX_INPUT_BYTES = 1024 * 1024;

function requireValue(valid, message) {
  if (!valid) throw new Error(`APIM preflight: ${message}`);
}

function plainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function nativeOrigin(value) {
  requireValue(typeof value === 'string'
    && /^https:\/\/[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ingest\.monitor\.azure\.com$/.test(value),
  'invalid native ingestion origin');
  return value;
}

function resourceName(id, subscription, group, type) {
  const prefix = `/subscriptions/${subscription}/resourceGroups/${group}/providers/${type}/`;
  requireValue(typeof id === 'string' && id.startsWith(prefix) && NAME.test(id.slice(prefix.length)),
    'invalid or mismatched native resource ID');
  return id.slice(prefix.length);
}

function verifyReadback(readback, id, native) {
  requireValue(plainObject(readback) && readback.id === id
    && readback.location === native.location && plainObject(readback.tags)
    && readback.tags.solution === 'copilot-otel-v1'
    && readback.tags['ownership-marker'] === native.ownership_marker
    && plainObject(readback.properties), 'native resource readback identity or ownership drift');
}

function verifyNativeStreams(dcr, native) {
  const destinations = new Map();
  requireValue(plainObject(dcr.destinations), 'DCR destinations missing');
  for (const [kind, resourceField, resourceId] of [
    ['logAnalytics', 'workspaceResourceId', native.workspace_resource_id],
    ['monitoringAccounts', 'accountResourceId', native.azure_monitor_workspace_resource_id],
  ]) {
    const entries = dcr.destinations[kind];
    requireValue(Array.isArray(entries) && entries.length > 0, 'native DCR destination missing');
    for (const entry of entries) {
      requireValue(plainObject(entry) && typeof entry.name === 'string' && NAME.test(entry.name)
        && entry[resourceField] === resourceId && !destinations.has(entry.name),
      'native DCR destination identity drift');
      destinations.set(entry.name, kind);
    }
  }
  requireValue(plainObject(dcr.directDataSources), 'native direct datasources missing');
  requireValue(Array.isArray(dcr.dataFlows) && dcr.dataFlows.length > 0
    && dcr.dataFlows.every(flow => plainObject(flow)
      && Array.isArray(flow.streams) && flow.streams.length > 0
      && flow.streams.every(stream => typeof stream === 'string')
      && Array.isArray(flow.destinations) && flow.destinations.length > 0
      && flow.destinations.every(name => typeof name === 'string' && destinations.has(name))),
  'DCR flow provenance missing');
  for (const [kind, expectedStreams, destinationKind] of [
    ['otelLogs', ['Microsoft-OTel-Logs'], 'logAnalytics'],
    ['otelTraces', ['Microsoft-OTel-Traces-Spans', 'Microsoft-OTel-Traces-Events', 'Microsoft-OTel-Traces-Resources'], 'logAnalytics'],
    ['otelMetrics', ['Custom-Metrics-Otel'], 'monitoringAccounts'],
  ]) {
    const sources = dcr.directDataSources[kind];
    requireValue(Array.isArray(sources) && sources.length > 0
      && sources.every(source => plainObject(source) && Array.isArray(source.streams)
        && source.streams.length > 0 && source.streams.every(stream => expectedStreams.includes(stream))),
    'native direct datasource stream mismatch');
    const sourceStreams = new Set(sources.flatMap(source => source.streams));
    for (const stream of expectedStreams) {
      requireValue(sourceStreams.has(stream), 'required internal native stream missing');
      const flows = dcr.dataFlows.filter(flow => flow.streams.includes(stream));
      requireValue(flows.length > 0 && flows.every(flow =>
        flow.destinations.every(name => destinations.get(name) === destinationKind)
        && (flow.outputStream == null || flow.outputStream === stream)),
      'internal native stream destination mismatch');
    }
  }
}

export function buildParameters(input) {
  requireValue(plainObject(input) && plainObject(input.nativeState), 'nativeState and configuration required');
  const native = input.nativeState;
  for (const value of [input.subscriptionId, input.ownershipMarker, native.subscription_id,
    native.ownership_marker, native.workspace_customer_id]) {
    requireValue(typeof value === 'string' && UUID.test(value), 'valid canonical UUIDs required');
  }
  requireValue(input.subscriptionId === native.subscription_id, 'subscription does not match native receipt');
  requireValue(input.ownershipMarker !== native.ownership_marker, 'gateway requires a fresh ownership marker');
  requireValue(typeof native.resource_group === 'string' && NAME.test(native.resource_group)
    && native.resource_group !== 'rg-copilot-otel-apim', 'invalid native resource group');
  requireValue(typeof input.location === 'string' && /^[a-z][a-z0-9]{1,39}$/.test(input.location)
    && typeof native.location === 'string' && /^[a-z][a-z0-9]{1,39}$/.test(native.location), 'invalid Azure location');
  requireValue((input.skuName ?? 'Developer') === 'Developer', 'only Developer is supported');
  requireValue(input.activateGateway === undefined || typeof input.activateGateway === 'boolean',
    'activation must be an explicit boolean');
  requireValue(typeof input.publisherName === 'string' && /^[\x20-\x7e]{1,100}$/.test(input.publisherName),
    'invalid publisher name');
  requireValue(typeof input.publisherEmail === 'string' && input.publisherEmail.length <= 100
    && /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(input.publisherEmail),
  'invalid publisher email');
  requireValue(input.dcrResourceId === native.dcr_resource_id
    && input.dceResourceId === native.dce_resource_id, 'explicit DCR/DCE IDs must match native receipt');
  const types = {
    dcr_resource_id: 'Microsoft.Insights/dataCollectionRules',
    dce_resource_id: 'Microsoft.Insights/dataCollectionEndpoints',
    workspace_resource_id: 'Microsoft.OperationalInsights/workspaces',
    azure_monitor_workspace_resource_id: 'Microsoft.Monitor/accounts',
  };
  for (const [field, type] of Object.entries(types)) {
    resourceName(native[field], input.subscriptionId, native.resource_group, type);
  }
  requireValue(typeof native.dcr_immutable_id === 'string' && /^dcr-[a-f0-9]{32}$/.test(native.dcr_immutable_id),
    'invalid DCR immutable ID');
  verifyReadback(input.dcrReadback, native.dcr_resource_id, native);
  verifyReadback(input.dceReadback, native.dce_resource_id, native);
  const dcr = input.dcrReadback.properties;
  const dce = input.dceReadback.properties;
  requireValue(dcr.immutableId === native.dcr_immutable_id
    && dcr.dataCollectionEndpointId === native.dce_resource_id, 'native DCR immutable ID or DCE binding drift');
  const logsOrigin = nativeOrigin(dce.logsIngestion?.endpoint);
  const metricsOrigin = nativeOrigin(dce.metricsIngestion?.endpoint);
  verifyNativeStreams(dcr, native);
  for (const [signal, stream, origin] of [
    ['logs', 'Microsoft-OTLP-Logs', logsOrigin],
    ['traces', 'Microsoft-OTLP-Traces', logsOrigin],
    ['metrics', 'Custom-Metrics-Otel', metricsOrigin],
  ]) {
    const endpoint = `${origin}/datacollectionRules/${native.dcr_immutable_id}/streams/${stream}/otlp/v1/${signal}`;
    requireValue(native[`${signal}_endpoint`] === endpoint, 'native endpoint provenance mismatch');
  }
  const values = {
    location: input.location, ownershipMarker: input.ownershipMarker, skuName: 'Developer',
    activateGateway: input.activateGateway ?? false,
    publisherEmail: input.publisherEmail, publisherName: input.publisherName,
    dcrResourceGroupName: native.resource_group,
    dcrName: resourceName(native.dcr_resource_id, input.subscriptionId, native.resource_group, types.dcr_resource_id),
    logsEndpoint: native.logs_endpoint, tracesEndpoint: native.traces_endpoint, metricsEndpoint: native.metrics_endpoint,
  };
  return {
    $schema: 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#',
    contentVersion: '1.0.0.0',
    parameters: Object.fromEntries(Object.entries(values).map(([name, value]) => [name, { value }])),
  };
}

async function privateFilePath(path) {
  const absolute = resolve(path);
  const parent = dirname(absolute);
  requireValue(await realpath(parent) === parent, 'symlinked parent directories are not supported');
  const stat = await lstat(parent);
  requireValue(stat.isDirectory() && !stat.isSymbolicLink()
    && stat.uid === process.getuid() && (stat.mode & 0o077) === 0,
  'input and output parent directories must be owned and private (0700)');
  return absolute;
}

export async function runCli(args) {
  requireValue(args.length === 4 && args[0] === '--input' && args[2] === '--output'
    && args[1] && args[3], 'usage: --input private-input.json --output new-private-parameters.json');
  let inputFile;
  try {
    requireValue(typeof constants.O_NOFOLLOW === 'number' && typeof process.getuid === 'function',
      'private file checks require a POSIX host with O_NOFOLLOW');
    const inputPath = await privateFilePath(args[1]);
    const outputPath = await privateFilePath(args[3]);
    inputFile = await open(inputPath, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const stat = await inputFile.stat();
    requireValue(stat.isFile() && stat.size <= MAX_INPUT_BYTES
      && stat.uid === process.getuid() && (stat.mode & 0o077) === 0,
    'input must be an owned private file (0600) of at most 1 MiB');
    const buffer = Buffer.alloc(MAX_INPUT_BYTES + 1);
    let total = 0;
    while (total < buffer.length) {
      const { bytesRead } = await inputFile.read(buffer, total, buffer.length - total, total);
      if (bytesRead === 0) break;
      total += bytesRead;
    }
    requireValue(total <= MAX_INPUT_BYTES, 'input exceeds 1 MiB');
    let data;
    try {
      data = JSON.parse(buffer.subarray(0, total).toString('utf8'));
    } catch {
      throw new Error('APIM preflight: input must be valid JSON');
    }
    const parameters = buildParameters(data);
    const output = await open(outputPath,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    try {
      await output.writeFile(`${JSON.stringify(parameters, null, 2)}\n`);
      await output.sync();
    } finally {
      await output.close();
    }
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('APIM preflight:')) throw error;
    throw new Error('APIM preflight: file access failed; output must be new and parent directory must exist');
  } finally {
    await inputFile?.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  runCli(process.argv.slice(2)).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
