import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import visualizations
from scripts.common import AppError, ROOT

UUID = "00000000-0000-4000-8000-000000000001"
GROUP = "rg-copilot-native-otel"
PREFIX = f"/subscriptions/{UUID}/resourceGroups/{GROUP}/providers/"
STATE = {
    "subscription_id": UUID, "resource_group": GROUP, "location": "eastus",
    "ownership_marker": UUID,
    "application_insights_resource_id": PREFIX + "Microsoft.Insights/components/native",
    "workspace_resource_id": PREFIX + "Microsoft.OperationalInsights/workspaces/native",
    "workspace_customer_id": UUID,
    "dcr_resource_id": PREFIX + "Microsoft.Insights/dataCollectionRules/native",
    "dcr_immutable_id": "dcr-" + "a" * 32,
    "traces_endpoint": "https://example.eastus-1.ingest.monitor.azure.com/datacollectionRules/dcr-"
                       + "a" * 32 + "/streams/Microsoft-OTLP-Traces/otlp/v1/traces",
    "metrics_endpoint": "https://example.eastus-1.metrics.ingest.monitor.azure.com/datacollectionRules/dcr-"
                        + "a" * 32 + "/streams/Custom-Metrics-Otel/otlp/v1/metrics",
}
MODEL = {"version": "Notebook/1.0", "items": [{"type": 1, "content": {"json": "Synthetic-only"}}]}


class VisualizationTests(unittest.TestCase):
    def test_workbook_template_associates_native_app_and_preserves_ownership(self):
        text = (ROOT / "infra/visualizations.bicep").read_text()
        for required in ("Microsoft.Insights/workbooks@2023-06-01", "kind: 'shared'",
                         "sourceId: applicationInsightsResourceId", "serializedData: workbookData",
                         "'ownership-marker': ownershipMarker", "category: 'workbook'"):
            self.assertIn(required, text)
        self.assertNotIn("Microsoft.Dashboard/grafana", text)
        self.assertNotIn("Microsoft.Compute", text)

    def test_parameters_are_nonsecret_and_model_is_serialized_once(self):
        params = visualizations.parameters(STATE, MODEL)
        self.assertEqual(params["parameters"]["applicationInsightsResourceId"]["value"],
                         STATE["application_insights_resource_id"])
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

    def test_readback_verifies_saved_definition_and_application_association(self):
        resource = {
            "id": PREFIX + "Microsoft.Insights/workbooks/" + UUID,
            "kind": "shared",
            "tags": {"solution": "copilot-native-otel", "ownership-marker": UUID},
            "properties": {"sourceId": STATE["application_insights_resource_id"].lower(),
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
