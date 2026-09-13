# v1 infrastructure contract

This is the infrastructure chapter of the
[administrator guide](../README.md). Use the
[deployment workflow](../docs/deployment.md) rather than bypassing ownership
checks with a direct template deployment.

## Entry points

| File | Responsibility |
| --- | --- |
| [`main.bicep`](main.bicep) | Subscription-scope deployment of `rg-copilot-otel-v1` and its resource module |
| [`resources.bicep`](resources.bicep) | Log Analytics workspace, Azure Monitor workspace, explicit DCE/DCR, scoped roles |
| [`main.bicepparam`](main.bicepparam) | Nonsecret example parameter shape; not a real ownership receipt |
| [`visualizations.bicep`](visualizations.bicep) | Separate LAW-linked workbook deployment |

```bash
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --what-if
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --apply
```

The deployment wrapper uses Azure CLI, validates the explicit subscription,
checks providers and ownership, and performs ARM validation and what-if before
apply. Azure resources are billable. What-if is an Azure operation and creates
private local receipts/artifacts; it is not an offline test or ingestion proof.

## Resources and routing

| Resource | Configuration and purpose |
| --- | --- |
| Resource group | `rg-copilot-otel-v1`; dedicated to this example |
| Log Analytics workspace (LAW) | `PerGB2018`; requested 30-day retention, 1 GB/day quota; native `OTelSpans`, `OTelEvents`, `OTelResources` |
| Azure Monitor workspace (AMW) | Native histogram storage and PromQL query endpoint; independent metric billing and retention |
| Data Collection Endpoint (DCE) | Explicit public HTTPS ingestion endpoint; Entra bearer authentication required |
| Data Collection Rule (DCR) | Direct OTel sources routed to LAW and AMW; linked to the explicit DCE |
| Scoped role assignments | DCR Monitoring Metrics Publisher; AMW Monitoring Data Reader; LAW Log Analytics Reader |
| Optional workbook deployment | Seven native-table query panels associated with LAW |

There is no Application Insights component, application reference, connection
string, instrumentation key, portal OTLP opt-in, collector, VM, container, or
AMA. No DCR-to-compute association is required. Azure may create AMW-managed
ingestion resources; these are distinct from the explicit DCE/DCR used by the
CLI. Inspect service-managed resources during lifecycle operations rather than
substituting their endpoints.

The DCR routes trace spans/events/resources to LAW and metrics to AMW. Its
optional logs route does not establish CLI logs-exporter support: v1 exercises
only traces (including span events) and metrics. Resource attributes are
preserved for run/service correlation. They are untrusted client metadata, not
an authorization or attestation mechanism.

Public network access is enabled for this evaluation. "Public" does not mean
unauthenticated; all sending and querying identities need appropriate Entra
tokens and scoped permissions. Private networking and enterprise credential
lifecycle are separate design work, not validated features of this example.

Native LAW rows on this no-Application-Insights path have an empty
`_ResourceId`. They are workspace-scoped, not DCR-associated records; LAW reader
access and workspace-scoped queries are required. The DCR is still the
publishing authorization scope, not the value to impose as a row filter.

## Endpoint contract

Use complete signal URLs returned in `.local/native-azure.json`:

```text
traces_endpoint:
https://<logs-dce-domain>/datacollectionRules/<immutable-dcr-id>/streams/Microsoft-OTLP-Traces/otlp/v1/traces

metrics_endpoint:
https://<metrics-dce-domain>/datacollectionRules/<immutable-dcr-id>/streams/Custom-Metrics-Otel/otlp/v1/metrics
```

The public trace route name differs from internal DCR OTel stream names.
The metrics stream is case-sensitive and must match the DCR. Traces use the
logs-ingestion DCE domain but never the logs payload URL. Do not derive one
signal's URL by appending a suffix to the other.

