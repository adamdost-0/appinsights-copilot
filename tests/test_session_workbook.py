"""Offline contracts for the native-only Workbook and read-only query validator."""

from copy import deepcopy
import importlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.common import AppError

SUB = "00000000-0000-4000-8000-000000000001"
WORKSPACE = "00000000-0000-4000-8000-000000000002"
RUN = "00000000-0000-4000-8000-000000000003"
CONVERSATION = "actual-conversation-not-the-run"
TRACE = "a" * 32
PREFIX = f"/subscriptions/{SUB}/resourceGroups/synthetic/providers/"
DCR = PREFIX + "Microsoft.Insights/dataCollectionRules/synthetic"
LAW = PREFIX + "Microsoft.OperationalInsights/workspaces/synthetic"


def state():
    return {"subscription_id": SUB, "workspace_customer_id": WORKSPACE,
            "dcr_resource_id": DCR, "workspace_resource_id": LAW}


def module():
    return importlib.import_module("scripts.session_workbook")


def queries():
    return {item["name"]: item["content"] for item in module().build_workbook(state())["items"]
            if item["type"] == 3}


def payload(name, empty=False):
    columns = module().PANEL_COLUMNS[name]
    values = {
        "RunId": RUN, "ConversationId": CONVERSATION, "TraceId": TRACE,
        "SessionKey": "conversation:" + CONVERSATION,
        "SpanId": "b" * 16, "ParentSpanId": "", "Scenario": "delegated",
        "Model": "synthetic-model", "Models": '["synthetic-model"]',
        "ToolName": "synthetic-tool", "ToolType": "function",
        "EventName": "github.copilot.user.message", "Operation": "chat",
        "Start": "2026-09-13T15:00:00Z", "End": "2026-09-13T15:01:00Z",
        "TimeGenerated": "2026-09-13T15:00:00Z",
        "SpanKey": TRACE + "/b", "ParentKey": "", "StatusCode": "",
        "Success": True, "Failed": False,
    }
    return {"tables": [{"name": "PrimaryResult",
                        "columns": [{"name": c, "type": "string" if c in values else "long"}
                                    for c in columns],
                        "rows": [] if empty else [[values.get(c, 2) for c in columns]]}]}


