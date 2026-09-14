# Verify real Copilot CLI telemetry

[Administrator guide](../README.md) | [Deploy](deployment.md) |
[Relay evidence](evidence/function-relay.md) | [Historical direct evidence](evidence/v1.md)

Complete CLI signal acceptance requires **a fresh, completed real CLI run and
matching persisted data in both Azure workspaces**. The narrower relay log
forwarding and CLI trace/event checks below do not close that metrics gate.
Unit fixtures, a standalone OTLP generator,
successful deployment, or CLI exit zero are not end-to-end ingestion proof.
All examples here use synthetic data only. The default route is now the
restricted managed-identity relay; optional existing Python CLI smoke/query
utilities continue to test their documented **direct authenticated** route.
They are tests, **not deployment prerequisites**. Historical v1 results are
not proof of this new relay.

## Relay no-client-auth acceptance

Record these independently after [relay deployment](relay-deployment.md):

| Check | Required observation |
| --- | --- |
| Platform boundary | Actual app/SCM allowlist matches approved nonempty IPv4 CIDRs; unmatched action Deny; HTTPS-only/TLS 1.2+ |
| Identity | System-assigned MI; identity host/deployment storage; exact DCR publisher assignment |
| Allowed caller | A fresh fabricated OTLP log request without Authorization, Function key, or `?code=` reaches `/v1/logs` |
| Upstream | Relay successfully forwards binary protobuf through the current explicit DCE using MI |
| LAW persistence | Exact new marker and synthetic service identity appear in the expected native log table in a bounded window |
| Denied caller | Live app denial from an unapproved source, either another external host or the same host after approved fail-closed removal of its allow rule; SCM live denial is a separate check |
| CLI signals | Separate fresh real CLI traces/events and AMW metrics, not inferred from a log generator |
| Privacy/UI | Separate policy and authenticated workbook-render acceptance, not inferred from HTTP/ARM success |

**Current measured status:** the final corrected-isolation run persisted one
standalone log, two actual CLI spans and three correlated events, with content
capture off. Complete CLI signal acceptance remains **open**: the relay native
metric query returned no series. See the authoritative
[relay evidence record](evidence/function-relay.md) for run IDs and follow-ups.
The initial real CLI smoke also produced spans/events, but its temporary home/work folders
were inside the checkout's private `.local` tree; ancestor Git metadata
exposed repository/branch names. The parent reports no project content was
read. That run must **not** serve as corrected-isolation acceptance.

The corrected harness uses a system temporary directory (normally
`/tmp/copilot-relay-*` on Linux), outside the checkout in the measured run.
Repeat CLI plus standalone log acceptance with a fresh shared UUID; do not
reuse the initial checkout-local UUID/counts as corrected-isolation proof.

Handler negative tests returned GET 405, JSON payload 415 and
unknown signal 404. Separately, live platform enforcement was verified by
temporarily removing the sole approved `/32` allow rule while keeping default
Deny: the same anonymous GET changed to **403**. Restoring the exact original
rule/priority/description returned GET **405** at the handler; final ingress
readback was preserved. No allow-all window was introduced.
The comment-free query was verified. Relay native metrics remain unverified;
an empty histogram query is not persistence proof, and metrics work must not
be inferred from the established logs-to-LAW result. Historical v1 evidence
is unchanged.

Use the runtime's official OpenTelemetry serializer fixture tool; save its
synthetic payload privately and retain the marker/time window. The fixture's
dev dependency is for testing, not the production ZIP. Do not copy normal
telemetry or send empty protobuf as acceptance:

If test dependencies are missing, install them first (deployment staging
installs production dependencies only):

```bash
npm --prefix src ci --ignore-scripts --no-audit --no-fund
```

```bash
umask 077
export RELAY_RUN_ID="$(node -p 'require("node:crypto").randomUUID()')"
export SYNTHETIC_LOG_PROTOBUF="$PWD/.local/relay-$RELAY_RUN_ID.pb"
export RELAY_LOG_MARKER="SYNTHETIC_RELAY_$RELAY_RUN_ID"
node src/tools/generate-logs-fixture.js \
  --run-id "$RELAY_RUN_ID" --output "$SYNTHETIC_LOG_PROTOBUF"
```

