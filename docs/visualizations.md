# Log Analytics session workbook

[Administrator guide](../README.md) | [Verify telemetry](verification.md)

The workbook is associated with the **Log Analytics workspace**, not an
Application Insights component. It queries native OTel tables for operational
session views. Historical direct-auth v1 saved-definition/readback, LAW source/gallery association,
all seven live queries, and both selected drilldowns
[passed](evidence/v1.md#live-workbook-proof). Authenticated portal rendering
remains unverified.

The new default relay changes transport/authentication, not these span-based
queries. Synthetic relay **logs** do not populate the seven session panels or
prove CLI spans/metrics. Require fresh signal-specific evidence; do not reuse
the historical workbook results as relay acceptance.

## Prepare data

First deploy or reuse the [owned native resources](deployment.md) and verify
metadata-only ingestion for the route being evaluated. The current relay
log/span/event proof is not complete CLI acceptance: native metrics remain
unverified. Retaining the existing workbook requires no redeployment and
does not establish fresh relay workbook or browser proof.

New workbook deployment/update validation requires a nonempty Tools panel.
Use already approved, verified synthetic tool data only if it remains within
the query's maximum 24-hour window. Otherwise obtain **separate approval for
a new isolated synthetic full-content/tool scenario**; prior approval is not
an automatic instruction to enable capture again. One optional existing
**direct-authenticated** test route is:

```bash
python3 -m scripts.native_smoke --scenario full-content
```

Verify its printed UUID:

```bash
export RUN_ID="<uuid-printed-by-the-full-content-command>"
python3 -m scripts.verify_native --run-id "$RUN_ID" --timeout-seconds 600
```

An already verified approved delegated fixture with observed tool activity
inside that window can also satisfy this prerequisite. The current relay
metadata-only smoke has no tool calls; do not weaken the nonempty Tools gate
or relabel direct-route tool data as relay proof. If no approved qualifying
data is available, stop before workbook deployment/update. The Python commands
above are optional ways to produce/query data, not a requirement to deploy
the workbook. The Azure CLI workflow below uses the private native receipt
and existing native ownership marker.

Review the preview before apply. The deployer needs workbook/deployment write
permissions and LAW query access. Viewers need workbook read access and
authorized access to the underlying LAW. Filters do not grant or restrict RBAC.

Require the complete receipt-owned native foundation before queries or ARM
operations. An absent or incomplete foundation stops deployment. Execute all
seven panel queries and selected span/event drilldowns before ARM validation
and what-if. Failed queries or proposed deletion block deployment. Existing
Python read-only live-query validation remains optional:

```bash
python3 -m scripts.session_workbook --validate
```

This queries Azure and requires LAW access; it is not an offline check.

## Deploy and update with Azure CLI (no Python prerequisite)

Use Node 22, Bash, jq and Azure CLI from the [deployment prerequisites](deployment.md).
Verify original native group/marker/LAW ownership first. If an existing workbook
is present, read it back and confirm exact ID, tags and source before updating;
do not adopt an unrelated workbook with a similar title.

Generate the Notebook definition using the **existing checked-in KQL**, safe
base64 parameter binding and native workspace datasource:

```bash
node <<'JS'
const fs = require('node:fs');
const state = JSON.parse(fs.readFileSync('.local/native-azure.json', 'utf8'));
const names = ['overview','sessions','tokens','latency','tools','spans','events'];
const filters = ['ConversationId','RunId','TraceId'];
const parameters = [{
  id: 'session-time', version: 'KqlParameterItem/1.0', name: 'TimeRange',
  type: 4, isRequired: true, value: {durationMs: 86400000},
  typeSettings: {selectableValues: [3600000,14400000,43200000,86400000]
    .map(durationMs => ({durationMs})), allowCustom: false}
}, ...filters.map(name => ({
  id: `session-${name}`, version: 'KqlParameterItem/1.0', name,
  type: 1, isRequired: false, value: '',
  description: `Exact ${name}; blank means all. Multiple selections combine with AND.`
}))];
const base = fs.readFileSync('queries/session_base.kql', 'utf8');
const items = [{
  type: 9, name: 'filters',
  content: {version: 'KqlParameterItem/1.0', parameters, style: 'above',
    queryType: 0, resourceType: 'microsoft.operationalinsights/workspaces'}
}, {type: 1, name: 'scope-and-semantics', content: {json:
  '## Copilot CLI session telemetry\nSpan summaries, not native metric proof or a complete audit. ' +
  'Only chat spans contribute token totals; missing metadata stays visible. ' +
  'RunId is optional synthetic metadata. ConversationId and TraceId are not user authentication. ' +
  'No prompt/tool bodies are projected. Blank selectors mean all; populated selectors combine with AND.'}}];
for (const name of names) {
  const content = {
    version: 'KqlItem/1.0', title: name, queryType: 0,
    resourceType: 'microsoft.operationalinsights/workspaces',
    crossComponentResources: [state.workspace_resource_id], size: 0,
    query: `// panel: ${name}\n${base}\n${fs.readFileSync(`queries/session_${name}.kql`, 'utf8')}`,
    visualization: name === 'tokens' ? 'barchart' : name === 'latency' ? 'timechart' : 'table',
    noDataMessage: 'No observed matching data; absence is not proof of no activity.',
    gridSettings: {filter: true, rowLimit: 1000}
  };
  if (name === 'tokens' || name === 'latency')
    content.chartSettings = {xAxis: name === 'tokens' ? 'Model' : 'TimeGenerated',
      yAxis: name === 'tokens' ? ['InputTokens','OutputTokens'] : ['MeanMs','P95Ms'],
      showLegend: true};
  if (name === 'spans')
    content.gridSettings.hierarchySettings = {treeType: 1, idColumn: 'SpanKey',
      parentColumn: 'ParentKey', expandTopLevel: true};
  items.push({type: 3, name, content});
}
const model = {version: 'Notebook/1.0', items, fallbackResourceIds: [state.workspace_resource_id],
  $schema: 'https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json'};