The sending protocol is **HTTPS binary OTLP/HTTP protobuf**, authenticated with
an Entra Monitor-audience bearer header. See the
[exact environment contract](../docs/verification.md#exact-exporter-and-authentication-contract).
The receipt's `metrics_query_endpoint` is the AMW PromQL origin, not an
ingestion endpoint.

## Naming and private state

Names use a deterministic `uniqueString(resourceGroup().id)` suffix so distinct
groups do not share resource names. The solution tag is `copilot-otel-v1`;
the group and owned resources carry the receipt's `ownership-marker`.
Role assignments use deterministic scoped identifiers. Repeat apply must
preserve identities and signal URLs; verify that in fresh readback rather than
inferring it from template compilation.

An intentional rebuild is different: a matching completed teardown receipt,
with the recorded managed group still absent, permits new workspace customer
and immutable DCR IDs while retaining the ownership marker. Keep active
receipts in place for that checked transition.

`.local/native-deployment.json` records the ownership intent before deployment.
`.local/native-azure.json` records validated resource/runtime metadata after
deployment, including subscription/group/location, ownership marker, LAW ARM
and customer IDs, AMW ARM ID/query endpoint, DCE/DCR IDs, immutable DCR ID, and
full signal endpoints. Neither is a credential store; both remain private.
No Application Insights resource identifier or connection string is required.

Before writing runtime state, an independent ARM GET checks the exact LAW ID,
location, ownership marker, and `properties.customerId`. This binds the query
customer UUID to the deployment's LAW rather than relying on a row-level
`_ResourceId`.

The first fresh successful apply writes
`.local/native-workspace-children.json` with `schema_version: 1`, the
subscription/group/LAW/customer/ownership binding, and a `saved_searches` map
from exact child ARM IDs to SHA-256 hashes of their full canonical JSON entries.
Only validated Azure-default saved-search shapes can enter this baseline;
matching a name or prefix alone is never deletion authorization.

Teardown requires the exact saved-search set and hashes to remain unchanged.
It first performs a fresh LAW ARM/location/ownership/customer-ID check, so a
same-name replacement workspace cannot reuse an earlier baseline.
Missing, changed, or new searches, custom tables, unexpected nonempty child
collections, and incomplete/error inventories cause refusal. Microsoft tables
must have the provider's Microsoft `tableType`. Normal repeat apply preserves
the private baseline byte-for-byte; a verified teardown/recreation rebinds it to
the new workspace customer UUID. An existing deployment without a baseline
cannot be adopted automatically through what-if, apply, or teardown.

Keep **four active lifecycle receipts** once cleanup has been attempted:
`native-deployment.json`, `native-azure.json`, `native-workspace-children.json`,
and the teardown command's `native-teardown.json`, all under `.local/`.
The baseline is a private `0600` file, not a published example or an editable
allowlist for adopting existing workspace children.

Preserve receipts across retries and repeat apply. Do not fabricate, overwrite,
or delete them to adopt an existing group. Same solution tags alone are not
proof of ownership. A partial deployment can leave Azure resources even if a
later validation step fails; inspect retained receipts and deployment state
before recovery. If ARM succeeded but readback prevented both operational state
and baseline writes, preserve `.local/native-deployment-result.json` and use
the [reviewed bootstrap recovery](../docs/deployment.md#recover-an-interrupted-first-apply).
The saved result is candidate evidence, never a substitute operational receipt.
Ownership tags are accident-prevention controls, not security
against an administrator able to forge tags or edit files.

Keep the group exclusive to this solution and serialize deployment, smoke,
workbook, and cleanup operations. Generic ARM inventory does not guarantee that
every provider child or external consumer is visible. Follow
[cleanup and rebuild](../docs/deployment.md#cleanup-and-rebuild) and preserve any
explicit refusal rather than broadening deletion.

## Retention and proof boundaries

Read back actual workspace and OTel table analytics/total retention after the
fresh deployment. A 30-day workspace setting alone does not prove every table's
retention. The 1 GB/day LAW cap is not a total cost ceiling and does not cap AMW
metric ingestion. Cardinality, other Azure charges, and Copilot inference need
separate budgeting.

```bash
az bicep build --file infra/main.bicep --stdout > /dev/null
az bicep build --file infra/resources.bicep --stdout > /dev/null
az bicep build-params --file infra/main.bicepparam --stdout > /dev/null
```

Compilation can warn when local Bicep type metadata is unavailable for a
preview API. Record the real warning and separately require ARM validation and
resource readback; neither suppressing the warning nor compiling proves
ingestion. Fresh deployment, repeat apply, and required native signal checks
[passed](../docs/evidence/v1.md); workbook acceptance remains separate.
Native OTLP remains preview, without an SLA, and not recommended for production.

## Sources

- [Manual orchestration; Application Insights is optional](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion#option-2-manual-resource-orchestration)
- [Azure native OTLP overview and SDK endpoint configuration](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/collect-use-observability-data)
- [DCE schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2024-03-11/datacollectionendpoints)
- [DCR schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2024-03-11/datacollectionrules)
- [LAW schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.operationalinsights/2023-09-01/workspaces)
- [AMW schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.monitor/2025-10-03/accounts)