This generates exactly one current INFO record with
`service.name=github-copilot`, string attribute `run.id=<UUID>`, boolean
`synthetic=true` and body `SYNTHETIC_RELAY_<UUID>`. The generator refuses
overwrites. From an **allowed**
deployment/test host, the positive HTTP request must have this shape:

```bash
export RELAY_LOGS_URL="$(jq -er .logs_endpoint .local/relay-azure.json)"
curl --disable --silent --show-error --fail-with-body --max-time 60 \
  --request POST "$RELAY_LOGS_URL" \
  --header 'Content-Type: application/x-protobuf' \
  --data-binary "@$SYNTHETIC_LOG_PROTOBUF" \
  --dump-header .local/relay-log-response-headers.txt \
  --output .local/relay-log-response.bin \
  --write-out '%{http_code}\n' > .local/relay-log-http-status.txt
```

There is deliberately **no client Azure token or Function key**; `--disable`
prevents a personal curl configuration from adding headers or other options.
Confirm the
expected successful OTLP response, including rejected-record/partial-success
semantics when applicable. The relay returns the upstream status/body and
selected headers, **not a wrapper JSON object**. Successful protobuf can have
an empty body; inspect captured content type, retry guidance and any protobuf
partial-success response without publishing raw headers/payloads.
A 2xx alone does not prove persistence or every
record's acceptance; correlate actual backend rows. Use a new UUID in a
fabricated body `SYNTHETIC_RELAY_<uuid>` and known synthetic
`service.name` so old data cannot satisfy the check.

Query the actual workspace with an independently authorized query identity,
not the relay identity. The parent's read-only ARM inspection found the
`OTelLogs` table already present, but ARM initially returned null columns.
The subsequent query-time `getschema` succeeded and confirmed `Body:string`,
`Attributes:dynamic`, and `ResourceAttributes:dynamic`. This is schema evidence,
**not fixture persistence proof**. Query the actual native schema when repeating
the evaluation: the DCR's `Microsoft-OTel-Logs` route is not `OTelEvents`.

```bash
LAW_CUSTOMER_ID="$(jq -er .workspace_customer_id "${NATIVE_STATE:-.local/native-azure.json}")"
jq -n '{query:"OTelLogs | getschema"}' > .local/relay-log-schema-query.json
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/$LAW_CUSTOMER_ID/query" \
  --resource https://api.loganalytics.io --headers Content-Type=application/json \
  --body @.local/relay-log-schema-query.json > .local/relay-log-schema-result.json
jq -e 'has("error")|not' .local/relay-log-schema-result.json >/dev/null
jq -e '(.tables|length) == 1 and (.tables[0].rows|length) > 0' \
  .local/relay-log-schema-result.json >/dev/null
```

Inspect column names/types privately; initial schema availability can lag.
Using those confirmed columns, this query finds only the fresh synthetic
body marker and UUID:

```bash
# Keep RELAY_LOG_MARKER from the exact new fixture above.
LAW_CUSTOMER_ID="$(jq -er .workspace_customer_id "${NATIVE_STATE:-.local/native-azure.json}")"
jq -n --arg marker "$RELAY_LOG_MARKER" --arg run "$RELAY_RUN_ID" \
  '{query:("OTelLogs | where TimeGenerated > ago(30m) | where tostring(Body) == "
    + ($marker|tojson)
    + " | where tostring(Attributes[\"run.id\"]) == " + ($run|tojson)
    + " | project TimeGenerated, Body, Attributes, ResourceAttributes")}' \
  > .local/relay-log-query.json
az rest --method post \
  --url "https://api.loganalytics.io/v1/workspaces/$LAW_CUSTOMER_ID/query" \
  --resource https://api.loganalytics.io --headers Content-Type=application/json \
  --body @.local/relay-log-query.json > .local/relay-log-query-result.json
jq -e 'has("error")|not' .local/relay-log-query-result.json >/dev/null
jq -e '(.tables|length) == 1 and (.tables[0].rows|length) > 0' \
  .local/relay-log-query-result.json >/dev/null
```

