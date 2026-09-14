# Deploy and rebuild with Azure CLI

[Administrator guide](../Administrators.md) | [Relay deployment](relay-deployment.md) |
[Agent and workbook context](../AGENTS.md)

These are operator-executed **Bash + Azure CLI + Bicep** runbooks, not Python
deployment wrappers. Read each review gate before executing its following
mutation. Do not run an entire document unattended. Linux is the documented
platform; PowerShell/Windows are not validated. Azure native OTLP is **preview,
without an SLA, and not recommended for production**.

The native stack is unchanged: `infra/main.bicep` invokes `resources.bicep`,
creating the dedicated `rg-copilot-otel-v1`, LAW, AMW, and explicit DCE/DCR.
No Application Insights component, portal OTLP opt-in, collector, VM, container,
or agent is required. The new default client route adds a restricted Function
in **another group**, then authenticates to this same DCE with managed identity.

## Prerequisites and private state

Install Azure CLI with Bicep, Bash, jq, Node.js 22/npm, zip/unzip, and curl.
Node/npm are needed for the relay/package and workbook generation, not the
native ARM deployment. Python 3.12+ is needed **only for optional existing
synthetic smoke/query tests**. Copilot/GitHub sign-in is needed for real CLI
inference, not for deployment.

`zip` is not guaranteed to be installed on the operator host. Check packaging
prerequisites before staging a release; install `zip`/`unzip` only through an
approved package-manager/sudo workflow. The [relay runbook](relay-deployment.md#package-and-deploy-code-with-azure-cli)
also documents local Core Tools packaging when installing `zip` is not
permitted. That alternative does not deploy or require Python.

Use an already authorized subscription; the examples do not change it silently.
For a new login use `az login`, then `az account set --subscription <approved-id>`.
Never print credentials or enable shell tracing.

```bash
set -euo pipefail
set +x
umask 077
test ! -L .local
mkdir -p .local
chmod 700 .local
export AZURE_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
test "$(az account show --query state -o tsv)" = Enabled
test "$(az cloud show --query name -o tsv)" = AzureCloud
az version
az bicep version
for PROVIDER in Microsoft.Insights Microsoft.OperationalInsights Microsoft.Monitor; do
  az provider show --namespace "$PROVIDER" --query registrationState -o tsv
done
```

Only if a required provider is unregistered, an authorized administrator can
run `az provider register --namespace <provider> --wait`. Do not guess feature
registrations or work around Azure policy. Use `eastus` only when approved.

Existing private `.local/native-azure.json` is the authoritative verified runtime
receipt. Keep it and `native-deployment.json`, `native-workspace-children.json`,
and any `native-teardown.json` intact. The historical Python lifecycle's
baseline/receipt guards are **not recreated or bypassed by this CLI runbook**.
For an existing stack, use its actual marker and principal parameters:

```bash
if test -f .local/native-azure.json; then
  jq -e --arg sub "$AZURE_SUBSCRIPTION_ID" \
    '.subscription_id == $sub and .resource_group == "rg-copilot-otel-v1"' \
    .local/native-azure.json >/dev/null
  OWNERSHIP_MARKER="$(jq -er .ownership_marker .local/native-azure.json)"
  LOCATION="$(jq -er .location .local/native-azure.json)"
else
  # A missing receipt is not permission to adopt a pre-existing group.
  test "$(az group exists -n rg-copilot-otel-v1)" = false
  LOCATION=eastus
  OWNERSHIP_MARKER="$(node -p 'require("node:crypto").randomUUID()')"
fi
export LOCATION OWNERSHIP_MARKER
```

Preserve deployment intent **before** any Azure write. Reuse that exact intent
on retries; if `.local/native-cli-parameters.json` already exists, inspect and
reuse it rather than regenerate its marker. If ARM already created the group,
use [interrupted-apply recovery](#recover-an-interrupted-first-apply).

## Roles and parameter file

| Identity | Role | Scope |
| --- | --- | --- |
| Native publisher (optional direct client) | Monitoring Metrics Publisher | Explicit DCR |
| Relay system-assigned identity | Monitoring Metrics Publisher | Same explicit DCR, separate assignment |
| Query operator | Log Analytics Reader | LAW |
| Query operator | Monitoring Data Reader | AMW |
| Deployer | Resource writes and scoped role-assignment authority | Approved deployment scopes |

The original template requires a publisher parameter even if all clients use
the relay. Retain the existing approved value on updates; changing it is not
revocation. The relay deployment adds its own principal, not a replacement.
Use object IDs, not application/client IDs. Directory lookup for a signed-in
user can use `az ad signed-in-user show --query id -o tsv`; non-user logins
require an explicit object ID/type. Supplying an ID does not change your login.

For a **new parameter file only**, set approved identities:

```bash
export PUBLISHER_OBJECT_ID="<approved-object-uuid>"
export PUBLISHER_TYPE=User
export OPERATOR_OBJECT_ID="$PUBLISHER_OBJECT_ID"
export OPERATOR_TYPE=User
test ! -e .local/native-cli-parameters.json
jq -n --arg location "$LOCATION" --arg marker "$OWNERSHIP_MARKER" \
  --arg publisher "$PUBLISHER_OBJECT_ID" --arg publisherType "$PUBLISHER_TYPE" \
  --arg operator "$OPERATOR_OBJECT_ID" --arg operatorType "$OPERATOR_TYPE" \
  '{location:{value:$location},ownershipMarker:{value:$marker},
    principalId:{value:$publisher},principalType:{value:$publisherType},
    operatorPrincipalId:{value:$operator},operatorPrincipalType:{value:$operatorType}}' \
  > .local/native-cli-parameters.json
```

`principalType` and `operatorPrincipalType` accept `User`, `ServicePrincipal`,
or `Group`. Reuse the original parameters when updating an existing deployment;
recover them privately from `az deployment sub show -n copilot-otel-v1 --query
properties.parameters` if necessary. Never overwrite the original receipts.

## Validate, preview, then apply

For an existing group, require its exact subscription, location, solution tag
`copilot-otel-v1`, ownership marker, resource IDs and inventory to match the
private receipts. Review workspace children and external consumers using
[read-only inventory capture](#capture-native-inventory), but **do not delete anything**. Refuse unexpected
resources or drift. ARM what-if is not an ownership check.

```bash
az bicep build --file infra/main.bicep --stdout >/dev/null
az deployment sub validate --name copilot-otel-v1 --location "$LOCATION" \
  --template-file infra/main.bicep --parameters @.local/native-cli-parameters.json \
  > .local/native-cli-validation.json
az deployment sub what-if --name copilot-otel-v1 --location "$LOCATION" \
  --template-file infra/main.bicep --parameters @.local/native-cli-parameters.json \
  > .local/native-cli-what-if.txt
```

**Review gate:** approve exactly the dedicated native resources, roles, region,
public authenticated DCE, cost, and intended changes. Resolve any deletion,
unexpected scope, tag replacement, or resource-identity change before applying.

```bash
az deployment sub create --name copilot-otel-v1 --location "$LOCATION" \
  --template-file infra/main.bicep --parameters @.local/native-cli-parameters.json \
  > .local/native-cli-result.json
jq -e '.properties.provisioningState == "Succeeded"' \
  .local/native-cli-result.json >/dev/null
jq -e '.properties.outputs.nativeState.value' .local/native-cli-result.json \
  > .local/native-cli-candidate.json
```

Read the candidate's exact LAW from ARM, independently of the deployment:

```bash
LAW_ID="$(jq -er .workspace_resource_id .local/native-cli-candidate.json)"
az rest --method get \
  --url "https://management.azure.com${LAW_ID}?api-version=2023-09-01" \
  > .local/native-cli-law.json
jq -e --slurpfile state .local/native-cli-candidate.json \
  '(.id|ascii_downcase) == ($state[0].workspace_resource_id|ascii_downcase)
   and .location == $state[0].location
   and .tags.solution == "copilot-otel-v1"
   and .tags["ownership-marker"] == $state[0].ownership_marker
   and .properties.customerId == $state[0].workspace_customer_id' \
  .local/native-cli-law.json >/dev/null
az group show -n rg-copilot-otel-v1 > .local/native-cli-group.json
az resource list -g rg-copilot-otel-v1 > .local/native-cli-inventory.json
az rest --method get \
  --url "https://management.azure.com$(jq -er .dcr_resource_id .local/native-cli-candidate.json)?api-version=2024-03-11" \
  > .local/native-cli-dcr.json
az rest --method get \
  --url "https://management.azure.com$(jq -er .dce_resource_id .local/native-cli-candidate.json)?api-version=2024-03-11" \
  > .local/native-cli-dce.json
```

Review exact group/marker/location, complete inventory, DCR immutable ID,
direct data sources/destinations, DCE linkage and full signal URLs. Traces/logs
use the DCE logs domain; metrics use its metrics domain. Public route names
are not identical to internal OTel stream names. Verify scoped assignments and
query permissions. No Application Insights resource should be introduced.

On repeat apply, compare **all** nativeState identity/endpoint fields with the
existing receipt and reject drift; retain the original file byte-for-byte:

```bash
if test -f .local/native-azure.json; then
  jq -e --slurpfile old .local/native-azure.json \
    'to_entries | all(.[]; .value == $old[0][.key])' \
    .local/native-cli-candidate.json >/dev/null
fi
```

Only for a genuinely **new**, reviewed CLI-managed deployment, select the
validated candidate before capturing inventory: the operational receipt does
not exist yet.

```bash
export NATIVE_STATE=.local/native-cli-candidate.json
test -f "$NATIVE_STATE"
```

Now preserve a private full workspace-child inventory using
[read-only inventory capture](#capture-native-inventory) in the same
shell. Record operator approval separately. Then install the validated candidate
without overwriting any operational receipt and select the installed receipt:

```bash
test ! -e .local/native-azure.json
test ! -L .local/native-azure.json
( set -o noclobber; cat .local/native-cli-candidate.json > .local/native-azure.json )
chmod 600 .local/native-azure.json
export NATIVE_STATE=.local/native-azure.json
```

This does not create a historical Python child-baseline receipt. Do not
manufacture one or claim its automatic teardown guard has run. The explicit
CLI lifecycle requires separately reviewed commands and approval under the
[resource lifecycle constraints](../AGENTS.md#resource-lifecycle-constraints).
Existing baseline receipts remain authoritative and unchanged.

## Capture native inventory

This is read-only Azure access for initial deployment and subsequent ownership
review. Use the private shell above and the explicitly selected `NATIVE_STATE`
candidate or installed receipt. Each capture gets a new private directory;
retain the original approved snapshot and never overwrite a baseline.

```bash
export NATIVE_STATE="${NATIVE_STATE:-.local/native-azure.json}"
export INVENTORY_DIR="$(mktemp -d "$PWD/.local/native-inventory.XXXXXXXX")"
export LAW_ID="$(jq -er .workspace_resource_id "$NATIVE_STATE")"
export AMW_ID="$(jq -er .azure_monitor_workspace_resource_id "$NATIVE_STATE")"
az group show -n rg-copilot-otel-v1 > "$INVENTORY_DIR/group.json"
az resource list -g rg-copilot-otel-v1 > "$INVENTORY_DIR/resources.json"
az rest --method get --url "https://management.azure.com${LAW_ID}?api-version=2023-09-01" \
  > "$INVENTORY_DIR/law.json"
az rest --method get --url "https://management.azure.com${AMW_ID}?api-version=2025-10-03" \
  > "$INVENTORY_DIR/amw.json"
az role assignment list --scope "$(jq -er .dcr_resource_id "$NATIVE_STATE")" --all \
  > "$INVENTORY_DIR/dcr-roles.json"
az role assignment list --scope "$LAW_ID" --all > "$INVENTORY_DIR/law-roles.json"
az role assignment list --scope "$AMW_ID" --all > "$INVENTORY_DIR/amw-roles.json"

capture_children() {
  local child="$1" api="$2" url page pages
  url="https://management.azure.com${LAW_ID}/${child}?api-version=${api}"
  pages="$(mktemp "$INVENTORY_DIR/pages.XXXXXXXX")"
  page="$(mktemp "$INVENTORY_DIR/page.XXXXXXXX")"
  while test -n "$url"; do
    case "$url" in
      "https://management.azure.com${LAW_ID}/"*) ;;
      *) echo "Unexpected pagination scope" >&2; return 1 ;;
    esac
    az rest --method get --url "$url" > "$page"
    jq -e '(.value|type) == "array" and (has("error")|not)' "$page" >/dev/null
    jq -c '.value' "$page" >> "$pages"
    url="$(jq -er '.nextLink // ""' "$page")"
  done
  jq -s '{value:add}' "$pages" > "$INVENTORY_DIR/$child.json"
  rm -- "$page" "$pages"
}
capture_children tables 2023-09-01
capture_children savedSearches 2020-08-01
capture_children dataExports 2020-08-01
capture_children linkedServices 2020-08-01
capture_children linkedStorageAccounts 2020-08-01
```

Verify exact LAW ARM/customer identity, location, ownership marker and complete
resource scope against the original receipt. Review tables, saved searches,
exports and links; foreign/custom children or unexpected consumers block an
update until explicitly reviewed. Generic resource listing does not enumerate
every child: inspect locks, policies, diagnostics, workbooks and external
consumers through their owning resource/provider surfaces.

Where the historical workspace-child baseline exists, compare its exact
LAW/customer/ownership binding, saved-search ID set and canonical-JSON SHA-256
hashes. Do not replace full-content comparison with name-prefix matching or
initialize a replacement baseline to suppress drift. Preserve operator
approval separately. This capture does not approve deletion or prove that
every possible external dependency has been discovered.

## Recover an interrupted first apply

Keep original intent, result, receipts, inventory, and marker. Read
`az deployment sub show -n copilot-otel-v1 --query properties.outputs.nativeState.value`
into a **candidate file**, not over `native-azure.json`. Require succeeded ARM
state, approved original parameters, complete owned inventory, fresh LAW
ARM/customer binding, exact DCR/DCE URLs and reviewed children as above.
An authorized reviewer must resolve partial/mismatched resources before any
retry. A missing or changed historical baseline is not automatic adoption:
preserve it for investigation and do not invoke legacy lifecycle operations
until that workflow's own recovery requirements are met.

## Continue and rebuild

Deploy the [restricted relay](relay-deployment.md), then require fresh
[logs forwarding and backend proof](../AGENTS.md#telemetry-evidence).
A deployment, token, HTTP 2xx, or empty-payload HTTP 400 is not persistence proof.
Allow bounded DCE/RBAC propagation and report failures rather than silently
retrying inference forever.

Read actual LAW and `OTelSpans`/`OTelEvents`/`OTelResources`/logs-table retention.
The requested 30 days and 1 GB/day LAW cap are **not a total cost ceiling or
table-level secure-erasure guarantee**; AMW metrics and relay storage/compute
have separate costs.

For a rebuild, first obtain explicit retirement approval and separately
reviewed Azure CLI commands under the
[resource lifecycle constraints](../AGENTS.md#resource-lifecycle-constraints),
including the relay's cross-group DCR assignment and AMW-managed group's disappearance.
Archive old CLI-managed receipts privately, retain historical lifecycle
receipts intact, and explicitly approve new deployment intent. Never reuse
old run evidence against replacement workspace/DCR identities. Repeat native
deployment, relay deployment, fresh signal verification, and workbook
acceptance. Name reuse may require Azure deletion propagation; do not force
purges or change ownership to evade it.

No command here commits, pushes, publishes evidence, or changes visibility.
