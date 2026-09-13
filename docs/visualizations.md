# Native Application Insights session explorer

The live Application Insights resource now has a shared workbook named
**Copilot CLI - Native Session Explorer**. It queries the native OTel tables
directly; no collector, Grafana server, or additional compute is deployed.

## Open the live view

In the Azure portal, open `ai-copilot-native-spj7ob6vqx5xm`, select **Workbooks**,
and open **Copilot CLI - Native Session Explorer** from the saved/shared
workbooks. Alternatively, use the `portal_url` printed by the deployment
command or stored in private `.local/visualizations.json`.

The default time range is **24 hours**. The examples were captured on
2026-09-13; choose an appropriate time range when reviewing them later.
The optional **ConversationId**, **RunId**, and **TraceId** filters narrow the
data. Blank means all; multiple populated filters are combined with AND.
Use the identifiers in the session and trace grids to select a conversation,
one synthetic CLI run, or a specific execution trace.

| Panel | Value |
| --- | --- |
| Overview | Observed spans, conversations, LLM/tool calls, reported tokens, failures, and missing-metadata indicators |
| Sessions | Conversation/run identity, scenario, observed start/end and wall time, models, calls, reported tokens, and failures |
| Model token usage | Reported input/output tokens by model |
| LLM latency | Mean and p95 latency over time, with observed call counts |
| Tool activity | Tool counts, failures, mean/p95 duration, and missing tool metadata |
| Trace/span drilldown | Execution steps, parent links, model/tool identity, timing offsets, duration, and status |
| Events | Timestamped native events correlated with the selected run/trace |

The workbook does not display prompts, responses, tool arguments, or tool
results by default. It is an operational view, not terminal replay or a complete
audit record.

## Interpretation

Token totals use **chat spans only**, not both chat and aggregate agent spans.
They are reported usage, not independently verified billing. Missing token
attributes are counted explicitly rather than claimed to represent zero
consumption. At the initial validation, three of eight chat spans lacked token
usage; the workbook exposed that gap. No model-price or monetary-cost estimate
is invented.

Conversation identity uses `gen_ai.conversation.id`; `copilot.run.id` is the
additional test-run identifier. These are not treated as interchangeable.
Session wall time describes the observed span interval in the selected window;
summing nested span durations would double-count overlapping work.

Failure counts describe reported span failures. The captured examples are small,
successful synthetic runs, not a reliability benchmark or a comprehensive
error-detection test. Partial/dropped traces can undercount activity. The earlier
native ingestion experiment's strict tool-name/type field-absence result remains
separate from both successful ingestion and this visualization.

## Repeatable deployment

First deploy and verify the [native OTLP stack](native-otlp.md). The visualization
uses its private `.local/native-azure.json` receipt and existing ownership
marker. The deploying identity needs workbook/deployment write access to the
dedicated native group and permission to query the native Log Analytics data.
Viewers need permission to read the workbook and its underlying data; workbook
filters are not an authorization boundary.

```bash
python3 -m scripts.session_workbook --validate
python3 -m scripts.visualizations --what-if
python3 -m scripts.visualizations --apply
```

The entry point `infra/visualizations.bicep` creates one
`Microsoft.Insights/workbooks` resource. Its `sourceId` associates it with the
native Application Insights component; the panel queries target the associated
Log Analytics workspace and explicitly scope to the application.

`scripts/session_workbook.py` generates the versioned `Notebook/1.0` definition
from the checked-in `queries/session_*.kql` templates. Parameter values use
base64 bindings and exact matching rather than unsafe raw KQL interpolation.

Deployment validates the real panel queries before applying, refuses deletions,
uses the native group's ownership checks, and reads the complete saved workbook
back from Azure. It verifies the saved definition, ownership tags, and source
application. The workbook ID is deterministic, so repeated apply updates the
same resource. Native ingestion resources and credentials are not changed.

Runtime definitions, query responses, deployment receipts, and browser evidence
remain in ignored `.local/`. They can contain resource/account identifiers;
do not commit them.

