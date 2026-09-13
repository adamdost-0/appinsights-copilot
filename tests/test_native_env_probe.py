"""Synthetic protocol fixtures only; these tests never invoke a real CLI."""

import base64
from copy import deepcopy
import http.client
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
from urllib.parse import urlsplit

from scripts import probe_native_env as probe
from scripts.common import AppError

RUN_ID = "00000000-0000-4000-8000-000000000001"
BASE = "http://127.0.0.1:12345"


def json_metrics(kind="exponentialHistogram", temporality=1):
    data = {"dataPoints": [{}]}
    if temporality is not None:
        data["aggregationTemporality"] = temporality
    return {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
        {"name": "synthetic.duration", kind: data}
    ]}]}]}


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def message(field, value):
    return varint(field * 8 + 2) + varint(len(value)) + value


def protobuf_metrics(kind=10, temporality=1):
    data = message(1, b"") + varint(2 * 8) + varint(temporality)
    metric = message(1, b"synthetic.duration") + message(kind, data)
    return message(1, message(2, message(2, metric)))


def records(protocol="http/json", kind="exponentialHistogram", temporality=1):
    if protocol == "http/json":
        trace = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{}]}]}]}).encode()
        metric = json.dumps(json_metrics(kind, temporality)).encode()
        content_type = "application/json"
    else:
        trace = message(1, message(2, message(2, b"")))
        metric = protobuf_metrics(10 if kind == "exponentialHistogram" else 9, temporality)
        content_type = "application/x-protobuf"
    return [
        probe.RequestRecord("/native-traces", content_type, True, trace),
        probe.RequestRecord("/native-metrics", content_type, True, metric),
    ]


