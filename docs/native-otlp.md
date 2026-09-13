# Direct native OTLP: environment-variable contract

The target is **Copilot CLI -> Azure Monitor native OTLP endpoints**, with no local
or remote OpenTelemetry Collector. Application Insights provides the application
experience; a Data Collection Rule (DCR) routes native traces to Log Analytics and
metrics to an Azure Monitor workspace. The previous collector-based proof is
historical evidence, not proof of this route.

Microsoft's [collection and analysis overview](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/collect-use-observability-data)
explicitly describes using the native endpoint URLs in Collector exporters **or
SDK configuration**. A Collector is not inherent to the endpoint contract; this
repository separately verifies the actual Copilot CLI environment-variable path.
Native OTLP support is a preview without an SLA and is not recommended by that
guidance for production workloads.

## Deploy native resources

The native stack is independent of the historical collector stack. It does not
require its `.local/azure.json`, Docker, or an Application Insights connection
string. On the deployment/operator machine, use Python 3.12+, Azure CLI with
Bicep, and an authenticated Azure account with resource deployment and scoped
role-assignment permissions.

```bash
az login
export AZURE_SUBSCRIPTION_ID="<authorized-subscription-uuid>"
python3 -m scripts.native_deploy --subscription "$AZURE_SUBSCRIPTION_ID" --what-if
python3 -m scripts.native_deploy --subscription "$AZURE_SUBSCRIPTION_ID" --apply
```

The IaC entry point is `infra/native-main.bicep`. It provisions the dedicated
`rg-copilot-native-otel` group and:

| Resource | Function |
| --- | --- |
| Log Analytics workspace | Native `OTelSpans`, `OTelEvents`, `OTelResources`, and optional `OTelLogs` storage |
| Workspace-based Application Insights | Application association; local/key-based authentication disabled |
| Azure Monitor workspace | Native metric/histogram storage and PromQL query endpoint |
| Data Collection Endpoint | Public HTTPS native ingestion endpoints; Entra authentication is required |
| Data Collection Rule | Direct OTLP sources, application reference, resource-attribute enrichment, and signal routing |
| Scoped role assignments | Monitoring Metrics Publisher on DCR and Monitoring Data Reader on AMW for the test operator |

There are no VMs, container apps, AKS clusters, collectors, agents, or DCR/compute
associations. Azure Monitor workspace creation can also create service-managed
default ingestion resources; the CLI uses the **explicit native DCR/DCE**
returned by this deployment, not those default resources.

The current signed-in user is the default test principal. For automation, pass
`--principal-id <object-uuid> --principal-type ServicePrincipal`. This template
gives that one proof-of-concept operator both publishing and metric-query
permissions. In enterprise use, separate publisher and auditor identities.
Log Analytics querying additionally requires approved workspace read access;
subscription/resource-group owner access supplied it in the live example.
The script does not grant broader subscription or directory roles.

The CLI test machine additionally needs Copilot CLI and GitHub authentication
available through `gh auth login` or a supported `COPILOT_GITHUB_TOKEN`,
`GH_TOKEN`, or `GITHUB_TOKEN` environment variable. The isolated runner does not
copy the normal user's Copilot home or private configuration.

The private `.local/native-deployment.json` ownership receipt is written before
deployment. Keep it for repeat apply; existing groups/resources with mismatched
ownership tags are refused. Runtime native connection metadata is written to
`.local/native-azure.json` only after a successful deployment. It contains no
tokens or connection string. Repeating the same apply retains resource IDs and
the DCR immutable ID/endpoint URLs.

Application Insights can asynchronously create an untagged **Failure Anomalies**
smart-detector rule. The ownership check recognizes only the standard detector
and a single exact link to an owned component, and leaves that rule untouched.
This is not permission to adopt or delete unowned resources.

The tested Bicep 0.42.1 compiler lacks local type metadata for AMW API
`2025-10-03` and emits BCP081. Compilation succeeds; actual ARM validation and
deployment succeeded with that API. No Bicep upgrade or preview-feature
registration was needed for this manually orchestrated direct path.

