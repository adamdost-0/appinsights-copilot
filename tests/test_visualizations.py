import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import visualizations
from scripts.common import AppError, ROOT

UUID = "00000000-0000-4000-8000-000000000001"
GROUP = "rg-copilot-otel-v1"
PREFIX = f"/subscriptions/{UUID}/resourceGroups/{GROUP}/providers/"
STATE = {
    "subscription_id": UUID, "resource_group": GROUP, "location": "eastus",
    "ownership_marker": UUID,
    "workspace_resource_id": PREFIX + "Microsoft.OperationalInsights/workspaces/native",
    "workspace_customer_id": UUID,
    "azure_monitor_workspace_resource_id": PREFIX + "Microsoft.Monitor/accounts/native",
    "metrics_query_endpoint": "https://example.eastus.prometheus.monitor.azure.com",
    "dce_resource_id": PREFIX + "Microsoft.Insights/dataCollectionEndpoints/native",
    "dcr_resource_id": PREFIX + "Microsoft.Insights/dataCollectionRules/native",
    "dcr_immutable_id": "dcr-" + "a" * 32,
    "traces_endpoint": "https://example.eastus-1.ingest.monitor.azure.com/datacollectionRules/dcr-"
                       + "a" * 32 + "/streams/Microsoft-OTLP-Traces/otlp/v1/traces",
    "metrics_endpoint": "https://example.eastus-1.metrics.ingest.monitor.azure.com/datacollectionRules/dcr-"
                        + "a" * 32 + "/streams/Custom-Metrics-Otel/otlp/v1/metrics",
    "logs_endpoint": "https://example.eastus-1.ingest.monitor.azure.com/datacollectionRules/dcr-"
                     + "a" * 32 + "/streams/Microsoft-OTLP-Logs/otlp/v1/logs",
}
MODEL = {"version": "Notebook/1.0", "items": [{"type": 1, "content": {"json": "Synthetic-only"}}]}


