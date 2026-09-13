"""Build native Workbook serializedData; never deploy or change ingestion.

build_workbook(state) returns the Notebook/1.0 object, not an ARM resource.
The parent deployer JSON-serializes it into properties.serializedData and sets
properties.sourceId to state["workspace_resource_id"]. Every query targets that
Log Analytics workspace and selects unassociated native rows (empty _ResourceId),
then filters the enriched service and any optional user selections.

validate_queries(state) executes the exact panel queries (24h, then selected
span/event drilldowns). It returns fixed diagnostics, headers and row counts,
never telemetry values. Successful projected results stay in a mode-0600 file.
Empty, partial, malformed or failed queries do not establish validation.

CLI: python3 -m scripts.session_workbook --validate
Reads .local/native-azure.json; writes .local/session-workbook-queries.json.
Only the Azure CLI's authenticated read-only query API is used, not CLI inference.

Schema: https://github.com/microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json
Bindings: https://learn.microsoft.com/azure/azure-monitor/visualize/workbooks-parameters
Time: https://learn.microsoft.com/azure/azure-monitor/visualize/workbooks-time
"""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from .common import AppError, LOCAL, ROOT, load_json, run_json, write_json

PANEL_COLUMNS = {
    "overview": ("ObservedSpans", "Conversations", "LLMCalls", "ToolCalls",
                 "InputTokens", "OutputTokens", "Failures", "ChatSpansMissingTokens",
                 "SpansMissingConversation", "SpansMissingDuration"),
    "sessions": ("SessionKey", "RunId", "ConversationId", "Scenario", "Start", "End", "WallTimeMs",
                 "LLMCalls", "ToolCalls", "InputTokens", "OutputTokens", "Models",
                 "Failures", "ChatSpansMissingTokens", "SpansMissingEndTime"),
    "tokens": ("Model", "LLMCalls", "InputTokens", "OutputTokens", "ChatSpansMissingTokens"),
    "latency": ("TimeGenerated", "LLMCalls", "MeanMs", "P95Ms", "SpansMissingDuration"),
    "tools": ("ToolName", "ToolType", "ToolCalls", "Failures", "MeanMs", "P95Ms",
              "TotalMs", "MissingToolName", "MissingToolType", "SpansMissingDuration"),
    "spans": ("Start", "End", "RunId", "ConversationId", "TraceId", "SpanId",
              "ParentSpanId", "Operation", "Model", "ToolName", "DurationMs",
              "OffsetMs", "StatusCode", "Success", "Failed", "SpanKey", "ParentKey"),
    "events": ("TimeGenerated", "EventName", "RunId", "ConversationId", "TraceId", "SpanId"),
}
TITLES = {
    "overview": "Observed CLI telemetry",
    "sessions": "CLI sessions and conversations",
    "tokens": "Chat tokens by model",
    "latency": "Chat latency over time (milliseconds)",
    "tools": "Tool usage, observed errors and duration (milliseconds)",
    "spans": "Trace / span drilldown",
    "events": "Events correlated to the selected spans",
}
FILTERS = ("ConversationId", "RunId", "TraceId")
NOTES = """## Copilot CLI session telemetry
This view includes ordinary CLI usage from `github-copilot` spans. `RunId`
(`copilot.run.id`) and `Scenario` (`copilot.audit.scenario`) are optional metadata,
not inclusion requirements. It does not attest the producer or establish an audit.
The data source and gallery association are the Log Analytics workspace. This v1
path selects native rows with empty `_ResourceId`, then scopes by the service
attribute above. DCR IDs are endpoint provenance in deployment
receipts, not a server-side resource association on these rows. These are span summaries,
not a metrics data source or a check of Azure Monitor workspace metric ingestion.

**Filters:** the time picker defaults to 24 hours; spans and events are capped at
the last 24 hours. Optional exact-match text filters apply to every panel: copy
`ConversationId` (`Attributes["gen_ai.conversation.id"]`), `RunId`
(`ResourceAttributes["copilot.run.id"]`) or `TraceId` from a grid into the
matching field above. Leave fields blank for all; combine fields with AND.
Clear `ConversationId` to see an entire run/trace, including subagent branches.
The grid search box only filters displayed rows; it does not change other panels.
Expand the span tree to follow `TraceId/SpanId` -> `TraceId/ParentSpanId` parent
links. `OffsetMs` is relative to the first visible span in its trace.

**Counting:** `SessionKey` is `conversation:<ConversationId>` when the actual
`gen_ai.conversation.id` is recorded, otherwise `trace:<TraceId>`. This trace
fallback does not invent a conversation ID or prove that one trace is a whole
CLI session. Rows are grouped by `(SessionKey, RunId, ConversationId, Scenario)`;
optional run/scenario metadata stays visible without excluding ordinary usage.
Separate subagent conversations stay separate; blank conversation IDs remain
unknown. If both conversation and trace IDs are missing, `SessionKey` stays blank
and unidentified spans may share a row. Overview conversations count distinct
nonempty `(RunId, ConversationId)` pairs. Only `chat` spans contribute token
totals and LLM calls; root `invoke_agent` totals are not added again. Tool calls
count `execute_tool` spans. Duplicate trace/span IDs are counted once.
Token sums include only recorded values; `ChatSpansMissingTokens` exposes missing
input/output counters, not zero usage. Models prefer response then request model.
Resource attributes are enriched by `ResourceAttributesId`, without restricting
resources to the selected span time range. Resource telemetry can arrive later
than spans: inline attributes remain usable, but spans without the required
service name are excluded until enrichment arrives. Refresh
after ingestion; a missing result is not evidence of no activity.

Missing traces may underreport sessions, tokens, tools and errors; time-window
boundaries and unrecorded parents can produce incomplete trees. Start/end/wall
time cover observed spans only; concurrent span durations are not wall time.
`SpansMissingDuration` counts absent, negative or nonfinite durations; these do
not contribute to latency averages, percentiles or duration sums. LLM/tool call
counts still include them. Zero duration is valid. Session `WallTimeMs` is unknown
if any end time is missing or precedes its span start; `SpansMissingEndTime`
exposes that incompleteness.
`Failures` counts observed span errors (`Success == false` or error status),
not an exhaustive audit. Grids show at most 1000 rows; summaries use all scoped
spans before display limits. There are no cost estimates or pricing assumptions.

No full prompt content, message bodies, tool arguments/results or event attribute
bags are projected. Tool names/types are metadata shown in the tool panel:
presence violates a strict tool-name/type-key absence policy, but is not a secret leak
by itself. This Workbook is not a privacy-policy pass/fail report.
"""


