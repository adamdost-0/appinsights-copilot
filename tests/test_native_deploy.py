from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import native_deploy
from scripts.common import AppError, ROOT, write_json


UUID = "00000000-0000-4000-8000-000000000001"


class NativeDeployTests(unittest.TestCase):
    def test_native_template_uses_direct_otel_sources_and_no_compute(self):
        text = (ROOT / "infra/native-resources.bicep").read_text()
        for required in (
            "directDataSources:", "otelMetrics:", "otelTraces:", "otelLogs:",
            "'Microsoft-OTel-Traces-Spans'", "'Microsoft-OTel-Traces-Events'",
            "'Microsoft-OTel-Traces-Resources'", "'Custom-Metrics-Otel'",
            "monitoringAccounts:", "enrichWithReference:", "replaceResourceIdWithReference: true",
            "DisableLocalAuth: true", "prometheusQueryEndpoint",
            "3913510d-42f4-4e42-8a64-420c390055eb", "b0d8363b-8ddd-447d-831f-62ca05bff136",
        ):
            self.assertIn(required, text)
        for forbidden in ("Microsoft.Compute", "Microsoft.App/", "containerApps",
                          "connectionString", "InstrumentationKey", "OtlpSupport", "dataSources:"):
            self.assertNotIn(forbidden, text)

    def test_receipt_is_stable_and_cannot_cross_subscriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_deploy, "LOCAL", Path(directory)):
                first = native_deploy.receipt(UUID, "eastus")
                second = native_deploy.receipt(UUID, "eastus")
                self.assertEqual(first, second)
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID[:-1] + "2", "eastus")
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID, "westus")

    def test_existing_group_requires_exact_ownership(self):
        receipt = {"subscription_id": UUID, "ownership_marker": UUID, "location": "eastus"}
        for group in (
            {"location": "eastus", "tags": {}},
            {"location": "eastus", "tags": {"solution": "copilot-native-otel", "ownership-marker": "wrong"}},
            {"location": "westus", "tags": {"solution": "copilot-native-otel", "ownership-marker": UUID}},
        ):
            with self.subTest(group=group):
                with patch.object(native_deploy, "run_json", side_effect=[True, group]):
                    with self.assertRaises(AppError):
                        native_deploy.check_group(receipt)

    def test_absent_group_is_allowed(self):
        with patch.object(native_deploy, "run_json", return_value=False):
            native_deploy.check_group({"subscription_id": UUID, "ownership_marker": UUID,
                                       "location": "eastus"})

    def test_linked_service_generated_failure_alert_is_preserved_but_other_scopes_fail(self):
        state = {"subscription_id": UUID, "ownership_marker": UUID, "location": "eastus"}
        tags = {"solution": "copilot-native-otel", "ownership-marker": UUID}
        component_id = f"/subscriptions/{UUID}/resourceGroups/{native_deploy.GROUP}/providers/Microsoft.Insights/components/native"
        component = {"id": component_id, "name": "native",
                     "type": "Microsoft.Insights/components", "tags": tags}
        alert = {"id": "/alert", "name": "Failure Anomalies - native",
                 "type": "microsoft.alertsmanagement/smartDetectorAlertRules", "tags": {}}
        for linked in (True, False):
            scope = component_id.lower() if linked else "/unrelated"
            detail = {"properties": {"scope": [scope], "detector": {"id": "FailureAnomaliesDetector"}}}
            replies = [True, {"location": "eastus", "tags": tags}, [component, alert], detail]
            with self.subTest(linked=linked), \
                 patch.object(native_deploy, "run_json", side_effect=replies) as execute:
                if linked:
                    native_deploy.check_group(state)
                    self.assertTrue(all("delete" not in call.args[0] for call in execute.call_args_list))
                else:
                    with self.assertRaises(AppError):
                        native_deploy.check_group(state)

    def test_corrupt_receipt_fails_before_cloud_access(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_deploy, "LOCAL", Path(directory)):
                write_json(Path(directory) / "native-deployment.json", {"subscription_id": UUID})
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID, "eastus")
                write_json(Path(directory) / "native-deployment.json",
                           {"subscription_id": UUID, "location": "eastus"})
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID, "eastus")

    def test_parameter_file_contains_no_credentials(self):
        receipt = {"ownership_marker": UUID, "location": "eastus"}
        values = native_deploy.parameters(receipt, UUID, "User")
        self.assertEqual(values["parameters"]["principalId"]["value"], UUID)
        self.assertEqual(values["parameters"]["ownershipMarker"]["value"], UUID)
        with self.assertRaises(AppError):
            native_deploy.parameters(receipt, UUID, "unsupported")
        with self.assertRaises(AppError):
            native_deploy.parameters(receipt, None, "User")
        self.assertNotIn("token", str(values).lower())

    def test_what_if_requests_machine_readable_output(self):
        state = {"subscription_id": UUID, "location": "eastus", "ownership_marker": UUID}
        responses = [
            {"id": UUID, "state": "Enabled", "environmentName": "AzureCloud"},
            *[{"registrationState": "Registered"} for _ in range(3)],
            {"properties": {"provisioningState": "Succeeded"}},
            {"status": "Succeeded", "changes": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_deploy, "LOCAL", Path(directory)), \
                 patch.object(native_deploy, "receipt", return_value=state), \
                 patch.object(native_deploy, "check_group"), \
                 patch.object(native_deploy, "run_json", side_effect=responses) as execute:
                native_deploy.deploy(UUID, "eastus", False, UUID)
        commands = [call.args[0] for call in execute.call_args_list]
        what_if = next(command for command in commands if "what-if" in command)
        self.assertIn("--no-pretty-print", what_if)

    def test_failed_or_malformed_preview_is_never_success(self):
        state = {"subscription_id": UUID, "location": "eastus", "ownership_marker": UUID}
        for preview in ({"status": "Failed", "error": {"code": "Denied"}, "changes": []},
                        {"status": "Succeeded"}, {"status": "Succeeded", "changes": [None]}):
            with self.subTest(preview=preview), tempfile.TemporaryDirectory() as directory:
                responses = [
                    {"id": UUID, "state": "Enabled", "environmentName": "AzureCloud"},
                    *[{"registrationState": "Registered"} for _ in range(3)],
                    {"properties": {"provisioningState": "Succeeded"}}, preview,
                ]
                with patch.object(native_deploy, "LOCAL", Path(directory)), \
                     patch.object(native_deploy, "receipt", return_value=state), \
                     patch.object(native_deploy, "check_group"), \
                     patch.object(native_deploy, "run_json", side_effect=responses):
                    with self.assertRaises(AppError):
                        native_deploy.deploy(UUID, "eastus", False, UUID)


if __name__ == "__main__":
    unittest.main()
