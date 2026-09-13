"""Offline-only constructed telemetry; none of these fixtures came from a CLI run."""

import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.common import AppError


RUN = "01234567-89ab-4cde-8fab-0123456789ab"
TRACE = "1" * 32
AGENT = "2" * 16
CHAT = "3" * 16
TOOL = "4" * 16
CHILD = "5" * 16
MARKER = "SYNTHETIC_AUDIT_" + RUN
VERSION = "1.0.84-5"
OTHER_RUN = "abcdefab-1234-4abc-8def-abcdefabcdef"


def attrs(values):
    return [{"key": k, "value": {"stringValue": v}} for k, v in values.items()]


def manifest(scenario="metadata-only"):
    return {
        "run_id": RUN, "scenario": scenario, "capture_content": scenario != "metadata-only",
        "marker": MARKER, "started_at": "2026-09-13T12:00:00Z",
        "finished_at": "2026-09-13T12:01:00+00:00", "exit_code": 0,
        "cli_version": VERSION, "evidence_path": "/unused/.local/evidence.jsonl",
        "collector_mode": "azure",
    }


def source_document(scenario="metadata-only"):
    resource = {"attributes": attrs({"copilot.run.id": RUN, "service.name": "github-copilot",
                                     "test.resource": "preserve-me", "service.version": VERSION})}
    spans = [
        {"name": "invoke_agent copilot", "traceId": TRACE, "spanId": AGENT, "kind": 1,
         "attributes": attrs({"gen_ai.operation.name": "invoke_agent"}),
         "events": [{"name": "copilot.session.start", "attributes": attrs({"event.detail": "safe"})}]},
        {"name": "chat model", "traceId": TRACE, "spanId": CHAT, "parentSpanId": AGENT,
         "kind": 3, "attributes": attrs({"gen_ai.operation.name": "chat"})},
    ]
    if scenario in ("full-content", "delegated"):
        spans[1]["attributes"] += attrs({"gen_ai.input.messages": json.dumps([{"content": MARKER}]),
                                        "gen_ai.output.messages": json.dumps([{"content": MARKER}])})
        spans.append({"name": "execute_tool fixture", "traceId": TRACE, "spanId": TOOL,
                      "parentSpanId": AGENT, "kind": "SPAN_KIND_INTERNAL",
                      "attributes": attrs({"gen_ai.operation.name": "execute_tool",
                                           "gen_ai.tool.call.arguments": '{"message":"' + MARKER + '"}',
                                           "gen_ai.tool.call.result": MARKER})})
    if scenario == "delegated":
        spans.append({"name": "invoke_agent child", "traceId": TRACE, "spanId": CHILD,
                      "parentSpanId": AGENT, "kind": 1,
                      "attributes": attrs({"gen_ai.operation.name": "invoke_agent"})})
    metrics = [
        {"name": name, "histogram": {"aggregationTemporality": 2, "dataPoints": [
            {"attributes": attrs({"gen_ai.operation.name": "chat"}),
             "count": "1", "sum": total, "bucketCounts": ["0", "1"], "explicitBounds": [1]}
        ]}}
        for name, total in [("gen_ai.client.token.usage", 42), ("gen_ai.client.operation.duration", 2)]
    ]
    return {"resourceSpans": [{"resource": resource, "scopeSpans": [
        {"scope": {"name": "github.copilot", "version": "0.0.0-fixture-sdk"}, "spans": spans}]}],
        "resourceMetrics": [{"resource": resource, "scopeMetrics": [
            {"scope": {"name": "github.copilot", "version": "0.0.0-fixture-sdk"}, "metrics": metrics}]}]}


