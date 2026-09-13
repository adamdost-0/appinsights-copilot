# Log Analytics session workbook

[Administrator guide](../README.md) | [Verify telemetry](verification.md)

The v1 workbook is associated with the **Log Analytics workspace**, not an
Application Insights component. It queries native OTel tables for operational
session views. Fresh saved-definition/readback, LAW source/gallery association,
all seven live queries, and both selected drilldowns
[passed](evidence/v1.md#live-workbook-proof). Authenticated portal rendering
remains unverified.

## Deploy and open

First deploy the [owned v1 resources](deployment.md) and verify metadata-only
ingestion. Workbook validation also requires a nonempty Tools panel, so
**explicitly approve an isolated synthetic full-content/tool scenario** before
proceeding:

```bash
python3 -m scripts.native_smoke --scenario full-content
```

Verify its printed UUID:

```bash
export RUN_ID="<uuid-printed-by-the-full-content-command>"
python3 -m scripts.verify_native --run-id "$RUN_ID" --timeout-seconds 600
python3 -m scripts.visualizations --what-if
python3 -m scripts.visualizations --apply
```

An already verified approved delegated fixture with observed tool activity can
also satisfy this prerequisite. Metadata-only alone is insufficient; do not
weaken the nonempty Tools validation gate. If synthetic content capture is not
approved, stop before workbook deployment. The visualization command uses the
private `.local/native-azure.json` receipt and the existing ownership marker.

Review the preview before apply. The deployer needs workbook/deployment write
permissions and LAW query access. Viewers need workbook read access and
authorized access to the underlying LAW. Filters do not grant or restrict RBAC.

Both deployment modes require the complete receipt-owned native foundation
before queries or ARM operations; an absent or incomplete foundation fails
closed. They then execute all seven panel queries and two selected span/event
drilldowns, followed by ARM validation and what-if. Failed queries or proposed
deletion block deployment. For read-only live-query validation without a
workbook deployment:

```bash
python3 -m scripts.session_workbook --validate
```

This queries Azure and requires LAW access; it is not an offline check.

Open **Copilot CLI - Session Explorer** using the `portal_url` emitted after
apply or retained privately in `.local/visualizations.json`. The saved LAW
source/gallery association is verified. It is intended to expose the workbook
under the workspace's **Workbooks** gallery; authenticated portal browsing and
rendering remain separate, unverified checks.
The default time range is 24 hours; select a range containing your fresh
synthetic run. Span/event queries are bounded to at most 24 hours in KQL.

## Seven panels

| Panel | Interpretation |
| --- | --- |
| Overview | Observed spans, conversations, chat/tool calls, reported tokens, failures, and missing metadata |
| Sessions | Conversation identity, optional diagnostic run/scenario, observed interval, models, call counts, token attributes, and failures |
| Chat tokens | Reported input/output token usage from chat spans, grouped by model |
| Latency | Chat-span latency over time, including mean/p95 and observed call counts |
| Tools | Observed tool activity, failures, durations, and missing tool metadata |
| Spans | Trace/span IDs, parent relationships, model/tool identity, timing, duration, and status |
| Events | Native span events correlated with selected runs and traces |

**RunId**, **ConversationId**, and **TraceId** are safe exact-match selectors.
Blank means all data in the selected time range; multiple populated selectors
combine with AND. The definition uses encoded parameter bindings rather than
unsafe raw KQL interpolation. Conversation identity and the synthetic run UUID
are different concepts and must not be treated as interchangeable.

For [approved normal usage](usage.md), built-in conversation and trace identity
drive navigation. `copilot.run.id` and `copilot.audit.scenario` are optional
synthetic diagnostic attributes, not enrollment requirements. Ordinary
`service.name=github-copilot` sessions remain visible without those labels; do
not label user sessions as synthetic to make them appear. This provides no
trustworthy user attribution or complete audit trail.

The Sessions panel's `SessionKey` uses `conversation:<actual-conversation-id>`,
falling back to `trace:<actual-trace-id>` when conversation identity is absent.
If both are unavailable, it stays blank/unknown; no identity is fabricated.
Missing conversation values remain blank even when a trace-based grouping key
is available.

The workbook does not display prompts, responses, tool arguments, or tool
results by default. It is not terminal replay or a complete security audit.

The fresh live-query dataset contains **4 conversations, 17 spans, and 16
events**. This includes an isolated synthetic session without RunId/Scenario;
selecting its actual conversation returned exactly 2 spans and 3 events. No
normal-user project or history was used. See the
[measured workbook findings](evidence/v1.md#live-workbook-proof).

## What the charts do and do not mean

Token totals use **chat spans only**, avoiding double counting with aggregate
agent spans. Missing token attributes are surfaced, not silently assumed to
mean zero consumption. Reported usage is not an independently verified invoice,
and no model-price estimate is implied.

The **token and latency panels use native span attributes**, not an AMW
histogram datasource. `scripts.verify_native` separately establishes required
AMW metric persistence through PromQL. A populated workbook cannot replace
that check.

Observed session wall time is the interval of returned spans in the selected
window, not the sum of nested durations. Failure counts reflect reported span
status; missing or dropped telemetry can undercount activity. Small synthetic
tests are not a latency benchmark or reliability study.

Missing or invalid durations are counted explicitly rather than represented as
zero. Session wall time remains null when a required span end is missing or
invalid. Resource enrichment can arrive later than its spans, so resource lookup
is not clipped to the span time window. Time filtering is applied in the
span/event KQL, not as a query-wide API or workbook time filter that would also
clip resource enrichment. Actual portal behavior still requires UI validation.

This workbook makes no claim to deliver built-in Application Insights Agents,
Grafana, or Live Metrics experiences. The solution provisions no Application
Insights component or Grafana service.

## Definition and live-query acceptance

[`infra/visualizations.bicep`](../infra/visualizations.bicep) deploys the workbook;
[`scripts/session_workbook.py`](../scripts/session_workbook.py) builds its
versioned definition from checked-in `queries/session_*.kql` templates.
The workbook's source association and panel datasource must both point to the
owned LAW. Native rows on this no-Application-Insights path have an empty
`_ResourceId`; they are workspace-scoped, not associated with the DCR through
that column. Do not filter them by the DCR resource ID. Queries use the exact
LAW and service/run/conversation/trace correlation, with LAW query permissions.
Resource lookup deduplicates resource IDs and uses a left outer join so missing
enrichment is visible rather than silently dropping spans.

Record these checks independently:

| Check | Required observation |
| --- | --- |
| ARM validation/apply | Successful result for the owned workbook deployment |
| Saved readback | Exact expected definition, LAW association, ownership tags, and stable identity |
| Workspace gallery | Saved workbook discoverable from the intended LAW in an authorized portal session |
| Seven live panel queries | Correct columns and populated expected results from fresh telemetry |
| Selector isolation | Selected run/conversation/trace returns only matching data; invalid input does not broaden scope |
| Repeat apply | Same workbook identity; no ingestion-resource or endpoint changes |
| Fresh data | A newly verified run appears through live queries without redeploying the workbook |
| Browser UI | Actual authenticated rendering and selector interaction, checked separately |

Keep generated definitions, query responses, receipts, readbacks, and browser
evidence under ignored `.local/`. A stored definition proves neither query
success nor browser rendering. The fresh resource/readback and live-query
checks passed independently; no authenticated rendering or screenshot is
claimed.

The apply receipt `.local/visualizations.json` distinguishes
`panel_queries_verified`, `saved_definition_verified`, and
`portal_render_verified`. The deployer leaves portal rendering false; a
successful resource GET is not browser evidence. The generated model is
`.local/native-session-workbook.json`, projected query rows are
`.local/session-workbook-queries.json`, and the query-validation report is
`.local/visualization-query-validation.json`. Keep all of them private.

## Portal acceptance

An authorized user should sign in through the normal Azure portal workflow:

1. Open the saved LAW workbook and choose the fresh-run time range.
2. Confirm all seven panels render without query or parameter errors.
3. Select a fresh run, then a conversation/trace, and compare returned counts
   with that run's measured evidence.
4. Confirm token/latency charts and missing-metadata indicators are visible.

Azure CLI authentication does not automatically authenticate a browser.
Do not copy personal browser profiles, cookies, or refresh tokens into
automation. If sign-in blocks inspection, report **UI not verified**, even if
ARM, readback, and all seven live queries succeed.

References: [Workbook ARM resource](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-06-01/workbooks),
[workbook parameters](https://learn.microsoft.com/en-us/azure/azure-monitor/visualize/workbooks-parameters).
