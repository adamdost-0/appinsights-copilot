"""Offline native backend contract tests. No CLI inference or Azure calls."""

from copy import deepcopy
from datetime import datetime
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts import verify_native as native
from scripts.common import AppError

RUN = "00000000-0000-4000-8000-000000000001"
NEGATIVE = "00000000-0000-4000-8000-000000000002"
SUB = "00000000-0000-4000-8000-000000000003"
WORKSPACE = "00000000-0000-4000-8000-000000000004"
PREFIX = f"/subscriptions/{SUB}/resourceGroups/synthetic/providers/"
APP = PREFIX + "Microsoft.Insights/components/synthetic"
DCR = PREFIX + "Microsoft.Insights/dataCollectionRules/synthetic"
IMMUTABLE = "dcr-" + "a" * 32
TRACE = "a" * 32
ROOT_SPAN = "b" * 16
CHAT_SPAN = "c" * 16
TOOL_SPAN = "d" * 16
CHILD_SPAN = "e" * 16
START = "2026-09-13T15:00:00+00:00"
FINISH = "2026-09-13T15:01:00+00:00"


def state():
    base = f"https://synthetic.eastus-1.ingest.monitor.azure.com/datacollectionRules/{IMMUTABLE}/streams/"
    return {
        "subscription_id": SUB, "workspace_customer_id": WORKSPACE,
        "dcr_resource_id": DCR, "dcr_immutable_id": IMMUTABLE,
        "application_insights_resource_id": APP,
        "workspace_resource_id": PREFIX + "Microsoft.OperationalInsights/workspaces/synthetic",
        "azure_monitor_workspace_resource_id": PREFIX + "Microsoft.Monitor/accounts/synthetic",
        "traces_endpoint": base + "Microsoft-OTLP-Traces/otlp/v1/traces",
        "metrics_endpoint": base + "Custom-Metrics-Native/otlp/v1/metrics",
        "metrics_query_endpoint": "https://synthetic.eastus.prometheus.monitor.azure.com",
    }


def manifest(scenario="metadata-only"):
    return {
        "run_id": RUN, "collector_mode": "native-azure", "scenario": scenario,
        "capture_content": scenario != "metadata-only", "marker": f"SYNTHETIC_AUDIT_{RUN}",
        "started_at": START, "finished_at": FINISH, "exit_code": 0,
        "status": "awaiting_verification", "cli_version": "GitHub Copilot CLI 1.0.84-5.",
        "dcr_resource_id": DCR, "application_insights_resource_id": APP,
    }


def resource(scenario="metadata-only"):
    return {"copilot.run.id": RUN, "copilot.audit.scenario": scenario,
            "service.name": "github-copilot", "service.version": "1.0.84-5"}


def span(operation, span_id, parent="", scenario="metadata-only"):
    return {
        "Table": "OTelSpans", "Name": operation + " synthetic", "Kind": "Internal",
        "StatusCode": "OK", "Success": True, "TraceId": TRACE, "SpanId": span_id,
        "ParentSpanId": parent, "Attributes": {"gen_ai.operation.name": operation},
        "ResourceAttributes": resource(scenario), "_ResourceId": APP,
        "TimeGenerated": START, "EndTime": FINISH, "DurationMs": 60000.0,
    }


def rows(scenario="metadata-only"):
    result = [span("invoke_agent", ROOT_SPAN, scenario=scenario),
              span("chat", CHAT_SPAN, ROOT_SPAN, scenario)]
    if scenario != "metadata-only":
        marker = manifest(scenario)["marker"]
        result[0]["Attributes"].update({"gen_ai.input.messages": marker,
                                         "gen_ai.output.messages": marker})
        result.append(span("execute_tool", TOOL_SPAN, ROOT_SPAN, scenario))
        result[-1]["Attributes"].update({"gen_ai.tool.call.arguments": marker,
                                          "gen_ai.tool.call.result": marker})
    if scenario == "delegated":
        result.append(span("invoke_agent", CHILD_SPAN, TOOL_SPAN, scenario))
    return result


def trace_payload(items=None):
    items = rows() if items is None else items
    columns = list(rows()[0])
    return {"tables": [{"name": "PrimaryResult",
                        "columns": [{"name": key, "type": "dynamic"} for key in columns],
                        "rows": [[item[key] for key in columns] for item in items]}]}


