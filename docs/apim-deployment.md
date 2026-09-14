# Deploy the API-key-authenticated APIM OTLP gateway

[Choose a path](../README.md) | [Native prerequisites](deployment.md) |
[Function alternative](relay-deployment.md) | [Client configuration](../Administrators.md) |
[Security](security-and-data.md)

This is the Azure CLI/Bicep runbook for a **separate APIM gateway**:
client API-scoped subscription key -> APIM system-assigned managed identity ->
existing authenticated DCE/DCR -> existing LAW/AMW. No Function, collector,
Application Insights component, or workstation Azure publisher role is added.
Use the explicit DCE's native OTLP endpoints, not the legacy workspace shared-key
Data Collector API.

The default Function path remains unchanged. APIM owns `rg-copilot-otel-apim`,
a fresh `copilot-otel-apim` solution marker, and its own receipt namespace.
Only an additive publisher role on the exact native DCR crosses group boundaries.
**The monitoring backend is shared, not independently provisioned:** quotas,
retention, native availability, and existing authorized direct senders remain
shared. No native or Function template is applied by this runbook.
Rows in the shared workspace are not thereby authenticated as APIM traffic:
the unchanged Function and authorized direct publishers can still ingest.
Do not use this route's API key as proof of provenance for all workspace rows.

Native OTLP remains preview without an SLA and is not recommended for production.
The APIM Developer tier is evaluation-only. Secure client admission alone does
not establish enterprise production readiness, delivery guarantees, authenticated
employee attribution, or DLP.

## Security contract and review gates

The API requires an active key in `X-Copilot-Telemetry-Key` and an explicitly
admitted subscription identity. Only the dedicated API-scoped subscription is
admitted: APIM's built-in all-access, all-APIs, and product credentials must not
bypass API-level policy. There is no open product or self-service enrollment.
APIM keys are shared secrets, not Entra audience/scope JWTs. OAuth would require
a separate design and client renewal mechanism.

Only POST `/otlp/v1/traces`, `/otlp/v1/logs`, and `/otlp/v1/metrics` can forward.
Backend origins and complete stream paths are bound from verified native state.
Query strings are rejected; client authentication headers are stripped.
Uncompressed binary `application/x-protobuf` with a canonical `Content-Length`
is the only APIM v1 representation. Missing length is rejected (411); streamed
chunked framing is unsupported. Declared size is bounded before body inspection,
then actual byte length and framing consistency are checked. A post-buffer
4 MiB check alone would not be a 4 MiB allocation bound on the managed gateway.
Gzip is rejected rather than claiming a decompression bound the managed gateway
has not demonstrated. The existing Function still supports bounded gzip.

Do not weaken routing, authentication, quotas, body limits, or diagnostic
redaction to pass E2E. Offline template/XML checks are not execution of the
APIM policy engine. Both control-plane readback and live data-plane tests are
mandatory before onboarding. APIM backend timeout is not the Function's complete
request/identity/response deadline. No durable retry or exactly-once claim is
made; an ambiguous timeout followed by client retry can duplicate telemetry.

All shell snippets run from the implementation worktree root. Execute each gate
separately, not the whole document unattended. No command here authorizes
committing secrets, pushing, changing repository visibility, deleting resources,
or adopting a pre-existing service.

## Private shell, tools, and existing native ownership

Use Bash, Azure CLI/Bicep, Node 22/npm, jq, and existing Python tests for offline
regression only. Do not execute Python deployment/teardown wrappers.

```bash
set -euo pipefail
set +x
umask 077
test ! -L .local
mkdir -p .local
chmod 700 .local
test "$(node -p 'process.versions.node.split(".")[0]')" = 22
az version
az bicep version
export AZURE_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
test "$(az account show --query state -o tsv)" = Enabled
test "$(az cloud show --query name -o tsv)" = AzureCloud
az provider show --namespace Microsoft.ApiManagement --query registrationState -o tsv
```

If registration is absent, an authorized administrator can run
`az provider register --namespace Microsoft.ApiManagement --wait`; registration
does not grant APIM deployment, policy, subscription-secret, or RBAC permissions.
For a new authorized login use `az login` followed by
`az account set --subscription <approved-subscription-uuid>`. Never silently
select a different subscription on an authorization error.

Select the **original verified** native receipt explicitly, using an absolute
path if the new worktree has no `.local/native-azure.json`. Do not duplicate,
rewrite, or regenerate the original marker/child baselines:

