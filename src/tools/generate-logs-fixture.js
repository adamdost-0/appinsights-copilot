import { open } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ProtobufLogsSerializer } from '@opentelemetry/otlp-transformer';

export function createLogRecord(runId, now = Date.now()) {
  if (typeof runId !== 'string' ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(runId)) {
    throw new Error('run-id must be a UUID');
  }
  const time = [Math.floor(now / 1000), (now % 1000) * 1_000_000];
  return {
    resource: { attributes: { 'service.name': 'github-copilot' } },
    instrumentationScope: { name: 'copilot-otlp-relay-fixture', version: '1.0.0' },
    hrTime: time,
    hrTimeObserved: time,
    severityNumber: 9,
    severityText: 'INFO',
    body: `SYNTHETIC_RELAY_${runId}`,
    attributes: { 'run.id': runId, 'synthetic': true },
    droppedAttributesCount: 0,
  };
}

export function generateFixture(runId, now = Date.now()) {
  const result = ProtobufLogsSerializer.serializeRequest([createLogRecord(runId, now)]);
  if (!result?.length) throw new Error('OTLP serialization produced no bytes');
  return Buffer.from(result);
}

async function main(args) {
  if (args.length !== 4 || args[0] !== '--run-id' || args[2] !== '--output' || !args[3]) {
    throw new Error('Expected --run-id UUID --output PATH');
  }
  const bytes = generateFixture(args[1]);
  const file = await open(args[3], 'wx', 0o600);
  try {
    await file.writeFile(bytes);
  } finally {
    await file.close();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(() => {
    console.error('Fixture generation failed: use --run-id UUID --output NEW_PATH; output must be writable and not exist.');
    process.exitCode = 1;
  });
}
