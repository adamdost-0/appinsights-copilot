# Dedicated Copilot OTEL monitoring resources

**Native OTLP entry point:** use [`native-main.bicep`](native-main.bicep) and
[the collector-free deployment guide](../docs/native-otlp.md). The instructions
below document the historical `main.bicep` collector/exporter experiment.

Local Copilot sends OTEL to the local collector. The collector exports to
workspace-based Application Insights, linked explicitly to this solution's Log
Analytics workspace. No collector or cloud-hosted compute is provisioned here.

## Deployment

Prerequisites: Python 3 with stdlib, an installed Azure CLI and Bicep compiler,
and a separately established authorized public AzureCloud session. These scripts
never log in, switch accounts/clouds, register providers, grant roles, or weaken
policy. The subscription must be an explicit canonical lowercase UUID.

```sh
python3 -m scripts.deploy --subscription SUBSCRIPTION_UUID --location eastus --what-if
python3 -m scripts.deploy --subscription SUBSCRIPTION_UUID --location eastus --apply
```

`--what-if` performs account/cloud, provider registration, subscription/provider
region, and ownership checks, followed by subscription deployment validation and
what-if. It makes no provisioning calls and writes no application state. Azure
CLI itself may maintain its normal local cache/logs. The script prints only
change-type counts, not raw what-if properties that might contain secrets.
Deletion, ignored, unsupported, and unknown changes are refused.

`--apply` performs the same sequence, rechecks ownership immediately before
creation, applies, validates safe outputs and inventory, saves the ownership
receipt, verifies the service configuration using ARM GET, and writes the
private collector configuration. Permission/policy failures are explicit and
stop processing; raw Azure errors are withheld because they can contain secrets.
Use Azure diagnostics privately for details, without copying connection strings
into issues or logs.

`main.bicep` runs at subscription scope and creates only
`rg-copilot-otel-audit`, plus the group-scoped `resources.bicep` deployment.
The region defaults to `eastus`; any region explicitly accepted by the
subscription and both resource providers is supported. Existing resources cannot
be relocated by changing `--location`. The deployment names are stable:
`copilot-otel-audit` and `copilot-otel-audit-resources`.

Resource names use `uniqueString(subscription().id, 'rg-copilot-otel-audit')`.
Repeated deployments pass the recorded names and reuse the recorded ownership
UUID. Both resources and the group have `solution=copilot-otel-audit` and
`ownership-marker=<UUID>` tags. The example `main.bicepparam` is nonsecret; its
example marker is not an ownership receipt. Use the Python CLI rather than
deploying the template directly to retain the ownership checks.

## Resource settings and limits

| Resource | Configuration |
| --- | --- |
| Log Analytics workspace | `Microsoft.OperationalInsights/workspaces@2023-09-01`, `PerGB2018`, retention 30 days, integer `workspaceCapping.dailyQuotaGb: 1`, resource-permission-only log access disabled |
| Application Insights | `Microsoft.Insights/components@2020-02-02`, `kind: web`, `Application_Type: web`, explicit `WorkspaceResourceId`, `DisableLocalAuth: false` |
| Both | Public ingestion and query enabled, solution and ownership tags |

Local authentication is deliberately enabled for the local collector's connection
string ingestion. This is not a policy bypass: if organizational policy forbids
these settings, deployment stops. Sovereign/custom clouds and adopting arbitrary
pre-existing resource groups are explicitly unsupported.

Readback checks IDs, tags, location, workspace linkage/customer GUID, SKU,
resource-permission access, local authentication, and public ingestion/query.
It prints the **actual workspace** daily cap and retention returned by Azure
rather than assuming that the requested settings took effect.

The daily cap is not an exact spending ceiling; ingestion can overshoot and
charges can arise from other operations. Application Insights has its own
ingestion limits; this template does not configure or claim to verify its
separate daily cap. Table-specific analytics/total retention can differ from
workspace retention, particularly for Application Insights tables. A 30-day
workspace setting does **not** guarantee deletion of all `App*` data after 30
days. This deployment does not set individual table retention or purge data.

## Local contracts

Only `.local/collector.env` is needed by the collector. It contains the complete
ARM-returned string, not just the instrumentation key:

```text
APPLICATIONINSIGHTS_CONNECTION_STRING=<full actual connection string>
```

The script uses atomic private writes: directory mode `0700`, file mode `0600`.
Treat this as an environment file, not a shell script; do not source or print it.
No template output, safe JSON field, or CLI message contains the connection
string. `ConnectionString` is retrieved privately from the component ARM
resource after apply, with API version `2020-02-02`.

`.local/azure.json` contains **exactly** these eight string fields:

```json
{
  "subscription_id": "SUBSCRIPTION_UUID",
  "resource_group": "rg-copilot-otel-audit",
  "application_insights_resource_id": "/subscriptions/SUBSCRIPTION_UUID/resourceGroups/rg-copilot-otel-audit/providers/Microsoft.Insights/components/NAME",
  "workspace_resource_id": "/subscriptions/SUBSCRIPTION_UUID/resourceGroups/rg-copilot-otel-audit/providers/Microsoft.OperationalInsights/workspaces/NAME",
  "workspace_customer_id": "WORKSPACE_CUSTOMER_UUID",
  "deployment_name": "copilot-otel-audit",
  "ownership_marker": "OWNERSHIP_UUID",
  "location": "eastus"
}
```

