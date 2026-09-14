# Deploy or update the restricted OTLP Function relay

[Native deployment](deployment.md) | [Administrators](../Administrators.md) |
[Authenticated APIM alternative](apim-deployment.md) |
[Agent context and evidence requirements](../AGENTS.md)

The default route is **client without an Azure bearer token or Function key ->
restricted HTTPS Function -> managed-identity-authenticated DCE -> existing
DCR -> LAW/AMW**. HTTP triggers deliberately use `authLevel: anonymous`.
The authorization boundary at ingress is the platform IPv4 allowlist, not the
handler. Any caller sharing an allowed NAT address can submit telemetry and
consume quota. This is not per-user authorization or a privacy filter.
For mandatory client credentials use the separately deployed
[APIM gateway](apim-deployment.md), not a change to this Function's trigger.
The APIM path does not require a Function deployment or a Function key.

## Contract and current Microsoft support

[`function.bicep`](../infra/function.bicep) deploys Linux **Flex Consumption
FC1 / Node 22**, with the Functions v4 programming model in `src`, into
`rg-copilot-otel-relay`. All owned top-level resources have solution tag
`copilot-otel-relay` and a **fresh** ownership marker, unrelated to the native
marker. Deterministic names are returned in `relayState`; do not guess them.

| Parameter/output | Meaning |
| --- | --- |
| `location`, `ownershipMarker` | Approved Flex region; relay's own UUID |
| `allowedIPv4Cidrs` | Required nonempty JSON array; canonical IPv4 networks, prefix 1..32; include client and deployment-agent egress |
| `dcrResourceGroupName`, `dcrName` | Existing DCR in the current subscription |
| `tracesEndpoint`, `logsEndpoint`, `metricsEndpoint` | Verified full native HTTPS URLs; no credentials |
| `relayState` | App name/ARM ID, principal, storage ARM ID, relay signal URLs, exact DCR role-assignment ID |

The app's nonsecret upstream environment is exactly `OTLP_TRACES_ENDPOINT`,
`OTLP_LOGS_ENDPOINT`, `OTLP_METRICS_ENDPOINT`. `ManagedIdentityCredential()`
uses the **system-assigned** identity; no `AZURE_CLIENT_ID`, connection string,
static token, storage key, or Application Insights connection is needed.
The identity receives **Storage Blob Data Owner** on this relay's storage
account for the HTTP-only host and deployment container, and **Monitoring
Metrics Publisher** on the exact existing DCR. No LAW/AMW query role is granted
to the relay. Adding queue/blob/Durable triggers requires a new permission
review; these grants describe only the HTTP relay.

`FUNCTIONS_REQUEST_BODY_SIZE_LIMIT=4194304` is a **required deployment app
setting**, already included in the template; it is not a supported `host.json`
property. The runtime independently enforces 4 MiB limits on raw input,
expanded gzip input and upstream responses.
Its immutable 30-second deadline covers request reading, managed-identity
acquisition and upstream forwarding. It makes one upstream attempt, with no
durable queue or relay retry. An ambiguous timeout can follow Azure acceptance,
so a client's retry can duplicate telemetry. The deployment deliberately omits
optional `AZURE_CLIENT_ID` because it uses the system-assigned identity.

Storage uses `AzureWebJobsStorage__accountName` and
`AzureWebJobsStorage__credential=managedidentity`; deployment uses
`functionAppConfig.deployment.storage.authentication.type=SystemAssignedIdentity`.
Blob anonymous/shared-key access is disabled; storage HTTPS/TLS 1.2 is required.
Storage has a public network endpoint but requires identity authorization;
this is **not private-endpoint storage**. Storage transactions/capacity and
Functions execution are billable, even without Application Insights. Maximum
40 instances is Flex's lower supported scale limit, not a spending ceiling.

The template puts allow rules and **Deny unmatched** on both app and SCM
configuration **in the initial site resource**, HTTPS-only and TLS 1.2 minimum.
SCM uses the same rules; FTPS and basic publishing credentials are disabled.
There is no create-public-then-lock-down step. Do not remove restrictions to
work around deployment failures. Allowlisted clients still use HTTPS.