### Application Insights-managed OTLP opt-in

The portal's **Turn on OTLP support** action is a separate, irreversible
onboarding operation. The IaC above implements **manual resource orchestration**;
it does not turn on that component-level option. Microsoft's
[native ingestion guide](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion#option-2-manual-resource-orchestration)
explicitly leaves the optional component's OTLP setting Off in the manual path.
Successful native ingestion therefore does not prove the portal option is On.

On 2026-09-13, the operator authorized trying the one-way conversion on the
existing example. Investigation stopped before mutation because the exact
existing-component write contract could not be verified:

- The published `components@2020-02-02-preview` schema and live component
  responses did not establish an OTLP enablement field or an AMW link.
  `WorkspaceResourceId` remains the **Log Analytics** workspace reference.
- Component capabilities and policy aliases did not expose the operation.
  Application-scoped DCR association reads returned `UnsupportedResourceType`;
  creating that association is not an established opt-in mechanism.
- The provider advertises the extension resource
  `Microsoft.Monitor/settings@2025-06-03-preview`, but its aliases were empty.
  A read of application-scoped `settings/default` returned `ResourceNotFound`.
  These facts do not establish a writable payload or even its role in opt-in.
- The automation browser required Microsoft sign-in. No authenticated portal
  request or generated template was available to establish the missing contract.

**Managed opt-in remains blocked and was not applied.** No guessed component
properties, preview-feature registrations, or replacement workspaces were
introduced. The existing native route remains intact; re-querying the delegated
run `d246b82f-3c60-49cb-832d-dd91b7c55a04` still passed with nine spans, six events,
and fourteen required histogram series. This rechecks existing evidence, not a
fresh post-conversion run.

To resume, use an authorized portal session to inspect/perform this component's
opt-in and record its generated deployment template or sanitized request method,
URL, and JSON body. Never share authorization headers, cookies, tokens, or
connection strings. Reuse the existing AMW only if the workflow supports it;
otherwise assess the new workspace and costs before proceeding. Retain private
before/after snapshots, reconcile resulting links, endpoints, DCR-scoped RBAC and
IaC ownership, then run a **new** synthetic session and workbook checks. Do not
blindly reapply the current component template after conversion, assume old
metrics migrate, or label built-in portal experiences verified without checking
them in an authenticated browser.

### Retention, costs, and cleanup

Live readback found 30-day retention and total retention on `OTelSpans`,
`OTelEvents`, and `OTelResources`, matching this LAW's 30-day setting. Its
workspace daily cap is 1 GB. **That cap does not cap AMW metric ingestion or
the total solution cost.** Metrics have their own service billing/retention;
do not assume the LAW settings apply to them. Unique run labels increase metric
cardinality and are bounded to these experiments.

No resources are automatically deleted. Before cleanup, inspect the dedicated
native group, role assignments, stored evidence, and any AMW-managed resources.
An authorized administrator can then delete the exact approved native resources
and follow Azure's managed-resource lifecycle. The legacy teardown script is
not a native-stack deletion command. Do not delete the historical group as part
of native cleanup.

## Environment variables

Use the **native OTLP connection information**, not the classic Application
Insights connection string or its `/v2/track` ingestion endpoint:

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

export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=DELTA
export OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION=base2_exponential_bucket_histogram
```

The endpoint variables above are inputs obtained from the provisioned native
resources. For manually orchestrated resources, their shapes are:

```text
https://<logs-dce-domain>/datacollectionRules/<dcr-immutable-id>/streams/Microsoft-OTLP-Traces/otlp/v1/traces
https://<metrics-dce-domain>/datacollectionRules/<dcr-immutable-id>/streams/Custom-Metrics-Otel/otlp/v1/metrics
```

These are **complete signal-specific URLs**. Do not append `/v1/traces` or
`/v1/metrics` again. The metrics stream name must exactly match the DCR.
This manually orchestrated direct DCR links the component through
`references.applicationInsights` and `enrichWithReference`. It does not require
an instrumentation key or `microsoft.applicationId` in the CLI environment;
do not import that requirement from the distinct Azure Monitor Agent route.
Use a fresh UUID for `RUN_ID` for each synthetic verification session.

Clear inherited settings that might select a different transport, destination,
or credential before using a managed profile. In particular,
`COPILOT_OTEL_FILE_EXPORTER_PATH` is not part of this path. The provided synthetic
harness starts from a narrow environment allowlist rather than inheriting a
normal user's telemetry, proxy, TLS, plugin, or instruction configuration.

### Entra authentication

The native endpoints require an Entra access token for Azure Monitor. An
Application Insights connection string is **not** an OTLP credential. The sending
identity needs **Monitoring Metrics Publisher**, scoped to the DCR.

For a bounded session, acquire the token without printing it or enabling shell
tracing:

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

The CLI decodes `%20` to the space in `Bearer <token>`. Never put tokens in
checked-in environment files, command examples containing real values, terminal
output, or telemetry attributes.

**A token in an environment variable does not refresh itself.** Updating a
parent shell does not update an already-running CLI process. Enterprise rollout
needs an approved short-lived credential delivery/session-lifetime mechanism;
this proof does not implement transparent refresh, device enrollment, or an
enterprise identity policy. The synthetic harness obtains a fresh token and
refuses one whose remaining lifetime cannot cover its bounded session.

## Measured CLI compatibility

On CLI **1.0.84-5**, two isolated synthetic sessions established:

| Setting | Observed behavior |
| --- | --- |
| Signal-specific trace and metric endpoints | Honored as exact, distinct request paths |
| HTTP JSON and HTTP protobuf protocols | Both emitted the corresponding content type |
| Percent-encoded authorization header | Decoded correctly at the receiver |
| Requested DELTA metric temporality | **Not honored:** eight observed metric families used cumulative temporality (`2`) |
| Requested exponential histograms | **Not honored:** the same eight families used explicit histograms |

The test fixture was a temporary loopback HTTP recorder. It did not transform,
forward, or ingest telemetry into Azure and is not a deployed collector. These
observations establish environment-variable behavior, **not Azure persistence**.

Reproduce the diagnostic without Azure resources:

```bash
python3 -m scripts.probe_native_env --run-cli --timeout-seconds 180
```

The explicit `--run-cli` flag authorizes two bounded synthetic CLI sessions.
The repeatable probe on 2026-09-13 produced report UUID
`ce58fae9-ad9d-4f3c-a4b2-4c9798904f36`. Both JSON and protobuf passed signal
routing, authorization decoding, content type, and trace/metric export checks.
Both failed the requested DELTA/exponential checks. Exit code `1` and status
`incompatible` refer to those requested environment settings, not to a measured
Azure ingestion rejection. Exit `2` indicates consent/configuration/internal
errors; exit `3` indicates a transport/CLI failure. Raw records and the detailed
report remain private under `.local/native-probe/`.

Microsoft documents delta temporality and exponential histogram aggregation as
requirements for Application Insights native OTLP experiences. Azure Monitor
also explicitly documents querying cumulative and explicit OTLP histograms.
The observed settings mismatch does **not** establish an ingestion blocker.
Query native histograms using `histogram_count` and `histogram_sum` against the
base metric name, not assumed `_count`, `_sum`, or `_bucket` series. Native
Azure acceptance and query results must be measured directly; a successful
trace export must not be presented as proof of metric ingestion.

### Privacy boundary

Native Azure spans contained `gen_ai.tool.definitions` with content capture
disabled: 18 entries containing only tool `name` and `type` metadata. No prompt,
response, or argument leak was established. This still fails the experiment's
stricter six-key-absence control. A direct route has no collector transform to
impose that stricter policy. Review the actual retained fields before enterprise
rollout; this experiment authorizes only isolated synthetic sessions, not
capturing normal user projects or existing conversation history.

## Synthetic runner

`scripts.native_smoke` reads the private `.local/native-azure.json` resource
receipt, obtains an Azure Monitor token, and runs the existing synthetic scenario
with both full native endpoint URLs. It never starts Docker or a collector and
does not rely on `.local/collector.json`.

```bash
python3 -m scripts.native_smoke --scenario metadata-only
python3 -m scripts.native_smoke --scenario full-content
python3 -m scripts.native_smoke --scenario delegated
```

The receipt requires `subscription_id`, `dcr_resource_id`, `dcr_immutable_id`,
`application_insights_resource_id`,
`workspace_customer_id`, `traces_endpoint`, and `metrics_endpoint`.
The runner validates native HTTPS endpoint shapes and resource identities before
placing credentials in the child environment. It records a fresh
`copilot.run.id`, versions, times, and endpoint/resource provenance in a private
manifest. Tokens are not persisted. Exit zero means the CLI session completed;
the manifest remains `awaiting_verification` with `azure_ingestion_proven: false`
until an actual backend check establishes persistence.

## Verify native persistence

Use the UUID printed by the native runner:

```bash
python3 -m scripts.verify_native --run-id "$RUN_ID" --timeout-seconds 600
```

No collector stop or local source-evidence file is required. The verifier queries
native LAW tables and the AMW PromQL endpoint using separate read-token
audiences. It validates run/resource/service/version identity, trace parenting,
events, synthetic content markers, required histogram counts/sums, and an empty
never-emitted-run control. Metric queries are evaluated at the manifest's fixed
finish time rather than the current time.

Private `results.json` and `raw-native-query.json` are written under
`.local/runs/<UUID>/`. Exit zero requires overall `status: passed`. The
metadata-only scenario in this build returns **exit 1 / `status: failed` for the
strict six-field-absence policy**, while separately reporting
`azure_ingestion_proven: true` when all required signals persisted. Do not hide
that policy failure or misreport it as an Azure ingestion rejection.
Full-content and delegated checks passed in the
[native example](evidence/native-example.md).

### Native troubleshooting

| Symptom | Check |
| --- | --- |
| Ingestion 401/403 | Monitor token audience/expiry and publisher permission on the exact DCR; use a fresh bounded session after permission propagation |
| Query 401/403 | LAW and AMW need separate query audiences and read permissions; ingestion permission does not grant read access |
| Empty native metrics | Query dotted base names with native histogram functions, exact resource/run labels, and the run's finish time; do not assume `_bucket`, `_count`, or `_sum` series |
| Empty or incomplete traces | Inspect private CLI diagnostics and actual native tables; initial readiness/permission delays and disposal warnings are not success evidence |
| CLI exit zero but missing Azure data | CLI export is best-effort; retain the failed/partial manifest and use the bounded verifier rather than treating process completion as persistence |
| DELTA/exponential env checks fail | This CLI ignores those format controls; native cumulative explicit histogram persistence was nevertheless verified |
| Metadata policy failure | Inspect the retained field structure; name/type-only tool metadata is not evidence of prompt/argument leakage |

Do not reuse the historical `scripts.verify_ingestion` command or classic
`AppDependencies`/`AppMetrics` queries for native proof. Do not relabel a partial
run or replayed diagnostic as a successful native CLI session.

## References

- [Deploy and validate the native session visualization](visualizations.md)
- [Native OTLP ingestion and resource orchestration](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/opentelemetry-protocol-ingestion)
- [Native metric and histogram query behavior](https://learn.microsoft.com/en-us/azure/azure-monitor/metrics/prometheus-opentelemetry-best-practices)
- Installed `copilot help monitoring` for the CLI's documented controls.
  Signal-specific endpoint behavior above was additionally measured because
  those endpoint overrides are not listed in that build's help text.
