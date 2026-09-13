import json
import os
from pathlib import Path
import signal
import time
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import common


class CommonTests(unittest.TestCase):
    def test_private_json_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state" / "azure.json"
            common.write_json(target, {"id": "synthetic"})
            self.assertEqual(common.load_json(target), {"id": "synthetic"})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_refuses_symlink_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "link").symlink_to(root / "real")
            with self.assertRaises(common.AppError):
                common.private_dir(root / "link" / "nested")

    def test_new_intermediate_directories_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "first" / "second"
            common.private_dir(target)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)

    def test_missing_state_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(common.AppError, "Cannot read"):
                common.load_json(Path(tmp) / "missing.json")

    def test_non_object_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("[]")
            with self.assertRaisesRegex(common.AppError, "object"):
                common.load_json(path)

    def test_command_error_redacts_secret(self):
        result = subprocess.CompletedProcess(["az"], 1, "", "key=secret-value")
        with patch("subprocess.run", return_value=result):
            with self.assertRaises(common.AppError) as error:
                common.run(["az"], env={"AZURE_CLIENT_SECRET": "secret-value"})
        self.assertNotIn("secret-value", str(error.exception))
        self.assertIn("exit 1", str(error.exception))

    def test_json_command_rejects_invalid_output(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(["az"], 0, "oops", "")):
            with self.assertRaisesRegex(common.AppError, "JSON"):
                common.run_json(["az"])

    def test_timeout_is_explicit(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["az"], 1)):
            with self.assertRaisesRegex(common.AppError, "timed out"):
                common.run(["az"], timeout=1)

    def test_uuid_validation(self):
        value = "00000000-0000-4000-8000-000000000001"
        self.assertEqual(common.validate_run_id(value), value)
        with self.assertRaises(common.AppError):
            common.validate_run_id("bad' | take 1")

    def test_redacts_connection_and_bearer(self):
        text = "InstrumentationKey=abc;IngestionEndpoint=https://example.invalid/ Authorization: Bearer abcdef"
        redacted = common.redact(text)
        self.assertNotIn("InstrumentationKey=abc", redacted)
        self.assertNotIn("Bearer abcdef", redacted)

    def test_session_timeout_terminates_process_group(self):
        process = Mock(pid=12345, returncode=-15)
        process.communicate.side_effect = [subprocess.TimeoutExpired(["copilot"], 1), ("", ""), ("", "")]
        def signal_group(pid, sig):
            if sig == 0:
                raise ProcessLookupError
        with patch.object(subprocess, "Popen", return_value=process) as spawn:
            with patch.object(os, "killpg", side_effect=signal_group) as kill:
                with self.assertRaisesRegex(common.AppError, "timed out"):
                    common.run_session(["copilot"], timeout=1)
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        kill.assert_any_call(12345, signal.SIGTERM)

    def test_session_returns_completed_output(self):
        process = Mock(pid=12345, returncode=0)
        process.communicate.return_value = ("synthetic", "")
        with patch.object(subprocess, "Popen", return_value=process):
            self.assertEqual(common.run_session(["copilot"]).stdout, "synthetic")

    def test_timeout_stops_detached_stdio_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
            parent = (
                "import subprocess,sys,time; "
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}],"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"open({str(pid_file)!r},'w').write(str(p.pid)); time.sleep(60)"
            )
            pid = None
            try:
                with self.assertRaises(common.AppError):
                    common.run_session(["python3", "-c", parent], timeout=1)
                pid = int(pid_file.read_text())
                status = Path(f"/proc/{pid}/status")
                for _ in range(50):
                    if not status.exists() or "\nState:\tZ" in status.read_text():
                        break
                    time.sleep(0.02)
                else:
                    self.fail("Owned descendant survived the timeout")
            finally:
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
