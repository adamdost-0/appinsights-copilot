import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import synthetic_session as run_smoke
from scripts.common import AppError

RUN_ID = "00000000-0000-4000-8000-000000000001"


class SmokeTests(unittest.TestCase):
    def test_clean_environment_removes_inherited_telemetry_and_secrets(self):
        inherited = {
            "PATH": "/usr/bin", "AZURE_CLIENT_SECRET": "do-not-forward",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=secret",
            "COPILOT_OTEL_FILE_EXPORTER_PATH": "/private/file",
            "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/private/instructions",
            "OTEL_EXPORTER_OTLP_CLIENT_KEY": "/private/key",
            "COPILOT_ALLOW_ALL": "true", "HTTPS_PROXY": "http://secret@proxy",
        }
        env = run_smoke.child_environment(Path("/tmp/isolated"), RUN_ID, "metadata-only",
                                         "synthetic-auth-token", inherited)
        for key in inherited:
            if key != "PATH":
                self.assertNotIn(key, env)
        self.assertEqual(env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "synthetic-auth-token")
        self.assertIn(RUN_ID, env["OTEL_RESOURCE_ATTRIBUTES"])

    def test_content_enabled_only_for_explicit_synthetic_scenario(self):
        env = run_smoke.child_environment(Path("/tmp/isolated"), RUN_ID, "full-content",
                                         "synthetic-auth-token", {})
        self.assertEqual(env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "true")
        with self.assertRaises(AppError):
            run_smoke.child_environment(Path("/tmp/isolated"), RUN_ID, "real-work", "token", {})

    def test_invocation_restricts_tools_and_paths(self):
        command = run_smoke.command_for("full-content", "MARKER", Path("/tmp/work"))
        self.assertIn("--available-tools=view", command)
        self.assertIn("--disallow-temp-dir", command)
        self.assertIn("--no-custom-instructions", command)
        self.assertIn("--disable-builtin-mcps", command)
        self.assertIn("--deny-tool=shell", command)
        self.assertNotIn("--allow-all", command)
        self.assertIn("SYNTHETIC_AUDIT_FIXTURE", command[-1])

    def test_metadata_has_no_exposed_tools(self):
        command = run_smoke.command_for("metadata-only", "MARKER", Path("/tmp/work"))
        self.assertIn("--available-tools", command)
        self.assertNotIn("--available-tools=view", command)

    def test_tool_arguments_can_carry_synthetic_marker(self):
        command = run_smoke.command_for("full-content", "SYNTHETIC_MARKER", Path("/tmp/work"))
        self.assertIn("/tmp/work/SYNTHETIC_MARKER.txt", command[-1])

    def test_no_credentials_fails_without_starting_cli(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(run_smoke, "run", side_effect=AppError("not authenticated")):
                with self.assertRaisesRegex(AppError, "GitHub"):
                    run_smoke.authentication_token()

    def test_uses_existing_github_token_without_persisting_it(self):
        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "synthetic"}):
            self.assertEqual(run_smoke.authentication_token(), "synthetic")

    def test_legacy_collector_execution_is_removed(self):
        self.assertFalse(hasattr(run_smoke, "execute"))
        self.assertFalse(hasattr(run_smoke, "evidence_file"))
        self.assertFalse(hasattr(run_smoke, "main"))
        root = Path(__file__).resolve().parent.parent
        self.assertFalse((root / "scripts" / "run_smoke.py").exists())

    def test_uuid_is_validated_before_environment_construction(self):
        with self.assertRaises(AppError):
            run_smoke.child_environment(Path("/tmp/isolated"), "invalid", "full-content", "x", {})

    def test_isolation_sets_data_and_state_roots_without_export_destination(self):
        env = run_smoke.child_environment(Path("/tmp/isolated"), RUN_ID, "metadata-only", "token", {})
        self.assertEqual(env["XDG_DATA_HOME"], "/tmp/isolated/.local/share")
        self.assertEqual(env["XDG_STATE_HOME"], "/tmp/isolated/.local/state")
        self.assertNotIn("OTEL_EXPORTER_OTLP_ENDPOINT", env)


if __name__ == "__main__":
    unittest.main()