Microsoft distinguishes legacy **ZipDeploy** from Flex **OneDeploy**:
Flex accepts a ready-to-run ZIP through the supported Azure CLI
`az functionapp deployment source config-zip` command, which selects OneDeploy.
Do not use the legacy `/api/zipdeploy` REST endpoint, `WEBSITE_RUN_FROM_PACKAGE`,
`linuxFxVersion`, `FUNCTIONS_WORKER_RUNTIME`, or `FUNCTIONS_EXTENSION_VERSION`
as Flex configuration. Runtime belongs in `functionAppConfig.runtime`.
Use a current Azure CLI with Entra-authenticated deployment support. The parent
operator verified Azure CLI **2.89.0**, Node **22.23.2**, and Flex availability
in `eastus`/`eastus2`; this runbook uses `eastus`. Core Tools **4.12.1** was
also present but is not used for deployment. These prerequisite checks are
not live Function deployment or ingestion proof.

First-party contracts:
[Flex management and CLI ZIP deployment](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-how-to?pivots=programming-language-javascript),
[IaC and identity storage](https://learn.microsoft.com/en-us/azure/azure-functions/functions-infrastructure-as-code),
[deployment technologies](https://learn.microsoft.com/en-us/azure/azure-functions/functions-deployment-technologies),
[access restrictions](https://learn.microsoft.com/en-us/azure/app-service/app-service-ip-restrictions),
[identity host storage](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference#connecting-to-host-storage-with-an-identity),
[site schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.web/2024-04-01/sites).

## Prepare parameters without changing native state

Use the prerequisites and private shell from [native deployment](deployment.md).
Check `Microsoft.Web` and `Microsoft.Storage` provider registration; register
only if needed and approved. Confirm region/runtime support before applying:

```bash
az functionapp list-flexconsumption-locations -o table
az functionapp list-flexconsumption-runtimes --location eastus --runtime node -o table
az functionapp deployment source config-zip --help
```

Use `.local/native-azure.json` if present, or explicitly select the parent's
existing private ARM output snapshot with
`export NATIVE_STATE=.local/relay-native-state.json`. The commands preserve
that selection and never rewrite native receipts. If neither source has been
selected/retained, capture the succeeded deployment output to a **separate
private candidate**:

```bash
if test -n "${NATIVE_STATE:-}"; then
  test -f "$NATIVE_STATE"
fi
export NATIVE_STATE="${NATIVE_STATE:-.local/native-azure.json}"
if ! test -f "$NATIVE_STATE"; then
  test "$(az deployment sub show -n copilot-otel-v1 \
    --query properties.provisioningState -o tsv)" = Succeeded
  az deployment sub show -n copilot-otel-v1 \
    --query properties.outputs.nativeState.value -o json \
    > .local/relay-native-candidate.json
  export NATIVE_STATE=.local/relay-native-candidate.json
fi
```

Independently verify this candidate against live DCR/DCE/LAW as in the native
runbook. Preserve every original receipt and baseline. Do not substitute an
unreviewed public URL for the upstream; the relay would send its Azure token
to that destination.

For the **first** relay deployment, ensure the relay group is absent, then
generate a fresh marker. For updates, retain the marker from
`.local/relay-azure.json` or original `.local/relay-parameters.json`, verify
the exact group's ownership/inventory, and reuse the parameters file.

```bash
test "$(az group exists -n rg-copilot-otel-relay)" = false
export RELAY_LOCATION=eastus
export RELAY_OWNERSHIP_MARKER="$(node -p 'require("node:crypto").randomUUID()')"
# Replace with approved actual public egress network(s), not private LAN addresses.
# The documentation address below is a placeholder and will not admit your host.
export ALLOWED_IPV4_CIDRS='["203.0.113.10/32"]'
```

For the current parent-owned deployment, use the already obtained approved
public IPv4 privately instead of the documentation placeholder. With that
value held in `APPROVED_EGRESS_IPV4`, construct a single-host allowlist without
printing it:

```bash
test -n "$APPROVED_EGRESS_IPV4"
export ALLOWED_IPV4_CIDRS="$(jq -cn --arg ip "$APPROVED_EGRESS_IPV4" '[$ip + "/32"]')"
```

The parent obtained the egress address using `curl https://api.ipify.org`.
Do not publish the actual address or insert it into Markdown; verify that the
same approved egress is used for both SCM deployment and client tests.

Generate and validate the complete parameter file. This is a required gate:
Bicep enforces nonempty list/lengths and Azure validates restriction syntax;
this preflight additionally rejects IPv6, /0, host-bit networks, non-HTTPS,
credentials, fragments, queries, wrong native route/immutable ID, and
cross-subscription DCRs. Do not bypass it by invoking a resource module directly.

```bash
node --input-type=module <<'JS'
import fs from 'node:fs';
import net from 'node:net';
import assert from 'node:assert/strict';
import { validateEndpoints } from './src/relay.js';
const state = JSON.parse(fs.readFileSync(process.env.NATIVE_STATE, 'utf8'));
assert.equal(state.subscription_id, process.env.AZURE_SUBSCRIPTION_ID);
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
assert(uuid.test(process.env.RELAY_OWNERSHIP_MARKER));
assert.notEqual(process.env.RELAY_OWNERSHIP_MARKER, state.ownership_marker);
const dcr = state.dcr_resource_id.split('/');
assert.equal(dcr.length, 9);
assert.equal(dcr[1].toLowerCase(), 'subscriptions');
assert.equal(dcr[2].toLowerCase(), state.subscription_id.toLowerCase());
assert.equal(dcr[3].toLowerCase(), 'resourcegroups');
assert.equal(dcr[5].toLowerCase(), 'providers');
assert.equal(dcr[6].toLowerCase(), 'microsoft.insights');
assert.equal(dcr[7].toLowerCase(), 'datacollectionrules');
const cidrs = JSON.parse(process.env.ALLOWED_IPV4_CIDRS);
assert(Array.isArray(cidrs) && cidrs.length > 0 && cidrs.length <= 100);
assert.equal(new Set(cidrs).size, cidrs.length);
for (const cidr of cidrs) {
  assert.equal(typeof cidr, 'string');
  const parts = cidr.split('/');
  assert.equal(parts.length, 2);
  const [ip, prefix] = parts;
  assert.equal(net.isIP(ip), 4);
  assert(/^(?:[1-9]|[12][0-9]|3[0-2])$/.test(prefix));
  const value = ip.split('.').reduce((n, byte) => n * 256 + Number(byte), 0);
  assert.equal(value % 2 ** (32 - Number(prefix)), 0, 'CIDR has host bits');
}
const parameters = {
  location: process.env.RELAY_LOCATION,
  ownershipMarker: process.env.RELAY_OWNERSHIP_MARKER,
  allowedIPv4Cidrs: cidrs, dcrResourceGroupName: dcr[4], dcrName: dcr[8]
};
const streams = {traces: 'Microsoft-OTLP-Traces', logs: 'Microsoft-OTLP-Logs',
                 metrics: 'Custom-Metrics-Otel'};
for (const signal of Object.keys(streams)) {
  const raw = state[`${signal}_endpoint`];
  assert.equal(typeof raw, 'string');
  assert(!/[?#@\\\s]/.test(raw), 'Endpoint contains a forbidden delimiter or whitespace');
  const url = new URL(raw);
  assert.equal(url.protocol, 'https:');
  assert.equal(url.username + url.password + url.hash + url.search + url.port, '');
  assert(url.hostname.endsWith('.ingest.monitor.azure.com'));
  assert.equal(url.pathname,
    `/datacollectionRules/${state.dcr_immutable_id}/streams/${streams[signal]}/otlp/v1/${signal}`);
  parameters[`${signal}Endpoint`] = raw;
}
validateEndpoints(Object.fromEntries(Object.keys(streams).map(signal =>
  [`OTLP_${signal.toUpperCase()}_ENDPOINT`, parameters[`${signal}Endpoint`]])));
fs.writeFileSync('.local/relay-parameters.json',
  JSON.stringify(Object.fromEntries(Object.entries(parameters).map(([k, value]) =>
    [k, {value}])), null, 2), {flag: 'wx', mode: 0o600});
JS
```

For updates, make an explicitly reviewed private replacement parameter file
with the **same marker/native URLs** and newly approved allowlist if needed.
Run this same validator with its output path changed to the replacement path;
never delete the old file to defeat exclusive creation. Preserve overlap with
the deployer's current egress while rotating approved addresses.
The final `validateEndpoints` call reuses the actual runtime's stricter native
hostname, immutable-ID and signal-route validation; it performs no Azure call
and requires no installed Function dependencies.

## Validate, what-if, apply, read back

```bash
az bicep build --file infra/function.bicep --stdout >/dev/null
az deployment sub validate -n copilot-otel-relay --location "$RELAY_LOCATION" \
  --template-file infra/function.bicep --parameters @.local/relay-parameters.json \
  > .local/relay-validation.json
az deployment sub what-if -n copilot-otel-relay --location "$RELAY_LOCATION" \
  --template-file infra/function.bicep --parameters @.local/relay-parameters.json \
  > .local/relay-what-if.txt
```

**Review gate:** approve only the dedicated relay group/resources plus the
single existing-DCR assignment. No native resource replacement, shared
resource adoption, unbounded allow rule, key/secret, or open anonymous
interval is acceptable. RBAC permissions must cover both groups.

```bash
az deployment sub create -n copilot-otel-relay --location "$RELAY_LOCATION" \
  --template-file infra/function.bicep --parameters @.local/relay-parameters.json \
  > .local/relay-result.json
jq -e '.properties.provisioningState == "Succeeded"' .local/relay-result.json >/dev/null
jq -e '.properties.outputs.relayState.value' .local/relay-result.json \
  > .local/relay-azure-candidate.json
export RELAY_APP="$(jq -er .function_app_name .local/relay-azure-candidate.json)"
az functionapp show -g rg-copilot-otel-relay -n "$RELAY_APP" \
  > .local/relay-app-readback.json
az functionapp config access-restriction show -g rg-copilot-otel-relay -n "$RELAY_APP" \
  > .local/relay-ingress-readback.json
az functionapp config show -g rg-copilot-otel-relay -n "$RELAY_APP" \
  > .local/relay-config-readback.json
az role assignment list --scope "$(jq -er .dcr_resource_id .local/relay-azure-candidate.json)" \
  --include-inherited --fill-principal-name false > .local/relay-dcr-roles.json
```

Readback must match the exact approved CIDRs/default-deny on app and SCM,
HTTPS/TLS, Node22 runtime, original group/marker, system identity, storage
identity permissions and exact DCR publisher scope. Preserve the role ID:
**deleting the relay group does not delete an assignment scoped to the DCR**.
With the observed Azure CLI 2.89.0 response, `az functionapp show` stores site
settings under `properties` (for example `properties.state`,
`properties.httpsOnly`, `properties.functionAppConfig`), while
`az functionapp config show` exposes configuration fields directly. Inspect
the actual saved JSON shape rather than interpreting a wrong-path null as a
disabled setting or successful readback.
Only after this readback, install a first receipt:

```bash
test ! -e .local/relay-azure.json
( set -o noclobber; cat .local/relay-azure-candidate.json > .local/relay-azure.json )
```

On repeat deployment, compare the existing receipt and preserve stable
identities/endpoints rather than overwrite it. A recreated system identity
requires new RBAC and explicit removal of its old assignment. Propagation may
delay storage/MI startup; retry boundedly without loosening access.

## Package and deploy code with Azure CLI

Run from repository root on Linux with Node **22**. Check `zip`, `unzip` and
`jq` before the default packaging path:

```bash
command -v zip
command -v unzip
command -v jq
```

The parent discovered `zip` was not initially on PATH. An authorized operator
may install the missing archive tools through the approved system package
manager; do not assume sudo permission. If installing `zip` is not permitted,
use the **local packaging-only** alternative below. `unzip` or another approved
ZIP inspection tool is still required to inspect the actual archive before
upload; a staging-file list alone is not archive verification.

The ZIP root must contain the **contents of `src`**:
`host.json`, `package.json`, production `node_modules`, `relay.js`,
`transport.js` and `functions/`, not an enclosing `src` or repository
directory. Do not ZIP the repository or local
working tree wholesale. No tests, tools, `.git`, `.local`, `.env`,
`local.settings.json`, credentials, or private telemetry belong in deployment.

```bash
test "$(node -p 'process.versions.node.split(".")[0]')" = 22
test -f src/host.json && test -f src/package.json && test -f src/package-lock.json
PACKAGE_DIR="$(mktemp -d "$PWD/.local/relay-package.XXXXXXXX")"
cp src/host.json src/package.json src/package-lock.json src/relay.js src/transport.js "$PACKAGE_DIR/"
mkdir "$PACKAGE_DIR/functions"
cp src/functions/otlp.js "$PACKAGE_DIR/functions/"
(
  cd "$PACKAGE_DIR"
  npm ci --omit=dev --ignore-scripts --no-audit --no-fund
  # ZIP entries must be readable/traversable by the Functions runtime.
  chmod 644 host.json package.json package-lock.json relay.js transport.js
  find functions node_modules -type d -exec chmod 755 {} +
  find functions node_modules -type f -exec chmod 644 {} +
  zip -q -r released-package.zip host.json package.json package-lock.json \
    relay.js transport.js functions node_modules
)
export RELAY_ZIP="$PACKAGE_DIR/released-package.zip"
chmod 600 "$RELAY_ZIP"
unzip -Z1 "$RELAY_ZIP" > .local/relay-package-files.txt
if grep -Eq '(^|/)(\.local|\.git|\.env|local\.settings\.json)(/|$)' .local/relay-package-files.txt; then
  echo "Forbidden package contents" >&2
  exit 1
fi
sha256sum "$RELAY_ZIP" > .local/relay-package.sha256
```

**Alternative when `zip` is unavailable:** after creating the same allowlisted
staging directory, running its production-only `npm ci`, and normalizing only
the staged runtime entry permissions as above, replace only the `zip` command
with:

```bash
(
  cd "$PACKAGE_DIR"
  func pack --no-build --skip-install --output released-package.zip
)
```

Core Tools 4.12.1 exposes these flags: `--no-build` avoids rebuilding and
`--skip-install` avoids replacing the already prepared production dependencies.
Use a new staging directory/output, with no tests, tools or private files.
Inspect the resulting ZIP and hash it with the same steps above. This is
**local archive creation only**, not `func publish`; the actual upload remains
`az functionapp deployment source config-zip`. Do not invoke Python deployment
helpers or broaden ingress to work around a missing packaging tool.

**Keep archive-entry permissions separate from artifact privacy.** With the
private shell's `umask 077`, copied/generated files otherwise become mode
600 and directories 700. That can produce Kudu
`Zip permission validation failed`, host HTTP 503 and a nonzero CLI status
check after accepted upload. Normalize **only the explicit staged runtime
files/directories** before ZIP creation: files 644, directories 755.
Leave `.local` and the staging parent directory 700; leave the ZIP and all
private evidence/receipts 600. Never recursively chmod the repository,
`.local`, source tree or user directories. These archive modes do not make the
local release artifact public. If future dependencies need executable files,
review those specific entry modes instead of assuming every dependency is
plain JavaScript.

The explicit application allowlist excludes `src/test`, `src/tools`, local
settings and private files. Inspect the ZIP root before deploying;
dependency packages may legitimately contain their own published tests/docs.
`--ignore-scripts` avoids dependency lifecycle execution: if a future runtime
dependency needs a build, approve it and change this packaging process
explicitly rather than silently shipping broken artifacts. The current pure
JavaScript dependencies need no native build.

**Deploy gate:** verify the package's reviewed allowlisted paths and production
dependencies, actual Node major, manifest entrypoint, and host route prefix.
The deploying host must be within the approved SCM allowlist and have Azure
deployment rights. Then:

```bash
az functionapp deployment source config-zip \
  --resource-group rg-copilot-otel-relay --name "$RELAY_APP" \
  --src "$RELAY_ZIP" --build-remote false --timeout 600 \
  > .local/relay-code-deployment.json
az functionapp function list -g rg-copilot-otel-relay -n "$RELAY_APP" \
  > .local/relay-functions-readback.json
```

**Accepted upload is not completed deployment.** Azure CLI can receive
OneDeploy HTTP 202, then exit nonzero with
`Failed to fetch host key to check for function app status`. Preserve that
failure, the returned deployment identifier and private diagnostics. It does
not establish either a ready Function or a failed package upload. Stop automatic
follow-on deployment steps and inspect the existing deployment's actual status,
site/configuration readback and HTTP routes. In the parent-observed failure,
Kudu reported **ZIP permission validation**, caused by mode-600/700 archive
entries from `umask 077`; rebuilding with the staged entry modes above resolved
that packaging failure without changing IAM or network restrictions.
Do not blindly redeploy, add a
client key, enable basic publishing auth, or relax app/SCM restrictions to make
the CLI's post-upload polling succeed. Require real no-client-auth route and
backend persistence proof before claiming successful code deployment.

Check the single `v1/{signal}` trigger, all three supported signal paths
`/v1/traces`, `/v1/logs`, `/v1/metrics`, and anonymous
trigger authorization, then perform positive **no-client-auth** request/backend
proof and negative off-allowlist proof under the
[evidence requirements](../AGENTS.md#telemetry-evidence), using the
[isolated relay tools](../src/README.md).
Do not add `?code=` or an `Authorization` header to make the positive probe
pass. A 404/403/5xx or missing LAW marker is not a success.

For **code-only updates**, repeat packaging, CLI deployment, route readback
and fresh acceptance with the existing app/receipt; no infrastructure recreation
is needed. Record the new artifact hash privately. Rollback deploys a retained
previously verified artifact using the same CLI command, then re-verifies.
Keep retained release ZIPs under private artifact policy; remove only specific
reviewed staging paths after validation, not `.local` or broad wildcard paths.

An unavailable/obsolete CLI is a prerequisite problem: update it through the
approved installation workflow and retry. Do not fall back to Python deploy
scripts or `func publish` when Azure CLI is available. Do not enable basic
publishing auth or public ingress as a workaround.