fs.writeFileSync('.local/workbook-cli-definition.json', JSON.stringify(model, null, 2), {mode: 0o600});
const values = {location: state.location, workspaceResourceId: state.workspace_resource_id,
  ownershipMarker: state.ownership_marker, workbookData: JSON.stringify(model)};
fs.writeFileSync('.local/workbook-cli-parameters.json',
  JSON.stringify(Object.fromEntries(Object.entries(values).map(([k,value]) => [k,{value}]))),
  {mode: 0o600});
JS
```

This standalone CLI-runbook definition preserves the seven native query
contracts and chart/selector semantics. Its shorter explanatory text/titles
are not byte-identical to the historical Python-generated model; review this
intentional presentation update before applying to an existing workbook.

Execute the actual panel KQL with the same bindings and a maximum 24-hour
window. Do not set a query-wide timespan that clips late resource enrichment:

```bash
node <<'JS'
const fs = require('node:fs');
const model = JSON.parse(fs.readFileSync('.local/workbook-cli-definition.json', 'utf8'));
for (const item of model.items.filter(x => x.type === 3)) {
  let query = item.content.query.replaceAll('{TimeRange}', 'between (ago(24h) .. now())');
  for (const name of ['ConversationId','RunId','TraceId'])
    query = query.replaceAll(`{${name}:base64}`, '');
  fs.writeFileSync(`.local/workbook-query-${item.name}.json`, JSON.stringify({query}), {mode: 0o600});
}
JS
LAW_CUSTOMER_ID="$(jq -er .workspace_customer_id .local/native-azure.json)"
for PANEL in overview sessions tokens latency tools spans events; do
  az rest --method post \
    --url "https://api.loganalytics.io/v1/workspaces/$LAW_CUSTOMER_ID/query" \
    --resource https://api.loganalytics.io --headers Content-Type=application/json \
    --body "@.local/workbook-query-$PANEL.json" > ".local/workbook-query-$PANEL-result.json"
  jq -e 'has("error")|not' ".local/workbook-query-$PANEL-result.json" >/dev/null
  jq -e '(.tables|length) == 1 and (.tables[0].rows|length) > 0' \
    ".local/workbook-query-$PANEL-result.json" >/dev/null
