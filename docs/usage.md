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

Complete the [synthetic verification](verification.md) first. The default
privacy posture permits only exact tool name/type definitions, not prompts,
responses, system content, schemas, or tool arguments/results. Content-off is
not a universal secret scrubber or pre-ingestion DLP control. If the
organization requires all six content-related keys to be absent, separately
evaluate the stricter policy before approving ordinary use.

Assign the actual sending Entra user, service principal, or approved group
**Monitoring Metrics Publisher on the explicit DCR**. Use the approved Azure
CLI authentication method for that identity. Merely supplying a principal ID
during deployment does not authenticate a user's workstation as that principal.
GitHub/Copilot authentication remains separate.

Keep auditors separate: they need **Log Analytics Reader on LAW** and
**Monitoring Data Reader on AMW**. Publishers do not need query permissions just
to send. The no-Application-Insights rows have empty `_ResourceId` and require
workspace-scoped LAW access; do not rely on DCR resource-context queries.

Distribute only the exact approved **nonsecret configuration** from the current
deployment: subscription ID and full per-signal `traces_endpoint` and
`metrics_endpoint`. Do not distribute the ownership receipt, access token,
or another user's Azure/GitHub credentials. Endpoint URLs are not passwords,
but still reveal infrastructure metadata and should stay within the approved
organization.

## Ephemeral Linux shell recipe

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

The bearer header is mandatory. GitHub tokens and Application Insights
connection strings are not Azure OTLP credentials. The protocol is binary
OTLP/HTTP protobuf over HTTPS, with exact separate trace and metric URLs.
There is no demonstrated standalone logs exporter; span events travel through
the traces endpoint.

## Token expiry and stopping

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