## What was actually verified

| Check | Observed result |
| --- | --- |
| ARM validation and deployment | Succeeded |
| Saved definition and Application Insights association | Read back and matched the generated model |
| Application-scoped workbook listing | Returned the saved workbook |
| Seven panel queries | All returned the required columns and populated results |
| Selected trace/event drilldowns | Returned only the selected data |
| Repeat apply | Preserved workbook identity and the native-ingestion receipt |
| Fresh telemetry update | A new real delegated CLI run appeared in the workbook queries without redeploying |
| Actual authenticated portal rendering | **Not verified: browser required Microsoft sign-in** |

Initial panel validation showed **3 session rows, 15 spans, and 13 events**.
A fresh, isolated delegated run,
`d246b82f-3c60-49cb-832d-dd91b7c55a04`, passed the native ingestion verifier with
9 spans, 6 events, and 14 required histogram series. Re-running the workbook
queries then showed **4 session rows, 24 spans, and 19 events**. The selected
fresh-session drilldowns returned its 9 spans and 6 events.

An isolated Playwright 1.62.0/Chromium browser reached
`login.microsoftonline.com` when opening the actual Application Insights portal.
Azure CLI authentication does not automatically authenticate that browser.
No personal browser profile, cookies, refresh tokens, or login credentials were
copied or injected. No screenshot of rendered telemetry is claimed.

An optional fetch of Microsoft's full workbook JSON schema was blocked by
GitHub organization SAML authorization; that access requirement was not bypassed.
ARM validation, definition readback, and live query checks succeeded independently.
Client-side schema validation and authenticated UI inspection remain separate
checks.

## Built-in Agents and Grafana views

Microsoft documents **Agents (Preview)**, transaction details/simple view, and
**Dashboards with Grafana** in Application Insights. The prebuilt Copilot entry
point is [Microsoft's Copilot dashboard link](https://aka.ms/amg/dash/gh-copilot).

Microsoft's [collection and analysis overview](https://learn.microsoft.com/en-us/azure/azure-monitor/containers/collect-use-observability-data)
states that Application Insights prebuilt dashboards and queries require
**delta temporality and exponential histograms** for OTLP metrics. The tested
Copilot CLI instead emitted cumulative explicit histograms despite the requested
environment settings. Azure accepted those metrics, but that does not establish
compatibility with the built-in metric experiences. Turning on component-level
OTLP support alone does not establish that the CLI's metric format changes.
The overview recommends Grafana for OTel metric scenarios and states that
**Live Metrics is unavailable** on the OTel path.

Their rendering and native-data bindings were not verified in the unauthenticated
browser. No custom Grafana dashboard with guessed datasource identifiers was
deployed. This workbook is the directly query-validated native visualization
delivered here; it does not establish that every built-in Copilot panel supports
the same native table/metric schema.

The workbook's token and latency panels use native **span data**, not AMW
histogram queries. AMW metric ingestion and histogram querying remain independently
verified by `scripts.verify_native`; they are not silently presented as this
workbook's datasource.

## Portal acceptance check

An authorized user can complete the remaining UI check in a normal signed-in
Azure browser:

1. Open the saved workbook and choose a range containing the synthetic runs.
2. Confirm all seven panels render without query or parameter errors.
3. Select the fresh run or its trace and confirm the 9-span/6-event drilldown.
4. Confirm reported token/latency charts and missing-metadata indicators are visible.

Record that outcome separately from API/query validation. Do not mark
`portal_render_verified` true merely because a workbook resource exists.

## References

- [Workbooks ARM resource contract](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-06-01/workbooks)
- [Workbook parameters and formatters](https://learn.microsoft.com/en-us/azure/azure-monitor/visualize/workbooks-parameters)
- [Agent visualization experience](https://learn.microsoft.com/en-us/azure/azure-monitor/app/agents-view)
- [Embedded Grafana experience](https://learn.microsoft.com/en-us/azure/azure-monitor/app/grafana-dashboards)
