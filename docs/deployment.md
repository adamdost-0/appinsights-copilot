# Deploy and rebuild with Azure CLI

[Administrator guide](../README.md) | [Infrastructure contract](../infra/README.md)

This workflow provisions a dedicated v1 group, an explicit DCE/DCR, Log
Analytics, and an Azure Monitor workspace. There is no Application Insights
component or portal OTLP opt-in. Microsoft documents Application Insights as
optional in [manual resource orchestration](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion#option-2-manual-resource-orchestration).

**Azure native OTLP is preview, has no SLA, and is not recommended for
production.** Obtain organizational approval for the region, public endpoints,
test data, identity roles, and costs before deployment. The examples use public
`AzureCloud` and `eastus`; do not assume another cloud/region is validated.

## Prerequisites and sign-in

Use Linux and Python **3.12+** with the standard library, Azure CLI with Bicep,
Copilot CLI, and GitHub CLI. Linux is the tested platform; PowerShell and Windows
are not validated. No Python third-party package, Docker daemon, hosted
collector, or monitoring agent is required.

Use an enabled authorized subscription and identities with permission to create
the dedicated resource group, deploy its monitoring resources, and assign the
scoped roles below. The scripts do not sign in or bypass Azure policy.

```bash
az login
export AZURE_SUBSCRIPTION_ID="<your-authorized-subscription-uuid>"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
gh auth login --hostname github.com
python3 -m scripts.preflight
```

Azure and GitHub authentication are separate. The GitHub account must be
authorized for Copilot inference. The isolated runner reads its token internally;
do not print tokens or copy a normal user's Copilot history/configuration.

The required resource providers are `Microsoft.Insights`,
`Microsoft.OperationalInsights`, and `Microsoft.Monitor`. Inspect them:

```bash
for PROVIDER in Microsoft.Insights Microsoft.OperationalInsights Microsoft.Monitor; do
  az provider show --namespace "$PROVIDER" \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --query '{namespace:namespace,state:registrationState}' --output table
done
```

If one is not registered, an authorized subscription administrator can explicitly
register that provider, for example:

```bash
az provider register --namespace Microsoft.Monitor \
  --subscription "$AZURE_SUBSCRIPTION_ID" --wait
```

Provider registration is not an OTLP portal opt-in. Do not guess feature
registrations or mutate unrelated monitoring resources to work around an error.

## Publishing and querying roles

| Role | Scope | Purpose |
| --- | --- | --- |
| Monitoring Metrics Publisher | Explicit DCR | Send authorized traces and metrics |
| Monitoring Data Reader | Azure Monitor workspace | Query native histogram metrics |
| Log Analytics Reader | Log Analytics workspace | Query native spans, events, resources |

By default, the signed-in user's object UUID is used for publishing and
querying. The deployer may need directory read permission to resolve it.
For a known identity, pass `--principal-id <object-uuid>` and
`--principal-type User`, `ServicePrincipal`, or `Group`. Use the **object ID**,
not an application/client ID. Non-user principals require an explicit ID.

The optional `--operator-principal-id` and `--operator-principal-type` select a
different query operator. Without them, the query identity defaults to the
publisher. With an explicit operator ID and no operator type, the type defaults
to `User`. For example, separate a service-principal publisher from an auditor
group:

```bash
export PUBLISHER_OBJECT_ID="<sender-service-principal-object-uuid>"
export AUDITOR_GROUP_OBJECT_ID="<auditor-group-object-uuid>"
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --what-if \
  --principal-id "$PUBLISHER_OBJECT_ID" --principal-type ServicePrincipal \
  --operator-principal-id "$AUDITOR_GROUP_OBJECT_ID" --operator-principal-type Group
```

After review, use the same identity flags with `--apply` in place of
`--what-if`. The identity used by the smoke command's Azure CLI session must
actually have publishing access; the identity used to verify/query must have
reader access. Assigning a role to a principal does not switch your Azure CLI
login to that principal. Deployer authority is separate from data-plane roles.
Changing principal flags is not automatic revocation of old role assignments;
review and revoke obsolete access through your approved RBAC process.

## Preview and apply

Run from the repository root:

```bash
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --what-if
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --apply
```

Review the proposed scope and cost before the second command. The wrapper uses
[`infra/main.bicep`](../infra/main.bicep) and
[`infra/resources.bicep`](../infra/resources.bicep); the checked-in
[`main.bicepparam`](../infra/main.bicepparam) illustrates parameters, not live
ownership. What-if validates against Azure and writes private local state but
does not provision the resources.

The group is **`rg-copilot-otel-v1`**, with solution tag `copilot-otel-v1`.
Resource names have a deterministic, unique per-group suffix. Repeat apply
reuses the receipt and checks stable resource IDs, workspace identity, immutable
DCR ID, and endpoint URLs. A provider may show `Modify` during repeat what-if;
read the real changes rather than assuming every repeat must show `NoChange`.
Fresh repeat apply preserved all ten checked identity/endpoint/ownership
fields; see the [v1 evidence record](evidence/v1.md).

Ownership intent is stored in `.local/native-deployment.json`; successful
deployment outputs are stored in `.local/native-azure.json`. Before persisting
runtime state, the deployer independently reads the exact LAW ARM resource and
checks its ID, location, ownership, and customer UUID. On the first fresh
successful apply, it also records the validated workspace-child baseline in
`.local/native-workspace-children.json`.

Preserve these private files. Ordinary repeat apply preserves the baseline
byte-for-byte. Missing baseline state on an existing deployment blocks automatic
what-if/apply/teardown adoption; do not delete or manufacture a baseline to
bypass ownership checks. An existing group with the wrong marker, location,
resource identity, or unexpected inventory is refused, not adopted. The group
must remain exclusive to this solution.

## Recover an interrupted first apply

Use this advanced recovery **only when both `.local/native-azure.json` and
`.local/native-workspace-children.json` are absent**, ARM completed successfully,
and a later LAW/child readback failed, for example with HTTP 503. Preserve
`.local/native-deployment-result.json` and the original ownership seed.
Do not delete working receipts to enter recovery; an already verified
deployment needs no recovery or modification.

The saved ARM result supplies a **candidate**, not an operational receipt.
`load_recovery_state` rejects unsuccessful/error results, wrong scope/ownership,
and existing operational state. Never copy candidate outputs directly into
`native-azure.json`.

After propagation, capture a complete read-only inventory from the verified
owned LAW. This writes only a private review artifact, not a baseline or an
operational receipt:

```bash
python3 - <<'PY'
import os
from scripts import native_deploy
from scripts.common import AppError, LOCAL, write_json

baseline = LOCAL / "native-workspace-children.json"
if baseline.exists() or baseline.is_symlink():
    raise AppError("Baseline already exists; this recovery path must not replace it")
candidate = native_deploy.load_recovery_state(os.environ["AZURE_SUBSCRIPTION_ID"])
native_deploy.check_account(candidate["subscription_id"])
if not native_deploy.check_group(candidate, require_complete=True):
    raise AppError("The complete owned resource group is required")
native_deploy.verify_workspace_binding(candidate)
inventory = native_deploy.read_workspace_children(candidate)
native_deploy.workspace_child_fingerprints(candidate, inventory)
write_json(LOCAL / "recovery-workspace-child-inventory.json", inventory)
print("Private inventory captured; stop and review it before initialization")
PY
```

**Stop between these commands.** An authorized operator must privately inspect
the complete captured inventory and approve the exact Azure-default children
and ownership scope. A familiar saved-search prefix is not approval. Do not
truncate the inventory, remove rejected entries, or publish its contents.

Only after that review, initialize from the reviewed snapshot:

```bash
python3 - <<'PY'
import os
from scripts import native_deploy
from scripts.common import LOCAL

candidate = native_deploy.load_recovery_state(os.environ["AZURE_SUBSCRIPTION_ID"])
reviewed_inventory = native_deploy.private_state(
    LOCAL / "recovery-workspace-child-inventory.json"
)
result = native_deploy.initialize_workspace_child_baseline(candidate, reviewed_inventory)
print(result["status"])
PY
```

The initializer rechecks the exact ownership seed/candidate, enabled account,
complete owned group, fresh LAW ARM/customer-ID binding, and a fresh complete
inventory against the reviewed hashes. Only after those checks does it write
the baseline and verified operational `native-azure.json`. It never overwrites
an existing baseline and performs no cloud mutations.

After successful initialization, retry normal `native_deploy --what-if` and
`--apply` with the same explicit subscription and approved identity flags.
No rebuild or cloud deletion is required for this recovery. On any failure,
preserve the saved result, seed, inventory, and any written state for private
investigation; do not reset files or relabel a failed result to bypass checks.

## Verify the new resources

Inspect the dedicated scope without printing credentials:

```bash
az group show --name rg-copilot-otel-v1 \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --query '{name:name,location:location,tags:tags}' --output json
az resource list --resource-group rg-copilot-otel-v1 \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --query '[].{name:name,type:type,location:location}' --output table
```

Require LAW, AMW, explicit DCE/DCR, and only expected owned resources, plus
scoped role assignments. There must be no Application Insights component.
Check actual DCR destinations, DCE link, endpoint URLs, scoped permissions,
workspace retention/cap, and table retention privately through ARM/resource
readback. Azure can create additional service-managed AMW ingestion resources
in a managed group; these do not replace the explicit DCE/DCR.

The requested LAW configuration is 30-day retention and a 1 GB/day cap.
**Neither is a total cost ceiling or a guarantee of table-level deletion.**
Read actual `OTelSpans`, `OTelEvents`, and `OTelResources` analytics/total
retention. AMW metrics have separate billing, retention, and cardinality costs.
Record measured findings in [v1 evidence](evidence/v1.md).

Then run [the synthetic scenarios and bounded verification](verification.md),
followed by [the LAW workbook](visualizations.md). A deployed resource, token,
or zero CLI exit is not proof of telemetry persistence.

Fresh DCE/DCR provisioning can return HTTP 503 while the ingestion data plane
becomes ready. Allow propagation, then run a new bounded synthetic CLI session
with a fresh UUID; require traces, events, and metrics in the backend before
claiming success. An empty authenticated probe returning HTTP 400 is not health
proof.

## Cleanup and rebuild

Cleanup is **explicit and irreversible**. Finish all inference and evidence
review first; keep the receipts and required raw evidence private. Serialize
lifecycle operations and assess any external consumers before deletion.

The new v1 deployment remains running. Its
[live read-only teardown prechecks](evidence/v1.md#acceptance-boundaries)
passed, including the exact baseline for 39 Azure-default saved searches and
the AMW-managed ingestion resources. The destructive v1 command was not executed
against this retained stack. Retain resources if an ownership check refuses
deletion; successful ingestion does not authorize bypassing it.

The v1 teardown command requires the exact group and confirmation:

```bash
python3 -m scripts.teardown \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group rg-copilot-otel-v1 --confirm
```

`--confirm` authorizes deletion, **not just an audit**. The command checks the
receipt, account, complete expected group inventory, relevant LAW children, and
AMW-managed ingestion linkage/inventory before deleting the v1 group through
Azure CLI. It lets Azure remove the AMW-managed group through the owning AMW's
lifecycle; it does not directly delete that managed group.

Before accepting the child baseline, teardown freshly reads the exact LAW ARM
resource and verifies its ID, location, ownership, and customer UUID. A same-name
replacement workspace must not inherit the prior workspace's deletion
authorization.

Foreign/custom workspace children, unowned inventory, malformed/incomplete
responses, and ownership mismatches cause refusal. These checks do not prove
the absence of every possible external consumer or eliminate concurrent-change
races. Do not bypass a refusal with broad resource deletion.

Azure-default saved searches must match the **exact saved-search ID set and
full canonical-JSON hashes** in the private workspace-child baseline. New,
modified, or missing searches fail the check; a familiar name/prefix or
default-looking shape alone never grants deletion ownership. Custom tables,
unexpected nonempty child collections, and incomplete/error inventories also
fail. Microsoft tables are permitted only when classified as such by the
provider. Missing or changed baseline state requires authorized private
inventory review and recovery, not an automatic reset.

The private `.local/native-teardown.json` records exact ownership and the AMW
managed-group identity before deletion. Its status becomes `deleted` only after
both groups are confirmed absent. A retry that finds the v1 group already
absent checks the managed group recorded in the matching teardown receipt and
refuses completion if that group remains. An `already_absent` result without
that receipt does not prove managed cleanup.

Local receipts and evidence are preserved, not deleted. If managed cleanup is
incomplete, investigate and allow Azure's supported lifecycle to finish, then
retry the same teardown command. Never force-delete the managed group.

For a **clean rebuild after verified deletion**, keep all four lifecycle
receipts in place: `.local/native-deployment.json`, `.local/native-azure.json`,
`.local/native-teardown.json`, and `.local/native-workspace-children.json`.
The deployer requires a matching completed teardown receipt and rechecks that
the managed group is absent before allowing new workspace customer and immutable
DCR IDs. Verified recreation replaces the child-baseline binding for the new
workspace customer UUID; ordinary repeat apply keeps identities, endpoints,
and the baseline unchanged.

```bash
python3 -m scripts.preflight
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --what-if
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --apply
```

Retain the same approved publisher/operator flags if you used explicit
identities. Preserve a private copy of the completed deployment's receipts with
its evidence before apply replaces the runtime outputs, but do not move or
delete the active receipts to bypass checks. The rebuild retains the ownership
marker and records the new runtime identities. Never verify an old run against
the new workspaces. Repeat smoke, verifier, and workbook commands with fresh
run UUIDs. If Azure's resource deletion/reuse lifecycle blocks recreation,
resolve that state rather than assuming immediate name reuse or forcing data
purges.

No command in this guide changes repository visibility, commits, pushes, or
posts anything publicly.