```bash
export NATIVE_STATE="/absolute/private/path/to/native-azure.json"
test -f "$NATIVE_STATE"
test ! -L "$NATIVE_STATE"
jq -e --arg sub "$AZURE_SUBSCRIPTION_ID" \
  '.subscription_id == $sub and .resource_group == "rg-copilot-otel-v1"' \
  "$NATIVE_STATE" >/dev/null
export APIM_REVIEW_DIR="$(mktemp -d "$PWD/.local/apim-review.XXXXXXXX")"
export DCR_ID="$(jq -er .dcr_resource_id "$NATIVE_STATE")"
export DCE_ID="$(jq -er .dce_resource_id "$NATIVE_STATE")"
export LAW_ID="$(jq -er .workspace_resource_id "$NATIVE_STATE")"
export AMW_ID="$(jq -er .azure_monitor_workspace_resource_id "$NATIVE_STATE")"
az group show -n rg-copilot-otel-v1 > "$APIM_REVIEW_DIR/native-group.json"
az resource list -g rg-copilot-otel-v1 > "$APIM_REVIEW_DIR/native-resources.json"
az rest --method get --url "https://management.azure.com${DCR_ID}?api-version=2024-03-11" \
  > "$APIM_REVIEW_DIR/dcr.json"
az rest --method get --url "https://management.azure.com${DCE_ID}?api-version=2024-03-11" \
  > "$APIM_REVIEW_DIR/dce.json"
az rest --method get --url "https://management.azure.com${LAW_ID}?api-version=2023-09-01" \
  > "$APIM_REVIEW_DIR/law.json"
az rest --method get --url "https://management.azure.com${AMW_ID}?api-version=2025-10-03" \
  > "$APIM_REVIEW_DIR/amw.json"
az role assignment list --scope "$DCR_ID" --include-inherited --fill-principal-name false \
  > "$APIM_REVIEW_DIR/dcr-roles-before.json"
```

Compare exact subscription, group, original solution/ownership tags, DCR
immutable ID, DCE linkage, stream paths and destinations, LAW ARM/customer ID,
and AMW query identity against the original receipt. Validate URL provenance
against live DCE domain values, not just a syntactically plausible Azure host.
The backend receives APIM's bearer token; a plausible but foreign URL is not
acceptable.

