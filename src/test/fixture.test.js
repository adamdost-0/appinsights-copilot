import assert from 'node:assert/strict';
import { test } from 'node:test';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, statSync, rmSync, writeFileSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { JsonLogsSerializer, ProtobufLogsSerializer } from '@opentelemetry/otlp-transformer';

const fixture = await import('../tools/generate-logs-fixture.js').catch((error) => {
  if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
  return {};
});
const command = new URL('../tools/generate-logs-fixture.js', import.meta.url).pathname;

test('fixture uses official typed OTLP encoders with current time and unique run marker', () => {
  assert.equal(typeof fixture.createLogRecord, 'function');
  const runId = randomUUID();
  const now = Date.now();
  const record = fixture.createLogRecord(runId, now);
  const payload = JSON.parse(Buffer.from(JsonLogsSerializer.serializeRequest([record])).toString());
  const resource = payload.resourceLogs[0];
  assert.deepEqual(resource.resource.attributes, [{ key: 'service.name', value: { stringValue: 'github-copilot' } }]);
  const log = resource.scopeLogs[0].logRecords[0];
  assert.equal(log.body.stringValue, `SYNTHETIC_RELAY_${runId}`);
  assert.equal(log.severityNumber, 9);
  assert.equal(log.severityText, 'INFO');
  assert.equal(log.timeUnixNano, (BigInt(now) * 1000000n).toString());
  assert.equal(log.observedTimeUnixNano, log.timeUnixNano);
  assert.ok(log.attributes.some((a) => a.key === 'run.id' && a.value.stringValue === runId));
  const bytes = fixture.generateFixture(runId, now);
  assert.ok(bytes.length > 0);
  assert.deepEqual(bytes, Buffer.from(ProtobufLogsSerializer.serializeRequest([record])));
});

test('fixture rejects malformed UUIDs rather than embedding arbitrary input', () => {
  assert.equal(typeof fixture.createLogRecord, 'function');
  for (const runId of ['', 'not-uuid', 'a'.repeat(36), '../private', `${randomUUID()}\n`]) {
    assert.throws(() => fixture.createLogRecord(runId), /run-id must be a UUID/);
  }
});

test('fixture CLI writes only a private protobuf file and refuses overwrites or symlinks', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'relay-fixture-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const output = join(directory, 'logs.pb');
  const stdout = execFileSync(process.execPath, [command, '--run-id', randomUUID(), '--output', output]);
  assert.equal(stdout.length, 0);
  assert.ok(readFileSync(output).length > 0);
  assert.equal(statSync(output).mode & 0o777, 0o600);
  const original = readFileSync(output);
  for (const path of [output, join(directory, 'link.pb')]) {
    if (path !== output) symlinkSync(output, path);
    const result = spawnSync(process.execPath, [command, '--run-id', randomUUID(), '--output', path]);
    assert.equal(result.status, 1);
    assert.equal(result.stdout.length, 0);
    assert.doesNotMatch(result.stderr.toString(), new RegExp(directory));
    assert.deepEqual(readFileSync(output), original);
  }
});

test('fixture CLI rejects unknown, missing and duplicate arguments without output', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'relay-invalid-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const output = join(directory, 'logs.pb');
  writeFileSync(output, 'existing');
  for (const args of [
    [], ['--run-id', 'bad', '--output', output],
    ['--run-id', randomUUID(), '--output', output, '--unknown', 'value'],
    ['--run-id', randomUUID(), '--run-id', randomUUID(), '--output', output],
  ]) {
    const result = spawnSync(process.execPath, [command, ...args]);
    assert.equal(result.status, 1);
    assert.equal(result.stdout.length, 0);
    assert.match(result.stderr.toString(), /Fixture generation failed/);
    assert.equal(readFileSync(output, 'utf8'), 'existing');
  }
});