def azure_rows(scenario="metadata-only", *, doc=None):
    doc = source_document(scenario) if doc is None else doc
    resource = {a["key"]: a["value"]["stringValue"]
                for a in doc["resourceSpans"][0]["resource"]["attributes"]}
    scope = doc["resourceSpans"][0]["scopeSpans"][0]["scope"]
    resource["instrumentationlibrary.name"] = scope["name"]
    if "version" in scope:
        resource["instrumentationlibrary.version"] = scope["version"]
    rows = []
    for span in doc["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        properties = dict(resource)
        properties.update({a["key"]: a["value"]["stringValue"] for a in span["attributes"]})
        rows.append({"Table": "AppDependencies", "Name": span["name"], "Id": span["spanId"],
                     "OperationId": TRACE, "ParentId": span.get("parentSpanId", ""),
                     "Properties": properties, "Sum": None, "ItemCount": None})
        for event in span.get("events", []):
            event_properties = dict(resource)
            event_properties.update({a["key"]: a["value"]["stringValue"] for a in event["attributes"]})
            rows.append({"Table": "AppTraces", "Name": event["name"], "Id": "",
                         "OperationId": TRACE, "ParentId": span["spanId"],
                         "Properties": event_properties, "Sum": None, "ItemCount": None})
    for metric in doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
        point = metric["histogram"]["dataPoints"][0]
        properties = dict(resource)
        properties.update({a["key"]: a["value"]["stringValue"] for a in point["attributes"]})
        rows.append({"Table": "AppMetrics", "Name": metric["name"], "Id": "",
                     "OperationId": "", "ParentId": "", "Properties": properties,
                     "Sum": point["sum"], "ItemCount": int(point["count"])})
    return rows


def response(rows):
    names = ["Table", "Name", "Id", "OperationId", "ParentId", "Properties", "Sum", "ItemCount"]
    return {"tables": [{"name": "PrimaryResult", "columns": [
        {"name": key, "type": "dynamic" if key == "Properties" else "string"} for key in names],
        "rows": [[row.get(key) for key in names] for row in rows]}]}


class VerificationCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.v = importlib.import_module("scripts.verify_ingestion")
        except ModuleNotFoundError:
            cls.v = None

    def setUp(self):
        self.assertIsNotNone(self.v, "verification implementation must exist")


class VerificationTests(VerificationCase):
    def compare(self, scenario="metadata-only", rows=None, doc=None):
        source = self.v.parse_source(json.dumps(doc or source_document(scenario)), RUN)
        return self.v.compare_evidence(manifest(scenario), source,
                                       azure_rows(scenario) if rows is None else rows)

    def test_positive_metadata_proof(self):
        result = self.compare()
        self.assertEqual(result["status"], "passed")
        self.assertGreaterEqual(result["counts"]["matched_spans"], 2)
        self.assertEqual(result["counts"]["matched_metrics"], 2)
        self.assertTrue(any("bucket" in item for item in result["limitations"]))
        self.assertNotIn("preserve-me", json.dumps(result))

    def test_azure_root_parent_may_equal_its_operation_id(self):
        rows = azure_rows()
        next(row for row in rows if row["Id"] == AGENT)["ParentId"] = TRACE
        self.assertEqual(self.compare(rows=rows)["status"], "passed")

    def test_operation_id_cannot_replace_a_non_root_span_parent(self):
        rows = azure_rows()
        next(row for row in rows if row["Id"] == CHAT)["ParentId"] = TRACE
        self.assertEqual(self.compare(rows=rows)["status"], "failed")

    def test_optional_content_truncation_is_an_exact_utf8_byte_prefix(self):
        original = "\u00e9" * 5000
        exported = original.encode("utf-8")[:8192].decode("utf-8")
        matched, truncated = self.v._compare_content(
            {"gen_ai.system_instructions": original}, {"gen_ai.system_instructions": exported})
        self.assertTrue(matched)
        self.assertEqual(truncated, 1)

    def test_optional_content_truncation_does_not_accept_8192_unicode_characters(self):
        original = "\u00e9" * 9000
        matched, _ = self.v._compare_content(
            {"gen_ai.system_instructions": original}, {"gen_ai.system_instructions": original[:8192]})
        self.assertFalse(matched)

    def test_positive_full_content_proof(self):
        result = self.compare("full-content")
        self.assertEqual(result["status"], "passed")
        self.assertNotIn(MARKER, json.dumps(result))

    def test_positive_delegated_proof(self):
        self.assertEqual(self.compare("delegated")["status"], "passed")

    def test_empty_backend_is_pending_not_success(self):
        self.assertEqual(self.compare(rows=[])["status"], "pending")

    def test_wrong_run_backend_is_not_proof(self):
        rows = azure_rows()
        for row in rows:
            row["Properties"]["copilot.run.id"] = "other"
        self.assertEqual(self.compare(rows=rows)["status"], "pending")

    def test_wrong_source_run_is_rejected(self):
        text = json.dumps(source_document()).replace(RUN, "other")
        with self.assertRaises(AppError):
            self.v.parse_source(text, RUN)

    def test_shared_collector_selects_requested_run_resource_groups(self):
        unrelated = json.loads(json.dumps(source_document("full-content")).replace(RUN, OTHER_RUN))
        text = "\n".join(map(json.dumps, [unrelated, source_document(), unrelated]))
        source = self.v.parse_source(text, RUN)
        self.assertEqual(len(source["spans"]), 2)
        self.assertEqual(len(source["metrics"]), 2)
        self.assertEqual(self.v.compare_evidence(manifest(), source, azure_rows())["status"], "passed")

    def test_unrelated_only_valid_evidence_is_not_selected(self):
        unrelated = json.dumps(source_document()).replace(RUN, OTHER_RUN)
        with self.assertRaises(AppError):
            self.v.parse_source(unrelated, RUN)

    def test_malformed_unrelated_group_is_not_quietly_skipped(self):
        unrelated = json.loads(json.dumps(source_document()).replace(RUN, OTHER_RUN))
        unrelated["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"] = "malformed"
        with self.assertRaisesRegex(AppError, "array"):
            self.v.parse_source(json.dumps(source_document()) + "\n" + json.dumps(unrelated), RUN)

    def test_scope_identity_must_be_actual_cli(self):
        for key, scope_key in (("resourceSpans", "scopeSpans"), ("resourceMetrics", "scopeMetrics")):
            doc = source_document()
            doc[key][0][scope_key][0]["scope"]["name"] = "unrelated-generator"
            with self.subTest(key=key), self.assertRaises(AppError):
                self.v.parse_source(json.dumps(doc), RUN)

    def test_service_identity_is_exact_not_substring(self):
        doc = source_document()
        for key in ("resourceSpans", "resourceMetrics"):
            doc[key][0]["resource"]["attributes"][1]["value"]["stringValue"] = "not-github-copilot"
        with self.assertRaises(AppError):
            self.v.parse_source(json.dumps(doc), RUN)

    def test_service_version_must_match_manifest_not_scope_version(self):
        source = self.v.parse_source(json.dumps(source_document()), RUN)
        for version in ("not-copilot", "1.0.83-1"):
            result = self.v.compare_evidence({**manifest(), "cli_version": version}, source, azure_rows())
            self.assertEqual(result["status"], "failed")
        self.assertEqual(self.compare()["status"], "passed")

    def test_missing_service_version_cannot_prove_cli_origin(self):
        doc = source_document()
        for key in ("resourceSpans", "resourceMetrics"):
            doc[key][0]["resource"]["attributes"] = [
                value for value in doc[key][0]["resource"]["attributes"] if value["key"] != "service.version"]
        result = self.compare(doc=doc, rows=azure_rows(doc=doc))
        self.assertEqual(result["status"], "failed")

    def test_recognized_cli_version_prefixes_normalize(self):
        source = self.v.parse_source(json.dumps(source_document()), RUN)
        data = {**manifest(), "cli_version": "GitHub Copilot CLI v" + VERSION}
        self.assertEqual(self.v.compare_evidence(data, source, azure_rows())["status"], "passed")

    def test_actual_cli_version_first_line_terminal_period_is_not_version_suffix(self):
        source = self.v.parse_source(json.dumps(source_document()), RUN)
        data = {**manifest(), "cli_version": "GitHub Copilot CLI 1.0.84-5."}
        self.assertEqual(self.v.compare_evidence(data, source, azure_rows())["status"], "passed")
        mismatch = {**data, "cli_version": "GitHub Copilot CLI 1.0.84-6."}
        self.assertEqual(self.v.compare_evidence(mismatch, source, azure_rows())["status"], "failed")

    def test_source_service_version_prefix_normalizes_without_scope_version_guess(self):
        doc = source_document()
        for key in ("resourceSpans", "resourceMetrics"):
            doc[key][0]["resource"]["attributes"][-1]["value"]["stringValue"] = "v" + VERSION
        self.assertEqual(self.compare(doc=doc, rows=azure_rows(doc=doc))["status"], "passed")

    def test_azure_scope_identity_cannot_differ_from_source(self):
        rows = azure_rows()
        rows[0]["Properties"]["instrumentationlibrary.name"] = "unrelated-generator"
        self.assertEqual(self.compare(rows=rows)["status"], "pending")

    def test_source_resource_run_id_must_be_string(self):
        doc = source_document()
        doc["resourceSpans"][0]["resource"]["attributes"][0]["value"] = {"intValue": "12"}
        with self.assertRaises(AppError):
            self.v.parse_source(json.dumps(doc), RUN)

    def test_standalone_generator_identity_is_not_cli_proof(self):
        doc = source_document()
        for key in ("resourceSpans", "resourceMetrics"):
            doc[key][0]["resource"]["attributes"][1]["value"]["stringValue"] = "standalone-generator"
        with self.assertRaises(AppError):
            self.v.parse_source(json.dumps(doc), RUN)

    def test_signal_run_id_cannot_override_resource_identity(self):
        doc = source_document()
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] += attrs(
            {"copilot.run.id": "other"})
        with self.assertRaises(AppError):
            self.v.parse_source(json.dumps(doc), RUN)

    def test_empty_source_is_rejected(self):
        for text in ("", "{}", '{"resourceSpans": []}', "{bad"):
            with self.subTest(text=text), self.assertRaises(AppError):
                self.v.parse_source(text, RUN)

    def test_json_lines_mixed_signals(self):
        doc = source_document()
        text = "\n".join(json.dumps({key: value}) for key, value in doc.items())
        self.assertEqual(len(self.v.parse_source(text, RUN)["metrics"]), 2)

    def test_typed_nested_attributes(self):
        values = [{"key": "nested", "value": {"kvlistValue": {"values": [
            {"key": "values", "value": {"arrayValue": {"values": [
                {"intValue": "4"}, {"boolValue": True}, {"doubleValue": 2.5}]}}}]}}}]
        self.assertEqual(self.v.decode_attributes(values), {"nested": {"values": [4, True, 2.5]}})

    def test_identical_repeated_response_model_attribute_canonicalizes(self):
        doc = source_document()
        attributes = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["attributes"]
        model = attrs({"gen_ai.response.model": "fixture-model"})[0]
        attributes.extend([model, copy.deepcopy(model)])
        source = self.v.parse_source(json.dumps(doc), RUN)
        self.assertEqual(source["spans"][1]["attributes"]["gen_ai.response.model"], "fixture-model")
        self.assertEqual(self.v.compare_evidence(manifest(), source, azure_rows(doc=doc))["status"], "passed")

    def test_conflicting_repeated_attribute_still_fails(self):
        values = attrs({"gen_ai.response.model": "fixture-model-a"})
        values += attrs({"gen_ai.response.model": "fixture-model-b"})
        with self.assertRaisesRegex(AppError, "Conflicting duplicate"):
            self.v.decode_attributes(values)

    def test_duplicate_typed_values_cannot_coerce_conflicts(self):
        for first, second in (({"boolValue": True}, {"intValue": "1"}),
                              ({"stringValue": "fixture"}, {"bytesValue": "fixture"})):
            with self.subTest(first=first), self.assertRaises(AppError):
                self.v.decode_attributes([{"key": "fixture", "value": first},
                                          {"key": "fixture", "value": second}])

    def test_local_source_checks_never_elevate_collector_mode_to_azure(self):
        self.assertTrue(hasattr(self.v, "check_source_evidence"), "local-only source proof API is required")
        for scenario in ("metadata-only", "full-content", "delegated"):
            data = {**manifest(scenario), "collector_mode": "local-only"}
            source = self.v.parse_source(json.dumps(source_document(scenario)), RUN)
            result = self.v.check_source_evidence(data, source)
            with self.subTest(scenario=scenario):
                self.assertEqual(result["status"], "source-validated")
                self.assertEqual(result["proof_scope"], "post-transform-collector-source-only")
                self.assertIs(result["azure_ingestion_proven"], False)
                self.assertEqual(data["collector_mode"], "local-only")
                self.assertEqual(self.v.compare_evidence(data, source, azure_rows(scenario))["status"], "failed")

    def test_local_source_api_keeps_marker_parenting_and_privacy_checks(self):
        self.assertTrue(hasattr(self.v, "check_source_evidence"), "local-only source proof API is required")
        for scenario, fault in (("full-content", "marker"), ("delegated", "parent"),
                                ("metadata-only", "privacy")):
            doc = source_document(scenario)
            spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
            if fault == "marker":
                spans[1]["attributes"] = [
                    attr for attr in spans[1]["attributes"] if attr["key"] != "gen_ai.output.messages"]
            elif fault == "parent":
                spans[-1]["parentSpanId"] = "f" * 16
            else:
                spans[0]["attributes"] += attrs({"gen_ai.tool.definitions": "fixture-only leak"})
            data = {**manifest(scenario), "collector_mode": "local-only"}
            source = self.v.parse_source(json.dumps(doc), RUN)
            with self.subTest(scenario=scenario):
                result = self.v.check_source_evidence(data, source)
                self.assertEqual(result["status"], "failed")
                self.assertIs(result["azure_ingestion_proven"], False)

    def test_missing_cli_signals_fails(self):
        doc = source_document()
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["attributes"] = []
        self.assertEqual(self.compare(doc=doc)["status"], "failed")

    def test_missing_lifecycle_fails_explicitly(self):
        doc = source_document()
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"] = []
        result = self.compare(doc=doc)
        self.assertEqual(result["status"], "failed")
        self.assertIn("lifecycle", " ".join(result["reasons"]))

    def test_observed_cli_1_0_84_lifecycle_names_are_accepted(self):
        doc = source_document()
        spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
        spans[0]["name"] = "invoke_agent"
        spans[1]["name"] = "chat claude-sonnet-5"
        spans[0]["events"] = [
            {"name": name, "attributes": attrs({"fixture.only": "true"})}
            for name in ("github.copilot.mcp.server.lifecycle", "github.copilot.session.usage_info",
                         "github.copilot.user.message")
        ]
        metrics = doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        duration = copy.deepcopy(metrics[1])
        duration["name"] = "gen_ai.invoke_agent.duration"
        metrics.append(duration)
        result = self.compare(doc=doc, rows=azure_rows(doc=doc))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["counts"]["matched_events"], 3)
        self.assertEqual(result["counts"]["matched_metrics"], 3)

    def test_usage_and_user_message_do_not_fabricate_lifecycle_proof(self):
        doc = source_document()
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"] = [
            {"name": name, "attributes": []}
            for name in ("github.copilot.session.usage_info", "github.copilot.user.message")
        ]
        result = self.compare(doc=doc, rows=azure_rows(doc=doc))
        self.assertEqual(result["status"], "failed")
        self.assertIn("lifecycle", " ".join(result["reasons"]))

    def test_unspecified_span_kind_maps_to_internal_dependency(self):
        doc = source_document()
        del doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["kind"]
        self.assertEqual(self.compare(doc=doc)["status"], "passed")

    def test_exception_event_is_not_misidentified_as_apptrace(self):
        doc = source_document()
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"].append({
            "name": "exception", "attributes": attrs({"exception.type": "FixtureError"})})
        result = self.compare(doc=doc)
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("AppExceptions", " ".join(result["reasons"]))

    def test_delegation_without_child_is_unsupported(self):
        self.assertEqual(self.compare("delegated", doc=source_document())["status"], "unsupported")

    def test_broken_azure_parenting_fails(self):
        rows = azure_rows("delegated")
        next(row for row in rows if row["Id"] == CHILD)["ParentId"] = "f" * 16
        self.assertEqual(self.compare("delegated", rows=rows)["status"], "failed")

    def test_broken_local_parenting_fails(self):
        doc = source_document("delegated")
        next(span for span in doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
             if span["spanId"] == CHILD)["parentSpanId"] = "f" * 16
        self.assertEqual(self.compare("delegated", doc=doc)["status"], "failed")

    def test_content_off_local_leak_fails(self):
        doc = source_document()
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] += attrs(
            {"gen_ai.system_instructions": "SECRET-CONTENT"})
        result = self.compare(doc=doc)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("SECRET-CONTENT", json.dumps(result))

    def test_raw_metadata_tool_definitions_leak_cannot_be_excused(self):
        doc = source_document()
        for span in doc["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            span["attributes"] += attrs({"gen_ai.tool.definitions": "x" * 841})
        result = self.compare(doc=doc)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Content-off source", " ".join(result["reasons"]))

    def test_post_transform_absence_does_not_attest_native_cli_privacy(self):
        result = self.compare()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result.get("proof_scope"), "post-transform-collector-to-azure")
        self.assertIs(result.get("native_cli_content_privacy_verified"), False)
        self.assertTrue(any("tool.definitions" in item for item in result["limitations"]))

    def test_content_off_azure_leak_fails(self):
        rows = azure_rows()
        rows[0]["Properties"]["gen_ai.tool.definitions"] = '[{"secret":"content"}]'
        self.assertEqual(self.compare(rows=rows)["status"], "failed")

    def test_nested_content_off_leak_fails(self):
        rows = azure_rows()
        rows[0]["Properties"]["gen_ai"] = {"input": {"messages": ["SECRET-CONTENT"]}}
        result = self.compare(rows=rows)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("SECRET-CONTENT", json.dumps(result))

    def test_nested_local_content_off_leak_fails(self):
        doc = source_document()
        doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append({
            "key": "gen_ai", "value": {"kvlistValue": {"values": [
                {"key": "input.messages", "value": {"stringValue": "SECRET-CONTENT"}}]}}})
        self.assertEqual(self.compare(doc=doc)["status"], "failed")

    def test_content_off_even_empty_gated_field_is_not_absent(self):
        rows = azure_rows()
        rows[0]["Properties"]["gen_ai.input.messages"] = ""
        self.assertEqual(self.compare(rows=rows)["status"], "failed")

    def test_full_content_missing_marker_fails(self):
        doc = source_document("full-content")
        tool = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][2]
        tool["attributes"][-1]["value"]["stringValue"] = "not the marker"
        self.assertEqual(self.compare("full-content", doc=doc)["status"], "failed")

    def test_full_and_delegated_require_marker_inputs_and_outputs(self):
        for scenario in ("full-content", "delegated"):
            for field in ("gen_ai.input.messages", "gen_ai.output.messages"):
                for value in (None, "no audit marker here"):
                    doc = source_document(scenario)
                    chat = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
                    chat["attributes"] = [attr for attr in chat["attributes"] if attr["key"] != field]
                    if value is not None:
                        chat["attributes"] += attrs({field: value})
                    result = self.compare(scenario, doc=doc, rows=azure_rows(scenario, doc=doc))
                    with self.subTest(scenario=scenario, field=field, value=value):
                        self.assertEqual(result["status"], "failed")

    def test_delegated_capture_false_cannot_bypass_content_proof(self):
        data = {**manifest("delegated"), "capture_content": False}
        with self.assertRaises(AppError):
            self.v.validate_manifest(data, RUN)
        source = self.v.parse_source(json.dumps(source_document()), RUN)
        self.assertEqual(self.v.compare_evidence(data, source, azure_rows())["status"], "failed")

    def test_full_content_backend_mismatch_fails(self):
        rows = azure_rows("full-content")
        rows[-3]["Properties"]["gen_ai.tool.call.result"] = "different"
        self.assertEqual(self.compare("full-content", rows=rows)["status"], "failed")

    def test_marker_only_in_tool_result_does_not_prove_arguments(self):
        doc = source_document("full-content")
        tool = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][2]
        for attribute in tool["attributes"]:
            if attribute["key"] == "gen_ai.tool.call.arguments":
                attribute["value"]["stringValue"] = '{"path":"fixture.txt"}'
        self.assertEqual(self.compare("full-content", doc=doc,
                                      rows=azure_rows("full-content", doc=doc))["status"], "failed")

    def test_documented_optional_property_truncation_reports_fidelity_loss(self):
        for field in ("gen_ai.system_instructions", "gen_ai.tool.definitions"):
            doc = source_document("full-content")
            value = json.dumps({"fixture_only": "x" * 9000})
            doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] += attrs({field: value})
            rows = azure_rows("full-content", doc=doc)
            rows[0]["Properties"][field] = value[:8192]
            result = self.compare("full-content", doc=doc, rows=rows)
            with self.subTest(field=field):
                self.assertEqual(result["status"], "passed")
                self.assertEqual(result["counts"].get("optional_content_truncations"), 1)
                self.assertTrue(any("not lossless" in note for note in result["limitations"]))
                self.assertNotIn("x" * 100, json.dumps(result))

    def test_arbitrary_optional_content_loss_is_not_documented_truncation(self):
        for actual in ("short unexpected truncation", "y" * 8192, None):
            doc = source_document("full-content")
            doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] += attrs({
                "gen_ai.tool.definitions": "x" * 9000})
            rows = azure_rows("full-content", doc=doc)
            if actual is None:
                del rows[0]["Properties"]["gen_ai.tool.definitions"]
            else:
                rows[0]["Properties"]["gen_ai.tool.definitions"] = actual
            with self.subTest(actual_length=len(actual) if actual else 0):
                self.assertEqual(self.compare("full-content", doc=doc, rows=rows)["status"], "failed")

    def test_documented_optional_event_truncation_reports_fidelity_loss(self):
        doc = source_document("full-content")
        event = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["events"][0]
        event["attributes"] += attrs({"gen_ai.system_instructions": "x" * 9000})
        rows = azure_rows("full-content", doc=doc)
        rows[1]["Properties"]["gen_ai.system_instructions"] = "x" * 8192
        result = self.compare("full-content", doc=doc, rows=rows)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["counts"].get("optional_content_truncations"), 1)

    def test_span_events_must_match_source_name_and_correlation(self):
        rows = azure_rows()
        rows[1]["Name"] = "invented event"
        self.assertEqual(self.compare(rows=rows)["status"], "pending")
        rows = azure_rows()
        rows[1]["ParentId"] = CHAT
        self.assertNotEqual(self.compare(rows=rows)["status"], "passed")

    def test_azure_pipe_ids_normalize(self):
        rows = azure_rows()
        for row in rows:
            if row["Id"]:
                row["Id"] = "|" + TRACE + "." + row["Id"] + "."
            if row["ParentId"]:
                row["ParentId"] = "|" + TRACE + "." + row["ParentId"] + "."
        self.assertEqual(self.compare(rows=rows)["status"], "passed")

    def test_metric_resource_attributes_must_survive(self):
        rows = azure_rows()
        del rows[-1]["Properties"]["test.resource"]
        self.assertNotEqual(self.compare(rows=rows)["status"], "passed")

    def test_zero_token_usage_does_not_prove_real_inference(self):
        doc = source_document()
        doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"]["dataPoints"][0]["sum"] = 0
        self.assertEqual(self.compare(doc=doc)["status"], "failed")

    def test_typed_array_content_matches_json_string_azure(self):
        doc = source_document("full-content")
        chat = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
        chat["attributes"][1]["value"] = {"arrayValue": {"values": [
            {"kvlistValue": {"values": attrs({"content": MARKER})}}]}}
        self.assertEqual(self.compare("full-content", doc=doc)["status"], "passed")

    def test_cumulative_histograms_not_summed(self):
        doc = source_document()
        hist = doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"]
        later = copy.deepcopy(hist["dataPoints"][0])
        later.update({"count": "2", "sum": 84})
        hist["dataPoints"].append(later)
        self.assertEqual(self.compare(doc=doc)["status"], "passed")

    def test_each_token_dimension_series_requires_its_own_snapshot(self):
        doc = source_document()
        points = doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"]["dataPoints"]
        points[0]["attributes"] += attrs({"gen_ai.token.type": "input"})
        output = copy.deepcopy(points[0])
        output["attributes"][-1]["value"]["stringValue"] = "output"
        points.append(output)
        rows = azure_rows(doc=doc)
        missing = self.compare(doc=doc, rows=rows)
        self.assertEqual(missing["status"], "pending")
        output_row = copy.deepcopy(rows[-2])
        output_row["Properties"]["gen_ai.token.type"] = "output"
        rows.append(output_row)
        result = self.compare(doc=doc, rows=rows)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["counts"]["matched_metrics"], 3)

    def test_zero_only_required_series_does_not_hide_behind_positive_series(self):
        doc = source_document()
        points = doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"]["dataPoints"]
        points[0]["attributes"] += attrs({"gen_ai.token.type": "input"})
        output = copy.deepcopy(points[0])
        output["attributes"][-1]["value"]["stringValue"] = "output"
        output["sum"] = 0
        points.append(output)
        self.assertEqual(self.compare(doc=doc, rows=azure_rows(doc=doc))["status"], "failed")

    def test_metric_numeric_dimension_identity_cannot_be_ignored(self):
        doc = source_document()
        points = doc["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["histogram"]["dataPoints"]
        points[0]["attributes"].append({"key": "fixture.dimension", "value": {"intValue": "1"}})
        other = copy.deepcopy(points[0])
        other["attributes"][-1]["value"]["intValue"] = "2"
        points.append(other)
        rows = azure_rows()
        rows[-2]["Properties"]["fixture.dimension"] = "1"
        self.assertEqual(self.compare(doc=doc, rows=rows)["status"], "pending")
        second = copy.deepcopy(rows[-2])
        second["Properties"]["fixture.dimension"] = "2"
        rows.append(second)
        self.assertEqual(self.compare(doc=doc, rows=rows)["status"], "passed")

    def test_distinct_metric_resource_series_cannot_share_one_backend_series(self):
        doc = source_document()
        other = copy.deepcopy(doc["resourceMetrics"][0])
        other["resource"]["attributes"] += attrs({"test.instance": "second"})
        doc["resourceMetrics"].append(other)
        self.assertEqual(self.compare(doc=doc, rows=azure_rows(doc=doc))["status"], "pending")

    def test_flattened_resource_dimension_collision_cannot_prove_two_series(self):
        doc = source_document()
        first = doc["resourceMetrics"][0]
        first["resource"]["attributes"] += attrs({"test.dimension": "resource"})
        first["scopeMetrics"][0]["metrics"][0]["histogram"]["dataPoints"][0]["attributes"] += attrs(
            {"test.dimension": "point"})
        second = copy.deepcopy(first)
        second["resource"]["attributes"][-1]["value"]["stringValue"] = "point"
        doc["resourceMetrics"].append(second)
        rows = azure_rows(doc=doc)
        for row in rows:
            if row["Table"] == "AppMetrics":
                row["Properties"]["test.dimension"] = (
                    "point" if row["Name"] == "gen_ai.client.token.usage" else "resource")
        extra_duration = copy.deepcopy(rows[-1])
        extra_duration["Properties"]["test.dimension"] = "point"
        rows.append(extra_duration)
        self.assertEqual(self.compare(doc=doc, rows=rows)["status"], "failed")

    def test_named_columns_not_positional(self):
        payload = response(azure_rows())
        table = payload["tables"][0]
        table["columns"].reverse()
        for row in table["rows"]:
            row.reverse()
        self.assertEqual(self.v.parse_query_response(payload), azure_rows())

    def test_partial_errors_and_malformed_rows_rejected(self):
        cases = [
            {"error": {"code": "PartialError", "message": "sensitive"}, **response(azure_rows())},
            {"tables": []}, {"tables": [{"columns": [], "rows": [[]]}]},
            {"tables": [{"columns": [{"name": "Name"}], "rows": [["x", "extra"]]}]},
            {**response([]), "partialError": {"code": "PartialError"}},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(AppError):
                self.v.parse_query_response(payload)

    def test_malformed_azure_property_and_numeric_types_rejected(self):
        for key, value in (("Properties", "not-json"), ("Properties", []),
                           ("ItemCount", True), ("ItemCount", -1), ("Sum", "NaN")):
            payload = response(azure_rows())
            names = [column["name"] for column in payload["tables"][0]["columns"]]
            payload["tables"][0]["rows"][-1][names.index(key)] = value
            with self.subTest(key=key, value=value), self.assertRaises(AppError):
                self.v.parse_query_response(payload)

    def test_wrong_operation_id_fails(self):
        rows = azure_rows()
        rows[0]["OperationId"] = "f" * 32
        self.assertEqual(self.compare(rows=rows)["status"], "failed")

    def test_query_is_run_time_and_resource_scoped(self):
        query = self.v.build_query(manifest(), "/subscriptions/test/components/test")
        self.assertIn(RUN, query)
        self.assertIn("2026-09-13T12:00:00", query)
        self.assertIn("2026-09-13T12:01:00", query)
        self.assertIn("_ResourceId", query)
        self.assertIn("AppDependencies", query)
        self.assertNotIn("AppRequests", query)

    def test_rest_command_explicit_subscription_audience_and_json(self):
        state = {"subscription_id": RUN, "workspace_customer_id": RUN,
                 "application_insights_resource_id": "/subscriptions/test/components/test"}
        with patch.object(self.v, "run_json", return_value=response(azure_rows())) as run:
            self.v.query_azure(state, manifest(), 17)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["az", "rest", "--method"])
        self.assertEqual(args[args.index("--method") + 1], "POST")
        self.assertEqual(args[args.index("--subscription") + 1], RUN)
        self.assertEqual(args[args.index("--resource") + 1], "https://api.loganalytics.io")
        self.assertIn(f"/v1/workspaces/{RUN}/query", args[args.index("--url") + 1])
        self.assertIn("query", json.loads(args[args.index("--body") + 1]))
        self.assertEqual(run.call_args.kwargs["env"]["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"], "no")
        self.assertEqual(run.call_args.kwargs["timeout"], 17)

    def test_query_literal_escaping(self):
        resource = '/subscriptions/test/components/a"; print "bad'
        query = self.v.build_query(manifest(), resource)
        self.assertIn("let resource_id = " + json.dumps(resource) + ";", query)

    def test_query_rendering_is_pure_with_explicit_template(self):
        template = "let run_id = __RUN_ID__; let resource_id = __RESOURCE_ID__;"
        with patch.object(Path, "read_text", side_effect=AssertionError("pure helper performed I/O")):
            query = self.v.build_query(manifest(), "/subscriptions/test/components/test",
                                      template=template)
        self.assertIn(RUN, query)

    def test_unavailable_query_template_has_actionable_error(self):
        state = {"subscription_id": RUN, "workspace_customer_id": RUN,
                 "application_insights_resource_id": "/subscriptions/test/components/test"}
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(AppError, "KQL"):
                self.v.query_azure(state, manifest(), 10)

    def test_manifest_contract(self):
        for change in ({"run_id": "other"}, {"capture_content": "false"},
                       {"exit_code": True}, {"cli_version": ""},
                       {"started_at": "2026-09-13T12:00:00"},
                       {"finished_at": "2026-09-12T12:00:00Z"},
                       {"scenario": "unknown"}, {"marker": "private prompt"}):
            with self.subTest(change=change), self.assertRaises(AppError):
                self.v.validate_manifest({**manifest(), **change}, RUN)

    def test_path_escape_and_symlinks_rejected(self):
        base = Path(__file__).parent / "fixtures" / "verification"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as tmp:
            root = Path(tmp).absolute()
            local = root / ".local"
            local.mkdir()
            evidence = local / "evidence.jsonl"
            evidence.write_text("{}", encoding="utf-8")
            link = local / "link"
            link.symlink_to(evidence)
            self.assertEqual(self.v.trusted_path(str(evidence), local), evidence)
            for path in (link, root / "escape", local / ".." / "escape"):
                with self.subTest(path=path), self.assertRaises(AppError):
                    self.v.trusted_path(str(path), local)
            directory = local / "linked-dir"
            directory.symlink_to(local, target_is_directory=True)
            with self.assertRaises(AppError):
                self.v.trusted_path(str(directory / "evidence.jsonl"), local)


class DriverTests(VerificationCase):
    # Exercise all I/O under an owned fixture directory.
    def setUp(self):
        super().setUp()
        base = Path(__file__).parent / "fixtures" / "verification"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).absolute()
        self.local = self.root / ".local"
        self.folder = self.local / "runs" / RUN
        self.folder.mkdir(parents=True)
        evidence = self.folder / "collector.jsonl"
        evidence.write_text(json.dumps(source_document()) + "\n", encoding="utf-8")
        self.data = {**manifest(), "evidence_path": str(evidence)}
        (self.folder / "manifest.json").write_text(json.dumps(self.data), encoding="utf-8")
        state = {"subscription_id": RUN, "workspace_customer_id": RUN,
                 "application_insights_resource_id": "/subscriptions/test/components/test"}
        (self.local / "azure.json").write_text(json.dumps(state), encoding="utf-8")
        self.clock = 0.0
        self.start_patch("ROOT", self.root)
        self.start_patch("LOCAL", self.local)

    def start_patch(self, name, value):
        patcher = patch.object(self.v, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sleep(self, duration):
        self.clock += duration

    def execute(self, query, timeout=5):
        return self.v.verify(RUN, timeout_seconds=timeout, query=query,
                             clock=lambda: self.clock, sleep=self.sleep)

    def test_deadline_is_failed_and_private_results_persist(self):
        result = self.execute(lambda *args: response([]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.clock, 5)
        self.assertIn("deadline", " ".join(result["reasons"]))
        for name in ("results.json", "raw-query.json"):
            path = self.folder / name
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_backend_eventually_matches(self):
        calls = []

        def query(*args):
            calls.append(args)
            return response([] if len(calls) == 1 else azure_rows())

        self.assertEqual(self.execute(query)["status"], "passed")
        self.assertEqual(len(calls), 2)

    def test_backend_validation_error_is_not_hidden_as_transport_failure(self):
        rows = azure_rows()
        rows[0]["Id"] = "not-a-valid-span-id"
        result = self.execute(lambda *args: response(rows))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 1)
        self.assertIn("Malformed telemetry correlation ID", " ".join(result["reasons"]))

    def test_rotated_collector_evidence_fails_without_querying(self):
        for name in ("collector-2026-09-13T12-00-00.000.jsonl",
                     "collector-2026-09-13T12-00-00.000.jsonl.gz", "collector.jsonl.1",
                     "collector-2026-09-13T13-32-18.886-size.jsonl",
                     "collector-2026-09-13T13-32-18.997-size.jsonl.gz"):
            with self.subTest(name=name):
                rotated = self.folder / name
                rotated.write_text("fixture-only rotated segment", encoding="utf-8")
                try:
                    result = self.execute(lambda *args: self.fail("must not query rotated evidence"))
                    self.assertEqual(result["status"], "failed")
                    self.assertIn("rotated", " ".join(result["reasons"]))
                finally:
                    rotated.unlink()

    def test_complete_json_without_final_lf_is_not_flushed_collector_evidence(self):
        Path(self.data["evidence_path"]).write_text(json.dumps(source_document()), encoding="utf-8")
        result = self.execute(lambda *args: self.fail("must not query unflushed evidence"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("final", " ".join(result["reasons"]))

    def test_exact_pinned_exporter_rotation_filename_is_rejected(self):
        folder = self.local / "collector-evidence"
        folder.mkdir()
        path = folder / "otel.json"
        path.write_text(json.dumps(source_document()) + "\n", encoding="utf-8")
        (folder / "otel-2026-09-13T13-32-18.886-size.json").write_text("fixture-only\n", encoding="utf-8")
        self.data["evidence_path"] = str(path)
        (self.folder / "manifest.json").write_text(json.dumps(self.data), encoding="utf-8")
        result = self.execute(lambda *args: self.fail("must not query rotated otel.json"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("rotated", " ".join(result["reasons"]))

    def test_evidence_modified_during_query_cannot_pass(self):
        def query(*args):
            path = Path(self.data["evidence_path"])
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            return response(azure_rows())

        result = self.execute(query)
        self.assertEqual(result["status"], "failed")
        self.assertIn("changed", " ".join(result["reasons"]))

    def test_truncated_final_json_line_has_explicit_diagnostic(self):
        Path(self.data["evidence_path"]).write_text('{"resourceSpans":[', encoding="utf-8")
        result = self.execute(lambda *args: self.fail("must not query truncated evidence"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("truncated", " ".join(result["reasons"]))

    def test_transient_retry_after(self):
        calls = []

        def query(*args):
            calls.append(args)
            if len(calls) == 1:
                raise AppError("HTTP 429 TooManyRequests Retry-After: 3")
            return response(azure_rows())

        self.assertEqual(self.execute(query)["status"], "passed")
        self.assertEqual(self.clock, 3)

    def test_retry_after_is_bounded_by_deadline(self):
        def query(*args):
            raise AppError("HTTP 429 Retry-After: 1000")

        self.assertEqual(self.execute(query)["status"], "failed")
        self.assertEqual(self.clock, 5)

    def test_server_error_backoff_is_bounded_and_exponential(self):
        times = []

        def query(*args):
            times.append(self.clock)
            raise AppError("HTTP 503 ServiceUnavailable")

        self.assertEqual(self.execute(query, timeout=10)["status"], "failed")
        self.assertEqual(times, [0, 1, 3, 7])
        self.assertEqual(self.clock, 10)

    def test_transport_failure_does_not_relabel_previous_raw_response(self):
        calls = []

        def query(*args):
            calls.append(args)
            if len(calls) == 1:
                return response([])
            raise AppError("HTTP 403 Forbidden")

        self.assertEqual(self.execute(query)["status"], "failed")
        raw = json.loads((self.folder / "raw-query.json").read_text(encoding="utf-8"))
        self.assertIsNone(raw["response"])
        self.assertEqual(raw["attempts"], 2)

    def test_auth_error_immediate_sanitized_failure(self):
        def query(*args):
            raise AppError("HTTP 403 Forbidden SECRET-CONTENT")

        result = self.execute(query)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.clock, 0)
        self.assertNotIn("SECRET-CONTENT", json.dumps(result))

    def test_partial_query_never_proves_success(self):
        payload = {**response(azure_rows()), "error": {"code": "PartialError"}}
        self.assertEqual(self.execute(lambda *args: payload)["status"], "failed")
        self.assertEqual(self.clock, 0)

    def test_missing_table_only_pending_until_deadline(self):
        payload = {"error": {"code": "SemanticError",
                             "message": "Failed to resolve table or column expression named 'AppTraces'"}}
        result = self.execute(lambda *args: payload)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.clock, 5)

    def test_late_response_cannot_pass_after_deadline(self):
        def query(*args):
            self.clock += 6
            return response(azure_rows())

        self.assertEqual(self.execute(query)["status"], "failed")

    def test_nonzero_cli_exit_never_queries(self):
        self.data["exit_code"] = 1
        (self.folder / "manifest.json").write_text(json.dumps(self.data), encoding="utf-8")
        with patch.object(self.v, "query_azure") as query:
            self.assertEqual(self.execute(query)["status"], "failed")
            query.assert_not_called()

    def test_local_only_or_missing_collector_mode_is_ineligible(self):
        for mode in ("local-only", None):
            self.data.pop("collector_mode", None)
            if mode is not None:
                self.data["collector_mode"] = mode
            (self.folder / "manifest.json").write_text(json.dumps(self.data), encoding="utf-8")
            result = self.execute(lambda *args: self.fail("ineligible run must not query"))
            with self.subTest(mode=mode):
                self.assertEqual(result["status"], "failed")
                self.assertIn("collector_mode", " ".join(result["reasons"]))

    def test_invalid_timeout_never_queries(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(AppError):
                self.execute(lambda *args: self.fail("must not query"), timeout)

    def test_missing_manifest_records_failed_result(self):
        (self.folder / "manifest.json").unlink()
        result = self.execute(lambda *args: self.fail("must not query"))
        self.assertEqual(result["status"], "failed")
        self.assertTrue((self.folder / "results.json").is_file())

    def test_cli_failure_exit_code(self):
        with patch.object(self.v, "verify", return_value={"status": "failed", "counts": {},
                                                         "reasons": ["deadline"]}):
            with patch("builtins.print"):
                self.assertEqual(self.v.main(["--run-id", RUN, "--timeout-seconds", "600"]), 1)

    def test_cli_success_exit_code(self):
        with patch.object(self.v, "verify", return_value={"status": "passed", "counts": {},
                                                         "reasons": []}):
            with patch("builtins.print"):
                self.assertEqual(self.v.main(["--run-id", RUN]), 0)


if __name__ == "__main__":
    unittest.main()