Complete the native runbook's
[read-only child/consumer inventory](deployment.md#capture-native-inventory)
with this same `NATIVE_STATE`; preserve original canonical saved-search hashes.
Inspect locks, role inheritance, diagnostics, exports, linked services, workbooks,
and external consumers. Record current Function receipt/resource/ingress/role
identity as well when a Function is deployed. Unexpected children, foreign
resources, missing original ownership state, or drift stop mutations.

The intended permissions are:

| Identity | Permission |
| --- | --- |
| APIM client | Dedicated API subscription key only |
| APIM MI | Monitoring Metrics Publisher on exact DCR |
| Verifier | Log Analytics Reader on LAW; Monitoring Data Reader on AMW |
| Deployer | Resource/policy writes within owned deployment scopes; exact DCR role-assignment authority |
| Credential operator | APIM subscription secret retrieval/rotation; no client requirement for these management rights |

Check exact authorized region, Developer availability/capacity, operator contact,
expected recurring cost, and budget before first creation. Service creation can
take many minutes; a deployment timeout is not permission to start a duplicate
deployment or erase its ownership intent.

Azure CLI 2.89 rejects `role assignment list --scope ... --all`: `--all`
selects subscription-wide enumeration and cannot be combined with scope.
Use the scoped command above (including inherited assignments) for DCR review.
The native and Function inventory examples use the same corrected form.

## Generate new intent and deploy the deny baseline

The template accepts only the supported Developer evaluation SKU. Capacity is
one; do not select an unreviewed tier by editing the generated ARM JSON. Policy
limits are intentionally fixed in checked-in policy: review changes to limits
as code, not an undocumented operator override.

For the first deployment require group absence. For an existing APIM group,
skip first-create marker generation and follow the recovery/update gate instead.
The current public-cloud examples use `eastus` only after approval:

```bash
test "$(az group exists -n rg-copilot-otel-apim)" = false
export APIM_LOCATION=eastus
export APIM_OWNERSHIP_MARKER="$(node -p 'require("node:crypto").randomUUID()')"
export APIM_PUBLISHER_NAME="Copilot telemetry evaluation"
export APIM_PUBLISHER_EMAIL="<approved-operator-contact-email>"
node --input-type=module <<'JS'
import fs from 'node:fs';
const read = path => JSON.parse(fs.readFileSync(path, 'utf8'));
const nativeState = read(process.env.NATIVE_STATE);
const input = {
  nativeState,
  subscriptionId: process.env.AZURE_SUBSCRIPTION_ID,
  ownershipMarker: process.env.APIM_OWNERSHIP_MARKER,
  location: process.env.APIM_LOCATION,
  publisherName: process.env.APIM_PUBLISHER_NAME,
  publisherEmail: process.env.APIM_PUBLISHER_EMAIL,
  skuName: 'Developer',
  activateGateway: false,
  dcrResourceId: nativeState.dcr_resource_id,
  dceResourceId: nativeState.dce_resource_id,
  dcrReadback: read(`${process.env.APIM_REVIEW_DIR}/dcr.json`),
  dceReadback: read(`${process.env.APIM_REVIEW_DIR}/dce.json`)
};
fs.writeFileSync('.local/apim-input.json', JSON.stringify(input, null, 2),
  {flag: 'wx', mode: 0o600});
JS
node src/tools/apim-preflight.mjs \
  --input .local/apim-input.json --output .local/apim-parameters.json
az bicep build --file infra/apim.bicep --stdout >/dev/null
az deployment sub validate -n copilot-otel-apim --location "$APIM_LOCATION" \
  --template-file infra/apim.bicep --parameters @.local/apim-parameters.json \
  > "$APIM_REVIEW_DIR/validation.json"
az deployment sub what-if -n copilot-otel-apim --location "$APIM_LOCATION" \
  --template-file infra/apim.bicep --parameters @.local/apim-parameters.json \
  --no-pretty-print --result-format FullResourcePayloads -o json \
  > "$APIM_REVIEW_DIR/what-if.json"
```

The preflight is offline parameter generation, not a Python/Azure deployment
wrapper or proof of ownership on its own. It refuses mismatched native state,
immutable IDs, origins/routes, stream provenance, and nonexclusive output.
Inputs contain operational metadata and must remain private. Validate the
native destinations/LAW/AMW identities independently as above.

**Apply gate:** review what-if, original native/Function inventory and receipt
bindings, authorization, location, cost and publisher contact. Allow only the
new owned APIM group/service/API/operations/policies and one additive publisher
assignment on the existing DCR. No existing native resource, Function, workbook,
other publisher or ownership marker may change. `activateGateway=false` installs
deny admission; do not deploy individual submodules to skip ordering.

```bash
az deployment sub create -n copilot-otel-apim --location "$APIM_LOCATION" \
  --template-file infra/apim.bicep --parameters @.local/apim-parameters.json \
  > "$APIM_REVIEW_DIR/baseline-result.json"
jq -e '.properties.provisioningState == "Succeeded"' \
  "$APIM_REVIEW_DIR/baseline-result.json" >/dev/null
jq -e '.properties.outputs.apimState.value' \
  "$APIM_REVIEW_DIR/baseline-result.json" > "$APIM_REVIEW_DIR/baseline-candidate.json"
export APIM_ID="$(jq -er .apim_resource_id "$APIM_REVIEW_DIR/baseline-candidate.json")"
export APIM_API_ID="$(jq -er .api_resource_id "$APIM_REVIEW_DIR/baseline-candidate.json")"
az group show -n rg-copilot-otel-apim > "$APIM_REVIEW_DIR/group.json"
az resource list -g rg-copilot-otel-apim > "$APIM_REVIEW_DIR/resources.json"
az rest --method get --url "https://management.azure.com${APIM_ID}?api-version=2024-05-01" \
  > "$APIM_REVIEW_DIR/service.json"
az rest --method get --url "https://management.azure.com${APIM_API_ID}?api-version=2024-05-01" \
  > "$APIM_REVIEW_DIR/api.json"
az rest --method get \
  --url "https://management.azure.com${APIM_ID}/policies/policy?api-version=2024-05-01&format=rawxml" \
  > "$APIM_REVIEW_DIR/service-policy.json"
az rest --method get \
  --url "https://management.azure.com${APIM_API_ID}/policies/policy?api-version=2024-05-01&format=rawxml" \
  > "$APIM_REVIEW_DIR/baseline-policy.json"
az rest --method get \
  --url "https://management.azure.com${APIM_API_ID}/operations?api-version=2024-05-01" \
  > "$APIM_REVIEW_DIR/operations.json"
az role assignment list --scope "$DCR_ID" --include-inherited --fill-principal-name false \
  > "$APIM_REVIEW_DIR/dcr-roles-after-baseline.json"
```

Preserve a baseline-deny receipt before activation, without overwriting it.
Verify exact solution/marker/location, stable resource IDs, system principal,
TLS settings, dedicated API path/header/subscription requirement, and the deny
policy. Enumerate **all** APIs, products, subscriptions, loggers, diagnostics,
revisions, and policies; follow ARM pagination without changing subscription or
resource scope. Service-level default subscriptions do not count as authorized
telemetry subscriptions. Capture each operation's policy separately:

```bash
export APIM_CHILD_DIR="$(mktemp -d "$APIM_REVIEW_DIR/children.XXXXXXXX")"
capture_apim_list() {
  local scope="$1" child="$2" destination="$3" base url page pages seen count=0
  case "$scope" in
    "$APIM_ID"|"$APIM_ID"/apis/*|"$APIM_ID"/products/*) ;;
    *) echo 'Unexpected APIM inventory scope' >&2; return 1 ;;
  esac
  case "$child" in
    apis|products|subscriptions|loggers|diagnostics|revisions|policies|operations) ;;
    *) echo 'Unexpected APIM child collection' >&2; return 1 ;;
  esac
  base="https://management.azure.com${scope}/${child}"
  url="${base}?api-version=2024-05-01"
  page="$(mktemp "$APIM_CHILD_DIR/page.XXXXXXXX")"
  pages="$(mktemp "$APIM_CHILD_DIR/pages.XXXXXXXX")"
  seen="$(mktemp "$APIM_CHILD_DIR/seen.XXXXXXXX")"
  while test -n "$url"; do
    case "$url" in "$base"\?*) ;; *) echo 'Pagination left collection' >&2; return 1;; esac
    count=$((count + 1))
    test "$count" -le 1000
    if grep -Fxq -- "$url" "$seen"; then echo 'Repeated pagination link' >&2; return 1; fi
    printf '%s\n' "$url" >> "$seen"
    az rest --method get --url "$url" > "$page"
    jq -e '(.value|type) == "array" and (has("error")|not)
      and (.nextLink == null or (.nextLink|type) == "string")' "$page" >/dev/null
    jq -c '.value' "$page" >> "$pages"
    url="$(jq -r '.nextLink // ""' "$page")"
  done
  test ! -e "$destination"
  ( set -o noclobber; jq -s '{value:add}' "$pages" > "$destination" )
  rm -- "$page" "$pages" "$seen"
}
safe_apim_name() {
  local pattern='^[A-Za-z0-9_-][A-Za-z0-9_.;-]*([=][0-9]+)?$'
  [[ "$1" =~ $pattern ]] && test "${#1}" -le 256
}
for CHILD in apis products subscriptions loggers diagnostics policies; do
  capture_apim_list "$APIM_ID" "$CHILD" "$APIM_CHILD_DIR/service-$CHILD.json"
done
jq -e '.value | all(.[]; (.name|type) == "string")' "$APIM_CHILD_DIR/service-apis.json" >/dev/null
jq -r '[.value[].name | split(";rev=")[0]] | unique[]' \
  "$APIM_CHILD_DIR/service-apis.json" > "$APIM_CHILD_DIR/api-names.txt"
while IFS= read -r API_NAME; do
  safe_apim_name "$API_NAME"
  API_SCOPE="$APIM_ID/apis/$API_NAME"
  API_DIR="$(mktemp -d "$APIM_CHILD_DIR/api.XXXXXXXX")"
  capture_apim_list "$API_SCOPE" revisions "$API_DIR/revisions.json"
  jq -e '.value | length > 0 and all(.[]; (.apiId|type) == "string")' "$API_DIR/revisions.json" >/dev/null
  jq -r '.value[].apiId' "$API_DIR/revisions.json" > "$API_DIR/revision-ids.txt"
  while IFS= read -r REVISION_ID; do
    case "$REVISION_ID" in "$API_SCOPE"|"$API_SCOPE;rev="*) ;; *) echo 'Foreign API revision' >&2; exit 1;; esac
    safe_apim_name "${REVISION_ID##*/}"
    REVISION_DIR="$(mktemp -d "$API_DIR/revision.XXXXXXXX")"
    printf '%s\n' "$REVISION_ID" > "$REVISION_DIR/resource-id.txt"
    for CHILD in policies diagnostics operations; do
      capture_apim_list "$REVISION_ID" "$CHILD" "$REVISION_DIR/$CHILD.json"
    done
    jq -e '.value | all(.[]; (.name|type) == "string")' "$REVISION_DIR/operations.json" >/dev/null
    jq -r '.value[].name' "$REVISION_DIR/operations.json" > "$REVISION_DIR/operation-names.txt"
    while IFS= read -r OP_ID; do
      safe_apim_name "$OP_ID"
      capture_apim_list "$REVISION_ID/operations/$OP_ID" policies "$REVISION_DIR/operation-$OP_ID-policies.json"
    done < "$REVISION_DIR/operation-names.txt"
  done < "$API_DIR/revision-ids.txt"
done < "$APIM_CHILD_DIR/api-names.txt"
jq -e '.value | all(.[]; (.name|type) == "string")' "$APIM_CHILD_DIR/service-products.json" >/dev/null
jq -r '.value[].name' "$APIM_CHILD_DIR/service-products.json" > "$APIM_CHILD_DIR/product-names.txt"
while IFS= read -r PRODUCT_ID; do
  safe_apim_name "$PRODUCT_ID"
  for CHILD in policies apis; do
    capture_apim_list "$APIM_ID/products/$PRODUCT_ID" "$CHILD" "$APIM_CHILD_DIR/product-$PRODUCT_ID-$CHILD.json"
  done
done < "$APIM_CHILD_DIR/product-names.txt"
for OP_ID in post-logs post-traces post-metrics; do
  az rest --method get \
    --url "https://management.azure.com${APIM_API_ID}/operations/${OP_ID}/policies/policy?api-version=2024-05-01&format=rawxml" \
    > "$APIM_REVIEW_DIR/operation-$OP_ID-policy.json"
done
```

Generic resource listing is not a complete APIM child inventory. The bounded
traversal above follows same-collection pagination and includes all discovered
API revisions, their diagnostics and operation/API policies, plus product
policies and API membership. Unexpected identifier shapes or inventory limits
stop review rather than silently skip resources. Compare canonical full policy text with the reviewed templates,
not just the presence of a policy resource. Confirm exactly the intended three
POSTs; no wildcard operation or other native destination is acceptable.
Verify the service-level default deny as well as the telemetry API policy.
Activation intentionally supplies the complete telemetry API admission policy;
other APIs must not inherit managed-identity forwarding.
Check the assignment ID/principal/role/scope against `apimState`; compare all
original DCR roles and reject any removal/change.

## Explicit activation and private key retrieval

Only after baseline readback passes, prepare a new exclusive input/parameter
pair with `activateGateway=true`, retaining all original intent:

```bash
node --input-type=module <<'JS'
import fs from 'node:fs';
const input = JSON.parse(fs.readFileSync('.local/apim-input.json', 'utf8'));
input.activateGateway = true;
fs.writeFileSync('.local/apim-active-input.json', JSON.stringify(input, null, 2),
  {flag: 'wx', mode: 0o600});
JS
node src/tools/apim-preflight.mjs \
  --input .local/apim-active-input.json --output .local/apim-active-parameters.json
az deployment sub validate -n copilot-otel-apim --location "$APIM_LOCATION" \
  --template-file infra/apim.bicep --parameters @.local/apim-active-parameters.json \
  > "$APIM_REVIEW_DIR/active-validation.json"
az deployment sub what-if -n copilot-otel-apim --location "$APIM_LOCATION" \
  --template-file infra/apim.bicep --parameters @.local/apim-active-parameters.json \
  --no-pretty-print --result-format FullResourcePayloads -o json \
  > "$APIM_REVIEW_DIR/active-what-if.json"
```

**Activation gate:** review only the intended admission-policy update and
dedicated API subscription addition; resources/identity/role must remain stable.
Existing modules first reapply deny before activation, so updates deliberately
interrupt admission while policies change. Schedule that interruption; this is
not a zero-downtime design. A reapply with activation enabled also requests the
subscription active: do not reactivate a suspended incident-response credential
without explicit review.

```bash
az deployment sub create -n copilot-otel-apim --location "$APIM_LOCATION" \
  --template-file infra/apim.bicep --parameters @.local/apim-active-parameters.json \
  > "$APIM_REVIEW_DIR/active-result.json"
jq -e '.properties.provisioningState == "Succeeded"' \
  "$APIM_REVIEW_DIR/active-result.json" >/dev/null
jq -e '.properties.outputs.apimState.value' \
  "$APIM_REVIEW_DIR/active-result.json" > "$APIM_REVIEW_DIR/active-candidate.json"
export APIM_SUBSCRIPTION_ID="$(jq -er .api_subscription_resource_id "$APIM_REVIEW_DIR/active-candidate.json")"
az rest --method get \
  --url "https://management.azure.com${APIM_SUBSCRIPTION_ID}?api-version=2024-05-01" \
  > "$APIM_REVIEW_DIR/subscription.json"
az rest --method get \
  --url "https://management.azure.com${APIM_API_ID}/policies/policy?api-version=2024-05-01&format=rawxml" \
  > "$APIM_REVIEW_DIR/active-policy.json"
```

Verify API subscription scope/state/tracing disabled and exact API-level
subscription-ID guard, rate/quota enforcement, fail-closed error handling and
operation inheritance. Repeat service/API/operation/role readbacks into new
files and compare unchanged identities, resource inventory and backend URLs.
Only after independent verification install a first active receipt:

```bash
test ! -e .local/apim-azure.json
test ! -L .local/apim-azure.json
( set -o noclobber; cat "$APIM_REVIEW_DIR/active-candidate.json" > .local/apim-azure.json )
chmod 600 .local/apim-azure.json
test ! -e "$APIM_REVIEW_DIR/subscription-secrets.json"
az rest --method post \
  --url "https://management.azure.com${APIM_SUBSCRIPTION_ID}/listSecrets?api-version=2024-05-01" \
  > "$APIM_REVIEW_DIR/subscription-secrets.json"
chmod 600 "$APIM_REVIEW_DIR/subscription-secrets.json"
export APIM_SUBSCRIPTION_KEY="$(jq -er .primaryKey "$APIM_REVIEW_DIR/subscription-secrets.json")"
export APIM_BASE="$(jq -er '.logs_endpoint | sub("/v1/logs$"; "")' .local/apim-azure.json)"
```

Treat even non-listSecrets subscription responses as potentially sensitive and
keep them private. Never print keys, key hashes, whole JSON responses, process
environments, or curl headers. Clients receive only their key and nonsecret
endpoints through approved distribution, not the ownership receipt.

Run bounded HTTP probes followed by an isolated actual CLI run:

```bash
node src/tools/probe-apim.mjs --endpoint "$APIM_BASE" \
  --output .local/apim-probes-first --include-other-routes
export RUN_ID="$(node -p 'require("node:crypto").randomUUID()')"
node src/tools/smoke-copilot.mjs --transport apim-gateway \
  --endpoint "$APIM_BASE" --run-id "$RUN_ID" --output ".local/apim-cli-$RUN_ID"
unset APIM_SUBSCRIPTION_KEY
```

Read and retain each manifest. A probe HTTP pass is still awaiting backend proof.
No-key/random-key tests must cover all three routes. Supply an independently
generated other/broad-scope credential through private `APIM_NEGATIVE_KEY` to
exercise that additional denial; do not label random-key testing wrong-scope
proof. Existing service all-access keys can be used only in this bounded
negative test, never distributed to clients. All credential retrieval uses the
same receipt-bound `az rest .../listSecrets` pattern to a new private file.
Use `--include-boundary` for an explicit exactly-4-MiB valid batch probe.
Oversized fixed-length input must be rejected as size failure; the chunked
probe exercises unsupported framing instead, not streamed-size enforcement.
Rate/quota testing must be separately bounded and avoid flooding the shared
destination; record those gates as unverified if not executed.

## Key rotation and revocation verification

Confirm the exact subscription and consumers before rotation. Load its secondary
key privately, distribute it through the approved channel, restart affected CLI
processes and prove fresh secondary-key ingestion before regenerating primary.
For a synthetic-only evaluation the probe tool can exercise the switch without
any ordinary client session.

```bash
export APIM_RETIRED_KEY="$(jq -er .primaryKey "$APIM_REVIEW_DIR/subscription-secrets.json")"
export APIM_SUBSCRIPTION_KEY="$(jq -er .secondaryKey "$APIM_REVIEW_DIR/subscription-secrets.json")"
```

Before regeneration run secondary-key positive controls **without** setting
`APIM_RETIRED_KEY` for the probe child, since the primary is not retired yet:

```bash
env -u APIM_RETIRED_KEY node src/tools/probe-apim.mjs --endpoint "$APIM_BASE" \
  --output .local/apim-probes-secondary
```

**Rotation gate:** query the secondary probe's positive before/after UUIDs using
the [backend correlation procedure](#backend-evidence-and-negative-controls).
Require nonempty exact matching rows with no partial errors and confirm consumer
cutover. An HTTP-only pass is insufficient. Only after that evidence is approved
regenerate primary:

```bash
az rest --method post \
  --url "https://management.azure.com${APIM_SUBSCRIPTION_ID}/regeneratePrimaryKey?api-version=2024-05-01" \
  > "$APIM_REVIEW_DIR/regenerate-primary-result.json"
az rest --method post \
  --url "https://management.azure.com${APIM_SUBSCRIPTION_ID}/listSecrets?api-version=2024-05-01" \
  > "$APIM_REVIEW_DIR/rotated-subscription-secrets.json"
export APIM_SUBSCRIPTION_KEY="$(jq -er .primaryKey "$APIM_REVIEW_DIR/rotated-subscription-secrets.json")"
node src/tools/probe-apim.mjs --endpoint "$APIM_BASE" \
  --output .local/apim-probes-rotated
unset APIM_SUBSCRIPTION_KEY APIM_RETIRED_KEY APIM_NEGATIVE_KEY
```

Expect old-key denial and new-key success after bounded propagation. Preserve
the first failure and use a new output directory for any authorized retry;
do not hide propagation failures by overwriting evidence. Correlate positive
and negative markers with LAW. Do not regenerate secondary while clients still
depend on it. Post-rotation retrieve/readback is not proof that the old key is
denied on the gateway.

## Backend evidence and negative controls

After deployment and policy readback, use the [private probe tool](../src/README.md)
and isolated APIM CLI smoke. The caller key must stay in environment/memory,
never a curl `-H` argument, URL, manifest, or debug trace. Do not enable `az
--debug` or APIM request/body tracing. Use generated synthetic data only.

HTTP results and persisted data are separate gates. Every positive and denied
probe has a distinct UUID and timestamp. Query the exact receipt-owned LAW with
the checked-in correlation query:

```bash
export RUN_ID="<fresh-lowercase-uuid-from-the-run-manifest>"
node --input-type=module <<'JS'
import fs from 'node:fs';
import assert from 'node:assert/strict';
assert(/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(process.env.RUN_ID));
const query = fs.readFileSync('queries/verify_relay.kql', 'utf8')
  .replaceAll('__RUN_ID__', process.env.RUN_ID);
fs.writeFileSync(`${process.env.APIM_REVIEW_DIR}/query-${process.env.RUN_ID}.json`,
  JSON.stringify({query}), {flag: 'wx', mode: 0o600});
JS
LAW_CUSTOMER_ID="$(jq -er .workspace_customer_id "$NATIVE_STATE")"
az rest --method post \
  --url "https://api.loganalytics.azure.com/v1/workspaces/${LAW_CUSTOMER_ID}/query" \
  --resource https://api.loganalytics.io \
  --headers Content-Type=application/json \
  --body "@$APIM_REVIEW_DIR/query-$RUN_ID.json" \
  > "$APIM_REVIEW_DIR/query-$RUN_ID-response.json"
```

Require valid table/column/row shapes, no `error` or partial errors, and nonempty
matching rows for positive runs. Logs require exact `run.id`,
`SYNTHETIC_RELAY_<UUID>`, and `github-copilot`; actual CLI spans use
`copilot.run.id`, and events join their trace/span IDs. Inspect returned content
attributes against the metadata-only policy. No standalone CLI Logs exporter
is claimed by a synthetic Logs fixture.

For denied cases require the expected gateway status **and** no rows for that
case's UUID over a bounded propagation window, with successful positive controls
before/after. Absence alone is not proof of no backend forwarding. Repeat missing
positive data at bounded intervals for at most ten minutes, writing each query
response to a new filename; never overwrite evidence or rerun inference
indefinitely. Query a fresh unrelated UUID as a wrong-run control.

Native AMW metrics are an independent gate. Generate a query URL from the
receipt's verified `metrics_query_endpoint`, a fresh actual CLI run UUID, and the
manifest's finish time. Use native histogram functions on dotted base names, for
example `histogram_count({__name__="gen_ai.client.token.usage",
"copilot.run.id"="<uuid>","service.name"="github-copilot"})`.
Bind/URL-encode selectors with Node `URLSearchParams`; never interpolate raw
user input. Then execute:

```bash
export APIM_CLI_MANIFEST=".local/apim-cli-$RUN_ID/manifest.json"
VERIFIED_METRICS_QUERY_URL="$(node --input-type=module <<'JS'
import fs from 'node:fs';
import assert from 'node:assert/strict';
const state = JSON.parse(fs.readFileSync(process.env.NATIVE_STATE, 'utf8'));
const manifest = JSON.parse(fs.readFileSync(process.env.APIM_CLI_MANIFEST, 'utf8'));
assert.equal(manifest.run_id, process.env.RUN_ID);
assert.equal(manifest.transport, 'apim-gateway');
assert.equal(manifest.status, 'awaiting_backend_verification');
assert.equal(manifest.capture_content, false);
assert(/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(manifest.run_id));
assert(Number.isFinite(Date.parse(manifest.finished_at)));
const url = new URL(state.metrics_query_endpoint);
assert.equal(url.protocol, 'https:');
assert.equal(url.username + url.password + url.port + url.search + url.hash, '');
assert.equal(url.pathname, '/');
assert(url.hostname.endsWith('.prometheus.monitor.azure.com'));
url.pathname = '/api/v1/query';
url.search = new URLSearchParams({
  query: `histogram_count({__name__="gen_ai.client.token.usage","copilot.run.id"=${JSON.stringify(manifest.run_id)},"service.name"="github-copilot"})`,
  time: manifest.finished_at
}).toString();
console.log(url.href);
JS
)"
az rest --method get --url "$VERIFIED_METRICS_QUERY_URL" \
  --resource https://prometheus.monitor.azure.com \
  > "$APIM_REVIEW_DIR/metrics-response.json"
```

Require Prometheus `status=success`, correct result shape, fresh nonempty series
with the exact labels and finite values, and a wrong-run empty control.
Count/sum queries must cover `gen_ai.client.token.usage`,
`gen_ai.client.operation.duration`, and `gen_ai.invoke_agent.duration` for the
metadata-only scenario, plus `gen_ai.execute_tool.duration` only when actual
approved tool activity is required and observed. One example token series is not
complete CLI acceptance. Require `microsoft.amwresourceid` to bind the expected
AMW; any optional metric application association must identify the DCR, not an
Application Insights component. Never sum cumulative
snapshots as independent consumption. Empty/partial metrics remain blocked
even when logs/spans/events pass. Do not relabel a Function/direct run manifest
to make the historical Python verifier accept APIM evidence.

## Updates, failures, rollback, and retirement

Preserve the original parameter intent, marker, ARM result, exact serialized
policies, APIM/API/subscription/role IDs, and private receipts. For interrupted
creation inspect the existing deployment, resource group and full child
inventory; never generate a new marker or adopt a service because a file is
missing. Capture deployment outputs to a **new candidate**, validate, then
install a first receipt exclusively. On repeat apply compare stable identities
against the original receipt; do not overwrite it.

Updates use the same validate/what-if/create commands against reviewed new
parameter/policy intent. Compare what-if and saved policy definitions and retain
the previous verified revision. Rollback is an explicitly reviewed application
of that retained configuration to APIM only, followed by fresh authentication
and persistence proof. Revoked keys must not be resurrected by rollback.
Do not create a new identity silently or leave an old DCR assignment unmanaged.

Nothing is automatically deleted. A separately approved retirement must first
verify full inventory/consumers, disable admission, remove only APIM's exact
receipt-owned external DCR role, and then retire the owned APIM resources using
reviewed Azure CLI commands. No generic delete command is supplied here: it
would bypass the repository's lifecycle approval gate. Preserve native and
Function resources, their publishers, all receipts and saved-search baselines.

## First-party references

- [Subscription scope, broad keys, and backend key forwarding](https://learn.microsoft.com/en-us/azure/api-management/api-management-subscriptions)
- [Managed identity policy and trusted backend responsibility](https://learn.microsoft.com/en-us/azure/api-management/authentication-managed-identity-policy)
- [APIM Bicep service schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.apimanagement/2024-05-01/service)
- [Subscription listSecrets](https://learn.microsoft.com/en-us/rest/api/apimanagement/subscription/list-secrets?view=rest-apimanagement-2024-05-01)
- [Primary key regeneration](https://learn.microsoft.com/en-us/rest/api/apimanagement/subscription/regenerate-primary-key?view=rest-apimanagement-2024-05-01)
- [Forward-request policy](https://learn.microsoft.com/en-us/azure/api-management/forward-request-policy)

Record APIM measurements separately from `evidence/function-relay.md` and
`evidence/v1.md`. No control-plane deployment or HTTP pass establishes native
metric persistence, Windows/GPO rollout, normal-user monitoring, or authenticated
workbook rendering.