def prometheus(value="2", labels=None):
    labels = resource() if labels is None else labels
    labels = {"microsoft.appresourceid": APP,
              "microsoft.amwresourceid": state()["azure_monitor_workspace_resource_id"], **labels}
    result = [] if value is None else [{"metric": labels,
                                        "value": [datetime.fromisoformat(FINISH).timestamp(), value]}]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def metric_payloads(scenario="metadata-only"):
    names = ["gen_ai.client.token.usage", "gen_ai.client.operation.duration",
             "gen_ai.invoke_agent.duration"]
    if scenario != "metadata-only":
        names.append("gen_ai.execute_tool.duration")
    return {name: {"count": prometheus("2", resource(scenario)),
                   "sum": prometheus("3.5", resource(scenario))} for name in names}


class ContractTests(unittest.TestCase):
    def test_manual_dcr_state_requires_no_instrumentation_key(self):
        self.assertEqual(native.validate_state(state()), state())
        self.assertEqual(native.validate_manifest(manifest(), RUN, state()), manifest())

    def test_wrong_run_resource_mode_or_unfinished_manifest_is_rejected(self):
        for key, value in (
                ("run_id", NEGATIVE), ("collector_mode", "azure"), ("collector_mode", "local-only"),
                ("application_insights_resource_id", APP + "wrong"),
                ("dcr_resource_id", DCR + "wrong"), ("status", "running"),
                ("exit_code", False), ("exit_code", 1), ("finished_at", None),
                ("finished_at", "2026-09-12T15:00:00Z"), ("capture_content", True),
                ("marker", "fake"), ("cli_version", "unrecognized")):
            with self.subTest(key=key, value=value), self.assertRaises(AppError):
                native.validate_manifest(dict(manifest(), **{key: value}), RUN, state())

    def test_cross_subscription_state_and_nonazure_query_endpoints_are_rejected(self):
        for key, value in (
                ("workspace_resource_id", state()["workspace_resource_id"].replace(SUB, NEGATIVE)),
                ("azure_monitor_workspace_resource_id", APP),
                ("metrics_query_endpoint", "https://private.example/api"),
                ("metrics_query_endpoint", "http://synthetic.prometheus.monitor.azure.com"),
                ("metrics_query_endpoint", state()["metrics_query_endpoint"] + "?token=bad"),
                ("metrics_query_endpoint", state()["metrics_query_endpoint"] + "/api/v1/query")):
            with self.subTest(key=key), self.assertRaises(AppError):
                native.validate_state(dict(state(), **{key: value}))

    def test_native_kql_uses_exact_identity_deduped_resources_and_event_parent_join(self):
        query = native.build_query(manifest(), state())
        for value in ("OTelSpans", "OTelEvents", "OTelResources", "arg_max(TimeGenerated",
                      "ResourceAttributesId", "copilot.run.id", "service.name", "service.version",
                      RUN, APP, "1.0.84-5", "TraceId, SpanId", START, FINISH):
            self.assertIn(value, query)
        self.assertIn("kind=leftouter", query)
        self.assertIn("kind=inner", query)
        self.assertIn("SpanResourceAttributes = take_any(ResourceAttributes) by TraceId, SpanId", query)
        self.assertNotIn("distinct TraceId, SpanId, SpanResourceAttributes", query)
        self.assertIn("between (start_time .. finish_time)", query)
        for forbidden in ("AppDependencies", "AppTraces", "AppMetrics", "datatable(", "isfuzzy=true"):
            self.assertNotIn(forbidden, query)

    def test_resource_metadata_is_bounded_by_span_ids_not_its_later_timestamp(self):
        query = native.build_query(manifest(), state())
        resource_lookup = query.split("let Resources =", 1)[1].split("let RunSpans", 1)[0]
        self.assertIn("Id in (WindowSpans | project ResourceAttributesId)", resource_lookup)
        self.assertNotIn("TimeGenerated between", resource_lookup)
        window_spans = query.split("let WindowSpans =", 1)[1].split("let Resources", 1)[0]
        self.assertIn("TimeGenerated between (start_time .. finish_time)", window_spans)
        self.assertIn("_ResourceId =~ resource_id", window_spans)

    def test_promql_uses_dotted_base_names_quoted_labels_and_fixed_finish_time(self):
        name = "gen_ai.client.token.usage"
        url = native.metric_query_url(state(), manifest(), name, "count")
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/api/v1/query")
        self.assertEqual(query["time"], [FINISH])
        self.assertEqual(query["query"], [
            f'histogram_count({{__name__="{name}","copilot.run.id"="{RUN}",'
            '"service.name"="github-copilot","copilot.audit.scenario"="metadata-only"})'])
        for suffix in ("_count\"", "_sum\"", "_bucket\""):
            self.assertNotIn(suffix, query["query"][0])
        self.assertIn(NEGATIVE, parse_qs(urlsplit(native.metric_query_url(
            state(), manifest(), name, "sum", run_id=NEGATIVE)).query)["query"][0])

    def test_parses_named_native_columns_and_dynamic_json_strings(self):
        payload = trace_payload()
        index = [col["name"] for col in payload["tables"][0]["columns"]].index("Attributes")
        payload["tables"][0]["rows"][0][index] = json.dumps(rows()[0]["Attributes"])
        parsed = native.parse_trace_response(payload)
        self.assertEqual(parsed, rows())

    def test_empty_table_is_pending_data_but_absent_table_is_malformed(self):
        self.assertEqual(native.parse_trace_response(trace_payload([])), [])
        for value in ('{"broken":', {}, {"tables": []}, {"tables": "bad"}):
            with self.subTest(value=value), self.assertRaises(AppError):
                native.parse_trace_response(value)

    def test_malformed_columns_rows_and_dynamic_json_are_rejected(self):
        for mutate in (
                lambda table: table["columns"].append(table["columns"][0]),
                lambda table: table["rows"][0].pop(),
                lambda table: table["rows"][0].__setitem__(0, "AppDependencies"),
                lambda table: table["rows"][0].__setitem__(0, ["OTelSpans"]),
                lambda table: table["rows"][0].__setitem__(8, "{invalid")):
            payload = trace_payload()
            mutate(payload["tables"][0])
            with self.assertRaises(AppError):
                native.parse_trace_response(payload)

    def test_query_errors_distinguish_auth_partial_missing_tables_and_transient(self):
        for message, retryable in (("403 Forbidden", False), ("401 Unauthorized 503", False),
                                   ("PartialError 503", False), ("429 TooManyRequests", True),
                                   ("503 ServiceUnavailable", True), ("501 server error", True),
                                   ("failed to resolve table OTelSpans", True),
                                   ("Failed to resolve table or column expression named 'OTelEvents'", True),
                                   ("Table 'OTelResources' does not exist", True),
                                   ("Failed to resolve column expression named 'UnknownField' in table OTelSpans", False),
                                   ("failed to resolve column bogus", False),
                                   ("connection timed out", False)):
            with self.subTest(message=message):
                self.assertEqual(native.query_error(message).retryable, retryable)
        for payload in (
                {"error": {"code": "PartialError"}, **trace_payload()},
                dict(prometheus(), warnings=["partial histogram data"]),
                dict(prometheus(), status="error", error="Unauthorized")):
            with self.assertRaises(AppError):
                if "tables" in payload:
                    native.parse_trace_response(payload)
                else:
                    native.parse_metric_response(payload, manifest())

    def test_metric_values_must_be_finite_nonnegative_and_identity_exact(self):
        for value in ("NaN", "Inf", "-1", True, None, "not-number"):
            payload = prometheus()
            payload["data"]["result"][0]["value"][1] = value
            with self.subTest(value=value), self.assertRaises(AppError):
                native.parse_metric_response(payload, manifest())
        for key, value in (("copilot.run.id", NEGATIVE), ("service.name", "other"),
                           ("service.version", "9.9.9"), ("copilot.audit.scenario", "delegated")):
            with self.subTest(key=key), self.assertRaises(AppError):
                native.parse_metric_response(prometheus(labels=dict(resource(), **{key: value})), manifest())

    def test_metric_matrix_and_stale_evaluation_timestamp_are_rejected(self):
        payload = prometheus()
        payload["data"]["resultType"] = "matrix"
        with self.assertRaises(AppError):
            native.parse_metric_response(payload, manifest())
        payload = prometheus()
        payload["data"]["result"][0]["value"][0] -= 600
        with self.assertRaises(AppError):
            native.parse_metric_response(payload, manifest())