class AssessmentTests(unittest.TestCase):
    def assess(self, requests=None, protocol="http/json"):
        return probe.assess_requests(records(protocol) if requests is None else requests,
                                     protocol, probe.endpoint_urls(BASE))

    def test_exact_signal_urls_differ_from_generic_base(self):
        self.assertEqual(probe.endpoint_urls(BASE), {
            "generic": BASE + "/base",
            "traces": BASE + "/native-traces",
            "metrics": BASE + "/native-metrics",
        })

    def test_only_ephemeral_style_loopback_http_origins_are_accepted(self):
        for url in ("https://127.0.0.1:12345", "http://example.com:12345",
                    "http://localhost:12345", BASE + "/path", BASE + "?q=x",
                    "http://user@127.0.0.1:12345", "http://127.0.0.1",
                    "http://127.0.0.1:0", BASE + "#fragment", "http://127.0.0.1:99999"):
            with self.subTest(url=url), self.assertRaises(AppError):
                probe.endpoint_urls(url)

    def test_isolated_environment_uses_synthetic_header_and_all_overrides(self):
        inherited = {
            "PATH": "/usr/bin", "LANG": "C", "AZURE_TOKEN": "private",
            "HOME": "/private", "COPILOT_HOME": "/private/config",
            "NODE_OPTIONS": "--require=/private/code", "HTTPS_PROXY": "http://private",
            "OTEL_EXPORTER_OTLP_HEADERS": "private",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "private",
            "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "/private",
        }
        env = probe.probe_environment(Path("/synthetic"), RUN_ID, "github-synthetic",
                                      "http/json", BASE, inherited)
        self.assertEqual(env["HOME"], "/synthetic")
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "github-synthetic")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_HEADERS"],
                         "Authorization=Bearer%20synthetic-probe")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"], BASE + "/native-traces")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"], BASE + "/native-metrics")
        for signal in ("", "_TRACES", "_METRICS"):
            self.assertEqual(env[f"OTEL_EXPORTER_OTLP{signal}_PROTOCOL"], "http/json")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"], "DELTA")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION"],
                         "base2_exponential_bucket_histogram")
        self.assertEqual(env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")
        self.assertNotIn("private", json.dumps(env))
        self.assertNotIn("OTEL_EXPORTER_OTLP_TRACES_HEADERS", env)

    def test_invalid_protocol_is_rejected(self):
        with self.assertRaises(AppError):
            probe.probe_environment(Path("/synthetic"), RUN_ID, "x", "grpc", BASE, {})

    def test_all_required_capabilities_pass_for_both_protocols(self):
        for protocol in probe.PROTOCOLS:
            with self.subTest(protocol=protocol):
                result = self.assess(protocol=protocol)
                self.assertEqual(result["status"], "compatible")
                self.assertTrue(result["native_metrics_configuration_supported"])
                self.assertFalse(result["azure_ingestion_proven"])
                self.assertEqual(set(result["capabilities"].values()), {"pass"})
                self.assertEqual(result["request_count"], 2)
                self.assertEqual(result["span_count"], 1)
                self.assertEqual(result["metrics"], [
                    {"kind": "exponentialHistogram", "temporality": 1, "data_point_count": 1}
                ])
                self.assertNotIn("synthetic.duration", json.dumps(result))

    def test_observed_cumulative_explicit_histograms_are_incompatible(self):
        for protocol in probe.PROTOCOLS:
            result = self.assess(records(protocol, "histogram", 2), protocol)
            self.assertEqual(result["status"], "incompatible")
            self.assertFalse(result["native_metrics_configuration_supported"])
            self.assertEqual(result["capabilities"]["delta_temporality"], "fail")
            self.assertEqual(result["capabilities"]["exponential_histograms"], "fail")

    def test_delta_accepts_enum_name_but_never_boolean_or_numeric_string(self):
        for value, expected in ((1, "pass"), ("AGGREGATION_TEMPORALITY_DELTA", "pass"),
                                (True, "fail"), (False, "fail"), ("1", "fail"),
                                (None, "fail"), (0, "fail"), (2, "fail")):
            with self.subTest(value=value):
                result = self.assess(records(temporality=value))
                self.assertEqual(result["capabilities"]["delta_temporality"], expected)

    def test_wrong_histogram_kind_and_empty_points_do_not_pass(self):
        for kind in ("gauge", "sum", "summary", "histogram"):
            with self.subTest(kind=kind):
                self.assertEqual(self.assess(records(kind=kind))["capabilities"]
                                 ["exponential_histograms"], "fail")
        request = records()[1]
        body = json_metrics()
        body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0][
            "exponentialHistogram"]["dataPoints"] = []
        request.body = json.dumps(body).encode()
        self.assertNotEqual(self.assess([records()[0], request])["status"], "compatible")

    def test_missing_or_single_signal_is_not_success_or_measured_metrics_support(self):
        for subset in ([], records()[:1], records()[1:]):
            with self.subTest(count=len(subset)):
                result = self.assess(subset)
                self.assertEqual(result["status"], "transport_error")
                self.assertFalse(result["azure_ingestion_proven"])
                self.assertFalse(result["native_metrics_configuration_supported"])
                self.assertIn("not_observed", result["capabilities"].values())

    def test_generic_appended_and_query_paths_fail_exact_override(self):
        for path in ("/base/v1/traces", "/native-traces/v1/traces",
                     "/native-traces?x=1", BASE + "/native-traces"):
            requests = records()
            requests[0].path = path
            result = self.assess(requests)
            self.assertEqual(result["capabilities"]["traces_endpoint_override"], "fail")
            self.assertEqual(result["status"], "incompatible")

    def test_all_requests_must_match_auth_and_content_type(self):
        for field, value, capability in (
                ("authorization_matches", False, "authorization_header_decoding"),
                ("content_type", "application/x-protobuf", "protocol_content_type"),
                ("content_type", "text/plain", "protocol_content_type")):
            requests = records()
            setattr(requests[0], field, value)
            result = self.assess(requests)
            self.assertEqual(result["capabilities"][capability], "fail")
            self.assertNotEqual(result["status"], "compatible")

    def test_json_content_type_allows_charset(self):
        requests = records()
        requests[0].content_type = "application/json; charset=utf-8"
        self.assertEqual(self.assess(requests)["capabilities"]["protocol_content_type"], "pass")

    def test_malformed_payloads_are_transport_errors_without_raw_diagnostics(self):
        for protocol, body in (("http/json", b"private-not-json"),
                               ("http/json", b'{"resourceMetrics":true}'),
                               ("http/json", b'{"resourceSpans":[{"scopeSpans":true}]}'),
                               ("http/protobuf", b"\x0a\xff"),
                               ("http/protobuf", b"\x00"),
                               ("http/protobuf", b"\x0b")):
            requests = records(protocol)
            requests[0].body = body
            result = self.assess(requests, protocol)
            self.assertEqual(result["status"], "transport_error")
            self.assertNotIn("private-not-json", json.dumps(result))

    def test_duplicate_metric_oneof_is_not_supported(self):
        requests = records()
        body = json_metrics()
        metric = body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
        metric["histogram"] = deepcopy(metric["exponentialHistogram"])
        requests[1].body = json.dumps(body).encode()
        self.assertEqual(self.assess(requests)["status"], "transport_error")

    def test_protobuf_unknown_fields_are_skipped_but_truncation_is_rejected(self):
        requests = records("http/protobuf")
        requests[1].body += varint(20 * 8 + 1) + b"\0" * 8
        requests[1].body += varint(21 * 8 + 5) + b"\0" * 4
        self.assertEqual(self.assess(requests, "http/protobuf")["status"], "compatible")
        requests[1].body = requests[1].body[:-1]
        self.assertEqual(self.assess(requests, "http/protobuf")["status"], "transport_error")

    def test_ignored_protocol_with_valid_other_encoding_is_measured_incompatibility(self):
        result = self.assess(records("http/protobuf"), "http/json")
        self.assertEqual(result["status"], "incompatible")
        self.assertEqual(result["capabilities"]["protocol_content_type"], "fail")
        self.assertEqual(result["capabilities"]["metrics_export"], "pass")

    def test_empty_exponential_histogram_does_not_prove_requested_aggregation(self):
        requests = records()
        body = json_metrics("sum")
        metrics = body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        metrics.append({"name": "synthetic.empty", "exponentialHistogram": {
            "aggregationTemporality": 1, "dataPoints": []}})
        requests[1].body = json.dumps(body).encode()
        result = self.assess(requests)
        self.assertFalse(result["native_metrics_configuration_supported"])
        self.assertNotEqual(result["capabilities"]["exponential_histograms"], "pass")

    def test_metric_projection_is_bounded_and_limit_is_explicit(self):
        requests = records()
        body = json_metrics()
        metrics = body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        metrics *= probe.MAX_METRIC_OBSERVATIONS + 1
        requests[1].body = json.dumps(body).encode()
        result = self.assess(requests)
        self.assertEqual(result["status"], "transport_error")
        self.assertIn("metric_observation_limit", result["errors"])
        self.assertLessEqual(len(result["metrics"]), probe.MAX_METRIC_OBSERVATIONS)
        self.assertFalse(result["native_metrics_configuration_supported"])


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "evidence"

    def post(self, recorder, body=b"{}", headers=None, path="/native-traces"):
        connection = http.client.HTTPConnection("127.0.0.1", recorder.port, timeout=2)
        self.addCleanup(connection.close)
        connection.request("POST", path, body, headers or {
            "Content-Type": "application/json",
            "Authorization": "Bearer synthetic-probe",
        })
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read()

    def test_private_raw_records_and_valid_empty_export_responses(self):
        for protocol in probe.PROTOCOLS:
            output = self.output / protocol.split("/")[-1]
            with probe.RecordingServer(output, protocol) as recorder:
                request = records(protocol)[0]
                status, content_type, body = self.post(recorder, request.body, {
                    "Content-Type": request.content_type,
                    "Authorization": "Bearer synthetic-probe",
                })
                self.assertEqual(status, 200)
                self.assertEqual(content_type, probe.CONTENT_TYPES[protocol])
                self.assertEqual(body, b"{}" if protocol == "http/json" else b"")
                self.assertTrue(recorder.records[0].authorization_matches)
            self.assertFalse(recorder.thread.is_alive())
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            files = list(output.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            raw = json.loads(files[0].read_text())
            self.assertEqual(base64.b64decode(raw["body_base64"]), request.body)
            self.assertNotIn("Bearer synthetic-probe", files[0].read_text())
            self.assertEqual(recorder.evidence_bytes, files[0].stat().st_size)

    def test_wrong_authorization_is_recorded_only_as_false(self):
        with probe.RecordingServer(self.output, "http/json") as recorder:
            self.post(recorder, headers={"Authorization": "Bearer must-not-save",
                                         "Content-Type": "application/json"})
        self.assertFalse(recorder.records[0].authorization_matches)
        self.assertNotIn("must-not-save", next(self.output.glob("*.json")).read_text())

    def test_payload_total_evidence_and_request_count_limits(self):
        for kwargs, body, expected in (
                ({"max_payload_bytes": 3}, b"1234", "payload_limit"),
                ({"max_evidence_bytes": 10}, b"{}", "evidence_limit"),
                ({"max_requests": 1}, b"{}", "request_limit")):
            with self.subTest(kwargs=kwargs):
                with probe.RecordingServer(self.output / expected, "http/json", **kwargs) as recorder:
                    if expected == "request_limit":
                        self.assertEqual(self.post(recorder)[0], 200)
                    self.assertEqual(self.post(recorder, body)[0], 413)
                self.assertIn(expected, recorder.errors)
                self.assertLessEqual(recorder.evidence_bytes, recorder.max_evidence_bytes)

    def test_total_limit_is_cumulative_not_per_request(self):
        with probe.RecordingServer(self.output, "http/json", max_evidence_bytes=300) as recorder:
            self.assertEqual(self.post(recorder)[0], 200)
            self.assertEqual(self.post(recorder)[0], 200)
            self.assertEqual(self.post(recorder)[0], 413)
        self.assertEqual(len(recorder.records), 2)
        self.assertLessEqual(sum(path.stat().st_size for path in self.output.glob("*.json")), 300)

    def raw_request(self, recorder, request):
        connection = socket.create_connection(("127.0.0.1", recorder.port), timeout=2)
        self.addCleanup(connection.close)
        connection.sendall(request)
        response = bytearray()
        while True:
            data = connection.recv(4096)
            if not data:
                break
            response.extend(data)
        return bytes(response)

    def test_rejects_ambiguous_lengths_and_unsupported_compression(self):
        for headers in (
                b"Content-Length: -1\r\n",
                b"Content-Length: nope\r\n",
                b"Content-Length: 2\r\nContent-Length: 3\r\n",
                b"Transfer-Encoding: chunked\r\n",
                b"Content-Length: 2\r\nTransfer-Encoding: \r\n",
                b"Content-Length: 2\r\nContent-Type: application/json\r\nContent-Type: text/plain\r\n",
                b"Content-Length: 2\r\nContent-Encoding: gzip\r\n"):
            with self.subTest(headers=headers):
                with probe.RecordingServer(self.output, "http/json") as recorder:
                    response = self.raw_request(recorder, b"POST /native-traces HTTP/1.1\r\n"
                                                b"Host: localhost\r\n" + headers + b"\r\n{}")
                self.assertIn(b" 400 ", response)
                self.assertIn("invalid_request", recorder.errors)
                self.assertEqual(recorder.records, [])

    def test_malformed_request_line_gets_bounded_error_response(self):
        with probe.RecordingServer(self.output, "http/json") as recorder:
            response = self.raw_request(recorder, b"POST /native-traces BAD/VERSION\r\n\r\n")
        self.assertIn(b"400", response)
        self.assertIn("invalid_request", recorder.errors)
        self.assertNotIn("handler_error", recorder.errors)

    def test_payload_limit_accepts_exact_boundary_and_rejects_one_more(self):
        with probe.RecordingServer(self.output, "http/json", max_payload_bytes=2) as recorder:
            self.assertEqual(self.post(recorder, b"{}")[0], 200)
            self.assertEqual(self.post(recorder, b"{} ")[0], 413)

    def test_duplicate_auth_header_is_not_a_match(self):
        with probe.RecordingServer(self.output, "http/json") as recorder:
            self.raw_request(recorder, b"POST /native-traces HTTP/1.1\r\nContent-Length: 2\r\n"
                             b"Authorization: Bearer synthetic-probe\r\n"
                             b"Authorization: Bearer synthetic-probe\r\n\r\n{}")
        self.assertFalse(recorder.records[0].authorization_matches)

    def test_partial_body_and_header_trickle_have_absolute_deadline(self):
        for partial in (b"POST /native-traces HTTP/1.1\r\nHost: ",
                        b"POST /native-traces HTTP/1.1\r\nContent-Length: 10\r\n\r\n{"):
            with self.subTest(partial=partial):
                with probe.RecordingServer(self.output, "http/json", request_timeout=0.15) as recorder:
                    start = time.monotonic()
                    self.raw_request(recorder, partial)
                    self.assertLess(time.monotonic() - start, 1)
                self.assertIn("request_timeout", recorder.errors)
                self.assertEqual(recorder.records, [])
                self.assertFalse(recorder.thread.is_alive())

    def test_cleanup_on_body_exception_closes_socket_and_thread(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with probe.RecordingServer(self.output, "http/json") as recorder:
                port = recorder.port
                raise RuntimeError("synthetic")
        self.assertFalse(recorder.thread.is_alive())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.1)

    def test_cleanup_error_is_explicit_and_still_joins_thread(self):
        recorder = probe.RecordingServer(self.output, "http/json")
        recorder.__enter__()
        actual_close = recorder.server.server_close

        def failing_close():
            actual_close()
            raise OSError("synthetic cleanup error")

        with patch.object(recorder.server, "server_close", side_effect=failing_close):
            with self.assertRaisesRegex(AppError, "cleanup"):
                recorder.close()
        self.assertFalse(recorder.thread.is_alive())

    def test_write_failure_is_not_acknowledged_as_success(self):
        with probe.RecordingServer(self.output, "http/json") as recorder:
            with patch.object(probe, "write_private", side_effect=AppError("private-details")):
                self.assertEqual(self.post(recorder)[0], 500)
        self.assertIn("evidence_write_error", recorder.errors)
        self.assertEqual(recorder.records, [])

    def test_worker_failure_is_reported_without_unhandled_thread_exception(self):
        recorder = probe.RecordingServer(self.output, "http/json")
        with patch.object(recorder.server, "handle_request", side_effect=OSError("private-error")):
            with recorder:
                recorder.thread.join(timeout=1)
        self.assertIn("recorder_worker_failed", recorder.errors)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.local = Path(self.temporary.name) / ".local"
        self.addCleanup(patch.stopall)
        patch.object(probe, "LOCAL", self.local).start()
        self.authenticate = patch.object(probe.run_smoke, "authentication_token",
                                         return_value="synthetic-github-secret").start()
        patch.object(probe.shutil, "which", return_value="/synthetic/bin/copilot").start()
        self.version = patch.object(probe, "run", return_value=subprocess.CompletedProcess(
            ["copilot", "--version"], 0, "GitHub Copilot CLI 1.0.84-5\nprivate extra text", "")).start()
        self.launch = patch.object(probe, "run_session", side_effect=self.fake_cli).start()
        self.homes = []
        self.protocols = []
        self.metric_kind = "exponentialHistogram"
        self.temporality = 1

    def fake_cli(self, command, *, env, cwd, timeout):
        self.assertEqual(command[0], "/synthetic/bin/copilot")
        self.assertEqual(timeout, 12)
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "synthetic-github-secret")
        self.assertEqual(env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"], "false")
        self.assertIn("--no-custom-instructions", command)
        self.assertIn("--disable-builtin-mcps", command)
        self.assertIn("--available-tools", command)
        self.assertNotIn("task", command)
        self.assertEqual(list(cwd.iterdir()), [])
        self.assertTrue(Path(env["HOME"]).is_dir())
        self.assertEqual(Path(env["HOME"]).stat().st_mode & 0o777, 0o700)
        self.homes.append(Path(env["HOME"]))
        self.protocols.append(env["OTEL_EXPORTER_OTLP_PROTOCOL"])
        for signal, record in zip(("TRACES", "METRICS"),
                                  records(self.protocols[-1], self.metric_kind, self.temporality)):
            url = urlsplit(env[f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT"])
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=2)
            try:
                connection.request("POST", url.path, record.body, {
                    "Content-Type": record.content_type, "Authorization": "Bearer synthetic-probe"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
            finally:
                connection.close()
        return subprocess.CompletedProcess(command, 0, "do-not-save-cli-output", "private-system-text")

    def test_consent_is_required_before_auth_cli_or_private_state(self):
        with self.assertRaisesRegex(AppError, "--run-cli"):
            probe.execute()
        self.authenticate.assert_not_called()
        self.version.assert_not_called()
        self.launch.assert_not_called()
        self.assertFalse(self.local.exists())
        with patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(probe.main([]), 2)
        self.assertIn("--run-cli", error.getvalue())

    def test_timeout_is_positive_and_capped_before_any_cli_invocation(self):
        for value in (0, -1, True, 601):
            with self.subTest(value=value), self.assertRaises(AppError):
                probe.execute(run_cli=True, timeout_seconds=value)
        self.launch.assert_not_called()
        self.authenticate.assert_not_called()

    def test_fixed_protocols_private_report_and_fresh_homes(self):
        with patch.dict(os.environ, {"NODE_OPTIONS": "private", "AZURE_TOKEN": "private",
                                     "COPILOT_HOME": "/private"}):
            report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(self.protocols, ["http/json", "http/protobuf"])
        self.assertEqual(len(set(self.homes)), 2)
        self.assertTrue(all(not path.exists() for path in self.homes))
        self.assertEqual(report["status"], "compatible")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["cli_version"], "1.0.84-5")
        self.assertFalse(report["azure_ingestion_proven"])
        self.assertTrue(report["native_metrics_configuration_supported"])
        self.assertEqual(len({item["run_id"] for item in report["scenarios"]}), 2)
        path = Path(report["report_path"])
        self.assertTrue(path.is_relative_to(self.local / "native-probe"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), report)
        self.assertEqual(report["request_count"], 4)
        for item in report["scenarios"]:
            self.assertEqual(item["request_count"], 2)
            self.assertTrue(Path(item["records_path"]).is_dir())
            self.assertEqual(item["cli_version"], "1.0.84-5")
        raw = "".join(file.read_text() for file in path.parent.rglob("*.json"))
        for secret in ("synthetic-github-secret", "do-not-save-cli-output", "private-system-text",
                       "private extra text"):
            self.assertNotIn(secret, raw)
        self.assertLessEqual(sum(file.stat().st_size for file in path.parent.rglob("*.json")),
                             probe.MAX_EVIDENCE_BYTES)
        for call in self.version.call_args_list:
            self.assertEqual(call.kwargs["env"]["COPILOT_OTEL_ENABLED"], "false")
            self.assertEqual(call.kwargs["env"]["OTEL_SDK_DISABLED"], "true")

    def test_measured_cli_incompatibility_is_persistent_nonzero_not_internal_error(self):
        self.metric_kind = "histogram"
        self.temporality = 2
        report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(report["status"], "incompatible")
        self.assertEqual(report["exit_code"], 1)
        self.assertFalse(report["native_metrics_configuration_supported"])
        self.assertTrue(all(item["status"] == "incompatible" for item in report["scenarios"]))
        self.assertTrue(Path(report["report_path"]).exists())

    def test_cli_failure_is_transport_error_and_does_not_expose_output(self):
        self.launch.side_effect = AppError("synthetic-github-secret private-output")
        report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(report["status"], "transport_error")
        self.assertEqual(report["exit_code"], 3)
        self.assertFalse(report["native_metrics_configuration_supported"])
        self.assertIn("cli_execution_failed", report["scenarios"][0]["errors"])
        self.assertNotIn("private-output", json.dumps(report))

    def test_no_requests_is_transport_error(self):
        self.launch.side_effect = None
        self.launch.return_value = subprocess.CompletedProcess([], 0, "", "")
        report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(report["request_count"], 0)

    def test_authentication_failure_is_safe_persistent_internal_error(self):
        self.authenticate.side_effect = AppError("private-auth-diagnostic")
        report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(report["status"], "internal_error")
        self.assertEqual(report["exit_code"], 2)
        self.assertIn("github_authentication_failed", report["errors"])
        self.assertNotIn("private-auth-diagnostic", json.dumps(report))
        self.launch.assert_not_called()
        self.assertTrue(Path(report["report_path"]).is_file())

    def test_cleanup_failure_overrides_success_and_is_reported(self):
        actual_close = probe.RecordingServer.close

        def failing_close(recorder):
            actual_close(recorder)
            raise AppError("private-cleanup-diagnostic")

        with patch.object(probe.RecordingServer, "close", failing_close):
            report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(report["status"], "internal_error")
        self.assertEqual(report["exit_code"], 2)
        self.assertFalse(report["native_metrics_configuration_supported"])
        self.assertIn("scenario_setup_or_cleanup_failed", report["scenarios"][0]["errors"])
        self.assertTrue(all(not home.exists() for home in self.homes))
        self.assertNotIn("private-cleanup-diagnostic", json.dumps(report))

    def test_main_emits_report_and_returns_measured_exit_code(self):
        self.metric_kind = "histogram"
        self.temporality = 2
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = probe.main(["--run-cli", "--timeout-seconds", "12"])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "incompatible")
        self.assertFalse(report["azure_ingestion_proven"])

    def test_bad_version_fails_before_inference(self):
        self.version.return_value.stdout = "private-unrecognized-version"
        report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(report["exit_code"], 2)
        self.launch.assert_not_called()
        self.assertNotIn("private-unrecognized-version", json.dumps(report))

    def test_metric_observation_limit_still_persists_complete_error_report(self):
        def oversized_metrics(command, *, env, cwd, timeout):
            result = self.fake_cli(command, env=env, cwd=cwd, timeout=timeout)
            if env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json":
                body = json_metrics()
                body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"] *= probe.MAX_METRIC_OBSERVATIONS + 1
                url = urlsplit(env["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"])
                connection = http.client.HTTPConnection(url.hostname, url.port, timeout=2)
                try:
                    connection.request("POST", url.path, json.dumps(body), {
                        "Content-Type": "application/json", "Authorization": "Bearer synthetic-probe"})
                    response = connection.getresponse()
                    response.read()
                finally:
                    connection.close()
            return result

        self.launch.side_effect = oversized_metrics
        report = probe.execute(run_cli=True, timeout_seconds=12)
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(json.loads(Path(report["report_path"]).read_text()), report)


if __name__ == "__main__":
    unittest.main()
