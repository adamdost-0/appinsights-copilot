"""Stdlib contract tests; opt-in Docker probe uses only synthetic telemetry.

Run: python3 -m unittest discover -s tests -p test_collector.py -v
Real image probe: COLLECTOR_DOCKER_TEST=1 <same command>
"""

import contextlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.request
import uuid

from scripts.common import AppError


ROOT = Path(__file__).resolve().parents[1]
IMAGE = ("otel/opentelemetry-collector-contrib:0.160.0@sha256:"
         "799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6")
SYNTHETIC = ("InstrumentationKey=00000000-0000-0000-0000-000000000001;"
             "IngestionEndpoint=http://127.0.0.1:9/")
CONTENT_KEYS = (
    "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
    "gen_ai.tool.definitions", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
)


class FakeDocker:
    """Model Docker's process boundary, not collector implementation details."""

    def __init__(self):
        self.calls = []
        self.containers = {}
        self.counter = 0
        self.exit_on_start = False
        self.fail_stop = False
        self.stop_exit_code = 0
        self.stop_oom_killed = False

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        command = args[1]
        output = ""
        if command == "ps":
            filters = [args[i + 1] for i, arg in enumerate(args) if arg == "--filter"]
            matches = list(self.containers.values())
            for item in filters:
                key, value = item.split("=", 1)
                if key == "id":
                    matches = [c for c in matches if c["Id"] == value]
                elif key == "name":
                    matches = [c for c in matches if c["Name"] == value.removeprefix("^").removesuffix("$")]
            output = "\n".join(c["Id"] for c in matches)
        elif command == "create":
            self.counter += 1
            cid = f"{self.counter:064x}"
            labels = {}
            for i, arg in enumerate(args):
                if arg == "--label":
                    key, value = args[i + 1].split("=", 1)
                    labels[key] = value
            self.containers[cid] = {
                "Id": cid, "Name": "/" + args[args.index("--name") + 1],
                "Labels": labels,
                "State": {"Running": False, "Status": "created", "ExitCode": 0, "OOMKilled": False},
            }
            output = cid + "\n"
        elif command == "inspect":
            output = json.dumps(self.containers[args[-1]])
        elif command == "start":
            item = self.containers[args[-1]]
            running = "-a" not in args and not self.exit_on_start
            item["State"] = {"Running": running,
                             "Status": "running" if running else "exited",
                             "ExitCode": 0 if not self.exit_on_start else 1, "OOMKilled": False}
        elif command == "stop":
            if self.fail_stop:
                raise AppError("docker failed: stop denied")
            self.containers[args[-1]]["State"]["Running"] = False
            self.containers[args[-1]]["State"]["Status"] = "exited"
            self.containers[args[-1]]["State"]["ExitCode"] = self.stop_exit_code
            self.containers[args[-1]]["State"]["OOMKilled"] = self.stop_oom_killed
        elif command == "rm":
            del self.containers[args[-1]]
        else:
            raise AssertionError(f"Unexpected Docker command: {args}")
        return subprocess.CompletedProcess(args, 0, output, "")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("scripts.collector"),
                             "collector CLI is not implemented")
        self.module = importlib.import_module("scripts.collector")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name) / ".local"
        self.local.mkdir(mode=0o700)
        self.env_path = self.local / "collector.env"
        self.env_path.write_text("APPLICATIONINSIGHTS_CONNECTION_STRING=" + SYNTHETIC + "\n")
        self.env_path.chmod(0o600)
        self.bridge = self.module.Collector(local=self.local)
        self.engine = FakeDocker()
        self.run_patch = patch.object(self.module, "run", self.engine)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.real_probe_health = self.module.probe_health
        self.health = patch.object(self.module, "probe_health", return_value=True).start()
        self.addCleanup(patch.stopall)

    def saved_state(self):
        return json.loads((self.local / "collector.json").read_text())

    def test_image_and_yaml_contract(self):
        self.assertEqual((ROOT / "collector/image.txt").read_text().strip(), IMAGE)
        config = (ROOT / "collector/otelcol.yaml").read_text()
        for text in ("azure_monitor:", "connection_string: ${env:APPLICATIONINSIGHTS_CONNECTION_STRING}",
                     "spaneventsenabled: true", "shutdown_timeout: 10s",
                     "endpoint: 0.0.0.0:4318", "endpoint: 0.0.0.0:13133",
                     "path: /health", "path: /evidence/otel.json",
                     "format: json", "max_megabytes: 10", "max_backups: 2"):
            self.assertIn(text, config)
        self.assertEqual(config.count("exporters: [azure_monitor, file]"), 2)
        self.assertNotIn("azuremonitor:", config)
        self.assertNotIn("append: true", config)

    def test_validate_without_deployer_files_uses_synthetic_env_not_arguments(self):
        self.env_path.unlink()
        result = self.bridge.validate()
        self.assertTrue(result["validated"])
        self.assertFalse(result["azure_ingestion_proven"])
        create, options = next(call for call in self.engine.calls if call[0][1] == "create")
        self.assertIn("--network", create)
        self.assertEqual(create[create.index("--network") + 1], "none")
        self.assertIn("APPLICATIONINSIGHTS_CONNECTION_STRING", create)
        self.assertIn("InstrumentationKey=", options["env"]["APPLICATIONINSIGHTS_CONNECTION_STRING"])
        self.assertNotIn("InstrumentationKey=", " ".join(create))
        self.assertFalse(self.engine.containers)
        self.assertFalse((self.local / "collector.json").exists())

    def test_rejects_changed_image_pin(self):
        with patch.object(self.module, "IMAGE_FILE", self.local / "bad-image.txt"):
            (self.local / "bad-image.txt").write_text("otel/opentelemetry-collector-contrib:latest\n")
            with self.assertRaisesRegex(AppError, "pin"):
                self.bridge.validate()
        self.assertFalse(self.engine.calls)

    def test_start_private_state_loopback_mounts_and_secret_transport(self):
        result = self.bridge.start()
        state = self.saved_state()
        for key in ("container_id", "ownership_marker", "evidence_path", "started_at"):
            self.assertIn(key, state)
        self.assertEqual(state["evidence_path"], str(self.local / "collector-evidence/otel.json"))
        self.assertTrue(state["started_at"].endswith("Z"))
        self.assertEqual(str(uuid.UUID(state["ownership_marker"])), state["ownership_marker"])
        self.assertTrue(result["running"])
        self.assertTrue(result["healthy"])
        self.assertFalse(result["azure_ingestion_proven"])
        create = next(args for args, _ in self.engine.calls if args[1] == "create")
        ports = [create[i + 1] for i, arg in enumerate(create) if arg == "--publish"]
        self.assertEqual(ports, ["127.0.0.1:4318:4318", "127.0.0.1:13133:13133"])
        self.assertIn(str(self.env_path), create)
        self.assertIn("--env-file", create)
        self.assertIn("--read-only", create)
        self.assertIn("--cap-drop", create)
        self.assertIn(f"{os.getuid()}:{os.getgid()}", create)
        mounts = [create[i + 1] for i, arg in enumerate(create) if arg == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(any("otelcol.yaml" in value and "readonly" in value for value in mounts))
        self.assertFalse(any(".azure" in value for value in mounts))
        self.assertNotIn(SYNTHETIC, json.dumps(self.engine.calls))
        for path in (self.local, self.local / "collector-evidence"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        for path in (self.local / "collector.json", Path(state["evidence_path"])):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_start_requires_private_nonempty_connection_string(self):
        cases = [
            ("", 0o600, "connection string"),
            ("APPLICATIONINSIGHTS_CONNECTION_STRING=not-a-connection-string\n", 0o600, "connection string"),
            ("APPLICATIONINSIGHTS_CONNECTION_STRING=" + SYNTHETIC + "\n", 0o644, "0600"),
            ("APPLICATIONINSIGHTS_CONNECTION_STRING=" + SYNTHETIC + "\nOTHER=value\n", 0o600, "only"),
        ]
        for content, mode, message in cases:
            with self.subTest(message=message):
                self.env_path.write_text(content)
                self.env_path.chmod(mode)
                with self.assertRaisesRegex(AppError, message):
                    self.bridge.start()
        self.assertFalse(self.engine.containers)

    def test_start_missing_env_has_actionable_error(self):
        self.env_path.unlink()
        with self.assertRaisesRegex(AppError, "collector.env"):
            self.bridge.start()

    def test_second_start_is_explicit_collision_not_replacement(self):
        self.bridge.start()
        calls = len(self.engine.calls)
        with self.assertRaisesRegex(AppError, "state|already"):
            self.bridge.start()
        self.assertFalse(any(args[1] in ("stop", "rm", "create")
                             for args, _ in self.engine.calls[calls:]))

    def test_name_collision_without_state_is_not_adopted(self):
        self.bridge.start()
        (self.local / "collector.json").unlink()
        with self.assertRaisesRegex(AppError, "collision"):
            self.bridge.start()
        self.assertEqual(len(self.engine.containers), 1)

    def test_status_checks_health_and_process_without_env_or_azure_state(self):
        self.bridge.start()
        self.env_path.unlink()
        self.assertTrue(self.bridge.status()["healthy"])
        self.engine.containers[self.saved_state()["container_id"]]["State"]["Running"] = False
        with self.assertRaisesRegex(AppError, "not running|exited"):
            self.bridge.status()

    def test_status_rejects_unhealthy_running_container(self):
        self.bridge.start()
        self.health.return_value = False
        with self.assertRaisesRegex(AppError, "health"):
            self.bridge.status()

    def test_start_checks_process_even_if_health_port_answers(self):
        self.engine.exit_on_start = True
        with self.assertRaisesRegex(AppError, "not running|exited"):
            self.bridge.start()
        self.assertFalse(self.engine.containers)
        self.assertFalse((self.local / "collector.json").exists())

    def test_unhealthy_start_cleans_up_owned_id(self):
        self.health.return_value = False
        with patch.object(self.module, "HEALTH_TIMEOUT", 0):
            with self.assertRaisesRegex(AppError, "health"):
                self.bridge.start()
        self.assertFalse(self.engine.containers)
        self.assertFalse((self.local / "collector.json").exists())

    def test_status_and_stop_refuse_foreign_label(self):
        self.bridge.start()
        self.engine.containers[self.saved_state()["container_id"]]["Labels"] = {}
        for method in (self.bridge.status, self.bridge.stop):
            with self.assertRaisesRegex(AppError, "ownership"):
                method()
        self.assertTrue((self.local / "collector.json").exists())
        self.assertFalse(any(args[1] in ("stop", "rm") for args, _ in self.engine.calls))

    def test_stale_state_fails_status_and_start_but_stop_clears_only_state(self):
        self.bridge.start()
        self.engine.containers.clear()
        with self.assertRaisesRegex(AppError, "stale|missing"):
            self.bridge.status()
        with self.assertRaisesRegex(AppError, "state"):
            self.bridge.start()
        result = self.bridge.stop()
        self.assertTrue(result["stale"])
        self.assertIs(result.get("clean_shutdown"), False)
        self.assertFalse((self.local / "collector.json").exists())
        self.assertTrue((self.local / "collector-evidence/otel.json").exists())

    def test_stop_is_graceful_by_owned_id_and_preserves_evidence(self):
        self.bridge.start()
        cid = self.saved_state()["container_id"]
        self.env_path.unlink()
        result = self.bridge.stop()
        self.assertIs(result.get("clean_shutdown"), True)
        command, options = next(call for call in self.engine.calls if call[0][1] == "stop")
        self.assertEqual(command[-1], cid)
        self.assertGreater(int(command[command.index("--time") + 1]), 10)
        self.assertGreater(options["timeout"], 30)
        self.assertFalse(self.engine.containers)
        self.assertTrue((self.local / "collector-evidence/otel.json").exists())

    def test_stop_failure_keeps_recoverable_state(self):
        self.bridge.start()
        self.engine.fail_stop = True
        with self.assertRaisesRegex(AppError, "stop denied"):
            self.bridge.stop()
        self.assertTrue((self.local / "collector.json").exists())
        self.assertTrue(self.engine.containers)

    def test_stop_abnormal_exit_retains_container_state_and_evidence(self):
        self.bridge.start(local_only=True)
        state = self.saved_state()
        cid = state["container_id"]
        container = json.loads(json.dumps(self.engine.containers[cid]))
        evidence = Path(state["evidence_path"])
        evidence.write_text('{"resourceSpans":[]}\n')
        before_evidence = evidence.read_bytes()
        for exit_code, oom in ((137, False), (137, True), (1, False), (0, True)):
            with self.subTest(exit_code=exit_code, oom=oom):
                self.engine.containers[cid] = json.loads(json.dumps(container))
                self.module.write_json(self.bridge.state_path, state)
                self.engine.containers[cid]["State"]["Running"] = True
                self.engine.containers[cid]["State"]["Status"] = "running"
                self.engine.stop_exit_code = exit_code
                self.engine.stop_oom_killed = oom
                before = len(self.engine.calls)
                with self.assertRaisesRegex(AppError, "clean shutdown|abnormal"):
                    self.bridge.stop()
                self.assertIn(cid, self.engine.containers)
                self.assertEqual(self.saved_state(), state)
                self.assertEqual(evidence.read_bytes(), before_evidence)
                self.assertFalse(any(args[1] == "rm" for args, _ in self.engine.calls[before:]))

    def test_stop_rejects_preexisting_abnormal_or_incomplete_exit_status(self):
        self.bridge.start(local_only=True)
        state = self.saved_state()
        cid = state["container_id"]
        container = json.loads(json.dumps(self.engine.containers[cid]))
        for process in (
            {"Running": False, "Status": "exited", "ExitCode": 137, "OOMKilled": False},
            {"Running": False, "Status": "exited", "ExitCode": 0},
            {"Running": False, "Status": "exited", "OOMKilled": False},
            {"Running": False, "Status": "created", "ExitCode": 0, "OOMKilled": False},
        ):
            with self.subTest(process=process):
                self.engine.containers[cid] = json.loads(json.dumps(container))
                self.module.write_json(self.bridge.state_path, state)
                self.engine.containers[cid]["State"] = process
                before = len(self.engine.calls)
                with self.assertRaisesRegex(AppError, "clean shutdown|abnormal"):
                    self.bridge.stop()
                self.assertEqual(self.saved_state(), state)
                self.assertFalse(any(args[1] in ("stop", "rm")
                                     for args, _ in self.engine.calls[before:]))

    def test_stop_disappearing_container_cannot_confirm_clean_shutdown(self):
        self.bridge.start(local_only=True)
        state = self.saved_state()
        original = self.engine

        def disappearing(args, **kwargs):
            result = original(args, **kwargs)
            if args[1] == "stop":
                original.containers.clear()
            return result

        with patch.object(self.module, "run", side_effect=disappearing):
            with self.assertRaisesRegex(AppError, "clean shutdown|disappeared"):
                self.bridge.stop()
        self.assertEqual(self.saved_state(), state)
        self.assertTrue(Path(state["evidence_path"]).exists())

    def test_stop_restores_private_permissions_on_rotated_evidence(self):
        self.bridge.start()
        evidence = self.local / "collector-evidence/otel.json"
        backup = evidence.with_name("otel-2026-09-13T00-00-00.000.json")
        backup.write_text('{"resourceSpans":[]}\n')
        for path in (evidence, backup):
            path.chmod(0o644)
        with self.assertRaisesRegex(AppError, "rotation"):
            self.bridge.status()
        self.bridge.stop()
        for path in (evidence, backup):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stop_does_not_chmod_symlink_or_hardlink_evidence(self):
        self.bridge.start()
        target = self.local / "unrelated"
        target.write_text("not collector evidence")
        target.chmod(0o640)
        backup = self.local / "collector-evidence/otel-foreign.json"
        backup.symlink_to(target)
        with self.assertRaisesRegex(AppError, "symlink"):
            self.bridge.stop()
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertTrue((self.local / "collector.json").exists())
        backup.unlink()
        os.link(target, backup)
        with self.assertRaisesRegex(AppError, "regular|link"):
            self.bridge.stop()
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_missing_or_malformed_state_is_not_success(self):
        for method in (self.bridge.status, self.bridge.stop):
            with self.assertRaisesRegex(AppError, "state|not started"):
                method()
        (self.local / "collector.json").write_text('{"container_id": "other"}')
        (self.local / "collector.json").chmod(0o600)
        with self.assertRaisesRegex(AppError, "state"):
            self.bridge.stop()

    def test_refuses_state_evidence_path_outside_contract(self):
        self.bridge.start()
        state = self.saved_state()
        state["evidence_path"] = "/tmp/foreign.json"
        (self.local / "collector.json").write_text(json.dumps(state))
        with self.assertRaisesRegex(AppError, "evidence_path|state"):
            self.bridge.stop()

    def test_symlink_env_and_evidence_rejected(self):
        real_env = self.local / "real-env"
        self.env_path.rename(real_env)
        self.env_path.symlink_to(real_env)
        with self.assertRaisesRegex(AppError, "symlink"):
            self.bridge.start()
        self.env_path.unlink()
        real_env.rename(self.env_path)
        evidence_dir = self.local / "collector-evidence"
        evidence_dir.mkdir(mode=0o700, exist_ok=True)
        (evidence_dir / "otel.json").symlink_to(real_env)
        with self.assertRaisesRegex(AppError, "symlink"):
            self.bridge.start()

    def test_evidence_is_preserved_on_restart_and_preflight_rejects_size_or_rotation(self):
        self.bridge.start()
        evidence = Path(self.saved_state()["evidence_path"])
        evidence.write_text('{"resourceSpans":[]}\n')
        self.bridge.stop()
        self.bridge.start()
        self.assertEqual(evidence.read_text(), '{"resourceSpans":[]}\n')
        with evidence.open("ab") as stream:
            stream.truncate(8 * 1024 * 1024)
        with self.assertRaisesRegex(AppError, "bound|size"):
            self.bridge.status()
        evidence.write_text("")
        evidence.with_name("otel-2026-09-13T00-00-00.000.json").write_text("{}\n")
        with self.assertRaisesRegex(AppError, "rotat"):
            self.bridge.status()
        self.bridge.stop()
        with self.assertRaisesRegex(AppError, "rotat"):
            self.bridge.start()

    def test_docker_failure_not_reported_as_missing_or_stopped(self):
        self.bridge.start()
        with patch.object(self.module, "run", side_effect=AppError("Docker daemon unavailable")):
            for method in (self.bridge.status, self.bridge.stop):
                with self.assertRaisesRegex(AppError, "daemon unavailable"):
                    method()
        self.assertTrue((self.local / "collector.json").exists())

    def test_start_failure_during_state_write_removes_only_created_container(self):
        with patch.object(self.module, "write_json", side_effect=AppError("state disk full")):
            with self.assertRaisesRegex(AppError, "state disk full"):
                self.bridge.start()
        self.assertFalse(self.engine.containers)

    def test_health_probe_does_not_follow_redirects_to_unrelated_service(self):
        with patch.object(self.module.urllib.request, "build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.url = "http://unrelated.invalid/health"
            self.assertFalse(self.real_probe_health(13133))

    def test_validate_reports_cleanup_failure_with_recoverable_owned_id(self):
        original = self.engine

        def fail_remove(args, **kwargs):
            if args[1] == "rm":
                raise AppError("daemon refused removal")
            return original(args, **kwargs)

        with patch.object(self.module, "run", side_effect=fail_remove):
            with self.assertRaises(AppError) as error:
                self.bridge.validate()
        self.assertIn("daemon refused removal", str(error.exception))
        self.assertIn(next(iter(self.engine.containers)), str(error.exception))

    def test_cli_commands_dispatch_and_errors_are_nonzero_redacted(self):
        for action in ("validate", "start", "status", "stop"):
            with self.subTest(action=action), patch.object(self.module, "Collector") as cls:
                getattr(cls.return_value, action).return_value = {"action": action}
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(self.module.main([action]), 0)
                self.assertEqual(json.loads(output.getvalue())["action"], action)
        with patch.object(self.module, "Collector") as cls:
            cls.return_value.status.side_effect = AppError("failed " + SYNTHETIC)
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(self.module.main(["status"]), 1)
            self.assertNotIn(SYNTHETIC, error.getvalue())
            self.assertIn("failed", error.getvalue())

    def test_local_only_start_never_reads_or_passes_connection_string(self):
        self.env_path.unlink()
        result = self.bridge.start(local_only=True)
        self.assertEqual(result["mode"], "local-only")
        self.assertEqual(self.saved_state()["mode"], "local-only")
        self.assertEqual(result["evidence_path"], str(self.local / "collector-evidence/otel.json"))
        self.assertFalse(result["azure_ingestion_proven"])
        create = next(args for args, _ in self.engine.calls if args[1] == "create")
        self.assertNotIn("--env-file", create)
        self.assertNotIn("--env", create)
        self.assertTrue(any("otelcol-local.yaml" in arg and "readonly" in arg for arg in create))
        self.assertEqual(self.bridge.status()["mode"], "local-only")
        self.assertEqual(self.bridge.stop()["mode"], "local-only")
        with self.assertRaisesRegex(AppError, "collector.env"):
            self.bridge.start()

    def test_local_only_validate_uses_file_only_config_without_synthetic_key(self):
        self.env_path.unlink()
        result = self.bridge.validate(local_only=True)
        self.assertEqual(result["mode"], "local-only")
        self.assertFalse(result["synthetic_connection_string"])
        self.assertFalse(result["azure_ingestion_proven"])
        create, options = next(call for call in self.engine.calls if call[0][1] == "create")
        self.assertNotIn("--env", create)
        self.assertNotIn("--env-file", create)
        self.assertNotIn("InstrumentationKey=", json.dumps(options))
        self.assertTrue(any("otelcol-local.yaml" in arg and "readonly" in arg for arg in create))
        self.assertFalse(self.engine.containers)

    def test_local_only_config_retains_evidence_bounds_and_has_no_azure_exporter(self):
        path = ROOT / "collector/otelcol-local.yaml"
        self.assertTrue(path.exists(), "local-only config missing")
        config = path.read_text()
        for value in ("endpoint: 0.0.0.0:4318", "endpoint: 0.0.0.0:13133",
                      "path: /health", "path: /evidence/otel.json", "format: json",
                      "max_megabytes: 10", "max_backups: 2"):
            self.assertIn(value, config)
        self.assertEqual(config.count("exporters: [file]"), 2)
        self.assertNotIn("azure_monitor", config)
        self.assertNotIn("APPLICATIONINSIGHTS_CONNECTION_STRING", config)

    def test_standard_mode_is_explicit_and_never_claims_ingestion(self):
        result = self.bridge.start()
        self.assertEqual(result["mode"], "azure")
        self.assertEqual(self.bridge.status()["mode"], "azure")
        self.assertEqual(self.bridge.stop()["mode"], "azure")
        self.assertEqual(self.bridge.validate()["mode"], "azure")
        self.assertFalse(result["azure_ingestion_proven"])

    def test_local_mode_cannot_be_relabelled_as_azure_in_state(self):
        self.bridge.start(local_only=True)
        state = self.saved_state()
        state["mode"] = "azure"
        (self.local / "collector.json").write_text(json.dumps(state))
        with self.assertRaisesRegex(AppError, "mode|ownership"):
            self.bridge.status()

    def test_mode_must_be_recognized_and_rotation_still_fails_in_local_mode(self):
        self.bridge.start(local_only=True)
        backup = self.local / "collector-evidence/otel-2026-09-13T00-00-00.000.json"
        backup.write_text("{}\n")
        with self.assertRaisesRegex(AppError, "rotation"):
            self.bridge.status()
        state = self.saved_state()
        state["mode"] = "verified"
        (self.local / "collector.json").write_text(json.dumps(state))
        with self.assertRaisesRegex(AppError, "mode"):
            self.bridge.status()

    def test_missing_or_unknown_state_mode_is_rejected_without_container_actions(self):
        self.bridge.start(local_only=True)
        original = self.saved_state()
        for mode in (None, "unknown", "", "verified"):
            state = dict(original)
            if mode is None:
                del state["mode"]
            else:
                state["mode"] = mode
            (self.local / "collector.json").write_text(json.dumps(state))
            for action in (self.bridge.status, self.bridge.stop):
                with self.subTest(mode=mode, action=action.__name__):
                    before = len(self.engine.calls)
                    with self.assertRaisesRegex(AppError, "mode"):
                        action()
                    self.assertEqual(self.engine.calls[before:], [])
                    self.assertTrue(self.bridge.state_path.exists())

    def test_cli_local_only_flag_is_explicit_and_only_for_start_or_validate(self):
        for action in ("start", "validate"):
            with self.subTest(action=action), patch.object(self.module, "Collector") as cls:
                getattr(cls.return_value, action).return_value = {"mode": "local-only"}
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.module.main([action, "--local-only"]), 0)
                getattr(cls.return_value, action).assert_called_once_with(local_only=True)
        for action in ("status", "stop"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.module.main([action, "--local-only"])
            self.assertEqual(error.exception.code, 2)

    def test_both_configs_filter_content_before_export_with_propagating_errors(self):
        processors = []
        for filename in ("otelcol.yaml", "otelcol-local.yaml"):
            config = (ROOT / "collector" / filename).read_text()
            with self.subTest(config=filename):
                self.assertIn("processors: [transform/privacy]", config)
                self.assertIn("error_mode: propagate", config)
                self.assertNotIn("error_mode: ignore", config)
                self.assertNotIn("error_mode: silent", config)
                self.assertIn('resource.attributes["copilot.audit.scenario"] != "full-content"', config)
                self.assertIn('resource.attributes["copilot.audit.scenario"] != "delegated"', config)
                self.assertNotIn('"copilot.scenario"', config)
                for context in ("span", "spanevent"):
                    self.assertIn(f"context: {context}", config)
                    for key in CONTENT_KEYS:
                        self.assertIn(f'delete_key({context}.attributes, "{key}")', config)
                processors.append(config.split("\nprocessors:\n", 1)[1].split("\nexporters:\n", 1)[0])
        self.assertEqual(len(processors), 2)
        self.assertEqual(processors[0], processors[1])


@unittest.skipUnless(os.environ.get("COLLECTOR_DOCKER_TEST") == "1", "opt-in pinned image probe")
class DockerProbeTests(unittest.TestCase):
    def test_pinned_image_config_health_jsonl_permissions_and_restart(self):
        from scripts.collector import Collector, probe_health, run

        # No shared host port: inspect the owned ID for Docker-assigned ports.
        with tempfile.TemporaryDirectory(prefix="collector-probe-") as directory:
            local = Path(directory) / ".local"
            local.mkdir(mode=0o700)
            collector = Collector(local=local, otlp_port=0, health_port=0)
            self.assertTrue(collector.validate()["validated"])
            self.assertTrue(collector.validate(local_only=True)["validated"])
            original_size = 0
            for iteration in range(2):
                try:
                    state = collector.start(local_only=True)
                    self.assertEqual(state["mode"], "local-only")
                    ports = json.loads(run([
                        "docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}",
                        state["container_id"],
                    ]).stdout)
                    otlp_port = int(ports["4318/tcp"][0]["HostPort"])
                    self.assertEqual(ports["4318/tcp"][0]["HostIp"], "127.0.0.1")
                    self.assertTrue(probe_health(state["health_port"]))
                    run_id = str(uuid.uuid4())
                    now = time.time_ns()
                    resource = {"attributes": [{"key": "copilot.run.id",
                                               "value": {"stringValue": run_id}}]}
                    span = {"traceId": "01" * 16, "spanId": "02" * 8,
                            "name": "collector-synthetic-probe", "kind": 1,
                            "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000000),
                            "events": [{"name": "probe.event", "timeUnixNano": str(now)}]}
                    payloads = {
                        "traces": {"resourceSpans": [{"resource": resource, "scopeSpans": [
                            {"scope": {"name": "collector-test"}, "spans": [span]}]}]},
                        "metrics": {"resourceMetrics": [{"resource": resource, "scopeMetrics": [
                            {"scope": {"name": "collector-test"}, "metrics": [
                                {"name": "probe.count", "gauge": {"dataPoints": [
                                    {"timeUnixNano": str(now), "asInt": "1"}]}}]}]}]},
                    }
                    for signal, payload in payloads.items():
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{otlp_port}/v1/{signal}",
                            data=json.dumps(payload).encode(),
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(request, timeout=5) as response:
                            self.assertEqual(response.status, 200)
                    evidence = Path(state["evidence_path"])
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        lines = evidence.read_text().splitlines()
                        records = [json.loads(line) for line in lines]
                        matching = [record for record in records if run_id in json.dumps(record)]
                        if len(matching) >= 2:
                            break
                        time.sleep(0.1)
                    self.assertTrue(any("resourceSpans" in record for record in matching))
                    self.assertTrue(any("resourceMetrics" in record for record in matching))
                    self.assertIn("probe.event", json.dumps(matching))
                    self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
                    self.assertGreater(evidence.stat().st_size, original_size)
                    original_size = evidence.stat().st_size
                    self.assertEqual(len(records), 2 * (iteration + 1))
                    if iteration == 1:
                        # Exercise the actual v0.160.0 rotation writer, not a YAML proxy.
                        span["attributes"] = [{"key": "probe.padding",
                                               "value": {"stringValue": "x" * (1024 * 1024)}}]
                        for _ in range(34):
                            request = urllib.request.Request(
                                f"http://127.0.0.1:{otlp_port}/v1/traces",
                                data=json.dumps(payloads["traces"]).encode(),
                                headers={"Content-Type": "application/json"})
                            with urllib.request.urlopen(request, timeout=5) as response:
                                self.assertEqual(response.status, 200)
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            backups = list(evidence.parent.glob("otel-*.json"))
                            if len(backups) == 2:
                                break
                            time.sleep(0.1)
                        self.assertEqual(len(backups), 2)
                        self.assertEqual(evidence.parent.stat().st_mode & 0o777, 0o700)
                        for path in (evidence, *backups):
                            self.assertLessEqual(path.stat().st_size, 10 * 1024 * 1024)
                            # The pinned rotation writer creates 0644 files, contained
                            # by the 0700 directory until stop restores 0600.
                            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                        with self.assertRaisesRegex(AppError, "rotation"):
                            collector.status()
                finally:
                    if (local / "collector.json").exists():
                        collector.stop()
                for path in evidence.parent.glob("otel*.json"):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse((local / "collector.json").exists())

    def test_occupied_loopback_port_fails_and_cleans_up_without_disrupting_listener(self):
        from scripts.collector import Collector, run

        with tempfile.TemporaryDirectory(prefix="collector-collision-") as directory:
            local = Path(directory) / ".local"
            local.mkdir(mode=0o700)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                collector = Collector(local=local, otlp_port=port, health_port=0)
                try:
                    with self.assertRaises(AppError):
                        collector.start(local_only=True)
                    self.assertFalse((local / "collector.json").exists())
                    remaining = run(["docker", "ps", "--all", "--quiet",
                                     "--filter", f"name=^/{collector.name}$"]).stdout.strip()
                    self.assertEqual(remaining, "")
                    with socket.create_connection(("127.0.0.1", port), timeout=2):
                        pass
                finally:
                    if (local / "collector.json").exists():
                        collector.stop()

    def test_local_only_accepts_otlp_http_protobuf_and_preserves_run_id(self):
        from scripts.collector import Collector, run

        def varint(value):
            result = bytearray()
            while value >= 128:
                result.append((value & 127) | 128)
                value >>= 7
            result.append(value)
            return bytes(result)

        def message(field, value):
            return varint((field << 3) | 2) + varint(len(value)) + value

        def fixed64(field, value):
            return varint((field << 3) | 1) + value.to_bytes(8, "little")

        with tempfile.TemporaryDirectory(prefix="collector-protobuf-") as directory:
            collector = Collector(local=Path(directory) / ".local", otlp_port=0, health_port=0)
            try:
                state = collector.start(local_only=True)
                cid = state["container_id"]
                ports = json.loads(run(["docker", "inspect", "--format",
                                        "{{json .NetworkSettings.Ports}}", cid]).stdout)
                port = int(ports["4318/tcp"][0]["HostPort"])
                run_id = str(uuid.uuid4())
                # Encode the minimal OTLP protobuf wire messages with stdlib only.
                attr = message(1, b"copilot.run.id") + message(2, message(1, run_id.encode()))
                resource = message(1, message(1, attr))
                now = time.time_ns()
                span = (message(1, b"\x01" * 16) + message(2, b"\x02" * 8) +
                        message(5, b"protobuf-local-probe") +
                        fixed64(7, now) + fixed64(8, now + 1000000) +
                        message(11, fixed64(1, now) + message(2, b"protobuf.event")))
                traces = message(1, resource + message(2, message(2, span)))
                point = fixed64(3, now) + fixed64(6, 1)
                metric = message(1, b"protobuf.count") + message(5, message(1, point))
                metrics = message(1, resource + message(2, message(2, metric)))
                for signal, body in (("traces", traces), ("metrics", metrics)):
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/{signal}", data=body,
                        headers={"Content-Type": "application/x-protobuf"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                evidence = Path(state["evidence_path"])
                self.assertTrue(evidence.is_absolute())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    lines = evidence.read_text().splitlines()
                    if len(lines) == 2:
                        break
                    time.sleep(0.1)
                records = [json.loads(line) for line in lines]
                self.assertEqual(len(records), 2)
                self.assertTrue(all(run_id in json.dumps(record) for record in records))
                self.assertTrue(any("resourceSpans" in record for record in records))
                self.assertTrue(any("resourceMetrics" in record for record in records))
                self.assertIn("protobuf.event", json.dumps(records))
                self.assertEqual(collector.status()["mode"], "local-only")
                self.assertFalse(collector.status()["azure_ingestion_proven"])
            finally:
                if collector.state_path.exists():
                    collector.stop()
            self.assertFalse(collector._exists(cid))

    def test_real_privacy_filter_strips_span_and_event_content_except_explicit_scenarios(self):
        from scripts.collector import Collector, run

        with tempfile.TemporaryDirectory(prefix="collector-privacy-") as directory:
            collector = Collector(local=Path(directory) / ".local", otlp_port=0, health_port=0)
            try:
                state = collector.start(local_only=True)
                cid = state["container_id"]
                ports = json.loads(run(["docker", "inspect", "--format",
                                        "{{json .NetworkSettings.Ports}}", cid]).stdout)
                port = int(ports["4318/tcp"][0]["HostPort"])
                now = time.time_ns()
                resources = []
                scenarios = (
                    ("metadata-only", None), (None, None), ("unrecognized", None),
                    ("full-content", None), ("delegated", None),
                    (None, "full-content"), (None, "delegated"),
                    ("metadata-only", "full-content"), ("unrecognized", "delegated"),
                    ("full-content", "metadata-only"),
                )
                for index, (scenario, undocumented_alias) in enumerate(scenarios):
                    run_id = str(uuid.uuid4())
                    resource_attributes = [{"key": "copilot.run.id", "value": {"stringValue": run_id}}]
                    if scenario is not None:
                        resource_attributes.append({"key": "copilot.audit.scenario",
                                                    "value": {"stringValue": scenario}})
                    if undocumented_alias is not None:
                        resource_attributes.append({"key": "copilot.scenario",
                                                    "value": {"stringValue": undocumented_alias}})
                    content = [{"key": key, "value": {"stringValue": "x" * 841}}
                               for key in CONTENT_KEYS]
                    safe = [{"key": "gen_ai.request.model", "value": {"stringValue": "synthetic"}},
                            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "123"}},
                            # A span-local scenario must not override the resource policy.
                            {"key": "copilot.audit.scenario", "value": {"stringValue": "full-content"}},
                            {"key": "copilot.scenario", "value": {"stringValue": "full-content"}}]
                    span = {"traceId": f"{index + 1:032x}", "spanId": f"{index + 1:016x}",
                            "name": "chat/invoke", "startTimeUnixNano": str(now),
                            "endTimeUnixNano": str(now + 1000000), "attributes": content + safe,
                            "events": [{"name": "tool.event", "timeUnixNano": str(now),
                                        "attributes": content + safe}]}
                    resources.append({"resource": {"attributes": resource_attributes},
                                      "scopeSpans": [{"spans": [span]}]})
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/traces",
                    data=json.dumps({"resourceSpans": resources}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                evidence = Path(state["evidence_path"])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    content = evidence.read_text()
                    if content.endswith("\n"):
                        break
                    time.sleep(0.1)
                exported = json.loads(content)["resourceSpans"]
                self.assertEqual(len(exported), len(resources))
                for expected, actual, (scenario, undocumented_alias) in zip(resources, exported, scenarios):
                    with self.subTest(scenario=scenario, undocumented_alias=undocumented_alias):
                        self.assertEqual(actual["resource"], expected["resource"])
                        span = actual["scopeSpans"][0]["spans"][0]
                        for attributes in (span["attributes"], span["events"][0]["attributes"]):
                            mapping = {item["key"]: item["value"] for item in attributes}
                            for key in CONTENT_KEYS:
                                if scenario in ("full-content", "delegated"):
                                    self.assertEqual(mapping.get(key), {"stringValue": "x" * 841})
                                else:
                                    self.assertNotIn(key, mapping.keys())
                            self.assertEqual(mapping["gen_ai.request.model"], {"stringValue": "synthetic"})
                            self.assertEqual(mapping["gen_ai.usage.input_tokens"], {"intValue": "123"})
                self.assertTrue(collector.status()["healthy"])
            finally:
                if collector.state_path.exists():
                    collector.stop()
            self.assertFalse(collector._exists(cid))


if __name__ == "__main__":
    unittest.main()
