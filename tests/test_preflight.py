import subprocess
import contextlib
import io
import unittest
from unittest.mock import patch

from scripts import preflight
from scripts.common import AppError


class PreflightTests(unittest.TestCase):
    def test_v1_has_no_collector_prerequisite(self):
        with patch.object(preflight, "tool_check", return_value={"status": "passed"}) as tools, \
             patch.object(preflight, "azure_check", return_value={"status": "passed"}), \
             patch.object(preflight, "run", return_value=subprocess.CompletedProcess([], 0, "help", "")), \
             patch.object(preflight, "write_private"), \
             patch.object(preflight.sys, "argv", ["preflight"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(preflight.main(), 0)
        self.assertEqual([call.args[0] for call in tools.call_args_list], ["az", "az", "copilot"])

    def test_help_exits_without_readiness_checks_or_receipt_writes(self):
        for option in ("--help", "-h"):
            with self.subTest(option=option):
                output = io.StringIO()
                with patch.object(preflight.sys, "argv", ["preflight", option]), \
                     patch.object(preflight, "tool_check", return_value={"status": "passed"}) as tools, \
                     patch.object(preflight, "azure_check", return_value={"status": "passed"}) as azure, \
                     patch.object(preflight, "run", return_value=subprocess.CompletedProcess([], 0, "help", "")) as run, \
                     patch.object(preflight, "write_private") as write, \
                     contextlib.redirect_stdout(output):
                    with self.assertRaises(SystemExit) as exit_result:
                        preflight.main()
                    self.assertEqual(exit_result.exception.code, 0)
                self.assertIn("usage:", output.getvalue())
                for action in (tools, azure, run, write):
                    action.assert_not_called()

    def test_unknown_arguments_fail_before_readiness_checks_or_receipt_writes(self):
        for option in ("--unknown", "unexpected"):
            with self.subTest(option=option):
                output = io.StringIO()
                with patch.object(preflight.sys, "argv", ["preflight", option]), \
                     patch.object(preflight, "tool_check", return_value={"status": "passed"}) as tools, \
                     patch.object(preflight, "azure_check", return_value={"status": "passed"}) as azure, \
                     patch.object(preflight, "run", return_value=subprocess.CompletedProcess([], 0, "help", "")) as run, \
                     patch.object(preflight, "write_private") as write, \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(output):
                    with self.assertRaises(SystemExit) as exit_result:
                        preflight.main()
                    self.assertEqual(exit_result.exception.code, 2)
                self.assertIn("unrecognized arguments", output.getvalue())
                for action in (tools, azure, run, write):
                    action.assert_not_called()

    def test_unauthenticated_azure_is_blocked(self):
        with patch.object(preflight, "run_json", side_effect=AppError("Please run az login")):
            check = preflight.azure_check()
        self.assertEqual(check["status"], "blocked")
        self.assertIn("az login", check["detail"])

    def test_wrong_cloud_is_blocked(self):
        with patch.object(preflight, "run_json", return_value={"name": "AzureUSGovernment"}):
            self.assertEqual(preflight.azure_check()["status"], "blocked")

    def test_active_public_cloud_account(self):
        with patch.object(preflight, "run_json", side_effect=[
            {"name": "AzureCloud"}, {"id": "synthetic", "state": "Enabled"}
        ]):
            self.assertEqual(preflight.azure_check()["status"], "passed")

    def test_missing_tool_is_not_installed_implicitly(self):
        with patch.object(preflight.shutil, "which", return_value=None):
            self.assertEqual(preflight.tool_check("copilot", ["--version"])["status"], "blocked")

    def test_cli_error_is_reported(self):
        with patch.object(preflight.shutil, "which", return_value="/bin/copilot"):
            with patch.object(preflight, "run", side_effect=AppError("CLI unavailable")):
                self.assertEqual(preflight.tool_check("copilot", ["--version"])["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
