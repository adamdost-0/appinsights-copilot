# Infrastructure contracts

[Administrator guide](../README.md) | [Native CLI runbook](../docs/deployment.md) |
[Relay CLI runbook](../docs/relay-deployment.md) | [Agent context](../AGENTS.md)

Deploy using the reviewed Markdown **Azure CLI/Bicep commands**. Python is not
a deployment or cleanup prerequisite. Template compilation is offline contract
validation, not live resource or ingestion proof.

| Template | Scope and responsibility |
| --- | --- |
| [`main.bicep`](main.bicep) | Existing subscription entry, `rg-copilot-otel-v1` |
| [`resources.bicep`](resources.bicep) | Unchanged native LAW/AMW/DCE/DCR and publisher/operator roles |
| [`main.bicepparam`](main.bicepparam) | Nonsecret example native parameter shape, not ownership |
| [`function.bicep`](function.bicep) | New subscription entry, separate `rg-copilot-otel-relay` |
| [`function-resources.bicep`](function-resources.bicep) | Flex FC1 Node22 app, private blob access, system MI, storage role, app/SCM ingress |
| [`function-publisher.bicep`](function-publisher.bicep) | Publisher role only, scoped to the existing DCR in its original group |
| [`visualizations.bicep`](visualizations.bicep) | Existing LAW-associated seven-panel workbook |

## Unchanged native resources

The dedicated native group uses solution tag `copilot-otel-v1` and its original
ownership marker. LAW is `PerGB2018`, with requested 30-day retention and
1 GB/day cap. AMW holds native histogram metrics with separate costs/retention.
DCR direct OTel sources route spans/events/resources and logs to LAW, metrics
to AMW, through an explicit public **authenticated** DCE. There is no
Application Insights resource/reference/connection string or DCR-to-compute
association. AMW can create separate service-managed ingestion resources;
do not substitute them for the explicit DCE/DCR.

The original native `principalId`/`principalType` publisher and
`operatorPrincipalId`/`operatorPrincipalType` query roles are unchanged.
`nativeState` supplies resource IDs, LAW customer ID, DCR immutable ID, AMW
query origin and exact `traces_endpoint`, `logs_endpoint`, `metrics_endpoint`.
Source these from verified `.local/native-azure.json` or reviewed
`az deployment sub show -n copilot-otel-v1 --query properties.outputs.nativeState.value`.

```text
traces:  https://<logs-domain>/datacollectionRules/<immutable-id>/streams/Microsoft-OTLP-Traces/otlp/v1/traces
logs:    https://<logs-domain>/datacollectionRules/<immutable-id>/streams/Microsoft-OTLP-Logs/otlp/v1/logs
metrics: https://<metrics-domain>/datacollectionRules/<immutable-id>/streams/Custom-Metrics-Otel/otlp/v1/metrics
```

Internal OTel stream names differ from public OTLP routes. Do not append a
signal suffix twice or derive a signal's URL from another signal's endpoint.
The AMW query origin is not an ingestion URL. Native LAW rows without
Application Insights have empty `_ResourceId`; query the exact LAW rather
than filtering rows by DCR ARM ID. Resource attributes are untrusted metadata.

## New relay resources and security

The separate group has solution tag `copilot-otel-relay` and a **fresh UUID**.
The Function is anonymous at the HTTP trigger but never open to unmatched
ingress: a required nonempty `allowedIPv4Cidrs` list, initial app/SCM
default-deny, HTTPS-only and minimum TLS 1.2 form the platform boundary.
SCM inherits the app rules; the deploying host must be allowlisted too.
Do not deploy with `/0`, temporary allow-all, or a create-then-lock-down sequence.

The [parameter preflight](../docs/relay-deployment.md#prepare-parameters-without-changing-native-state)
validates canonical IPv4 networks and required full **HTTPS**, credential-free,
native URLs, immutable DCR routes and current subscription. Bicep enforces
nonempty/length contracts; do not claim it performs regex/URL host validation.
The runtime validates upstream URLs as an additional boundary, not a substitute
for approved endpoint provenance. No secret parameter or generated key is used.

The deliberately **system-assigned** identity uses `ManagedIdentityCredential()`
without a client ID. It has Storage Blob Data Owner on the relay-only account
for HTTP host/deployment storage, and Monitoring Metrics Publisher on the
exact native DCR. Shared-key and anonymous blob access are disabled. Storage
network access remains public but authenticated; private networking is not
claimed. The relay is billable and adds operational/cold-start/availability
dependencies; it is not a collector, durable queue or DLP filter.

`OTLP_TRACES_ENDPOINT`, `OTLP_LOGS_ENDPOINT`, `OTLP_METRICS_ENDPOINT` contain the
upstream native URLs. The `relayState` output supplies the function app name,
ARM ID, principal, storage ARM ID, `/v1/{traces,logs,metrics}` URLs, and exact
`publisher_role_assignment_id`. Keep it separately in `.local/relay-azure.json`;
never replace `nativeState` or distribute private ownership receipts to clients.

Use modern **Flex OneDeploy via Azure CLI `config-zip`**, not the legacy Kudu
ZipDeploy REST API. Package the runtime **contents of `src`** at ZIP root:
`host.json`, manifests, `relay.js`, `transport.js`, `functions/` and production
dependencies only, never an enclosing `src/` or its tests/tools.
No Python deploy scripts or `func publish` are
required. [The relay runbook](../docs/relay-deployment.md) documents current
Microsoft sources, exact settings, ZIP inspection, updates and rollback.

## Ownership, teardown and proof

Keep all historical native lifecycle receipts and exact saved-search hash
baseline intact. CLI deployment does not automatically reproduce the legacy
Python inventory/teardown guard or authorize adoption of an existing group.
New CLI-managed inventory and operator approvals remain separate private
artifacts. Wrong markers, changed customer UUIDs, incomplete inventory,
unknown consumers or child drift must stop lifecycle changes.

The relay's DCR publisher assignment is **outside its own group**. Remove or
explicitly review that exact assignment before native teardown; adding the
Function/storage to the native group or changing the v1 teardown guard is not
acceptable. Recreating the system identity also requires stale-role cleanup.
Let AMW remove its own managed group through its supported lifecycle.

```bash
az bicep build --file infra/main.bicep --stdout >/dev/null
az bicep build --file infra/resources.bicep --stdout >/dev/null
az bicep build-params --file infra/main.bicepparam --stdout >/dev/null
az bicep build --file infra/function.bicep --stdout >/dev/null
az bicep build --file infra/visualizations.bicep --stdout >/dev/null
```

Read actual retention at table level; the LAW cap is not a total cost ceiling.
Native OTLP remains **preview, no SLA, not recommended for production**.
[Historical direct-auth evidence](../docs/evidence/v1.md) is unchanged and must
not be presented as relay forwarding, standalone logs, or portal-render proof.
