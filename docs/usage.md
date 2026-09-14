# Onboard approved normal CLI sessions

[Administrator guide](../README.md) | [Privacy](security-and-data.md) |
[Workbook](visualizations.md)

This is an administrator recipe for **organizationally approved metadata-only
monitoring of ordinary Copilot CLI sessions**. It is not a test command and has
not been executed against normal-user code or history for this example. All
release proof comes from isolated synthetic sessions.

Azure native OTLP remains preview, without an SLA, and not recommended for
production. Approval to evaluate it does not make this example enterprise
production-ready.

## Administrator approval and onboarding

Before enabling a normal session, obtain the organization's approval for the
preview service, data residency, metadata disclosure, retention, costs, user
notice, and access controls. Review the exact installed CLI and its approved
configuration, including plugins, MCP servers, hooks, custom instructions, and
tools. Normal sessions use the user's real profile and working directory;
synthetic test isolation is deliberately not claimed here.

Complete the [synthetic verification](verification.md) first. **The current
relay evidence does not close this onboarding gate:** native AMW metric
queries returned no series, and authenticated workbook rendering remains
unverified. Linux synthetic log/span/event and label transport proof is not
enterprise rollout acceptance. The default
privacy posture permits only exact tool name/type definitions, not prompts,
responses, system content, schemas, or tool arguments/results. Content-off is
not a universal secret scrubber or pre-ingestion DLP control. If the
organization requires all six content-related keys to be absent, separately
evaluate the stricter policy before approving ordinary use.

For the **default relay route**, approve the client's public IPv4 egress CIDR
in the Function's platform allowlist. The client needs **neither an Azure
ingestion token nor a Function key**. The relay's system-assigned identity
holds Monitoring Metrics Publisher on the DCR and refreshes its upstream token
through `ManagedIdentityCredential`. GitHub/Copilot authentication is still
required for inference. Anyone sharing an allowed NAT address can send;
network admission is not individual user authentication.

For the **optional direct authenticated route**, assign the actual sending
Entra user, service principal, or approved group
**Monitoring Metrics Publisher on the explicit DCR**. Use the approved Azure
CLI authentication method for that identity. Merely supplying a principal ID
during deployment does not authenticate a user's workstation as that principal.
GitHub/Copilot authentication remains separate.

Keep auditors separate: they need **Log Analytics Reader on LAW** and
**Monitoring Data Reader on AMW**. Publishers do not need query permissions just
to send. The no-Application-Insights rows have empty `_ResourceId` and require
workspace-scoped LAW access; do not rely on DCR resource-context queries.

Distribute only the exact approved **nonsecret configuration** from the current
deployment: relay `/v1/traces` and `/v1/metrics` URLs by default; native full
signal URLs and subscription only for optional direct authentication.
Do not distribute the ownership receipt, access token,
or another user's Azure/GitHub credentials. Endpoint URLs are not passwords,
but still reveal infrastructure metadata and should stay within the approved
organization.

## Default relay: ephemeral Linux shell recipe

After [relay deployment](relay-deployment.md) and fresh acceptance, use
administrator-provided HTTPS URLs from the separate relay receipt. This is
approved ordinary use, **not a synthetic test**. Do not use normal-user
sessions as release evidence.

```bash
(
  set +x
  set -eu
  RELAY_TRACES_ENDPOINT="<approved-https-function-host>/v1/traces"
  RELAY_METRICS_ENDPOINT="<approved-https-function-host>/v1/metrics"
  unset COPILOT_OTEL_FILE_EXPORTER_PATH OTEL_EXPORTER_OTLP_ENDPOINT
  unset OTEL_EXPORTER_OTLP_HEADERS OTEL_EXPORTER_OTLP_TRACES_HEADERS
  unset OTEL_EXPORTER_OTLP_METRICS_HEADERS OTEL_EXPORTER_OTLP_LOGS_HEADERS
  unset OTEL_RESOURCE_ATTRIBUTES
  export COPILOT_OTEL_ENABLED=true
  export COPILOT_OTEL_EXPORTER_TYPE=otlp-http
  export COPILOT_OTEL_SOURCE_NAME=github.copilot
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="$RELAY_TRACES_ENDPOINT"
  export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="$RELAY_METRICS_ENDPOINT"
  export OTEL_SERVICE_NAME=github-copilot
  export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
  copilot --secret-env-vars=COPILOT_GITHUB_TOKEN,GH_TOKEN,GITHUB_TOKEN
)
```

No `Authorization`, `x-functions-key` or `?code=` is supplied. Do not acquire
or export an Azure token for this route. The Function app must stay default-deny
outside the approved networks; if the public egress changes, request an approved
allowlist update, not an allow-all rule. Exit the subshell to discard the
temporary settings. There is no client Azure token-expiry problem on this
route, but ingress changes, MI/RBAC failures, upstream outages, cold starts,
quota and export failures can still cause loss while inference works.

