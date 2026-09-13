# Verify real Copilot CLI telemetry

[Administrator guide](../README.md) | [Deploy](deployment.md) |
[Evidence status](evidence/v1.md)

Verification requires **a fresh, completed real CLI run and matching persisted
data in both Azure workspaces**. Unit fixtures, a standalone OTLP generator,
successful deployment, or CLI exit zero are not end-to-end ingestion proof.
All examples here use synthetic data only.

## Local checks

Run from the repository root with Python 3.12+:

```bash
python3 -m scripts.preflight
python3 -m unittest discover -s tests -v
az bicep build --file infra/main.bicep --stdout > /dev/null
az bicep build --file infra/resources.bicep --stdout > /dev/null
az bicep build-params --file infra/main.bicepparam --stdout > /dev/null
az bicep build --file infra/visualizations.bicep --stdout > /dev/null
copilot help monitoring
```

Preflight saves installed CLI help privately under `.local/versions/`.
Compilation and tests establish only their tested contracts, not Azure
permissions, preview-region availability, ingestion, or portal rendering.
Actual fresh versions and outcomes belong in the evidence record.

## Exact exporter and authentication contract

The synthetic runner obtains destination URLs from
`.local/native-azure.json`, gets an Entra Monitor token through Azure CLI, and
passes a narrow environment to the child CLI. It uses:

```bash
export COPILOT_OTEL_ENABLED=true
export COPILOT_OTEL_EXPORTER_TYPE=otlp-http
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_METRICS_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="$AZURE_OTLP_TRACES_ENDPOINT"
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="$AZURE_OTLP_METRICS_ENDPOINT"
export OTEL_SERVICE_NAME=github-copilot
export OTEL_RESOURCE_ATTRIBUTES="copilot.run.id=$RUN_ID,copilot.audit.scenario=metadata-only"
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false
```

The two endpoint variables above mean the **exact full URLs returned by the
deployment**, not guessed addresses or a shared base URL. They have these shapes:

```text
https://<logs-dce-domain>/datacollectionRules/<immutable-dcr-id>/streams/Microsoft-OTLP-Traces/otlp/v1/traces
https://<metrics-dce-domain>/datacollectionRules/<immutable-dcr-id>/streams/Custom-Metrics-Otel/otlp/v1/metrics
```

The metrics stream must match the deployed DCR exactly, including case. Do not
append a second signal suffix. The traces URL uses the DCE's logs-ingestion
**domain**, but a traces **path**. Never send trace or metric payloads to a logs
URL. No standalone logs exporter is demonstrated: span events travel with
traces and appear in `OTelEvents`.

The required header is:

```text
OTEL_EXPORTER_OTLP_HEADERS='Authorization=Bearer%20<token>'
```

`%20` encodes the space in `Bearer <token>`. To understand token acquisition
without printing the credential:

```bash
set +x
AZURE_MONITOR_TOKEN="$(az account get-access-token \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource https://monitor.azure.com/ \
  --query accessToken --output tsv)" || exit 1
test -n "$AZURE_MONITOR_TOKEN" || exit 1
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20${AZURE_MONITOR_TOKEN}"
unset AZURE_MONITOR_TOKEN
```

This snippet shows the credential contract, **not a normal-user test launch**.
Use the isolated smoke runner below for all tests; it also checks that token
lifetime covers the bounded session. Clear the header after inspecting the
contract with `unset OTEL_EXPORTER_OTLP_HEADERS`. Never print the environment,
save tokens in files, or put real tokens in documentation.

For separately approved ordinary use rather than testing, follow
[normal-session onboarding](usage.md). That recipe is not executed as release
proof and does not enable content capture.