The verifier can consume these snake_case fields without reading collector.env.
The receipt records resource ownership, not successful telemetry ingestion or
completion of every readback check. After create, safe outputs and the parent
identity/tag/top-level inventory checks are validated, then the receipt is saved
**before** workspace/component readback or secret retrieval. It is retained if
subsequent readback or collector.env writing fails, so a later retry can reuse
the recorded resources once permissions or service configuration are corrected.
A failed run never reports success; an older collector.env can remain after
failure and must not be treated as evidence of a successful deployment.

## Parent ownership checks and teardown refusal

An existing group is accepted **only** with a matching local receipt, exact
subscription/group/resource IDs, matching location and ownership tags on the
group and both resources, a generic inventory consisting of exactly those two
resources, and a matching deployment name. Same-name or same-solution tags alone
are insufficient. Additional entries in generic inventory, mismatched markers,
missing recorded resources, malformed receipts, and symlink state paths are
refused. There is no adoption/force/skip-check flag.

Generic ARM listing omits provider-specific child/proxy resources; these are
**parent ownership checks, not a complete inventory**. The CLI does not query
ancillary child APIs (tables, favorites, diagnostics, or other extensions), and
their availability or read permissions are not deployment prerequisites.
After apply, the only ARM `az rest` reads are the recorded workspace and
Application Insights component. Their readback and connection-string handling
remain mandatory, with failures reported explicitly after saving the safe
ownership receipt.

**Automated deletion of an existing group is disabled**, including when the
receipt and parent inventory checks pass. This is the conservative fallback,
not an overrideable warning. The command below checks the recorded parent scope
and then returns nonzero with an explicit refusal:

```sh
python3 -m scripts.teardown \
  --subscription SUBSCRIPTION_UUID \
  --resource-group rg-copilot-otel-audit \
  --confirm
```

No `az group delete` or resource-delete request is issued. An already-absent
group remains a successful no-op only with a valid matching receipt and verified
account/cloud. `--confirm` confirms the audit scope; it cannot enable deletion.
Subscription/provider region checks remain deployment-only.

Hidden custom tables, private AI artifacts, and other child/proxy/extension
resources are not discovered by the parent checks. Their presence is neither
approved nor ruled out by a successful deployment/reuse check, and cannot cause
this CLI to delete a group because deletion is unconditionally refused.
An administrator must inspect all child scopes and external consumers, confirm
intended data loss, and perform teardown separately. A successful deployment
is not authorization or proof for manual group deletion.

**No local files are deleted**, including collector.env, azure.json, or unrelated
files. Stop the collector and remove/archive only the specific local files you
intend to retire. A retained receipt for an absent group blocks fresh deployment
until explicitly archived by the operator.

Ownership tags are an accident-prevention mechanism, not a security boundary
against administrators who can forge tags and edit local receipts. Keep this
group exclusive to this solution and serialize lifecycle operations: Azure
offers no transaction spanning these inventory reads and subsequent operations.
Concurrent changes remain a race for deployment/reuse and for any separately
performed manual teardown. This implementation makes no claim of exhaustive
immunity to concurrent changes.

If the first apply fails before safe outputs/inventory are obtained (or the
receipt cannot be written), resources may exist without a receipt. The scripts
will refuse to adopt or delete them automatically. An administrator must review
the stable subscription deployment history and actual resource IDs/tags, then
perform deliberate recovery outside this CLI. Do not blindly fabricate a
receipt, remove unrelated resources, or disable policy to bypass the refusal.

## Offline validation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_deploy tests.test_teardown -v
az bicep build --file infra/main.bicep --stdout --only-show-errors > /dev/null
az bicep build --file infra/resources.bicep --stdout --only-show-errors > /dev/null
```

Tests mock only the subprocess boundary and use temporary private state under
`infra/`, cleaned after each test. They do not log in or provision resources.
Use the installed Bicep version; no upgrade is needed. For verification that
must not write Azure CLI configuration outside owned paths, isolate
`AZURE_CONFIG_DIR` under `infra/`, point its `bin/bicep` at the installed compiler,
and disable telemetry/version checks. Compilation validates the emitted ARM
settings but cannot prove tenant permissions, policy acceptance, regional
capacity, ingestion, or live resource behavior.

Official references consulted before implementation:

- [Workspace and Application Insights Bicep examples](https://learn.microsoft.com/azure/azure-monitor/app/create-workspace-resource#configure-application-insights-resources)
- [Workspace 2023-09-01 schema](https://learn.microsoft.com/azure/templates/microsoft.operationalinsights/2023-09-01/workspaces)
- [Application Insights 2020-02-02 schema](https://learn.microsoft.com/azure/templates/microsoft.insights/2020-02-02/components)