class WorkbookTests(unittest.TestCase):
    def test_public_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.session_workbook"),
                             "The requested Workbook module is not implemented")

    def test_notebook_is_serializable_and_targets_law_not_app(self):
        workbook = module().build_workbook(state())
        self.assertEqual(workbook["version"], "Notebook/1.0")
        self.assertEqual(json.loads(json.dumps(workbook)), workbook)
        self.assertEqual(workbook["fallbackResourceIds"], [LAW])
        self.assertNotIn("properties", workbook)
        self.assertEqual(set(queries()), {
            "overview", "sessions", "tokens", "latency", "tools", "spans", "events"})
        for content in queries().values():
            self.assertEqual(content["version"], "KqlItem/1.0")
            self.assertEqual(content["queryType"], 0)
            self.assertEqual(content["resourceType"], "microsoft.operationalinsights/workspaces")
            self.assertEqual(content["crossComponentResources"], [LAW])
            self.assertNotIn("timeContextFromParameter", content)
            self.assertNotIn("timeContext", content)

    def test_invalid_missing_or_cross_subscription_scope_is_rejected_before_io(self):
        bad = []
        for key in state():
            value = state()
            del value[key]
            bad.append(value)
        for key, value in (
                ("subscription_id", "not-a-uuid"),
                ("workspace_customer_id", WORKSPACE + "'; union *"),
                ("workspace_resource_id", DCR),
                ("dcr_resource_id", LAW),
                ("workspace_resource_id", LAW.replace(SUB, WORKSPACE)),
                ("dcr_resource_id", DCR.replace(SUB, WORKSPACE)),
                ("dcr_resource_id", DCR + '"/query'),
                ("dcr_resource_id", DCR + "\n| union *"),
                ("workspace_resource_id", LAW + "?api-version=bad")):
            bad.append(dict(state(), **{key: value}))
        with patch("scripts.session_workbook.run_json") as run:
            for value in bad:
                with self.subTest(value=value):
                    with self.assertRaises(AppError):
                        module().build_workbook(value)
                    with self.assertRaises(AppError):
                        module().validate_queries(value)
            run.assert_not_called()

    def test_native_templates_preserve_json_braces_and_scope_every_table(self):
        for content in queries().values():
            query = content["query"]
            for expected in ("OTelSpans", "OTelResources", '_ResourceId == ""',
                             "ago(24h)", "{TimeRange}", "dynamic({})",
                             "arg_max(TimeGenerated", "by TraceId, SpanId",
                             'ResourceAttributes["service.name"]', '"github-copilot"'):
                self.assertIn(expected, query)
            for forbidden in ("AppTraces", "AppDependencies", "AppMetrics", "union *",
                              "workspace(", "app(", "isfuzzy", "__DCR_RESOURCE_ID__",
                              "Microsoft.Insights/components", "OTelMetrics", DCR,
                              "let resource_id", "_ResourceId =~"):
                self.assertNotIn(forbidden, query)
        event = queries()["events"]["query"]
        self.assertIn("OTelEvents", event)
        self.assertIn("join kind=inner", event)
        self.assertIn("on TraceId, SpanId", event)
        self.assertIn("| project TimeGenerated, EventName", event)

    def test_every_native_table_requires_empty_resource_association(self):
        for name, content in queries().items():
            with self.subTest(panel=name):
                self.assertEqual(content["query"].count('| where _ResourceId == ""'),
                                 3 if name == "events" else 2)
                self.assertNotIn("isempty(_ResourceId)", content["query"])
        text = module().NOTES
        self.assertIn("empty `_ResourceId`", text)
        self.assertIn("DCR IDs are endpoint provenance", text)
        self.assertNotIn("scoped to the configured DCR", text)

    def test_ordinary_cli_needs_service_but_not_harness_resource_attributes(self):
        for name, content in queries().items():
            with self.subTest(panel=name):
                query = content["query"]
                self.assertIn('| where tostring(ResourceAttributes["service.name"]) == "github-copilot"',
                              query)
                self.assertNotIn("where isnotempty(RunId)", query)
                self.assertNotIn("Scenario in (", query)
                self.assertIn("where isempty(selected_run) or RunId == selected_run", query)
        self.assertNotIn("synthetic-only", module().NOTES)
        self.assertIn("optional", module().NOTES)
        self.assertNotIn("synthetic", module().TITLES["sessions"])

    def test_sessions_use_conversation_or_trace_without_inventing_conversation_identity(self):
        query = queries()["sessions"]["query"]
        self.assertIn("SessionKey", module().PANEL_COLUMNS["sessions"])
        self.assertIn('isnotempty(ConversationId), strcat("conversation:", ConversationId)', query)
        self.assertIn('isnotempty(TraceId), strcat("trace:", TraceId)', query)
        self.assertIn("by SessionKey, RunId, ConversationId, Scenario", query)
        self.assertNotIn("ConversationId = TraceId", query)
        self.assertNotIn("ConversationId = RunId", query)
        self.assertIn("SpansMissingConversation", queries()["overview"]["query"])

    def test_resource_enrichment_is_deduplicated_optional_and_not_span_time_bounded(self):
        query = queries()["sessions"]["query"]
        resources = query.split("let Resources =", 1)[1].split("let Spans =", 1)[0]
        self.assertIn("where isnotempty(Id)", resources)
        self.assertIn("where Id in (WindowSpans | project ResourceAttributesId)", resources)
        self.assertIn("arg_max(TimeGenerated, Attributes) by Id", resources)
        self.assertNotIn("ago(", resources)
        self.assertNotIn("{TimeRange}", resources)
        self.assertIn("join kind=leftouter Resources on ResourceAttributesId", query)
        self.assertIn("coalesce(ResourceAttributes, dynamic({}))", query)
        self.assertIn("coalesce(JoinedResourceAttributes, dynamic({}))", query)

    def test_invalid_durations_are_null_and_missing_durations_are_visible(self):
        for name in ("overview", "latency", "tools"):
            self.assertIn("SpansMissingDuration", module().PANEL_COLUMNS[name])
            self.assertIn("SpansMissingDuration = countif(isnull(DurationMs))",
                          queries()[name]["query"])
        query = queries()["spans"]["query"]
        self.assertIn("isfinite(todouble(DurationMs))", query)
        self.assertIn("todouble(DurationMs) >= 0", query)
        self.assertIn("real(null)", query)
        self.assertIn("EndTime >= TimeGenerated", query)
        self.assertIn("datetime(null)", query)
        self.assertIn("SpansMissingEndTime", module().PANEL_COLUMNS["sessions"])
        self.assertIn("SpansMissingEndTime == 0", queries()["sessions"]["query"])

    def test_actual_conversations_chat_only_tokens_and_missing_data_are_explicit(self):
        query = queries()["sessions"]["query"]
        self.assertIn('ConversationId = tostring(Attributes["gen_ai.conversation.id"])', query)
        self.assertIn('RunId = tostring(ResourceAttributes["copilot.run.id"])', query)
        self.assertIn("by SessionKey, RunId, ConversationId, Scenario", query)
        self.assertIn('InputTokens = iff(Operation == "chat",', query)
        self.assertIn('OutputTokens = iff(Operation == "chat",', query)
        self.assertIn('Attributes["gen_ai.usage.input_tokens"]', query)
        self.assertIn('Attributes["gen_ai.usage.output_tokens"]', query)
        self.assertIn("InputTokens = sum(InputTokens)", query)
        self.assertIn("ChatSpansMissingTokens", query)
        self.assertIn("WallTimeMs", query)
        self.assertNotIn("ConversationId = RunId", query)
        self.assertIn('| where Operation == "chat"', queries()["tokens"]["query"])
        self.assertIn('| where Operation == "chat"', queries()["latency"]["query"])
        self.assertIn('Operation == "execute_tool"', queries()["tools"]["query"])
        for content in queries().values():
            self.assertNotIn('Attributes["gen_ai.input.messages"]', content["query"])
            self.assertNotIn('Attributes["gen_ai.tool.call.arguments"]', content["query"])

    def test_optional_filters_are_base64_encoded_not_raw_kql_interpolation(self):
        workbook = module().build_workbook(state())
        parameters = next(i["content"]["parameters"] for i in workbook["items"] if i["type"] == 9)
        params = {p["name"]: p for p in parameters}
        self.assertEqual(params["TimeRange"]["value"], {"durationMs": 86400000})
        for name in ("ConversationId", "RunId", "TraceId"):
            self.assertEqual(params[name]["value"], "")
            self.assertFalse(params[name]["isRequired"])
            for content in queries().values():
                self.assertIn("base64_decode_tostring('{" + name + ":base64}')", content["query"])
                self.assertNotIn("{" + name + "}", content["query"])
        hostile = "\"'; union * //\n{TimeRange}\\"
        query = module()._bind_query(queries()["sessions"]["query"],
                                     {"ConversationId": hostile})
        self.assertNotIn(hostile, query)
        self.assertNotIn("{ConversationId:base64}", query)
        self.assertIn("dynamic({})", query)
        self.assertNotIn("{TimeRange}", query)

    def test_visualizations_parent_links_and_limitations_are_defined(self):
        self.assertEqual(queries()["tokens"]["visualization"], "barchart")
        self.assertEqual(queries()["latency"]["visualization"], "timechart")
        spans = queries()["spans"]
        hierarchy = spans["gridSettings"]["hierarchySettings"]
        self.assertEqual(hierarchy["idColumn"], "SpanKey")
        self.assertEqual(hierarchy["parentColumn"], "ParentKey")
        for col in ("ParentSpanId", "DurationMs", "OffsetMs", "SpanKey", "ParentKey"):
            self.assertIn(col, spans["query"])
        text = "\n".join(i["content"]["json"] for i in module().build_workbook(state())["items"]
                         if i["type"] == 1)
        for phrase in ("ordinary CLI", "SessionKey", "underreport", "no cost estimates", "not a secret leak",
                       "subagent", "observed span errors", "ConversationId", "RunId", "TraceId",
                       "missing", "24 hours", "chat", "span summaries", "not a metrics",
                       "arrive later", "DCR"):
            self.assertIn(phrase, text)


