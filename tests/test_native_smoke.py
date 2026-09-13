import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import native_smoke
from scripts.common import AppError


UUID = "00000000-0000-4000-8000-000000000001"
DCR = "dcr-" + "a" * 32
ARM = f"/subscriptions/{UUID}/resourceGroups/native-test/providers/Microsoft.Insights"
STATE = {
    "subscription_id": UUID,
    "dcr_resource_id": ARM + "/dataCollectionRules/native-test",
    "dcr_immutable_id": DCR,
    "application_insights_resource_id": ARM + "/components/native-test",
    "workspace_customer_id": UUID,
    "traces_endpoint": (
        f"https://example.eastus-1.ingest.monitor.azure.com/datacollectionRules/{DCR}"
        "/streams/Microsoft-OTLP-Traces/otlp/v1/traces"
    ),
    "metrics_endpoint": (
        f"https://example.eastus-1.metrics.ingest.monitor.azure.com/datacollectionRules/{DCR}"
        "/streams/Custom-Metrics-Otel/otlp/v1/metrics"
    ),
}


class NativeSmokeTests(unittest.TestCase):
    def test_valid_native_state(self):
        self.assertEqual(native_smoke.validate_state(STATE), STATE)

    def test_rejects_missing_fields_and_non_native_destinations(self):
        for field in STATE:
            with self.subTest(field=field):
                state = copy.deepcopy(STATE)
                del state[field]
                with self.assertRaises(AppError):
                    native_smoke.validate_state(state)
        for endpoint in (
            STATE["traces_endpoint"].replace("https:", "http:"),
            STATE["traces_endpoint"].replace("monitor.azure.com", "monitor.azure.com.evil.example"),
            STATE["traces_endpoint"].replace("https://", "https://user:password@"),
            STATE["traces_endpoint"] + "?token=secret",
            STATE["traces_endpoint"] + "#fragment",
            STATE["traces_endpoint"].replace(DCR, "dcr-" + "b" * 32),
            STATE["traces_endpoint"].replace("/otlp/v1/traces", "/v2/track"),
            "https://eastus.in.applicationinsights.azure.com/v2/track",
        ):
            with self.subTest(endpoint=endpoint):
                state = {**STATE, "traces_endpoint": endpoint}
                with self.assertRaises(AppError):
                    native_smoke.validate_state(state)

    def test_rejects_cross_subscription_or_wrong_resource_types(self):
        for field in ("dcr_resource_id", "application_insights_resource_id"):
            for value in ("/invalid", STATE[field].replace(UUID, UUID[:-1] + "2")):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(AppError):
                        native_smoke.validate_state({**STATE, field: value})

    def test_native_environment_has_exact_endpoints_and_no_local_collector(self):
        env = native_smoke.native_environment(Path("/tmp/isolated"), UUID, "metadata-only",
                                               "github-token", "azure-token", STATE)
        self.assertEqual(env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"], STATE["traces_endpoint"])
        self.assertEqual(env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"], STATE["metrics_endpoint"])
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env)
        self.assertNotIn("COPILOT_OTEL_FILE_EXPORTER_PATH", env)
        self.assertEqual(env["OTEL_EXPORTER_OTLP_HEADERS"], "Authorization=Bearer%20azure-token")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"], "http/protobuf")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_METRICS_PROTOCOL"], "http/protobuf")
        self.assertNotIn("microsoft.applicationId", env["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertEqual(env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")

    def test_native_environment_does_not_inherit_private_configuration(self):
        with patch.dict("os.environ", {
            "OTEL_EXPORTER_OTLP_CLIENT_KEY": "/private/key",
            "AZURE_CLIENT_SECRET": "private",
            "COPILOT_OTEL_FILE_EXPORTER_PATH": "/private/evidence",
        }):
            env = native_smoke.native_environment(Path("/tmp/isolated"), UUID, "full-content",
                                                   "github-token", "azure-token", STATE)
        for key in ("OTEL_EXPORTER_OTLP_CLIENT_KEY", "AZURE_CLIENT_SECRET",
                    "COPILOT_OTEL_FILE_EXPORTER_PATH"):
            self.assertNotIn(key, env)
        self.assertEqual(env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "true")

    def test_monitor_token_has_explicit_audience_subscription_and_lifetime(self):
        with patch.object(native_smoke, "run_json", return_value={
            "accessToken": "synthetic-token", "expires_on": str(int(time.time()) + 3600),
        }) as request:
            self.assertEqual(native_smoke.monitor_token(UUID, 180), "synthetic-token")
        args = request.call_args.args[0]
        self.assertEqual(args[args.index("--resource") + 1], "https://monitor.azure.com/")
        self.assertEqual(args[args.index("--subscription") + 1], UUID)

    def test_invalid_or_expiring_token_is_rejected_without_disclosure(self):
        for payload in ({}, {"accessToken": "secret-token"},
                        {"accessToken": "secret-token", "expires_on": "invalid"},
                        {"accessToken": "secret-token", "expires_on": 1},
                        {"accessToken": "", "expires_on": int(time.time()) + 3600}):
            with self.subTest(payload=payload):
                with patch.object(native_smoke, "run_json", return_value=payload):
                    with self.assertRaises(AppError) as error:
                        native_smoke.monitor_token(UUID, 180)
                self.assertNotIn("secret-token", str(error.exception))

    def test_execute_has_no_collector_requirement_or_false_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(native_smoke, "LOCAL", output), \
                 patch.object(native_smoke, "load_json", return_value=STATE), \
                 patch.object(native_smoke, "monitor_token", return_value="azure-secret"), \
                 patch.object(native_smoke, "authentication_token", return_value="github-secret"), \
                 patch.object(native_smoke, "run") as run, \
                 patch.object(native_smoke, "run_session") as launch:
                run.return_value.stdout = "GitHub Copilot CLI 1.0.84-5.\n"
                launch.return_value.returncode = 0
                launch.return_value.stdout = "azure-secret github-secret"
                launch.return_value.stderr = ""
                manifest = native_smoke.execute("metadata-only")
            command = launch.call_args.args[0]
            self.assertIn("--secret-env-vars=COPILOT_GITHUB_TOKEN,OTEL_EXPORTER_OTLP_HEADERS",
                          command)
            self.assertEqual(manifest["collector_mode"], "native-azure")
            self.assertEqual(manifest["status"], "awaiting_verification")
            self.assertFalse(manifest["azure_ingestion_proven"])
            self.assertNotIn("evidence_path", manifest)
            self.assertEqual(manifest["dcr_resource_id"], STATE["dcr_resource_id"])
            self.assertIsNotNone(manifest["finished_at"])
            for path in output.rglob("*"):
                if path.is_file():
                    self.assertNotIn("azure-secret", path.read_text())
                    self.assertNotIn("github-secret", path.read_text())

    def test_invalid_timeout_or_scenario_never_launches(self):
        for scenario, timeout in (("metadata-only", 0), ("real-project", 180)):
            with self.subTest(scenario=scenario, timeout=timeout):
                with patch.object(native_smoke, "run_session") as launch:
                    with self.assertRaises(AppError):
                        native_smoke.execute(scenario, timeout)
                launch.assert_not_called()

    def test_session_failure_redacts_raised_error_and_persists_failed_manifest(self):
        import json

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(native_smoke, "LOCAL", output), \
                 patch.object(native_smoke, "load_json", return_value=STATE), \
                 patch.object(native_smoke, "monitor_token", return_value="azure-secret"), \
                 patch.object(native_smoke, "authentication_token", return_value="github-secret"), \
                 patch.object(native_smoke, "run") as run, \
                 patch.object(native_smoke, "run_session",
                              side_effect=AppError("export failed: Bearer%20azure-secret github-secret")):
                run.return_value.stdout = "GitHub Copilot CLI 1.0.84-5.\n"
                with self.assertRaises(AppError) as raised:
                    native_smoke.execute("metadata-only")
            self.assertNotIn("azure-secret", str(raised.exception))
            self.assertNotIn("github-secret", str(raised.exception))
            manifests = list(output.glob("runs/*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(manifest["azure_ingestion_proven"])
            self.assertIsNotNone(manifest["finished_at"])


if __name__ == "__main__":
    unittest.main()
