# Monitor GitHub Copilot CLI with Azure Monitor

Send binary OTLP/HTTP protobuf to an **IP-restricted Azure Function relay**,
without an Azure bearer token or Function key on the client. The relay uses
managed identity to authenticate to the existing Azure Monitor DCE/DCR.
Log Analytics stores native traces/events/logs; an Azure Monitor workspace
stores native metrics. Optional direct authenticated DCE export remains supported.

An alternative [APIM gateway](docs/apim-deployment.md) requires an API-scoped
subscription key on the client and uses its own managed identity upstream.
It is independently owned, but deliberately shares the native monitoring
destination. APIM support is subject to the runbook's live acceptance gates;
existing Function/direct evidence does not prove this new path.

**Azure native OTLP ingestion is preview, without an SLA, and not recommended
for production.** The relay does not change that status or provide tamper-proof
auditing, per-user authorization, privacy filtering, or guaranteed delivery.
Any caller sharing an allowed public egress IP can submit data.

```text
Copilot CLI (binary OTLP/HTTP; no Azure token or Function key)
  | HTTPS from approved IPv4 CIDRs only; unmatched app/SCM traffic denied
  v
Node 22 Azure Function /v1/traces, /v1/logs, /v1/metrics
  | system-assigned managed identity + Entra Monitor token
  v
Existing explicit DCE -> DCR
  |-- traces/events/resources/logs --> Log Analytics
  `-- native histogram metrics ----> Azure Monitor workspace

Optional: approved client + Entra bearer token -> same DCE/DCR directly
Alternative: client + API-scoped key -> APIM /otlp/v1/{signal} -> same DCE/DCR
Log Analytics -> seven-panel session workbook (span-based, not metric proof)
```

| Path | Client admission | Backend authentication | Deployment |
| --- | --- | --- | --- |
| Function (current default) | Approved public egress IP; no client key | Function managed identity | [Separate Function group](docs/relay-deployment.md) |
| APIM alternative | Dedicated API-scoped subscription key | APIM managed identity | [Separate APIM group](docs/apim-deployment.md) |
| Direct DCE | Entra Monitor bearer token and DCR publishing role | Client identity | [Native stack](docs/deployment.md) |

An APIM key proves possession of a subscription credential, not a human or
device identity. Entra audience/scope validation is a distinct future option,
not an implemented property of subscription-key authentication.

There is no Application Insights component, collector, VM, container, Azure
Monitor Agent, or portal OTLP opt-in. The relay is a billable Flex Consumption
Function plus identity-protected storage, separate from the native resources.
Microsoft's [manual orchestration](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion#option-2-manual-resource-orchestration)
makes Application Insights optional.

## Start here

Use Linux, Azure CLI/Bicep, Bash, jq, Node **22**/npm, zip/unzip and curl.
**Deployment is documented Azure CLI commands, not Python scripts or
`func publish`.** Python 3.12+ remains optional for existing synthetic
smoke/query tests. Copilot/GitHub sign-in is separate from Azure deployment
authentication.

1. Read [security and privacy](docs/security-and-data.md), then deploy or reuse
   the native stack through [Azure CLI deployment](docs/deployment.md).
2. Deploy the [restricted relay](docs/relay-deployment.md), supplying a
   **nonempty approved IPv4 CIDR allowlist** before creating the app. Package
   only production code/dependencies and deploy with Azure CLI `config-zip`.
   Alternatively, use the [authenticated APIM runbook](docs/apim-deployment.md);
   do not deploy both gateways merely to evaluate one.
3. For Function, require [fresh no-client-auth logs forwarding and LAW persistence](AGENTS.md#telemetry-evidence),
   plus off-allowlist denial. For APIM, require the runbook's
   [authenticated positive/negative and revocation checks](docs/apim-deployment.md#explicit-activation-and-private-key-retrieval)
   and [fresh LAW/AMW evidence](docs/apim-deployment.md#backend-evidence-and-negative-controls);
   unauthenticated forwarding must fail. CLI spans/events have separate relay proof;
   **complete CLI signal acceptance is still blocked on native AMW metrics**.
   Privacy and authenticated workbook rendering are independent gates.
4. After separate organizational approval, configure the local host using
   [Administrators.md](Administrators.md). Agent context and remaining work for
   the [workbook](AGENTS.md#workbook-context-and-agent-tasks) live in `AGENTS.md`.

The native group stays **`rg-copilot-otel-v1`**, tagged `copilot-otel-v1`.
The relay is isolated in **`rg-copilot-otel-relay`**, tagged
`copilot-otel-relay`, with its **own fresh ownership marker**. Its publisher
role assignment is scoped to the existing DCR and needs explicit removal/review
before native teardown. Do not weaken existing ownership guards.

APIM owns `rg-copilot-otel-apim`, its own ownership marker, and separate private
receipts. Its only intended existing-stack write is an additive DCR publisher
assignment. Independent gateway deployment is **not full backend isolation**:
all paths share ingestion quotas and native availability. No existing Function,
LAW, AMW, DCE, DCR configuration, or workbook is redeployed by APIM.

Keep original native receipts/baselines and all new relay artifacts under
ignored, private `.local/`. Never overwrite native state with relay endpoints.
LAW's requested 30-day retention and 1 GB/day cap are **not a solution spending
ceiling**; metrics, relay compute/storage and inference have separate costs.

## Evidence and guide

[Live Function relay evidence](docs/evidence/function-relay.md) is the
authoritative record of the demonstrated no-client-auth logs-to-LAW path and
default-deny enforcement. Corrected CLI acceptance uses temporary execution
directories outside the repository to prevent ancestor Git metadata capture;
use the final evidence there, not the initial checkout-local run's UUID/counts.
**Relay native metrics ingestion is not verified**; do not
treat the configured metrics route as measured proof.
Opt-in Linux [hostname and user attribution](Administrators.md#opt-in-user-attribution)
were verified on isolated CLI spans and correlated events, not on metric
series. These are client-supplied labels, not authenticated employee identity;
Windows, AD and GPO rollout remain untested.

[Historical v1 evidence](docs/evidence/v1.md) records the prior **direct,
authenticated** synthetic execution: 15 spans, 13 events and 26 required
metric series, plus saved workbook/live-query checks. It is **not relay logs
proof**. Authenticated portal rendering remains unverified. The relay results
are recorded separately, not inferred from Bicep compilation, HTTP success,
or the historical record.

| Guide | Purpose |
| --- | --- |
| [Infrastructure contract](infra/README.md) | Existing native contract and new relay modules |
| [Relay runtime](src/README.md) | Handler configuration, transport limits and local tests |
| [Deployment](docs/deployment.md) / [Function](docs/relay-deployment.md) / [APIM](docs/apim-deployment.md) | Separate Azure CLI runbooks and acceptance gates |
| [Administrators](Administrators.md) | Local-host environment variables, host/user attribution and optional direct authentication |
| [Agent context](AGENTS.md) | Architecture, evidence boundaries, workbook intent and remaining agent tasks |
| [Troubleshooting](docs/troubleshooting.md) | Ingress, MI, deployment, routing and backend failures |
| [Security](docs/security-and-data.md) | Data, access, retention and cost boundaries |

The repository remains **private**. Documentation does not authorize commits,
pushes, publishing evidence, changing visibility, or exporting normal-user
content. All test evidence must use isolated synthetic data.