def _validate_state(state: dict) -> None:
    if not isinstance(state, dict):
        raise AppError("Workbook state must be an object")
    uuid = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    for key in ("subscription_id", "workspace_customer_id"):
        if not isinstance(state.get(key), str) or not re.fullmatch(uuid, state[key]):
            raise AppError(f"Workbook state requires a valid {key}")
    name = r"[A-Za-z0-9][A-Za-z0-9_.()-]*"
    prefix = rf"/subscriptions/{re.escape(state['subscription_id'])}/resourceGroups/{name}/providers/"
    for key, kind in (
            ("dcr_resource_id", r"Microsoft\.Insights/dataCollectionRules"),
            ("workspace_resource_id", r"Microsoft\.OperationalInsights/workspaces")):
        if not isinstance(state.get(key), str) or not re.fullmatch(prefix + kind + "/" + name, state[key], re.I):
            raise AppError(f"Workbook state has an invalid or cross-subscription {key}")


def _template(name: str) -> str:
    try:
        return (ROOT / "queries" / f"session_{name}.kql").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise AppError(f"Cannot read session_{name}.kql") from error


def build_workbook(state: dict) -> dict:
    """Return a JSON-serializable Notebook/1.0 payload scoped to the native LAW."""
    _validate_state(state)
    parameters = [{
        "id": "session-time", "version": "KqlParameterItem/1.0", "name": "TimeRange",
        "type": 4, "isRequired": True, "value": {"durationMs": 86400000},
        "typeSettings": {"selectableValues": [
            {"durationMs": n} for n in (3600000, 14400000, 43200000, 86400000)],
            "allowCustom": False},
    }]
    parameters.extend({
        "id": "session-" + name, "version": "KqlParameterItem/1.0", "name": name,
        "type": 1, "isRequired": False, "value": "",
        "description": f"Exact {name}; blank means all. Copy from the grids below.",
    } for name in FILTERS)
    items = [{
        "type": 9, "name": "filters",
        "content": {"version": "KqlParameterItem/1.0", "parameters": parameters,
                    "style": "above", "queryType": 0,
                    "resourceType": "microsoft.operationalinsights/workspaces"},
    }, {"type": 1, "name": "scope-and-semantics", "content": {"json": NOTES}}]
    base = _template("base")
    for name in PANEL_COLUMNS:
        content = {
            "version": "KqlItem/1.0", "title": TITLES[name], "queryType": 0,
            "resourceType": "microsoft.operationalinsights/workspaces",
            "crossComponentResources": [state["workspace_resource_id"]],
            "size": 0,
            "query": f"// panel: {name}\n" + base + "\n" + _template(name),
            "visualization": {"tokens": "barchart", "latency": "timechart"}.get(name, "table"),
            "noDataMessage": "No observed native data for these filters; absence is not proof of no activity.",
            "gridSettings": {"filter": True, "rowLimit": 1000},
        }
        if name in ("tokens", "latency"):
            content["chartSettings"] = {
                "xAxis": "Model" if name == "tokens" else "TimeGenerated",
                "yAxis": ["InputTokens", "OutputTokens"] if name == "tokens" else ["MeanMs", "P95Ms"],
                "showLegend": True,
            }
        if name == "spans":
            content["gridSettings"]["hierarchySettings"] = {
                "treeType": 1, "idColumn": "SpanKey", "parentColumn": "ParentKey",
                "expandTopLevel": True,
            }
        items.append({"type": 3, "name": name, "content": content})
    return {
        "version": "Notebook/1.0", "items": items,
        "fallbackResourceIds": [state["workspace_resource_id"]],
        "$schema": "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json",
    }