class ProofTests(unittest.TestCase):
    def assess(self, scenario="metadata-only", *, trace_rows=None, metrics=None, negative=None):
        return native.assess(
            manifest(scenario), state(), rows(scenario) if trace_rows is None else trace_rows,
            metric_payloads(scenario) if metrics is None else metrics,
            prometheus(None) if negative is None else negative, NEGATIVE)

    def test_real_native_rows_and_each_required_metric_establish_proof(self):
        for scenario in ("metadata-only", "full-content", "delegated"):
            with self.subTest(scenario=scenario):
                result = self.assess(scenario)
                self.assertEqual(result["status"], "passed")
                self.assertTrue(result["azure_ingestion_proven"])
                self.assertEqual(result["counts"]["metric_families"], 3 if scenario == "metadata-only" else 4)
                self.assertTrue(result["checks"]["trace_persistence"])
                self.assertTrue(result["checks"]["metric_persistence"])
                self.assertTrue(result["checks"]["negative_control"])
                self.assertNotIn(manifest()["marker"], json.dumps(result))

    def test_empty_rows_never_prove_absence_privacy_or_ingestion(self):
        result = self.assess(trace_rows=[])
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["azure_ingestion_proven"])
        self.assertFalse(result["native_cli_content_privacy_verified"])

    def test_missing_metrics_or_zero_only_families_are_pending_not_azure_rejection(self):
        for metrics in ({}, {METRIC: value for METRIC, value in metric_payloads().items()
                             if METRIC != "gen_ai.client.token.usage"}):
            result = self.assess(metrics=metrics)
            self.assertEqual(result["status"], "pending")
            self.assertFalse(result["azure_ingestion_proven"])
        metrics = metric_payloads()
        metrics["gen_ai.client.token.usage"]["sum"] = prometheus("0")
        self.assertFalse(self.assess(metrics=metrics)["azure_ingestion_proven"])

    def test_metadata_leak_fails_privacy_but_preserves_true_ingestion_proof(self):
        for field in ("gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
                      "gen_ai.tool.definitions", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"):
            evidence = rows()
            evidence[0]["Attributes"][field] = "synthetic-private-text"
            with self.subTest(field=field):
                result = self.assess(trace_rows=evidence)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["azure_ingestion_proven"])
                self.assertFalse(result["native_cli_content_privacy_verified"])
                self.assertIn("metadata_content_fields_present", result["reasons"])
                self.assertNotIn("synthetic-private-text", json.dumps(result))

    def test_metadata_nested_resource_event_and_metric_content_are_checked(self):
        for location in ("resource", "event", "metric"):
            evidence, metrics = rows(), metric_payloads()
            if location == "resource":
                evidence[0]["ResourceAttributes"]["gen_ai"] = {"tool": {"definitions": []}}
            elif location == "event":
                event = deepcopy(evidence[0])
                event.update(Table="OTelEvents", Kind="", StatusCode="", Success=None,
                             ParentSpanId="", EndTime=None, DurationMs=None)
                event["Attributes"] = {"gen_ai.tool.definitions": "synthetic"}
                evidence.append(event)
            else:
                for statistic in ("count", "sum"):
                    metrics["gen_ai.client.token.usage"][statistic]["data"]["result"][0]["metric"][
                        "gen_ai.tool.definitions"] = "synthetic"
            with self.subTest(location=location):
                result = self.assess(trace_rows=evidence, metrics=metrics)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["azure_ingestion_proven"])

    def test_content_missing_does_not_erase_successful_native_ingestion(self):
        evidence = rows("full-content")
        del evidence[-1]["Attributes"]["gen_ai.tool.call.result"]
        result = self.assess("full-content", trace_rows=evidence)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["azure_ingestion_proven"])
        self.assertFalse(result["checks"]["content"])

    def test_tool_arguments_and_result_must_share_actual_tool_span(self):
        evidence = rows("full-content")
        evidence[0]["Attributes"]["gen_ai.tool.call.result"] = evidence[-1]["Attributes"].pop(
            "gen_ai.tool.call.result")
        self.assertEqual(self.assess("full-content", trace_rows=evidence)["status"], "partial")

    def test_event_content_can_be_correlated_to_its_actual_span(self):
        evidence = rows("full-content")
        event = deepcopy(evidence[-1])
        event.update(Table="OTelEvents", Kind="", StatusCode="", Success=None,
                     ParentSpanId="", EndTime=None, DurationMs=None)
        event["Attributes"] = {"gen_ai.tool.call.result": evidence[-1]["Attributes"].pop("gen_ai.tool.call.result")}
        evidence.append(event)
        self.assertEqual(self.assess("full-content", trace_rows=evidence)["status"], "passed")

    def test_wrong_native_identity_is_fatal_not_silently_filtered(self):
        for key, value in (("_ResourceId", APP + "-wrong"), ("TimeGenerated", "2026-09-12T15:00:00Z"),
                           ("TraceId", "invalid"), ("SpanId", "0" * 16)):
            evidence = rows()
            evidence[0][key] = value
            with self.subTest(key=key):
                result = self.assess(trace_rows=evidence)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["azure_ingestion_proven"])
        for key, value in (("copilot.run.id", NEGATIVE), ("service.name", "fake"),
                           ("service.version", "9.9.9"), ("copilot.audit.scenario", "delegated")):
            evidence = rows()
            evidence[0]["ResourceAttributes"][key] = value
            self.assertEqual(self.assess(trace_rows=evidence)["status"], "failed")

    def test_missing_parent_is_pending_but_cycles_and_conflicting_ids_are_fatal(self):
        evidence = rows()
        evidence[1]["ParentSpanId"] = TOOL_SPAN
        self.assertEqual(self.assess(trace_rows=evidence)["status"], "pending")
        evidence[0]["ParentSpanId"] = CHAT_SPAN
        evidence[1]["ParentSpanId"] = ROOT_SPAN
        self.assertEqual(self.assess(trace_rows=evidence)["status"], "failed")
        evidence = rows()
        evidence.append(dict(evidence[0], Name="conflicting span"))
        self.assertEqual(self.assess(trace_rows=evidence)["status"], "failed")

    def test_event_cannot_claim_an_unrelated_or_missing_parent_span(self):
        evidence = rows()
        event = deepcopy(evidence[0])
        event.update(Table="OTelEvents", TraceId="f" * 32, Kind="", StatusCode="", Success=None,
                     ParentSpanId="", EndTime=None, DurationMs=None)
        evidence.append(event)
        self.assertFalse(self.assess(trace_rows=evidence)["azure_ingestion_proven"])

    def test_delegation_requires_two_invocations_with_ancestry_not_merely_two_roots(self):
        evidence = rows("delegated")
        evidence[-1]["ParentSpanId"] = ""
        result = self.assess("delegated", trace_rows=evidence)
        self.assertFalse(result["azure_ingestion_proven"])
        self.assertEqual(result["counts"]["delegated_agent_children"], 0)
        result = self.assess("delegated")
        self.assertEqual(result["counts"]["delegated_agent_children"], 1)

    def test_chat_and_tools_require_invocation_ancestor(self):
        for scenario, index in (("metadata-only", 1), ("full-content", 2)):
            evidence = rows(scenario)
            evidence[index]["ParentSpanId"] = ""
            self.assertFalse(self.assess(scenario, trace_rows=evidence)["azure_ingestion_proven"])

    def test_success_fields_and_span_duration_are_not_invented(self):
        for key, value in (("Success", False), ("StatusCode", "Error"), ("DurationMs", -1)):
            evidence = rows()
            evidence[0][key] = value
            self.assertNotEqual(self.assess(trace_rows=evidence)["status"], "passed")

    def test_negative_never_emitted_uuid_must_return_no_data(self):
        negative_labels = dict(resource(), **{"copilot.run.id": NEGATIVE})
        result = self.assess(negative=prometheus("1", negative_labels))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["azure_ingestion_proven"])

    def test_count_sum_pairs_cannot_cross_dimensions_or_sum_cumulative_snapshots(self):
        metrics = metric_payloads()
        metrics["gen_ai.client.token.usage"]["count"] = prometheus("2", dict(resource(), direction="input"))
        metrics["gen_ai.client.token.usage"]["sum"] = prometheus("3", dict(resource(), direction="output"))
        self.assertFalse(self.assess(metrics=metrics)["azure_ingestion_proven"])
        metrics = metric_payloads()
        sample = metrics["gen_ai.client.token.usage"]["count"]["data"]["result"][0]
        metrics["gen_ai.client.token.usage"]["count"]["data"]["result"].append(deepcopy(sample))
        self.assertEqual(self.assess(metrics=metrics)["status"], "failed")

    def test_fractional_negative_or_nonfinite_histogram_counts_are_rejected(self):
        for value in ("1.5", "-1", "NaN"):
            metrics = metric_payloads()
            metrics["gen_ai.client.token.usage"]["count"] = prometheus(value)
            result = self.assess(metrics=metrics)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["azure_ingestion_proven"])

    def test_native_metric_azure_resource_labels_must_match_state(self):
        for label in ("microsoft.appresourceid", "microsoft.amwresourceid"):
            for value in (None, APP + "-wrong"):
                metrics = metric_payloads()
                for statistic in ("count", "sum"):
                    labels = metrics["gen_ai.client.token.usage"][statistic]["data"]["result"][0]["metric"]
                    if value is None:
                        del labels[label]
                    else:
                        labels[label] = value
                with self.subTest(label=label, value=value):
                    result = self.assess(metrics=metrics)
                    self.assertEqual(result["status"], "failed")
                    self.assertFalse(result["azure_ingestion_proven"])

    def test_native_metric_azure_resource_identity_is_case_insensitive(self):
        metrics = metric_payloads()
        for family in metrics.values():
            for payload in family.values():
                for sample in payload["data"]["result"]:
                    for key in ("microsoft.appresourceid", "microsoft.amwresourceid"):
                        sample["metric"][key] = sample["metric"][key].lower()
        self.assertEqual(self.assess(metrics=metrics)["status"], "passed")