The sender needs Monitoring Metrics Publisher on the DCR. A GitHub token or
Application Insights connection string is not Azure authentication. An
environment token cannot renew itself; reacquiring a token in a parent shell
does not update a running CLI. See [identity boundaries](security-and-data.md#identity-and-access).

Do not add delta-temporality or exponential-histogram environment overrides as
an assumed CLI fix. Such settings do not establish that the installed CLI
honors them. Native ingestion must be tested with the CLI's actual histogram
output. Cumulative explicit histograms are **not categorically rejected**;
fresh v1 native backend count/sum checks passed with the tested CLI. This does
not prove support for delta/exponential environment overrides or compatibility
with other Azure experiences. See [measured evidence](evidence/v1.md).

## Execute the three bounded scenarios

Authenticate Azure and GitHub separately as described in
[deployment](deployment.md). The runner obtains GitHub authentication from a
supported token variable or the existing `gh` login internally; do not print
`gh auth token` or copy a normal Copilot profile into the synthetic home.

Start with metadata-only:

```bash
python3 -m scripts.native_smoke --scenario metadata-only
```

Retain its printed UUID and verify that exact run:

```bash
export RUN_ID="<actual-completed-run-uuid>"
python3 -m scripts.verify_native --run-id "$RUN_ID" --timeout-seconds 600
```

Only with explicit approval for isolated synthetic content, run each of:

```bash
python3 -m scripts.native_smoke --scenario full-content
python3 -m scripts.native_smoke --scenario delegated
```

Each command produces a **different** run UUID. Run `verify_native` separately
for each UUID. Do not substitute a fixture UUID, reuse another scenario's
identifier, relabel a failed manifest, or infer success from a requested action.
Smoke runs invoke real inference and can incur Copilot usage.

Smoke sessions default to a 180-second bound; `--timeout-seconds` accepts an
integer from 1 through 3600. The token must remain valid for the session timeout
plus a 120-second safety margin. The default validation policy is
`--privacy-policy metadata-only-v1`. An optional
`--privacy-policy strict-absence` on a **new metadata-only run** evaluates the
stronger six-key-absence policy; it does not alter CLI export or silently replace
the v1 default. The verifier follows the recorded policy.

| Scenario | Capture | Required observed behavior |
| --- | --- | --- |
| `metadata-only` | False | Positive CLI spans/events and required metrics; no prompt, response, system content, tool arguments/results or schema; exact tool name/type metadata allowed |
| `full-content` | True, synthetic only | Real fixture read with `view`; marker-bearing input/output plus arguments/result on the same observed tool span |
| `delegated` | True, synthetic only | Actual bounded child-agent invocation, valid parent relationships, and required synthetic tool/content evidence |

The harness isolates temporary `HOME`, `COPILOT_HOME`, XDG configuration, and
working directory. It excludes normal custom instructions and built-in MCPs.
Tools are restricted to none for metadata-only, `view` for full-content, and
`task` plus `view` for delegated; shell, write, and URL tools are denied.
The synthetic fixture marker binds the run, file path, content, and expected
response. A timeout or nonzero CLI exit remains failure even if some data arrives.

## Native trace, event, and resource proof

The verifier uses [the checked-in KQL template](../queries/verify_native.kql)
and the recorded LAW, run UUID, service identity, CLI version, scenario, and
completed time window. It validates returned column names and complete results
rather than assuming a schema from documentation.

`OTelSpans` carries operations and parent links; `OTelEvents` carries span
events; `OTelResources` supplies correlated resource attributes. The precise
fresh schema and correlation fields must be recorded from actual backend
responses. Missing resource rows, wrong-run matches, empty data, malformed
attributes, and partial query results cannot prove a pass.

On the direct no-Application-Insights path, native rows have an empty
`_ResourceId` and are workspace-scoped. DCR-scoped publishing permissions do not
make the DCR a row-level resource association. Query the exact LAW with LAW
reader access and correlate run/service/resource metadata; do not require
`_ResourceId` to equal the DCR ID.

Require observed `invoke_agent`, `chat`, and scenario-appropriate `execute_tool`
operations. Delegation requires actual parent/child relationships, not just a
common trace ID. Checked identifiers correlate the backend to the completed
manifest; they are not cryptographic producer attestation.

Metadata-only validation follows the
[exact name/type-only policy](security-and-data.md#metadata-only-policy).
It checks returned attributes, not all pre-ingestion traffic or process memory.
A result can demonstrate persistence while failing privacy; do not collapse
those independent outcomes into a single unqualified success.

## Independent native histogram proof

Metrics are queried at the **Azure Monitor workspace PromQL endpoint**, not
from LAW token charts. The verifier checks the required dotted base metric
names and individual histogram count/sum snapshots:

| Metric | Required scope |
| --- | --- |
| `gen_ai.client.token.usage` | All scenarios; retain input/output dimensions |
| `gen_ai.client.operation.duration` | All scenarios |
| `gen_ai.invoke_agent.duration` | All scenarios |
| `gen_ai.execute_tool.duration` | Content/tool scenarios |

Representative PromQL, substituting the real completed run UUID:

```promql
histogram_count({__name__="gen_ai.client.token.usage","copilot.run.id"="<RUN_UUID>","service.name"="github-copilot"})
histogram_sum({__name__="gen_ai.client.token.usage","copilot.run.id"="<RUN_UUID>","service.name"="github-copilot"})
```

The scripted verifier supplies the scenario and fixed evaluation time as well.
Use the recorded AMW endpoint and authorized identity; do not paste raw API
responses into public evidence. Do not invent suffixed `_count`/`_sum` series,
drop dimensions to hide a missing series, or add successive cumulative
snapshots as independent usage. Count/sum persistence is not bucket fidelity or
proof that SDK temporality/aggregation environment controls work.

## Read the result honestly

Private per-run artifacts are under `.local/runs/<UUID>/`: `manifest.json`,
CLI diagnostics, `raw-native-query.json`, and `results.json`.
The verification timeout is bounded at 600 seconds in the commands above;
overrides must be finite, greater than zero, and no more than 3600 seconds.
Retryable missing/transient data may be polled within that deadline;
authorization failures, malformed/partial responses, and required-signal
failures are not silently converted to success.

Freshly provisioned ingestion can return HTTP 503 before the data plane is
ready, even after ARM reports success. After propagation, run a **new bounded
synthetic CLI session with a new UUID** and verify both workspaces. Metrics
without the required spans/events are not a pass. An authenticated empty
protobuf request returning HTTP 400 is not a readiness check; HTTP 401 for an
anonymous request confirms that authentication is required, not that a
complete telemetry payload will ingest successfully.

Record actual aggregate counts, privacy findings, histogram series coverage,
and failure reasons in [v1 evidence](evidence/v1.md) only after reviewing fresh
results. Keep run UUID mappings and raw data private. This verifies required
persisted signals, not lossless export of every emitted record or every CLI
feature. Optional hooks, skills, MCPs, compaction, and sandbox behavior are not
covered unless deliberately and safely exercised.

## References

- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference); installed `copilot help monitoring` is version-specific.
- [Azure manual OTLP ingestion](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion)
- [OTelSpans schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/otelspans)
- [OTelEvents schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/otelevents)
- [OTelResources schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/otelresources)
- [Prometheus and OpenTelemetry metric practices](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/prometheus-opentelemetry-best-practices)