If the measured schema differs, use its verified fields and record that
distinction; do not invent rows or silently treat missing tables as success.
Check the synthetic service identity in the returned resource attributes;
if enrichment is absent, inspect the measured resource linkage rather than
inventing a `ServiceName` column or treating missing metadata as verified.
Compare the exact expected one-record marker/service/run/time and inspect
complete column/row shapes, errors/partial errors, and OTLP rejection response.
Bound polling to ten minutes with e.g. 15-second intervals. Auth/schema errors
stop immediately; absent data at the deadline fails acceptance. Preserve raw
responses privately, and publish only reviewed sanitized findings separately
from historical `docs/evidence/v1.md`.

For the negative test, an authorized external test host outside the allowlist
can demonstrate ingress denial. Alternatively, as exercised by the parent,
an approved operator can temporarily remove the test host's sole allow rule
while preserving default Deny, so that host is genuinely unapproved during
the request. Preserve the exact original rule/CIDR/priority/description,
ensure ARM management access can restore it, observe HTTP 403, then restore
the identical rule and verify handler behavior plus final ingress readback.
This is a **fail-closed interruption**, not an allow-all interval; coordinate
it with users and do not leave the app accidentally inaccessible.

A spoofed `X-Forwarded-For`, omitted client header or handler-only unit test
does not test platform admission. App denial and SCM restriction readback are
distinct from a live SCM request-denial test; do not claim the latter without
executing it. If neither live negative method is authorized/available, report
source-IP enforcement unverified; saved configuration is a weaker check.

The relay logs test proves only that synthetic log path. It does not establish
a Copilot standalone logs exporter, complete CLI monitoring, metrics ingestion,
normal-user use, privacy filtering, or lossless delivery. A logs-only successful
run must not be relabeled as the three-scenario CLI acceptance below.

### Real CLI smoke required for relay acceptance

The relay acceptance requires **both** the standalone synthetic log above and
a real, **isolated metadata-only** Copilot CLI run through the relay. Use the
Node test runner for this separate CLI proof **after the standalone log's
exact marker has been verified in LAW**. This helper performs no deployments.
It uses fresh temporary home/work directories under Node's `os.tmpdir()`
(normally `/tmp/copilot-relay-*` on Linux), disables custom instructions
and built-in MCPs, supplies only the required environment, and passes no Azure
token/OTLP authorization headers. It still needs approved GitHub/Copilot
authentication and incurs inference usage:

```bash
# Confirm the system temp location is outside every checkout, including ancestors.
node -p 'require("node:os").tmpdir()'
# Reuse the fresh UUID whose standalone log was just verified.
: "${RELAY_RUN_ID:?Complete the fresh standalone log fixture first}"
export RELAY_CLI_RUN_ID="$RELAY_RUN_ID"
export RELAY_BASE_URL="$(node -e \
  'console.log(new URL(JSON.parse(require("node:fs").readFileSync(".local/relay-azure.json")).traces_endpoint).origin)')"
node src/tools/smoke-copilot.mjs --endpoint "$RELAY_BASE_URL" \
  --run-id "$RELAY_CLI_RUN_ID" --output ".local/relay-cli-$RELAY_CLI_RUN_ID"
```

The helper also accepts just `--endpoint` and a new private `--output` directory;
omitting `--run-id` generates a fresh UUID. In that case, read the actual
`run_id` from the written `manifest.json` for all subsequent queries. Never
substitute the output-directory label for the generated UUID. The helper
retains its private manifest and redacted stdout/stderr/diagnostic logs and
does not inherit Azure authentication into the isolated CLI environment.
Only the retained output directory is under private `.local`; execution
home/work directories must not be there. A private directory inside a checkout
does not prevent Git ancestor discovery. Before accepting a run, confirm the
corrected harness/regression and ensure repository/branch metadata from the
normal checkout did not leak into the synthetic telemetry.
The helper uses the parent process's system-temp selection; it does not itself
reject a repository-local `TMPDIR`, `TMP` or `TEMP`. Review that location before
inference rather than treating the directory-name regression as a universal
Git-isolation guarantee.
The combined recipe above deliberately supplies the log fixture's **same
fresh UUID** so one query can correlate both independent producers.

