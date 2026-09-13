# Direct native OTLP evidence

On **2026-09-13**, the local **GitHub Copilot CLI 1.0.84-5** sent real synthetic
session telemetry directly to Azure Monitor's native OTLP HTTPS endpoints.
There was **no collector, local proxy, or telemetry forwarding process** in
this path. Separate trace/metric environment variables supplied the complete
DCR endpoint URLs; a protected environment header carried a short-lived Entra
token.

## Deployment

The independently deployed native resources in East US are:

- Resource group: `rg-copilot-native-otel`
- Application Insights: `ai-copilot-native-spj7ob6vqx5xm`
- Log Analytics: `law-copilot-native-spj7ob6vqx5xm`
- Azure Monitor workspace: `amw-copilot-native-spj7ob6vqx5xm`
- DCE: `dce-copilot-native-spj7ob6vqx5xm`
- DCR: `dcr-copilot-native-spj7ob6vqx5xm`

ARM validation, what-if, deployment, and repeat apply succeeded. The resource
IDs, DCR immutable ID, and native ingestion URLs remained stable on repeat
apply. Application Insights local/key authentication is disabled. The operator
has publisher access scoped to the DCR and metric-reader access scoped to AMW.

This is manual direct ingestion using `directDataSources` and a DCR Application
Insights reference. No undocumented component “OTLP enabled” property was added.
The subscription's `OtlpApplicationInsights` feature was `NotRegistered` during
the successful deployment; the AMA/AKS preview onboarding path was not used.

## Actual native storage results

Run-scoped KQL queried `OTelSpans` and joined `OTelEvents` by trace/span identity.
PromQL queried each required histogram's base metric name with both
`histogram_count` and `histogram_sum`, using exact run, scenario, service, and
application identity checks.

| Scenario | Run UUID | Native spans | Native events | Required histogram series |
| --- | --- | ---: | ---: | ---: |
| Metadata-only | `6d60f76a-8f0d-4781-a060-a7b7bce593bb` | 2 | 3 | 5 |
| Full-content | `dc27216b-f296-4ec4-ad85-51b7bd10767e` | 4 | 4 | 7 |
| Delegated | `30f7ca97-ab1f-411f-8e88-7efebf72a4b4` | 9 | 6 | 14 |

That is **15 native spans, 13 native events, and 26 required histogram series**.
The two histogram query functions inspect the same underlying series; their
returned values must not be counted as independent ingested metric records.

The automated `scripts.verify_native` command confirmed all three runs in one
attempt each, with `azure_ingestion_proven: true`. Full-content and delegated
returned `status: passed`; metadata-only returned `status: failed` solely for
the strict field-absence control described below. Delegation included a complete
root, two agent invocations, one linked delegated child, and no missing parents.
The native command did not depend on a collector evidence file.

Required families were `gen_ai.client.token.usage`,
`gen_ai.client.operation.duration`, and `gen_ai.invoke_agent.duration`, plus
`gen_ai.execute_tool.duration` for the tool scenarios. Every required returned
series had finite, positive histogram counts and measurement sums. Separate
never-emitted UUID selectors returned no results. Queries used each manifest's
fixed finish time so delayed verification did not lose data to the instant-query
lookback window.

**Azure accepted the CLI's cumulative explicit histograms.** The CLI ignored
requested DELTA/exponential-histogram environment settings, but that did not
prevent these native metric observations from being stored and queried.
Application Insights prebuilt-experience requirements remain a separate
compatibility question. No collector converted the metrics.

## Content and privacy

Full-content and delegated native spans preserved the synthetic markers in
prompt/response attributes and actual tool-call arguments/results. Only
temporary synthetic fixtures were read; normal user projects and configuration
were excluded.

**The strict content-key-absence control failed for tool-definition metadata.** Both its
invocation and chat span contained `gen_ai.tool.definitions`, representing
18 entries and 695 stored UTF-8 bytes per field. Each entry contained only
`name` and `type` fields, not descriptions or argument schemas. No synthetic
prompt/response marker appeared in the gated metadata-only content fields.
This is not evidence that prompts, responses, tool arguments, or credentials
leaked. It does mean the experiment's stricter requirement that all six gated
attribute keys be absent was not satisfied. The direct path has no collector
filter to impose that stricter policy.

## Warm-up and proof boundaries

Initial native sessions `6a0d8baa-210f-4019-84ed-1ccc1695a4be` and
`2769ea03-9a3d-431d-92ce-b6c2c71248e9` did not yield complete trace evidence.
The first did yield native token metrics. They were not relabeled as successful
final scenarios; fresh sessions produced the complete results above.
An initial query found no spans before data appeared. A CLI shutdown diagnostic
also reported its OTel disposal exceeding 1000 ms. Those observations do not
isolate the cause of the early partial delivery.

Zero-record protobuf requests separately returned trace HTTP 204 and metric HTTP
200, validating endpoint/authentication reachability only. They produced no
telemetry records and were **not** counted as ingestion proof. Likewise, the
temporary loopback environment probes were diagnostics, not Azure evidence.

Live readback showed 30-day retention/total retention for the native span, event,
and resource tables, a 1 GB/day LAW cap, and the expected workspace linkage.
The LAW cap does not constrain AMW metric costs or constitute a total spending
ceiling. Resources remain available for inspection.

Private manifests, raw query responses, and detailed diagnostics remain under
ignored `.local/`. No token, connection string, subscription ID, principal ID,
or full captured content is included in this evidence document.

The full regression suite passed **320 tests with no skips**, including the
opt-in historical Docker probes. These tests are separate from the live native
intake proof; their temporary test collectors are not in the native data path.

See [the native deployment and environment instructions](../native-otlp.md)
for reproduction. The [earlier 77-row evidence](local-example.md) belongs to
the separate classic collector/exporter path and is not reused as native proof.
