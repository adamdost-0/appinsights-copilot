# Copilot CLI native OTLP to Azure Application Insights

The target deployment is **collector-free**: Copilot CLI uses environment
variables to send directly to Azure Monitor's native OTLP endpoints associated
with Application Insights.

```text
Copilot CLI -- HTTPS OTLP/protobuf + Entra token --> native Azure Monitor endpoints
            separate traces and metrics URLs       |-- Log Analytics (traces)
                                                   `-- Azure Monitor workspace (metrics)
```

Start with the [native environment-variable contract](docs/native-otlp.md).
The installed CLI honors separate signal URLs and authentication headers.
The dedicated native Azure stack has been deployed and repeat apply preserves
its identities and URLs. **Automated Azure checks prove ingestion for all three
synthetic scenarios: 15 native spans, 13 events, and 26 required histogram
series.** Azure accepted the CLI's cumulative explicit histograms even though
its requested DELTA/exponential-histogram settings were not honored.
See [the native evidence record](docs/evidence/native-example.md); the earlier
77-row result below belongs only to the historical path.

**Native privacy caveat:** capture-off still emitted tool **name/type metadata**
in `gen_ai.tool.definitions`. That fails the experiment's strict key-absence
control, but is not evidence of prompt or argument leakage. Keep this experiment
synthetic-only; successful ingestion is not a complete-audit guarantee.

Deploy with `python3 -m scripts.native_deploy --subscription "$AZURE_SUBSCRIPTION_ID" --apply`.
Run `python3 -m scripts.native_smoke --scenario full-content`, then verify its
printed UUID with `python3 -m scripts.verify_native --run-id "$RUN_ID"`.
The [native guide](docs/native-otlp.md) covers prerequisites, what-if, all three
scenarios, exact environment variables, token lifetime, and the expected
metadata-policy failure.

## Visual session explorer

The live Application Insights resource now includes **Copilot CLI - Native
Session Explorer**, a shared workbook with session summaries, model token
usage, latency, tool activity, and trace/event drilldowns. Its seven panel
queries and selected-session filters have been tested against live native data;
a fresh verified session appeared without redeploying the view.

Open **Application Insights → Workbooks**, or use the portal URL emitted by
`python3 -m scripts.visualizations --apply`.
See [visualization setup and evidence](docs/visualizations.md).
Authenticated portal rendering remains unverified because the isolated browser
required Microsoft sign-in; saved-resource and query validation are not claimed
as a screenshot/UI test.

## Historical collector-based compatibility proof

The initial experiment verified this different, synthetic-only path:

```text
Copilot CLI -- OTLP HTTP/protobuf --> 127.0.0.1:4318
           local OpenTelemetry Collector Contrib v0.160.0 (digest pinned)
           |-- private OTLP JSON-lines evidence
           `-- azure_monitor --> workspace-based Application Insights
                                 linked Log Analytics workspace
```

**End-to-end Azure telemetry verification passed for all three synthetic scenarios on 2026-09-13.** Real Log Analytics queries matched post-transform collector evidence from fresh, unchanged Azure-mode manifests: 77 backend rows across dependencies, traces, and metrics. Deployment and repeat apply also succeeded. The native CLI did not meet the experiment's strict content-key-absence control, and optional content truncation means this exporter route is not lossless. See [the evidence record](docs/evidence/local-example.md) for counts and exact proof boundaries.

The CLI exports OTLP, not classic Application Insights connection-string telemetry. Only the collector receives the connection string. Its beta `azure_monitor` exporter explicitly enables span events. Azure conversion is not lossless; the private file copy allows source/backend comparison. This is an observability experiment, not a complete, tamperproof cybersecurity audit.

**Observed privacy divergence:** CLI 1.0.84-5 emitted nonempty tool definitions despite content capture being disabled in the raw metadata-only diagnostic. The required collector-side privacy filter is a downstream mitigation, not proof of native CLI compliance. See [the privacy boundary and current mitigation status](docs/security-and-data.md#observed-content-off-divergence-and-collector-boundary) before exporting.

## Reproduce the historical collector experiment

Use Linux, Python **3.12+** (tested on 3.12.3), Azure CLI with Bicep, a local Docker daemon, and an authenticated Copilot CLI account. Read [security and data handling](docs/security-and-data.md) before capture. Full-content consent covers **only synthetic sessions in temporary isolated homes and working directories**, never normal projects or existing CLI configuration.

Run commands from the repository root:

```bash
python3 -m scripts.preflight
python3 -m unittest discover -s tests -v
az bicep build --file infra/main.bicep --stdout > /dev/null
python3 -m scripts.collector validate
```

Preflight returns nonzero when blocked; do not disguise that outcome. Compilation and image/config validation do not deploy Azure resources or prove intake. Follow [deployment](docs/deployment.md) for explicit subscription selection, what-if, apply, collector lifecycle, and teardown. Follow [verification](docs/verification.md) for the real CLI scenarios and bounded backend checks.

Live verification order: **start collector -> run chosen synthetic scenario(s), retain each printed UUID -> stop collector to flush -> verify each UUID**. Source evidence must remain complete and immutable throughout verification; no CLI or collector may continue writing it.

Runtime state, secrets, captured output, and raw telemetry live under ignored, private `.local/`. Do not commit them. Successful deployment or verification does **not** automatically clean up resources. Azure teardown is audit-only: it refuses deletion of any existing group because bounded inventory cannot prove complete ownership. Manual cleanup requires separate administrator inspection.

The sample Azure resources remain available for inspection; the collector is stopped. Actual `AppDependencies`, `AppMetrics`, and `AppTraces` retention is **90 days**, despite the workspace's 30-day setting. See the evidence record before making retention or cost assumptions.

## Scope and navigation

| Document | Purpose |
| --- | --- |
| [Deployment](docs/deployment.md) | Prerequisites, two Azure resources, pinned collector, explicit cleanup |
| [Verification](docs/verification.md) | Scenario commands, source/backend proof, interpretation limits |
| [Troubleshooting](docs/troubleshooting.md) | Diagnose exporter, source, collector, Azure, then query |
| [Security and data](docs/security-and-data.md) | Consent, trust boundaries, credentials, retention and cost |
| [Verification evidence](docs/evidence/local-example.md) | Live Azure counts, local proof, tested versions, and explicit limitations |

Implementation work can be split into disjoint IaC/deployment, collector, harness/verifier, and documentation workstreams. Give each file one owner; assign a single coordinator for shared mutations, cloud actions, and live inference. Parallel implementation is not permission for concurrent deployments or duplicate smoke runs. No factory is required.