Require the resulting manifest to say `awaiting_backend_verification`, not
`failed`; this is **not an ingestion pass**. Correlate that CLI UUID with
persisted spans/events using [`queries/verify_relay.kql`](../queries/verify_relay.kql).
The template unions native logs, spans and events, with resource enrichment
and service filters. Its logs branch uses the same UUID as the standalone
fixture above. A CLI-only run does not generate that standalone log record;
if you intentionally use different UUIDs, query each separately instead of
claiming the missing log branch is a CLI exporter failure.

```bash
[[ "$RELAY_CLI_RUN_ID" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]]
sed "s/__RUN_ID__/$RELAY_CLI_RUN_ID/g" queries/verify_relay.kql \
  > .local/relay-cli-query.kql
if grep -Eq '//|/\*' .local/relay-cli-query.kql; then
  echo "Use comment-free KQL for this CLI path, or az rest JSON" >&2
  exit 1
fi
az monitor log-analytics query --workspace "$LAW_CUSTOMER_ID" \
  --analytics-query "$(cat .local/relay-cli-query.kql)" \
  --output json > .local/relay-cli-query-result.json
```

This command uses the Azure CLI `log-analytics` extension. Install it through
the approved CLI extension workflow only if missing. The KQL itself bounds
time; do not add a query-wide `--timespan` that clips late resource enrichment.
The observed CLI query path returned `[]` when multiline input began with a
`//` line comment; removing comments revealed the six already-ingested rows.
The parent revalidated the **comment-free** template against the backend.
Do not substitute `/* ... */`: the attempted block comments failed with Kusto
syntax error `SYN0002`. For queries needing supported `//` line comments or
literal `//` strings,
send the query as an `az rest` JSON body instead of stripping content silently.
The guard above deliberately refuses `//` and `/*` tokens. A reformatted template
must be revalidated live before claiming the exact new query text works;
an empty CLI response alone is not proof of absent ingestion.