class QueryTests(unittest.TestCase):
    def fake_rest(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.assertEqual(command[:2], ["az", "rest"])
        self.assertEqual(command[command.index("--subscription") + 1], SUB)
        self.assertEqual(kwargs["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"], "no")
        url = command[command.index("--url") + 1]
        audience = command[command.index("--resource") + 1]
        if "/workspaces/" in url:
            self.assertEqual(audience, "https://api.loganalytics.io")
            self.assertEqual(command[command.index("--method") + 1], "POST")
            body = json.loads(command[command.index("--body") + 1])
            self.assertNotIn("timespan", body)
            self.assertIn(f"datetime({START})", body["query"])
            self.assertIn(f"datetime({FINISH})", body["query"])
            self.assertIn("OTelSpans", body["query"])
            return trace_payload()
        self.assertEqual(audience, "https://prometheus.monitor.azure.com")
        self.assertEqual(command[command.index("--method") + 1], "GET")
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["time"], [FINISH])
        if NEGATIVE in params["query"][0]:
            return prometheus(None)
        return prometheus()

    def test_only_core_read_queries_with_explicit_audiences_and_subscription(self):
        self.calls = []
        with patch.object(native, "run_json", side_effect=self.fake_rest):
            raw = native.query_native(state(), manifest(), NEGATIVE, 30)
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(set(raw["metrics"]), set(native.METRICS))
        self.assertEqual(raw["negative"], prometheus(None))
        commands = json.dumps([command for command, _ in self.calls])
        for forbidden in ("get-access-token", "Bearer", "extension", "create", "deploy",
                          "https://monitor.azure.com/", "_bucket"):
            self.assertNotIn(forbidden, commands)

    def test_auth_failure_stops_immediately_without_returning_empty_success(self):
        with patch.object(native, "run_json", side_effect=AppError("403 Forbidden private details")) as query:
            with self.assertRaises(native.QueryError) as error:
                native.query_native(state(), manifest(), NEGATIVE, 30)
        self.assertFalse(error.exception.retryable)
        self.assertEqual(query.call_count, 1)
        self.assertNotIn("private details", str(error.exception))

    def test_partial_native_response_stops_before_more_queries(self):
        with patch.object(native, "run_json", return_value=dict(trace_payload(), partialError="bad")) as query:
            with self.assertRaises(native.QueryError):
                native.query_native(state(), manifest(), NEGATIVE, 30)
        self.assertEqual(query.call_count, 1)

    def test_query_rejects_nonnegative_control_identity_before_any_request(self):
        with patch.object(native, "run_json") as query:
            with self.assertRaises(AppError):
                native.query_native(state(), manifest(), RUN, 30)
        query.assert_not_called()

    def test_invalid_query_deadlines_are_refused_before_any_request(self):
        with patch.object(native, "run_json") as query:
            for timeout in (True, -1, 0, float("nan"), float("inf")):
                with self.subTest(timeout=timeout), self.assertRaises(AppError):
                    native.query_native(state(), manifest(), NEGATIVE, timeout)
        query.assert_not_called()

    def test_transient_failure_is_retryable_but_malformed_json_is_not(self):
        for detail, retryable in (("429 throttled", True), ("503 unavailable", True),
                                  ("az returned invalid JSON", False)):
            with patch.object(native, "run_json", side_effect=AppError(detail)):
                with self.assertRaises(native.QueryError) as error:
                    native.query_native(state(), manifest(), NEGATIVE, 30)
                self.assertEqual(error.exception.retryable, retryable)


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.local = Path(self.temporary.name) / ".local"
        self.folder = self.local / "runs" / RUN
        self.folder.mkdir(parents=True)
        self.patch = patch.object(native, "LOCAL", self.local)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.write_inputs()
        self.now = 0.0
        self.sleeps = []

    def write_inputs(self, scenario="metadata-only"):
        (self.local / "native-azure.json").write_text(json.dumps(state()))
        (self.folder / "manifest.json").write_text(json.dumps(manifest(scenario)))

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.assertGreater(seconds, 0)
        self.sleeps.append(seconds)
        self.now += seconds

    def raw(self, scenario="metadata-only"):
        return {"traces": trace_payload(rows(scenario)), "metrics": metric_payloads(scenario),
                "negative": prometheus(None)}

    def verify(self, query, timeout=10):
        return native.verify(RUN, timeout, query=query, clock=self.clock, sleep=self.sleep)

    def test_completed_native_proof_is_persisted_privately_without_source_evidence(self):
        with patch.object(native, "uuid4", return_value=NEGATIVE):
            result = self.verify(lambda *args: self.raw())
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["azure_ingestion_proven"])
        self.assertEqual(result["attempts"], 1)
        for filename in ("results.json", "raw-native-query.json"):
            path = self.folder / filename
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.folder / "results.json").read_text()), result)
        raw = json.loads((self.folder / "raw-native-query.json").read_text())
        self.assertEqual(raw["negative_run_id"], NEGATIVE)
        self.assertNotIn("evidence_path", raw)

    def test_empty_ingestion_then_success_retries_only_within_deadline(self):
        responses = [dict(self.raw(), traces=trace_payload([])), self.raw()]
        result = self.verify(lambda *args: responses.pop(0))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_privacy_failure_pending_metrics_still_measures_eventual_persistence(self):
        first = self.raw()
        content_index = [col["name"] for col in first["traces"]["tables"][0]["columns"]].index("Attributes")
        first["traces"]["tables"][0]["rows"][0][content_index]["gen_ai.tool.definitions"] = "synthetic-private"
        second = deepcopy(first)
        first["metrics"] = {}
        responses = [first, second]
        result = self.verify(lambda *args: responses.pop(0))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["azure_ingestion_proven"])
        self.assertEqual(result["attempts"], 2)
        self.assertNotIn("synthetic-private", json.dumps(result))

    def test_authentication_or_partial_failures_are_not_retried(self):
        for detail in ("403 Forbidden private-text", "401 Unauthorized", "PartialError 503"):
            self.sleeps.clear()
            with patch.object(native, "query_native", side_effect=AppError(detail)) as query:
                result = native.verify(RUN, 10, clock=self.clock, sleep=self.sleep)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["azure_ingestion_proven"])
            self.assertEqual(query.call_count, 1)
            self.assertEqual(self.sleeps, [])
            self.assertNotIn("private-text", json.dumps(result))

    def test_retryable_429_5xx_or_pending_native_table_can_recover(self):
        for detail in ("429", "503", "failed to resolve table OTelResources"):
            count = 0

            def query(*args):
                nonlocal count
                count += 1
                if count == 1:
                    raise AppError(detail)
                return self.raw()

            result = self.verify(query)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(count, 2)

    def test_malformed_backend_payload_fails_instead_of_retrying_empty_data(self):
        for payload in ({"traces": "not-json"}, dict(self.raw(), traces={"tables": []}),
                        dict(self.raw(), traces=dict(trace_payload(), partialError={"code": "bad"}))):
            with self.subTest(payload=payload):
                self.sleeps.clear()
                result = self.verify(lambda *args: payload)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["attempts"], 1)
                self.assertFalse(result["azure_ingestion_proven"])
                self.assertEqual(self.sleeps, [])

    def test_deadline_and_attempt_cap_leave_missing_metrics_unproven(self):
        result = self.verify(lambda *args: dict(self.raw(), metrics={}), timeout=3)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["azure_ingestion_proven"])
        self.assertIn("native_verification_deadline_exceeded", result["reasons"])
        self.assertLessEqual(self.now, 3)
        self.assertLessEqual(result["attempts"], native.MAX_ATTEMPTS)

    def test_metrics_time_stays_at_finish_even_when_polling_for_ten_minutes(self):
        times = []

        def query(s, m, negative_id, timeout):
            times.append(parse_qs(urlsplit(native.metric_query_url(
                s, m, native.METRICS[0], "count")).query)["time"][0])
            return dict(self.raw(), metrics={}) if self.now < 310 else self.raw()

        result = self.verify(query, timeout=600)
        self.assertEqual(result["status"], "passed")
        self.assertGreater(self.now, 300)
        self.assertEqual(set(times), {FINISH})

    def test_wrong_manifest_or_state_stops_before_any_query(self):
        value = manifest()
        value["dcr_resource_id"] += "-wrong"
        (self.folder / "manifest.json").write_text(json.dumps(value))
        with patch.object(native, "query_native") as query:
            result = native.verify(RUN, 10)
        self.assertEqual(result["status"], "failed")
        query.assert_not_called()

    def test_symlinked_run_folder_or_input_is_refused(self):
        original = self.folder / "manifest.json"
        target = self.folder / "other.json"
        original.rename(target)
        original.symlink_to(target)
        with patch.object(native, "query_native") as query:
            result = native.verify(RUN, 10)
        self.assertEqual(result["status"], "failed")
        query.assert_not_called()

    def test_response_evidence_size_is_bounded_and_failure_persistent(self):
        with patch.object(native, "MAX_RESPONSE_BYTES", 100):
            result = self.verify(lambda *args: self.raw())
        self.assertEqual(result["status"], "failed")
        self.assertTrue((self.folder / "results.json").is_file())
        self.assertFalse(result["azure_ingestion_proven"])

    def test_main_prints_only_safe_summary_and_exposes_no_fixture_mode(self):
        result = self.verify(lambda *args: self.raw())
        result["private_response"] = "do-not-print"
        with patch.object(native, "verify", return_value=result):
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(native.main(["--run-id", RUN]), 0)
        public = json.loads(output.getvalue())
        self.assertNotIn("private_response", public)
        self.assertNotIn("limitations", public)
        self.assertIn("counts", public)
        for option in ("--fixture", "--source-evidence", "--local-only"):
            with patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    native.main(["--run-id", RUN, option, "fake"])
            self.assertEqual(error.exception.code, 2)

    def test_invalid_timeout_or_uuid_never_queries(self):
        for timeout in (0, -1, True, float("nan"), float("inf"), 3601):
            with self.subTest(timeout=timeout), self.assertRaises(AppError):
                native.verify(RUN, timeout)
        with self.assertRaises(AppError):
            native.verify("../private", 10)


if __name__ == "__main__":
    unittest.main()
