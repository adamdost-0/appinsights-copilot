import subprocess
import unittest
from unittest.mock import patch

from scripts import preflight
from scripts.common import AppError


class PreflightTests(unittest.TestCase):
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
            self.assertEqual(preflight.tool_check("docker", ["version"])["status"], "blocked")

    def test_daemon_error_is_reported(self):
        with patch.object(preflight.shutil, "which", return_value="/bin/docker"):
            with patch.object(preflight, "run", side_effect=AppError("daemon unavailable")):
                self.assertEqual(preflight.tool_check("docker", ["version"])["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
