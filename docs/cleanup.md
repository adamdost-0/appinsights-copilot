# Reviewed cleanup without Python deployment tooling

[Native deployment](deployment.md) | [Relay deployment](relay-deployment.md)

Deletion is **explicit and irreversible**. These Azure CLI commands do not
inherit the historical Python teardown's automated ownership proof. An
authorized operator must approve the complete inventory, original ownership
receipts, workspace-child baseline, external consumers and exact deletion
scope before executing a deletion block. A missing receipt, failed/partial
inventory, changed resource identity, unknown consumer or drift means **stop**.
Do not treat tags, familiar prefixes, or this document as blanket authorization.

Use `set -euo pipefail`, `set +x`, `umask 077`, the verified current subscription
and private `.local` directory from [deployment](deployment.md). Serialize
lifecycle changes; compare fresh readbacks immediately before deletion.
Inventory APIs do not prove the absence of every external dependency or race.
Do not force-delete AMW-managed resources.

## Remove the relay first

Preserve `.local/relay-azure.json`, original parameters, artifact hashes, and
evidence. Review exact subscription/group, solution tag `copilot-otel-relay`,
original fresh marker, app/storage/plan IDs, system principal, roles,
deployment container, and any child resources:

```bash
az group show -n rg-copilot-otel-relay > .local/relay-cleanup-group.json
az resource list -g rg-copilot-otel-relay > .local/relay-cleanup-inventory.json
RELAY_APP_ID="$(jq -er .function_app_resource_id .local/relay-azure.json)"
RELAY_DCR_ID="$(jq -er .dcr_resource_id .local/relay-azure.json)"
RELAY_ROLE_ID="$(jq -er .publisher_role_assignment_id .local/relay-azure.json)"
az role assignment list --scope "$RELAY_DCR_ID" --all > .local/relay-cleanup-dcr-roles.json
jq -e --slurpfile state .local/relay-azure.json \
  '.name == $state[0].resource_group and .location == $state[0].location
   and .tags.solution == "copilot-otel-relay"
   and .tags["ownership-marker"] == $state[0].ownership_marker' \
  .local/relay-cleanup-group.json >/dev/null
jq -e --slurpfile state .local/relay-azure.json \
  '[.[] | select(.id == $state[0].publisher_role_assignment_id
    and .principalId == $state[0].principal_id
    and (.scope|ascii_downcase) == ($state[0].dcr_resource_id|ascii_downcase)
    and (.roleDefinitionId|endswith("/3913510d-42f4-4e42-8a64-420c390055eb")))]
    | length == 1' .local/relay-cleanup-dcr-roles.json >/dev/null
```

**Cross-group caveat:** the new publisher assignment is on the old v1 DCR,
not inside the relay group. It must be explicitly reviewed and removed when
retiring the relay, including any obsolete assignments from recreated system
identities. Do not remove the native direct publisher or other principals'
roles. The historical v1 teardown may refuse this additional assignment;
never change its guard/baseline to conceal the relay.

After separately approving the exact role and group deletion:

```bash
az role assignment delete --ids "$RELAY_ROLE_ID"
az role assignment list --scope "$RELAY_DCR_ID" --all \
  > .local/relay-cleanup-dcr-roles-after.json
jq -e --arg id "$RELAY_ROLE_ID" 'all(.[]; .id != $id)' \
  .local/relay-cleanup-dcr-roles-after.json >/dev/null
az group delete -n rg-copilot-otel-relay --yes
test "$(az group exists -n rg-copilot-otel-relay)" = false
```

Preserve the deletion outcome and removed role ID privately. Deleting this
group also deletes the relay storage/code; it must **not** delete the v1
group or AMW-managed group. If a retry finds the role/group absent, verify
that exact saved identity rather than accepting a generic CLI failure as absence.
Permission/network failures are not `not found`.

## Capture native inventory

This section is **read-only Azure access**, reusable at initial deployment and
before cleanup. Each capture uses a new private directory; retain the reviewed
initial snapshot. Never overwrite a historical baseline or a prior review.

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

Verify fresh LAW ARM ID, location, marker, and customer UUID against the
original receipt, so a replacement workspace cannot inherit deletion approval.
Require exact group scope/marker and only the owned LAW, AMW, DCE, DCR,
approved workbook and known child resources/assignments. Review all workspace
tables: require provider classification `Microsoft` unless explicit separate
ownership/removal authority exists. Require empty dataExports/linkedServices/
linkedStorageAccounts unless separately approved; stop for foreign/custom
children. Inspect locks, policy, diagnostics, workbooks and external consumers
through their owning resource/provider surfaces; generic `az resource list`
does not enumerate every child.

When the historical `.local/native-workspace-children.json` exists, require its
exact LAW/customer/ownership binding and **saved-search ID set/full canonical
JSON SHA-256 hashes**, not name-prefix matching. This read-only Node comparison
does not modify that baseline:

```bash
node <<'JS'
const fs = require('node:fs'), crypto = require('node:crypto');
const assert = require('node:assert/strict');
const read = p => JSON.parse(fs.readFileSync(p, 'utf8'));
const state = read(process.env.NATIVE_STATE);
const baseline = read('.local/native-workspace-children.json');
const law = read(`${process.env.INVENTORY_DIR}/law.json`);
assert.equal(baseline.schema_version, 1);
for (const key of ['subscription_id','resource_group','workspace_resource_id',
                   'workspace_customer_id','ownership_marker'])
  assert.equal(baseline[key], state[key]);
assert.equal(law.id.toLowerCase(), state.workspace_resource_id.toLowerCase());
assert.equal(law.properties.customerId, state.workspace_customer_id);
assert.equal(law.location, state.location);
assert.equal(law.tags['ownership-marker'], state.ownership_marker);
assert.equal(law.tags.solution, 'copilot-otel-v1');
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object')
    return `{${Object.keys(value).sort().map(k =>
      `${canonical(k)}:${canonical(value[k])}`).join(',')}}`;
  if (typeof value === 'number')
    assert(Number.isSafeInteger(value), 'Noninteger requires canonical-format review');
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
    c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
}
const entries = read(`${process.env.INVENTORY_DIR}/savedSearches.json`).value;
assert.equal(new Set(entries.map(x => x.id)).size, entries.length);
const hashes = Object.fromEntries(entries.map(entry => [
  entry.id, crypto.createHash('sha256').update(canonical(entry)).digest('hex')
]));
assert.deepEqual(hashes, baseline.saved_searches);
JS
```

If the historical baseline is absent on a historical deployment, **stop**; do
not manufacture it or bootstrap adoption during cleanup. A new CLI-managed
deployment instead requires the retained full initial inventory, original
receipt, exact resource binding and explicit reviewer approval. Compare fresh
full saved searches with that approved initial snapshot, and refuse new,
modified or missing entries. Operator review is mandatory, not an automatic
default-child allowlist.

## Inspect the AMW-managed lifecycle

The AMW's `properties.defaultIngestionSettings` supplies its service-managed
DCE/DCR IDs; these are **not** the explicitly deployed native endpoints. Require
both IDs to belong to one distinct managed resource group in this subscription.
Set the exact reviewed group name from those IDs, not from a guessed prefix:

```bash
jq -e '.properties.defaultIngestionSettings' "$INVENTORY_DIR/amw.json" \
  > "$INVENTORY_DIR/amw-managed-linkage.json"
export MANAGED_GROUP="<exact-reviewed-group-from-AMW-linkage>"
az group show -n "$MANAGED_GROUP" > "$INVENTORY_DIR/managed-group.json"
az resource list -g "$MANAGED_GROUP" > "$INVENTORY_DIR/managed-resources.json"
jq -e --arg amw "$AMW_ID" \
  '(.managedBy|ascii_downcase) == ($amw|ascii_downcase)' \
  "$INVENTORY_DIR/managed-group.json" >/dev/null
```

Require the managed group inventory to consist of the exact linked DCE/DCR;
unknown inventory/linkage or wrong `managedBy` stops cleanup. Preserve its
exact ID and approved native inventory in a private cleanup receipt **before**
deleting anything. Resource IDs alone are not proof of no external consumers.

## Delete native resources only after approval

First remove/review the relay assignment as above and stop any optional direct
clients. If desired, delete **only** the verified workbook before native
cleanup using its exact ID and ownership from the workbook receipt; otherwise
include it explicitly in the approved native inventory. Read back the saved
definition before deletion. Never delete by display name alone.

```bash
# Optional workbook-only retirement after exact ID/ownership review:
export WORKBOOK_ID="<exact-owned-workbook-ARM-id>"
az resource delete --ids "$WORKBOOK_ID" --api-version 2023-06-01
```

**Final review gate:** approve the exact original native group, owned children,
unchanged baseline, expected roles, AMW lifecycle and deletion consequences.
Immediately recapture/compare inventory to detect drift. Only then:

```bash
az group delete -n rg-copilot-otel-v1 --yes
test "$(az group exists -n rg-copilot-otel-v1)" = false
# Azure must remove the AMW-managed group via its owner's lifecycle.
test "$(az group exists -n "$MANAGED_GROUP")" = false
```

If the managed group remains, cleanup is **incomplete**. Preserve state, allow
supported lifecycle propagation, and recheck; investigate service issues rather
than force-delete the managed group. A retry with the native group absent must
still use the original saved managed-group ID. Never call an untracked absent
native group a complete cleanup.

Retain receipts/evidence privately per policy; do not recursively remove
`.local`, the repository, or user directories. Do not label these manual CLI
results as a successful historical `scripts.teardown` receipt. The old
Python deployment/teardown scripts are legacy tools, **not prerequisites** for
this workflow. For recreation, follow [deployment](deployment.md#continue-and-rebuild)
with new identities and fresh verification.