def _bind_query(query: str, selection: dict | None = None, *,
                time_filter: str = "between (ago(24h) .. now())") -> str:
    selection = {} if selection is None else selection
    for name in FILTERS:
        value = selection.get(name, "")
        if not isinstance(value, str):
            raise AppError("Workbook filter values must be strings")
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        query = query.replace("{" + name + ":base64}", encoded)
    return query.replace("{TimeRange}", time_filter)


def _parse_response(payload: dict, name: str) -> list[dict]:
    def check_errors(value):
        if not isinstance(value, dict):
            raise AppError("malformed_response")
        if any(key in value for key in ("error", "errors", "partialError", "partialErrors")):
            raise AppError("query_response_error")
        if value.get("warnings") or value.get("infos"):
            raise AppError("query_response_annotations")

    check_errors(payload)
    tables = payload.get("tables")
    if not isinstance(tables, list) or len(tables) != 1:
        raise AppError("malformed_response")
    table = tables[0]
    check_errors(table)
    columns = table.get("columns")
    if (not isinstance(columns, list)
            or any(not isinstance(c, dict) for c in columns)
            or [c.get("name") for c in columns] != list(PANEL_COLUMNS[name])):
        raise AppError("unexpected_columns")
    rows = table.get("rows")
    if (not isinstance(rows, list)
            or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
        raise AppError("malformed_rows")
    if not rows:
        raise AppError("empty_data")
    result = [dict(zip(PANEL_COLUMNS[name], row)) for row in rows]
    if name == "overview":
        count = result[0]["ObservedSpans"]
        if type(count) is not int or count <= 0:
            raise AppError("no_observed_spans")
    return result


def validate_queries(state: dict, *, output_path: Path | None = None) -> dict:
    """Query every panel and selected drilldowns; persist rows privately, return a safe report."""
    workbook = build_workbook(state)
    output_path = LOCAL / "session-workbook-queries.json" if output_path is None else Path(output_path)
    finish = datetime.now(timezone.utc)
    start = finish - timedelta(hours=24)
    time_filter = f"between (datetime({start.isoformat()}) .. datetime({finish.isoformat()}))"
    panels = {i["name"]: i["content"]["query"] for i in workbook["items"] if i["type"] == 3}
    report = {"ok": False, "panels": {}, "selection_checks": {}}
    evidence = {"started_at": start.isoformat(), "finished_at": finish.isoformat(), "queries": {}}

    def execute(name, selection=None, *, selected=False):
        key = "selected_" + name if selected else name
        query = _bind_query(panels[name], selection, time_filter=time_filter)
        diagnostic = {"ok": False, "columns": list(PANEL_COLUMNS[name]), "row_count": 0}
        evidence["queries"][key] = {"query": query}
        rows = []
        try:
            payload = run_json([
                "az", "rest", "--method", "POST", "--url",
                f"https://api.loganalytics.azure.com/v1/workspaces/{state['workspace_customer_id']}/query",
                "--resource", "https://api.loganalytics.io",
                "--subscription", state["subscription_id"],
                # Span/event time bounds are in KQL; a request-wide timespan also
                # clips resource enrichment that may precede or follow those spans.
                "--body", json.dumps({"query": query}),
                "--output", "json",
            ])
        except AppError:
            diagnostic["error"] = "query_failed"
        else:
            try:
                rows = _parse_response(payload, name)
            except AppError as error:
                diagnostic["error"] = str(error)
            else:
                diagnostic.update(ok=True, row_count=len(rows))
                evidence["queries"][key]["tables"] = payload["tables"]
                if selected and any(any(row[field] != value for field, value in selection.items())
                                    for row in rows):
                    diagnostic.update(ok=False, error="selection_mismatch")
        report["selection_checks" if selected else "panels"][name] = diagnostic
        return rows

    results = {name: execute(name) for name in panels}
    # An observed event supplies a selection that must populate both detail panels.
    candidate = next((row for row in results["events"]
                      if all(isinstance(row[field], str) for field in FILTERS)
                      and row["TraceId"]), None)
    if candidate:
        selection = {field: candidate[field] for field in FILTERS if candidate[field]}
        for name in ("spans", "events"):
            execute(name, selection, selected=True)
    else:
        for name in ("spans", "events"):
            report["selection_checks"][name] = {
                "ok": False, "columns": list(PANEL_COLUMNS[name]), "row_count": 0,
                "error": "no_correlated_selection",
            }
    report["ok"] = all(p["ok"] for group in ("panels", "selection_checks")
                       for p in report[group].values())
    evidence["report"] = report
    write_json(output_path, evidence)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", required=True,
                        help="Run read-only native panel queries and save private evidence")
    parser.parse_args(argv)
    try:
        report = validate_queries(load_json(LOCAL / "native-azure.json"))
    except AppError:
        print(json.dumps({"ok": False, "error": "workbook_state_or_evidence_failure"}))
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