class VisualizationTests(unittest.TestCase):
    def test_workbook_template_associates_workspace_and_preserves_v1_ownership(self):
        text = (ROOT / "infra/visualizations.bicep").read_text()
        for required in ("Microsoft.Insights/workbooks@2023-06-01", "kind: 'shared'",
                         "sourceId: workspaceResourceId", "serializedData: workbookData",
                         "'ownership-marker': ownershipMarker", "category: 'workbook'",
                         "solution: 'copilot-otel-v1'",
                         "displayName: 'Copilot CLI - Session Explorer'",
                         "guid(resourceGroup().id, 'copilot-otel-v1-session-explorer')"):
            self.assertIn(required, text)
        self.assertNotIn("Microsoft.Dashboard/grafana", text)
        self.assertNotIn("Microsoft.Compute", text)
        self.assertNotIn("applicationInsights", text)
        self.assertNotIn("DCR-associated", text)
        self.assertNotIn("Synthetic verification data", text)
        self.assertIn("Native Copilot span summaries in Log Analytics", text)

    def test_parameters_are_nonsecret_and_model_is_serialized_once(self):
        params = visualizations.parameters(STATE, MODEL)
        self.assertEqual(params["parameters"]["workspaceResourceId"]["value"],
                         STATE["workspace_resource_id"])
        self.assertNotIn("applicationInsightsResourceId", params["parameters"])
        serialized = params["parameters"]["workbookData"]["value"]
        self.assertEqual(json.loads(serialized), MODEL)
        self.assertNotIn("token", serialized.lower())

    def test_invalid_native_scope_and_notebook_are_rejected(self):
        for change in ({"resource_group": "unrelated"},
                       {"ownership_marker": "not-a-uuid"},
                       {"workspace_resource_id": "/unrelated"}):
            with self.subTest(change=change):
                with self.assertRaises(AppError):
                    visualizations.parameters({**STATE, **change}, MODEL)
        for model in ({}, {"version": "Notebook/1.0", "items": []},
                      {"version": "wrong", "items": [1]}):
            with self.subTest(model=model):
                with self.assertRaises(AppError):
                    visualizations.parameters(STATE, model)

    def test_readback_verifies_saved_definition_and_workspace_association(self):
        resource = {
            "id": PREFIX + "Microsoft.Insights/workbooks/" + UUID,
            "kind": "shared",
            "tags": {"solution": "copilot-otel-v1", "ownership-marker": UUID},
            "properties": {"sourceId": STATE["workspace_resource_id"].lower(),
                           "category": "workbook", "serializedData": json.dumps(MODEL)},
        }
        visualizations.verify_readback(STATE, MODEL, resource, resource["id"])
        for mutation in ("source", "data", "tags", "id"):
            modified = copy.deepcopy(resource)
            if mutation == "source":
                modified["properties"]["sourceId"] = "/other"
            elif mutation == "data":
                modified["properties"]["serializedData"] = "{}"
            elif mutation == "tags":
                modified["tags"] = {}
            else:
                modified["id"] = "/other"
            with self.subTest(mutation=mutation):
                with self.assertRaises(AppError):
                    visualizations.verify_readback(STATE, MODEL, modified, resource["id"])

    def test_apply_validates_queries_and_readback_without_claiming_portal_render(self):
        resource_id = PREFIX + "Microsoft.Insights/workbooks/" + UUID
        resource = {
            "id": resource_id, "kind": "shared",
            "tags": {"solution": "copilot-otel-v1", "ownership-marker": UUID},
            "properties": {"sourceId": STATE["workspace_resource_id"],
                           "category": "workbook", "serializedData": json.dumps(MODEL)},
        }
        responses = [
            {"properties": {"provisioningState": "Succeeded"}},
            {"status": "Succeeded", "changes": []},
            {"properties": {"provisioningState": "Succeeded", "outputs": {
                "workbookResourceId": {"value": resource_id}}}},
            resource,
        ]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(visualizations, "LOCAL", Path(directory)), \
                patch.object(visualizations, "load_json", return_value=STATE), \
                patch.object(visualizations, "build_workbook", return_value=MODEL), \
                patch.object(visualizations, "check_group", return_value=True) as group, \
                patch.object(visualizations, "validate_queries", return_value={"ok": True}) as queries, \
                patch.object(visualizations, "run_json", side_effect=responses) as run:
            receipt = visualizations.deploy(True)
            self.assertEqual(json.loads((Path(directory) / "visualizations.json").read_text()),
                             receipt)
        queries.assert_called_once_with(STATE)
        group.assert_called_once_with(STATE, require_complete=True)
        self.assertEqual(receipt["workspace_resource_id"], STATE["workspace_resource_id"])
        self.assertEqual(receipt["dcr_resource_id"], STATE["dcr_resource_id"])
        self.assertNotIn("application_insights_resource_id", receipt)
        self.assertEqual(receipt["workbook_name"], "Copilot CLI - Session Explorer")
        self.assertTrue(receipt["saved_definition_verified"])
        self.assertTrue(receipt["panel_queries_verified"])
        self.assertFalse(receipt["portal_render_verified"])
        self.assertEqual(receipt["portal_url"], "https://portal.azure.com/#resource" + resource_id)
        self.assertIn(f"https://management.azure.com{resource_id}"
                      "?api-version=2023-06-01&canFetchContent=true", run.call_args.args[0])

    def test_preview_validates_queries_but_does_not_create_or_claim_browser_success(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(visualizations, "LOCAL", Path(directory)), \
                patch.object(visualizations, "load_json", return_value=STATE), \
                patch.object(visualizations, "build_workbook", return_value=MODEL), \
                patch.object(visualizations, "check_group", return_value=True) as group, \
                patch.object(visualizations, "validate_queries", return_value={"ok": True}), \
                patch.object(visualizations, "run_json", side_effect=[
                    {"properties": {"provisioningState": "Succeeded"}},
                    {"status": "Succeeded", "changes": []},
                ]) as run:
            receipt = visualizations.deploy(False)
        self.assertEqual(receipt["status"], "previewed")
        group.assert_called_once_with(STATE, require_complete=True)
        self.assertTrue(receipt["panel_queries_verified"])
        self.assertIsNot(receipt.get("portal_render_verified"), True)
        self.assertEqual([call.args[0][3] for call in run.call_args_list], ["validate", "what-if"])

    def test_failed_panel_queries_prevent_arm_validation_and_deployment(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(visualizations, "LOCAL", Path(directory)), \
                patch.object(visualizations, "load_json", return_value=STATE), \
                patch.object(visualizations, "build_workbook", return_value=MODEL), \
                patch.object(visualizations, "check_group", return_value=True), \
                patch.object(visualizations, "validate_queries", return_value={"ok": False}), \
                patch.object(visualizations, "run_json") as run:
            with self.assertRaisesRegex(AppError, "failed live validation"):
                visualizations.deploy(True)
        run.assert_not_called()

    def test_absent_foundation_prevents_queries_and_arm_operations(self):
        for apply in (False, True):
            with self.subTest(apply=apply), tempfile.TemporaryDirectory() as directory, \
                    patch.object(visualizations, "LOCAL", Path(directory)), \
                    patch.object(visualizations, "load_json", return_value=STATE), \
                    patch.object(visualizations, "build_workbook", return_value=MODEL), \
                    patch.object(visualizations, "check_group", return_value=False) as group, \
                    patch.object(visualizations, "validate_queries", return_value={"ok": False}) as queries, \
                    patch.object(visualizations, "run_json") as run:
                with self.assertRaisesRegex(AppError, "requires an existing complete native foundation"):
                    visualizations.deploy(apply)
                group.assert_called_once_with(STATE, require_complete=True)
                queries.assert_not_called()
                run.assert_not_called()

    def test_preview_refuses_any_deletion(self):
        with self.assertRaises(AppError):
            visualizations.check_preview({"status": "Succeeded", "changes": [
                {"changeType": "Delete", "resourceId": "/owned"},
            ]})
        for preview in ({}, {"status": "Failed", "changes": []},
                        {"status": "Succeeded", "changes": [None]}):
            with self.subTest(preview=preview):
                with self.assertRaises(AppError):
                    visualizations.check_preview(preview)
        visualizations.check_preview({"status": "Succeeded", "changes": []})


if __name__ == "__main__":
    unittest.main()