Verify exact UUID/service/completed time window and nonempty expected
logs/spans/events from the combined run, then evaluate the [metadata-only field policy](security-and-data.md#metadata-only-policy)
on returned telemetry. Native AMW metrics need separate bounded PromQL
count/sum checks as described below; a populated LAW query does not prove them.
This runner's manifest is **not the historical Python verifier's manifest
contract**: do not relabel it or feed it into `verify_native` as if it were.
Use a new, separate optional direct-route smoke run for that verifier.
Retain CLI diagnostics and all backend results privately.

## Hostname attribution

For an isolated, metadata-only CLI test that explicitly includes this runtime's
hostname, use a new output directory and UUID. This is an opt-in test; the smoke
helper does not add host identity by default or inherit arbitrary telemetry
configuration from the current session. A responsive relay or CLI exit alone
is not acceptance.

```bash
set -euo pipefail
umask 077
RUN_ID="$(node -p 'require("node:crypto").randomUUID()')"
HOST_OUTPUT=".local/relay-host-$RUN_ID"
RELAY_BASE_URL="$(jq -er '.traces_endpoint | sub("/v1/traces$"; "")' .local/relay-azure.json)"
node src/tools/smoke-copilot.mjs --endpoint "$RELAY_BASE_URL" \
  --run-id "$RUN_ID" --host-name "$(node -p 'require("node:os").hostname()')" \
  --output "$HOST_OUTPUT"
QUERY="$(sed "s/__RUN_ID__/$RUN_ID/g" queries/verify_relay.kql)"
az monitor log-analytics query \
  --workspace "$(jq -er .workspace_customer_id "${NATIVE_STATE:-.local/native-azure.json}")" \
  --analytics-query "$QUERY" -o json \
  > "$HOST_OUTPUT/query.json"
export HOST_OUTPUT
node <<'JS'
const fs = require('node:fs');
const assert = require('node:assert/strict');
const directory = process.env.HOST_OUTPUT;
const manifest = JSON.parse(fs.readFileSync(`${directory}/manifest.json`));
const rows = JSON.parse(fs.readFileSync(`${directory}/query.json`));
assert.equal(manifest.capture_content, false);
assert.equal(manifest.client_azure_auth, false);
assert.equal(manifest.status, 'awaiting_backend_verification');
assert.equal(manifest.host_name, require('node:os').hostname());
const spans = rows.filter(row => row.Signal === 'spans');
const events = rows.filter(row => row.Signal === 'events');
assert(spans.length >= 2 && events.length > 0, 'Required CLI telemetry not yet observed');
for (const row of [...spans, ...events]) {
  const resource = JSON.parse(row.ResourceAttributes);
  assert.equal(resource['host.name'], manifest.host_name);
  assert.equal(resource['copilot.run.id'], manifest.run_id);
  assert.equal(resource['service.name'], 'github-copilot');
}
console.log(`Hostname matched on ${spans.length} spans and ${events.length} correlated events`);
JS
```

If rows are still propagating, repeat only the query/comparison for the same
completed run within a bounded window (for example five minutes); do not
mistake an empty result for success. Preserve failures separately before any
fresh inference retry. The executed test's first run had no matching rows
within its window. After an end-to-end relay preflight, a fresh run verified
the exact hostname on two spans and three correlated events. The first
attempt's delivery failure was not established as a hostname parsing issue
or conclusively diagnosed as a cold start.

The result is actual CLI trace/event data in Log Analytics, not a fabricated
standalone CLI log record. The event query joins parent-span resource metadata.
See [the measured evidence](evidence/function-relay.md#hostname-follow-up).

## User attribution

The smoke helper also accepts an explicit `--user-id`, independently of
`--host-name`. Neither label is added automatically. For a fresh Linux
synthetic test using the current OS account and hostname:

```bash
set -euo pipefail
umask 077
RUN_ID="$(node -p 'require("node:crypto").randomUUID()')"
USER_OUTPUT=".local/relay-user-$RUN_ID"
RELAY_BASE_URL="$(jq -er '.traces_endpoint | sub("/v1/traces$"; "")' .local/relay-azure.json)"
node src/tools/smoke-copilot.mjs --endpoint "$RELAY_BASE_URL" \
  --run-id "$RUN_ID" \
  --host-name "$(node -p 'require("node:os").hostname()')" \
  --user-id "$(node -p 'require("node:os").userInfo().username')" \
  --output "$USER_OUTPUT"
QUERY="$(sed "s/__RUN_ID__/$RUN_ID/g" queries/verify_relay.kql)"
az monitor log-analytics query \
  --workspace "$(jq -er .workspace_customer_id "${NATIVE_STATE:-.local/native-azure.json}")" \
  --analytics-query "$QUERY" -o json \
  > "$USER_OUTPUT/query.json"
export USER_OUTPUT
node <<'JS'
const fs = require('node:fs');
const assert = require('node:assert/strict');
const os = require('node:os');
const directory = process.env.USER_OUTPUT;
const manifest = JSON.parse(fs.readFileSync(`${directory}/manifest.json`));
const rows = JSON.parse(fs.readFileSync(`${directory}/query.json`));
assert.equal(manifest.capture_content, false);
assert.equal(manifest.client_azure_auth, false);
assert.equal(manifest.status, 'awaiting_backend_verification');
assert.equal(manifest.host_name, os.hostname());
assert.equal(manifest.user_id, os.userInfo().username);
const spans = rows.filter(row => row.Signal === 'spans');
const events = rows.filter(row => row.Signal === 'events');
assert(spans.length >= 2 && events.length > 0, 'Required CLI telemetry not yet observed');
for (const row of [...spans, ...events]) {
  const resource = JSON.parse(row.ResourceAttributes);
  assert.equal(resource['user.id'], manifest.user_id);
  assert.equal(resource['host.name'], manifest.host_name);
  assert.equal(resource['copilot.run.id'], manifest.run_id);
  assert.equal(resource['service.name'], 'github-copilot');
}
console.log(`User and host matched on ${spans.length} spans and ${events.length} correlated events`);
JS
```

Repeat only the query/comparison within a bounded propagation window when
needed, as for hostname verification. Require exact values on the freshly
correlated records, and verify that a wrong-user filter returns no rows.
Keep the real identity and query results private. Correlated span events
inherit the labels through the query's parent-span resource join; this is
not standalone CLI `OTelLogs` output or verified native metrics attribution.
This demonstrates transport of a client-provided identifier, not authenticated
employee attribution. See [the client recipe](usage.md#opt-in-user-attribution).

## Optional existing direct-route tests

Run these optional existing tests from the repository root with Python 3.12+:

```bash
python3 -m scripts.preflight
python3 -m unittest discover -s tests -v
az bicep build --file infra/main.bicep --stdout > /dev/null
az bicep build --file infra/resources.bicep --stdout > /dev/null
az bicep build-params --file infra/main.bicepparam --stdout > /dev/null
az bicep build --file infra/visualizations.bicep --stdout > /dev/null
az bicep build --file infra/function.bicep --stdout > /dev/null
copilot help monitoring
```

Preflight saves installed CLI help privately under `.local/versions/`.
Compilation and tests establish only their tested contracts, not Azure
permissions, preview-region availability, ingestion, or portal rendering.
Actual fresh versions and outcomes belong in the evidence record.

## Exact exporter and authentication contract

The **default relay** environment uses the relay receipt's HTTPS
`/v1/traces` and `/v1/metrics` URLs and clears global/per-signal OTLP headers;
see [the complete no-client-token recipe](usage.md#default-relay-ephemeral-linux-shell-recipe).
The following contract describes the **optional direct authenticated test
runner**, not the relay. Never overwrite native endpoint receipts to redirect
it or claim that an unchanged direct test ran through the Function.

The existing direct-auth synthetic runner obtains destination URLs from
`.local/native-azure.json`, gets an Entra Monitor token through Azure CLI, and
passes a narrow environment to the child CLI. It uses:

```bash
export COPILOT_OTEL_ENABLED=true
export COPILOT_OTEL_EXPORTER_TYPE=otlp-http
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="$AZURE_OTLP_TRACES_ENDPOINT"
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="$AZURE_OTLP_METRICS_ENDPOINT"
export OTEL_SERVICE_NAME=github-copilot
export OTEL_RESOURCE_ATTRIBUTES="copilot.run.id=$RUN_ID,copilot.audit.scenario=metadata-only"
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
```

The two endpoint variables above mean the **exact full URLs returned by the
deployment**, not guessed addresses or a shared base URL. They have these shapes:

```text
https://<logs-dce-domain>/datacollectionRules/<immutable-dcr-id>/streams/Microsoft-OTLP-Traces/otlp/v1/traces
https://<metrics-dce-domain>/datacollectionRules/<immutable-dcr-id>/streams/Custom-Metrics-Otel/otlp/v1/metrics
```

The metrics stream must match the deployed DCR exactly, including case. Do not
append a second signal suffix. The traces URL uses the DCE's logs-ingestion
**domain**, but a traces **path**. Never send trace or metric payloads to a logs
URL. No standalone logs exporter is demonstrated: span events travel with
traces and appear in `OTelEvents`.

The required header is:

```text
OTEL_EXPORTER_OTLP_HEADERS='Authorization=Bearer%20<token>'
```

`%20` encodes the space in `Bearer <token>`. To understand token acquisition
without printing the credential:

```bash
set +x
AZURE_MONITOR_TOKEN="$(az account get-access-token \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource https://monitor.azure.com/ \
  --query accessToken --output tsv)" || exit 1
test -n "$AZURE_MONITOR_TOKEN" || exit 1
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20${AZURE_MONITOR_TOKEN}"
unset AZURE_MONITOR_TOKEN
```

This snippet shows the credential contract, **not a normal-user test launch**.
Use the isolated smoke runner below for all tests; it also checks that token
lifetime covers the bounded session. Clear the header after inspecting the
contract with `unset OTEL_EXPORTER_OTLP_HEADERS`. Never print the environment,
save tokens in files, or put real tokens in documentation.

For separately approved ordinary use rather than testing, follow
[normal-session onboarding](usage.md). That recipe is not executed as release
proof and does not enable content capture.

The sender needs Monitoring Metrics Publisher on the DCR. A GitHub token or
Application Insights connection string is not Azure authentication. An
environment token cannot renew itself; reacquiring a token in a parent shell
does not update a running CLI. See [identity boundaries](security-and-data.md#identity-and-access).

Do not add delta-temporality or exponential-histogram environment overrides as
an assumed CLI fix. Such settings do not establish that the installed CLI
honors them. Native ingestion must be tested with the CLI's actual histogram
output. Cumulative explicit histograms are **not categorically rejected**;
fresh v1 native backend count/sum checks passed with the tested CLI. This does
not prove support for delta/exponential environment overrides or compatibility
with other Azure experiences. See [measured evidence](evidence/v1.md).

## Optional direct route: execute the three bounded scenarios

Authenticate Azure and GitHub separately as described in
[deployment](deployment.md). The runner obtains GitHub authentication from a
supported token variable or the existing `gh` login internally; do not print
`gh auth token` or copy a normal Copilot profile into the synthetic home.

Start with metadata-only:

```bash
python3 -m scripts.native_smoke --scenario metadata-only
```

Retain its printed UUID and verify that exact run:

```bash
export RUN_ID="<actual-completed-run-uuid>"
python3 -m scripts.verify_native --run-id "$RUN_ID" --timeout-seconds 600
```

Only with explicit approval for isolated synthetic content, run each of:

```bash
python3 -m scripts.native_smoke --scenario full-content
python3 -m scripts.native_smoke --scenario delegated
```

Each command produces a **different** run UUID. Run `verify_native` separately
for each UUID. Do not substitute a fixture UUID, reuse another scenario's
identifier, relabel a failed manifest, or infer success from a requested action.
Smoke runs invoke real inference and can incur Copilot usage.

Smoke sessions default to a 180-second bound; `--timeout-seconds` accepts an
integer from 1 through 3600. The token must remain valid for the session timeout
plus a 120-second safety margin. The default validation policy is
`--privacy-policy metadata-only-v1`. An optional
`--privacy-policy strict-absence` on a **new metadata-only run** evaluates the
stronger six-key-absence policy; it does not alter CLI export or silently replace
the v1 default. The verifier follows the recorded policy.

| Scenario | Capture | Required observed behavior |
| --- | --- | --- |
| `metadata-only` | False | Positive CLI spans/events and required metrics; no prompt, response, system content, tool arguments/results or schema; exact tool name/type metadata allowed |
| `full-content` | True, synthetic only | Real fixture read with `view`; marker-bearing input/output plus arguments/result on the same observed tool span |
| `delegated` | True, synthetic only | Actual bounded child-agent invocation, valid parent relationships, and required synthetic tool/content evidence |

The harness isolates temporary `HOME`, `COPILOT_HOME`, XDG configuration, and
working directory. It excludes normal custom instructions and built-in MCPs.
Tools are restricted to none for metadata-only, `view` for full-content, and
`task` plus `view` for delegated; shell, write, and URL tools are denied.
The synthetic fixture marker binds the run, file path, content, and expected
response. A timeout or nonzero CLI exit remains failure even if some data arrives.

## Native trace, event, and resource proof

The verifier uses [the checked-in KQL template](../queries/verify_native.kql)
and the recorded LAW, run UUID, service identity, CLI version, scenario, and
completed time window. It validates returned column names and complete results
rather than assuming a schema from documentation.

`OTelSpans` carries operations and parent links; `OTelEvents` carries span
events; `OTelResources` supplies correlated resource attributes. The precise
fresh schema and correlation fields must be recorded from actual backend
responses. Missing resource rows, wrong-run matches, empty data, malformed
attributes, and partial query results cannot prove a pass.

On the direct no-Application-Insights path, native rows have an empty
`_ResourceId` and are workspace-scoped. DCR-scoped publishing permissions do not
make the DCR a row-level resource association. Query the exact LAW with LAW
reader access and correlate run/service/resource metadata; do not require
`_ResourceId` to equal the DCR ID.

Require observed `invoke_agent`, `chat`, and scenario-appropriate `execute_tool`
operations. Delegation requires actual parent/child relationships, not just a
common trace ID. Checked identifiers correlate the backend to the completed
manifest; they are not cryptographic producer attestation.

Metadata-only validation follows the
[exact name/type-only policy](security-and-data.md#metadata-only-policy).
It checks returned attributes, not all pre-ingestion traffic or process memory.
A result can demonstrate persistence while failing privacy; do not collapse
those independent outcomes into a single unqualified success.

## Independent native histogram proof

Metrics are queried at the **Azure Monitor workspace PromQL endpoint**, not
from LAW token charts. The verifier checks the required dotted base metric
names and individual histogram count/sum snapshots:

| Metric | Required scope |
| --- | --- |
| `gen_ai.client.token.usage` | All scenarios; retain input/output dimensions |
| `gen_ai.client.operation.duration` | All scenarios |
| `gen_ai.invoke_agent.duration` | All scenarios |
| `gen_ai.execute_tool.duration` | Content/tool scenarios |

Representative PromQL, substituting the real completed run UUID:

```promql
histogram_count({__name__="gen_ai.client.token.usage","copilot.run.id"="<RUN_UUID>","service.name"="github-copilot"})
histogram_sum({__name__="gen_ai.client.token.usage","copilot.run.id"="<RUN_UUID>","service.name"="github-copilot"})
```

The scripted verifier supplies the scenario and fixed evaluation time as well.
It consumes the **direct-route** manifest, not the relay smoke manifest.
For relay acceptance, query the same required metric families independently
using the relay run's UUID, service/scenario labels and completed window;
retain input/output dimensions and require nonempty count/sum results.
Do not change the relay receipt or feed it into the direct verifier to
manufacture a pass. This relay gate is currently **unverified**.
Use the recorded AMW endpoint and authorized identity; do not paste raw API
responses into public evidence. Do not invent suffixed `_count`/`_sum` series,
drop dimensions to hide a missing series, or add successive cumulative
snapshots as independent usage. Count/sum persistence is not bucket fidelity or
proof that SDK temporality/aggregation environment controls work.

## Read the result honestly

Private per-run artifacts are under `.local/runs/<UUID>/`: `manifest.json`,
CLI diagnostics, `raw-native-query.json`, and `results.json`.
The verification timeout is bounded at 600 seconds in the commands above;
overrides must be finite, greater than zero, and no more than 3600 seconds.
Retryable missing/transient data may be polled within that deadline;
authorization failures, malformed/partial responses, and required-signal
failures are not silently converted to success.

Freshly provisioned ingestion can return HTTP 503 before the data plane is
ready, even after ARM reports success. After propagation, run a **new bounded
synthetic CLI session with a new UUID** and verify both workspaces. Metrics
without the required spans/events are not a pass. An authenticated empty
protobuf request returning HTTP 400 is not a readiness check; HTTP 401 for an
anonymous request confirms that authentication is required, not that a
complete telemetry payload will ingest successfully.

Record actual aggregate counts, privacy findings, histogram series coverage,
and failure reasons only after reviewing fresh results. Keep current relay
findings in [relay evidence](evidence/function-relay.md); preserve
[v1 evidence](evidence/v1.md) as the historical direct-auth record rather than
overwriting it with new runs. Keep raw data private. This verifies required
persisted signals, not lossless export of every emitted record or every CLI
feature. Optional hooks, skills, MCPs, compaction, and sandbox behavior are not
covered unless deliberately and safely exercised.

## References

- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference); installed `copilot help monitoring` is version-specific.
- [Azure manual OTLP ingestion](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion)
- [OTelSpans schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/otelspans)
- [OTelEvents schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/otelevents)
- [OTelResources schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/otelresources)
- [Prometheus and OpenTelemetry metric practices](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/prometheus-opentelemetry-best-practices)