class ValidationTests(unittest.TestCase):
    def validate(self, override=None):
        def response(args):
            body = json.loads(args[args.index("--body") + 1])
            name = body["query"].split("// panel: ", 1)[1].splitlines()[0]
            return override(name) if override else payload(name)
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "queries.json"
            with patch("scripts.session_workbook.run_json", side_effect=response) as run:
                report = module().validate_queries(state(), output_path=evidence)
            stored = json.loads(evidence.read_text())
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
            return report, run.call_args_list, stored

    def test_executes_all_panels_and_selected_drilldowns_and_keeps_rows_private(self):
        report, calls, stored = self.validate()
        self.assertTrue(report["ok"])
        self.assertEqual(set(report["panels"]), set(module().PANEL_COLUMNS))
        self.assertEqual(set(report["selection_checks"]), {"spans", "events"})
        self.assertEqual(len(calls), 9)
        self.assertNotIn(CONVERSATION, json.dumps(report))
        self.assertNotIn(TRACE, json.dumps(report))
        self.assertIn(CONVERSATION, json.dumps(stored))
        for call in calls:
            args = call.args[0]
            self.assertEqual(args[:4], ["az", "rest", "--method", "POST"])
            self.assertEqual(args[args.index("--url") + 1],
                             f"https://api.loganalytics.azure.com/v1/workspaces/{WORKSPACE}/query")
            self.assertEqual(args[args.index("--resource") + 1], "https://api.loganalytics.io")
            self.assertEqual(args[args.index("--subscription") + 1], SUB)
            body = json.loads(args[args.index("--body") + 1])
            self.assertNotIn("timespan", body)
            self.assertNotIn(DCR, body["query"])
            self.assertIn('| where _ResourceId == ""', body["query"])
            self.assertNotIn("{TimeRange}", body["query"])
        for name, result in report["panels"].items():
            self.assertGreater(result["row_count"], 0)
            self.assertEqual(result["columns"], list(module().PANEL_COLUMNS[name]))

    def test_errors_partial_empty_and_malformed_responses_are_not_success(self):
        def malformed(name):
            data = payload(name)
            data["tables"][0]["rows"][0].pop()
            return data
        variants = [
            lambda n: {"error": {"code": "PartialError", "message": "private prompt"}},
            lambda n: dict(payload(n), partialError={"message": "private prompt"}),
            lambda n: dict(payload(n), warnings=["private prompt"]),
            lambda n: {"tables": []},
            lambda n: {"tables": [{"columns": [], "rows": [[]]}]},
            lambda n: payload(n, empty=True),
            malformed,
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                report, _, _ = self.validate(variant)
                self.assertFalse(report["ok"])
                self.assertTrue(all(not p["ok"] for p in report["panels"].values()))
                self.assertNotIn("private prompt", json.dumps(report))

    def test_ordinary_cli_drilldown_validation_does_not_require_a_run_or_conversation(self):
        for conversation in (CONVERSATION, ""):
            def ordinary(name):
                data = payload(name)
                columns = module().PANEL_COLUMNS[name]
                for field, value in (("RunId", ""), ("Scenario", ""),
                                     ("ConversationId", conversation)):
                    if field in columns:
                        data["tables"][0]["rows"][0][columns.index(field)] = value
                return data
            with self.subTest(conversation=conversation):
                report, calls, stored = self.validate(ordinary)
                self.assertTrue(report["ok"])
                self.assertEqual(len(calls), 9)
                for name in ("spans", "events"):
                    self.assertTrue(report["selection_checks"][name]["ok"])
                    selected = stored["queries"]["selected_" + name]["query"]
                    self.assertIn("let selected_run = base64_decode_tostring('');", selected)

    def test_drilldown_still_requires_trace_identity_and_rejects_wrong_selected_trace(self):
        def no_trace(name):
            data = payload(name)
            if name == "events":
                data["tables"][0]["rows"][0][module().PANEL_COLUMNS[name].index("TraceId")] = ""
            return data
        report, calls, _ = self.validate(no_trace)
        self.assertFalse(report["ok"])
        self.assertEqual(len(calls), 7)
        self.assertEqual(report["selection_checks"]["spans"]["error"], "no_correlated_selection")
        counts = {}
        def wrong_trace(name):
            data = payload(name)
            counts[name] = counts.get(name, 0) + 1
            if name == "spans" and counts[name] == 2:
                data["tables"][0]["rows"][0][module().PANEL_COLUMNS[name].index("TraceId")] = "c" * 32
            return data
        report, _, _ = self.validate(wrong_trace)
        self.assertFalse(report["ok"])
        self.assertEqual(report["selection_checks"]["spans"]["error"], "selection_mismatch")

    def test_zero_activity_overview_cannot_pass_on_aggregate_row_alone(self):
        def zero(name):
            data = payload(name)
            if name == "overview":
                index = module().PANEL_COLUMNS[name].index("ObservedSpans")
                data["tables"][0]["rows"][0][index] = 0
            return data
        report, _, _ = self.validate(zero)
        self.assertFalse(report["ok"])
        self.assertEqual(report["panels"]["overview"]["error"], "no_observed_spans")

    def test_cli_command_failures_are_fixed_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("scripts.session_workbook.run_json", side_effect=AppError("private prompt")):
                report = module().validate_queries(state(), output_path=Path(directory) / "q.json")
        self.assertFalse(report["ok"])
        self.assertNotIn("private prompt", json.dumps(report))
        self.assertTrue(all(p["error"] == "query_failed" for p in report["panels"].values()))

    def test_cli_reads_native_state_returns_failure_and_prints_only_safe_report(self):
        with patch("scripts.session_workbook.load_json", return_value=state()) as load:
            with patch("scripts.session_workbook.validate_queries",
                       return_value={"ok": False, "panels": {}}):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(module().main(["--validate"]), 1)
            self.assertEqual(load.call_args.args[0].name, "native-azure.json")
            self.assertEqual(json.loads(output.getvalue()), {"ok": False, "panels": {}})


if __name__ == "__main__":
    unittest.main()
