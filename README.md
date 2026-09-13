# Monitor GitHub Copilot CLI with Azure Monitor: v1 administrator guide

Send GitHub Copilot CLI telemetry directly to Azure Monitor using **HTTPS
binary OTLP/HTTP protobuf and Microsoft Entra authentication**. Use Log
Analytics for sessions, traces, and events, and an Azure Monitor workspace for
native histogram metrics.

**v1 versions this example, not the Azure service.** Azure Monitor native OTLP
ingestion remains **preview, without an SLA, and not recommended for production
workloads**. This is an administrator evaluation guide, not an enterprise
production-readiness or complete cybersecurity-audit claim. Fresh v1
[native ingestion verification passed](docs/evidence/v1.md) for all three
synthetic scenarios: **15 spans, 13 events, and 26 required metric series**.
The workbook's saved definition, LAW association, seven live queries, and
selected drilldowns also passed; authenticated portal rendering is not verified.
No earlier deployment results are presented as proof of this rebuild.

```text
GitHub Copilot CLI
  | HTTPS binary OTLP/HTTP protobuf + Entra bearer header
  v
Manually provisioned Data Collection Endpoint (DCE)
  |
Data Collection Rule (DCR)
  |-- traces + span events --> Log Analytics
  |                           OTelSpans / OTelEvents / OTelResources
  `-- metrics -------------> Azure Monitor workspace
                              native histograms queried with PromQL

Log Analytics --> seven-panel Azure Monitor workbook
```

There is **no Application Insights component**, portal OTLP opt-in step,
collector, VM, container, or Azure Monitor Agent. Microsoft's
[manual orchestration option](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion#option-2-manual-resource-orchestration)
explicitly makes Application Insights optional; its
[overview](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/collect-use-observability-data)
also supports endpoint URLs in SDK configuration. This example configures the
CLI's exporter directly.

## Start here

Use Linux, **Python 3.12+** with the standard library, Azure CLI with Bicep,
Copilot CLI, and GitHub CLI authentication. Linux is the tested platform;
PowerShell and Windows are not validated. You need an authorized Azure public
cloud subscription and permission to deploy resources and assign scoped roles.
Read the [privacy boundaries](docs/security-and-data.md) before inference.

From the repository root, after Azure and GitHub sign-in:

```bash
export AZURE_SUBSCRIPTION_ID="<your-authorized-subscription-uuid>"
python3 -m scripts.preflight
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --what-if
python3 -m scripts.native_deploy \
  --subscription "$AZURE_SUBSCRIPTION_ID" --location eastus --apply
python3 -m scripts.native_smoke --scenario metadata-only
```

The smoke command prints a fresh run UUID. Verify that exact completed run:

```bash
export RUN_ID="<uuid-printed-by-the-smoke-command>"
python3 -m scripts.verify_native --run-id "$RUN_ID" --timeout-seconds 600
```

Workbook validation requires observed tool activity; metadata-only alone does
not populate its Tools panel. **Only after approval for isolated synthetic
content capture**, run and verify the full-content fixture:

```bash
python3 -m scripts.native_smoke --scenario full-content
```

Use this command's new UUID, not the metadata-only UUID:

```bash
export RUN_ID="<uuid-printed-by-the-full-content-command>"
python3 -m scripts.verify_native --run-id "$RUN_ID" --timeout-seconds 600
python3 -m scripts.visualizations --what-if
python3 -m scripts.visualizations --apply
```

If synthetic content capture is not approved, stop before workbook deployment;
do not remove the nonempty Tools validation gate.

These commands create billable resources and run real inference. Review what-if
before apply. A successful deployment or CLI exit does not prove ingestion;
verification must query both destination workspaces. A saved workbook and
successful panel queries do not prove authenticated portal rendering.

After the synthetic evaluation and a separate organizational privacy/access
approval, use [normal CLI session onboarding](docs/usage.md) to enable
metadata-only monitoring for ordinary use. That recipe is documented, not
executed as part of this example's proof; all verification uses synthetic data.

## One guide, organized by administrator task

| Chapter | Use it for |
| --- | --- |
| [Deploy and rebuild](docs/deployment.md) | Azure CLI prerequisites, roles, ownership, apply, and cleanup |
| [Infrastructure contract](infra/README.md) | Bicep resources, endpoints, receipts, and settings |
| [Onboard normal CLI sessions](docs/usage.md) | Separately approved metadata-only use, ephemeral bearer headers, and session expiry |
| [Verify telemetry](docs/verification.md) | Exact exporter environment, three synthetic scenarios, KQL and PromQL proof |
| [Read the workbook](docs/visualizations.md) | Seven Log Analytics panels, safe selectors, and UI acceptance |
| [Security and data](docs/security-and-data.md) | Metadata-only policy, credentials, privacy, retention, and cost |
| [Troubleshoot](docs/troubleshooting.md) | Authentication, protocol, schema, ingestion, and ownership failures |
| [v1 evidence status](docs/evidence/v1.md) | Fresh-execution status and publication boundaries |
| [LinkedIn draft](docs/linkedin.md) | Shareable v1 introduction; not posted |

The resource group is `rg-copilot-otel-v1`, with solution tag
`copilot-otel-v1` and unique per-group resource suffixes. Keep ownership receipts
and all runtime evidence under ignored, private `.local/`; never publish them.
Retention is configured to 30 days in Log Analytics with a 1 GB/day workspace
cap, **not a spending ceiling**. Azure Monitor workspace metrics have separate
billing, retention, and cardinality considerations.

The repository remains **private**. This guide and the LinkedIn draft do not
grant repository access or authorize publishing, committing, pushing, or
changing visibility.