done
```

Review all expected column names against each checked-in query projection,
positive meaningful overview counts (not just an aggregate row), missing-data
indicators, complete results and zero error/partial-error/warning annotations.
Run selected Spans and Events queries as well: replace the matching
`{RunId:base64}`, `{ConversationId:base64}`, or `{TraceId:base64}` in the original
definition with the **base64 encoding** of the actual fresh selected value,
leave others blank, and execute the same API. Require only matching IDs,
nonempty expected rows, and no invalid-selection scope broadening. Do not
substitute unescaped raw values into KQL. Review queries before deployment;
HTTP success or an overview row containing zeros is insufficient.

```bash
az deployment group validate -g rg-copilot-otel-v1 -n copilot-otel-workbook \
  --template-file infra/visualizations.bicep --parameters @.local/workbook-cli-parameters.json \
  > .local/workbook-cli-validation.json
az deployment group what-if -g rg-copilot-otel-v1 -n copilot-otel-workbook \
  --template-file infra/visualizations.bicep --parameters @.local/workbook-cli-parameters.json \
  > .local/workbook-cli-what-if.txt
```

**Review gate:** approve only the existing owned workbook creation/update,
stable identity, native marker/source and intended presentation; no ingestion
resource/endpoint changes or deletion. Then:

```bash
az deployment group create -g rg-copilot-otel-v1 -n copilot-otel-workbook \
  --template-file infra/visualizations.bicep --parameters @.local/workbook-cli-parameters.json \
  > .local/workbook-cli-result.json
jq -e '.properties.provisioningState == "Succeeded"' .local/workbook-cli-result.json >/dev/null
WORKBOOK_ID="$(jq -er .properties.outputs.workbookResourceId.value .local/workbook-cli-result.json)"
az rest --method get --url "https://management.azure.com${WORKBOOK_ID}?api-version=2023-06-01" \
  > .local/workbook-cli-readback.json
jq -e --slurpfile model .local/workbook-cli-definition.json \
  --slurpfile state .local/native-azure.json \
  '(.properties.serializedData|fromjson) == $model[0]
   and (.properties.sourceId|ascii_downcase) == ($state[0].workspace_resource_id|ascii_downcase)
   and .tags.solution == "copilot-otel-v1"
   and .tags["ownership-marker"] == $state[0].ownership_marker' \
  .local/workbook-cli-readback.json >/dev/null
```

Repeat deployment uses the same deterministic workbook identity and requires
the same ownership/query/what-if/readback gates. Follow [workbook-only or full
cleanup](cleanup.md#delete-native-resources-only-after-approval); do not invoke
Python deployment/teardown scripts as prerequisites.

Open **Copilot CLI - Session Explorer** from the owned LAW's Workbooks gallery
in an authorized portal session. The saved LAW
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

The historical direct-auth live-query dataset contains **4 conversations, 17 spans, and 16
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
histogram datasource. `scripts.verify_native` separately checks required
AMW metric persistence through PromQL for the optional direct-authenticated
runner. Relay metrics require their own fresh queries and remain unverified.
A populated workbook cannot replace that check.

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
the Node recipe above builds the CLI-runbook definition from checked-in
`queries/session_*.kql` templates. The existing
[`scripts/session_workbook.py`](../scripts/session_workbook.py) remains an
optional legacy definition/query test utility, not a deployment prerequisite.
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
success nor browser rendering. The historical direct-auth resource/readback and live-query
checks passed independently; no new relay check, authenticated rendering or screenshot is
claimed.

The historical Python apply receipt `.local/visualizations.json` distinguishes
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