The relay has a `/v1/logs` route for OTLP log producers and synthetic forwarding
tests. This **does not establish a standalone Copilot CLI logs exporter**;
CLI span events still travel with traces. Do not invent unsupported CLI logs
configuration or mistake a synthetic log generator for real CLI proof.

## Opt-in hostname attribution

The CLI can include the originating runtime's hostname as an OpenTelemetry
resource attribute. Set this **in the same shell as the approved relay
configuration above, before starting Copilot**:

```bash
export OTEL_RESOURCE_ATTRIBUTES="host.name=$(node -p 'encodeURIComponent(require("node:os").hostname())')"
```

This resolves the hostname on the sending machine and percent-encodes the
attribute value. If you already set approved resource attributes, include
`host.name` once in that list instead of replacing the other attributes.
Do not put a literal `%COMPUTERNAME%` or `$HOSTNAME` placeholder in the value
and assume the exporter expands it. A GPO/logon script must resolve each
machine's value before the CLI inherits its environment.

An isolated Linux CLI test through the existing relay verified the exact
OS-reported hostname in Log Analytics. No Function or DCR change was needed.
See [the repeatable hostname test](verification.md#hostname-attribution).
For recent host-tagged spans, run:

```kusto
OTelSpans
| where TimeGenerated > ago(1d)
| extend HostName = tostring(ResourceAttributes["host.name"]),
         ConversationId = tostring(Attributes["gen_ai.conversation.id"])
| where isnotempty(HostName)
| where tostring(ResourceAttributes["service.name"]) == "github-copilot"
| project TimeGenerated, HostName, ConversationId, Name, TraceId, SpanId
| order by TimeGenerated desc
```

The hostname identifies the environment running the CLI: a VM/container may
report its own name rather than the underlying physical workstation's name.
It is **client-asserted metadata**, not an authenticated device identity.
Keep names within approved telemetry/privacy policy. Span events can be
attributed through their parent span/resource join; do not assume the same
field exists directly on every event row. Windows/GPO rollout and native
metric-series hostname labels have not been tested.

## Opt-in user attribution

To label both the host and the local OS account running the CLI, set this in
the approved relay shell before starting Copilot:

```bash
export OTEL_RESOURCE_ATTRIBUTES="$(node -p 'const os = require("node:os"); "host.name=" + encodeURIComponent(os.hostname()) + ",user.id=" + encodeURIComponent(os.userInfo().username)')"
```

This explicitly supplies **resource attributes** `host.name` and `user.id`.
It does not resolve the signed-in GitHub account, authenticate an Entra user,
or change the CLI's separate `enduser.pseudo.id`. A local username is not
globally unique; use the hostname/user pair for this example. For enterprise
reporting, select an approved stable directory identifier and its mapping
instead of assuming a local username is an immutable person identifier.

Resolve user-specific values in the **user's logon context or launcher**.
Do not run a computer-startup script as SYSTEM and distribute that account
as every user's identity. Merge these keys once with any other approved
resource attributes; do not accidentally erase them or add duplicate keys.

For a separately reviewed Windows launcher, PowerShell's
`[System.Security.Principal.WindowsIdentity]::GetCurrent().Name` can explicitly
resolve the current Windows account (for example `DOMAIN\user` for a domain
account). Resolve it at per-user logon, encode the value as one resource
attribute, and merge it with the approved settings. This is guidance, **not
tested Windows, AD or GPO deployment**; it neither attests directory membership
to the relay nor makes a mutable account name an immutable employee identifier.

In Log Analytics:

```kusto
OTelSpans
| where TimeGenerated > ago(1d)
| extend UserId = tostring(ResourceAttributes["user.id"]),
         HostName = tostring(ResourceAttributes["host.name"]),
         ConversationId = tostring(Attributes["gen_ai.conversation.id"])
| where isnotempty(UserId)
| where tostring(ResourceAttributes["service.name"]) == "github-copilot"
| project TimeGenerated, UserId, HostName, ConversationId, Name, TraceId, SpanId
| order by TimeGenerated desc
```

`UserId` above is a query projection of the supplied resource attribute, not
a claim that Azure automatically populated a built-in authenticated-user
column. Both labels are **client-asserted and can be changed by the sender**.
The relay authenticates itself upstream, not the claimed employee. User
identifiers require appropriate privacy notice, retention and access control.
The [isolated verification](verification.md#user-attribution) keeps message
content capture off.

## Optional direct-authenticated Linux shell recipe

Use this only after approval, from the ordinary working directory the user
intends to use. Authenticate Azure CLI and Copilot through their normal approved
sign-in flows first. Replace the three configuration values below with the
administrator-provided values; the URLs must be the exact HTTPS DCE/DCR signal
URLs, not a base URL or a logs URL.

Run the complete parenthesized block in an approved shell. It does not change
the parent shell's telemetry environment or write a bearer header to a script
or environment file:

```bash
(
  set +x
  set -eu

  AZURE_SUBSCRIPTION_ID="<approved-subscription-uuid>"
  AZURE_OTLP_TRACES_ENDPOINT="<exact-approved-https-traces-url>"
  AZURE_OTLP_METRICS_ENDPOINT="<exact-approved-https-metrics-url>"

  unset COPILOT_OTEL_FILE_EXPORTER_PATH OTEL_EXPORTER_OTLP_ENDPOINT
  unset OTEL_EXPORTER_OTLP_TRACES_HEADERS OTEL_EXPORTER_OTLP_METRICS_HEADERS
  unset OTEL_RESOURCE_ATTRIBUTES

  export COPILOT_OTEL_ENABLED=true
  export COPILOT_OTEL_EXPORTER_TYPE=otlp-http
  export COPILOT_OTEL_SOURCE_NAME=github.copilot
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="$AZURE_OTLP_TRACES_ENDPOINT"
  export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="$AZURE_OTLP_METRICS_ENDPOINT"
  export OTEL_SERVICE_NAME=github-copilot
  export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false

  AZURE_MONITOR_TOKEN="$(az account get-access-token \
    --subscription "$AZURE_SUBSCRIPTION_ID" \
    --resource https://monitor.azure.com/ \
    --query accessToken --output tsv)" || exit 1
  test -n "$AZURE_MONITOR_TOKEN" || exit 1
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20${AZURE_MONITOR_TOKEN}"
  unset AZURE_MONITOR_TOKEN

  copilot --secret-env-vars=OTEL_EXPORTER_OTLP_HEADERS,COPILOT_GITHUB_TOKEN,GH_TOKEN,GITHUB_TOKEN
)
```

The cleared variables remove local file/base-endpoint, per-signal credential,
and diagnostic-resource overrides. Have the administrator reconcile any
organization-managed telemetry settings, SDK-disable flags, proxy/TLS settings,
or additional environment files before launch; do not use this recipe to bypass
policy. Do not enable delta/exponential metric overrides as an assumed fix.

The CLI's `--secret-env-vars` option strips the named values from shell and MCP
server environments and redacts them from output. It does not protect against
privileged host inspection or guarantee that arbitrary plugins cannot access
credentials. Keep the header out of prompts, files, screenshots, shell tracing,
and environment dumps. Retain any additional organization-required secret
variable names when applying this option.

The bearer header is mandatory **only for this direct DCE route**. GitHub tokens and Application Insights
connection strings are not Azure OTLP credentials. The protocol is binary
OTLP/HTTP protobuf over HTTPS, with exact separate trace and metric URLs.
There is no demonstrated standalone logs exporter; span events travel through
the traces endpoint.

## Optional direct route: token expiry and stopping

An Azure CLI login can acquire a token without prompting on every launch, but
it is still authentication. The header is a **static token snapshot for that
CLI process**, not an automatic refresh service. Inspect token expiry through
the approved identity workflow before launch; the nonsecret `expires_on` field
from `az account get-access-token` reports Unix expiry time. Do not print the
full token response.

End the CLI session before the token expires. After expiry or renewed access,
exit and run the approved recipe again to acquire a fresh token. Updating a
parent shell or refreshing Azure CLI does not replace the token in an existing
CLI process. Export may fail while inference continues, so a working CLI is not
proof that monitoring is healthy. This recipe enforces no session deadline and
does not solve long-running credential refresh, revocation, or delivery gaps.

Exit the launched CLI and subshell to discard these temporary environment
values. Do not add the bearer header to shell startup files, checked-in
configuration, or persistent `.env` files. Organization-wide rollout requires
its own approved session-lifetime and credential-delivery design. Azure CLI
still maintains its normal authentication cache under the organization's
credential policy; exiting this subshell does not erase that cache.

## Find ordinary sessions

Use the [LAW workbook](visualizations.md) with a relevant time range and the
CLI's built-in conversation or trace identifier. Ordinary sessions do not need
`copilot.run.id` or `copilot.audit.scenario`; those are optional diagnostic
columns used by the synthetic harness. Leave RunId blank when browsing
ordinary sessions. Do not add usernames, email addresses, repository paths, or
synthetic scenario labels merely to make sessions discoverable.

The bounded `verify_native --run-id` command verifies synthetic manifests; it
is not a normal-session enrollment or continuous privacy-monitoring service.
Normal-session acceptance and ongoing data review require the organization's
approved operational process. Workbook charts use span data; AMW histogram
querying remains independent.

No normal-user session is part of this example's evidence. Telemetry and
conversation IDs do not establish trustworthy user attribution, complete
activity capture, or tamper-proof cybersecurity auditing. On a suspected
content or credential exposure, stop the affected export and follow the
organization's privacy/incident process rather than publishing raw data.
