"""Bounded, opt-in CLI environment compatibility diagnostic, NOT an OTLP collector.

Run ``python3 -m scripts.probe_native_env --run-cli`` to spend two synthetic CLI
inferences (JSON, then protobuf). Only a temporary loopback HTTP recording fixture
receives telemetry; nothing is forwarded and Azure ingestion is never proven.
Raw evidence is sensitive despite metadata-only capture and belongs only in the
private .local/native-probe/<uuid> directory. Do not publish raw records.

Public helpers: endpoint_urls, probe_environment, assess_requests, RequestRecord,
RecordingServer, execute, main. Exit codes: 0 compatible, 1 measured incompatibility,
2 consent/configuration/internal error, 3 transport or CLI execution failure.
"""

from dataclasses import dataclass
import argparse
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from .common import AppError, LOCAL, private_dir, run, run_session, write_private
from . import run_smoke

PROTOCOLS = ("http/json", "http/protobuf")
CONTENT_TYPES = {"http/json": "application/json", "http/protobuf": "application/x-protobuf"}
SYNTHETIC_HEADERS = "Authorization=Bearer%20synthetic-probe"
METRIC_KINDS = {5: "gauge", 7: "sum", 9: "histogram", 10: "exponentialHistogram", 11: "summary"}
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_REQUESTS = 128
REQUEST_TIMEOUT = 2.0
MAX_REPORT_BYTES = 1024 * 1024
MAX_METRIC_OBSERVATIONS = 1024
EXIT_CODES = {"compatible": 0, "incompatible": 1, "internal_error": 2, "transport_error": 3}


@dataclass
class RequestRecord:
    path: str
    content_type: str
    authorization_matches: bool
    body: bytes


class _ProjectionLimit(ValueError):
    """The safe metric summary exceeded its bounded observation count."""


def endpoint_urls(base_url: str) -> dict[str, str]:
    """Require a loopback HTTP origin; signal URLs intentionally bypass /base."""
    try:
        parts = urlsplit(base_url)
        valid = (parts.scheme == "http" and parts.hostname == "127.0.0.1"
                 and parts.port is not None and 0 < parts.port <= 65535
                 and parts.netloc == f"127.0.0.1:{parts.port}"
                 and not parts.path and not parts.query and not parts.fragment
                 and base_url == f"http://{parts.netloc}")
    except ValueError as error:
        raise AppError("Expected http://127.0.0.1:<port> loopback origin") from error
    if not valid:
        raise AppError("Expected http://127.0.0.1:<port> loopback origin")
    return {"generic": base_url + "/base", "traces": base_url + "/native-traces",
            "metrics": base_url + "/native-metrics"}


def probe_environment(home: Path, run_id: str, token: str, protocol: str,
                      base_url: str, inherited: dict[str, str] | None = None) -> dict[str, str]:
    """Reuse smoke isolation; never inherit OTLP credentials or private config."""
    if protocol not in PROTOCOLS:
        raise AppError("Only the fixed HTTP JSON/protobuf protocols are supported")
    urls = endpoint_urls(base_url)
    env = run_smoke.child_environment(home, run_id, "metadata-only", token, inherited)
    env.update({
        "OTEL_EXPORTER_OTLP_ENDPOINT": urls["generic"],
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": urls["traces"],
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": urls["metrics"],
        "OTEL_EXPORTER_OTLP_HEADERS": SYNTHETIC_HEADERS,
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "DELTA",
        "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION":
            "base2_exponential_bucket_histogram",
    })
    for signal in ("", "_TRACES", "_METRICS"):
        env[f"OTEL_EXPORTER_OTLP{signal}_PROTOCOL"] = protocol
    return env


def _objects(value: dict, key: str) -> list[dict]:
    result = value.get(key, [])
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ValueError("Invalid OTLP object array")
    return result


def _temporality(value):
    if type(value) is int and value in (0, 1, 2):
        return value
    enums = {"AGGREGATION_TEMPORALITY_UNSPECIFIED": 0,
             "AGGREGATION_TEMPORALITY_DELTA": 1, "AGGREGATION_TEMPORALITY_CUMULATIVE": 2}
    if isinstance(value, str) and value in enums:
        return enums[value]
    return None if value is None else "invalid"


def _json_projection(body: bytes) -> tuple[str, int, list[dict]]:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Invalid OTLP export")
    signals = [signal for signal, key in (("traces", "resourceSpans"),
                                         ("metrics", "resourceMetrics")) if key in data]
    if len(signals) != 1:
        raise ValueError("Expected exactly one signal")
    signal = signals[0]
    spans = 0
    metrics = []
    if signal == "traces":
        for resource in _objects(data, "resourceSpans"):
            for scope in _objects(resource, "scopeSpans"):
                spans += len(_objects(scope, "spans"))
    else:
        for resource in _objects(data, "resourceMetrics"):
            for scope in _objects(resource, "scopeMetrics"):
                for metric in _objects(scope, "metrics"):
                    if len(metrics) >= MAX_METRIC_OBSERVATIONS:
                        raise _ProjectionLimit
                    kinds = [kind for kind in METRIC_KINDS.values() if kind in metric]
                    if len(kinds) != 1 or not isinstance(metric[kinds[0]], dict):
                        raise ValueError("Invalid metric data oneof")
                    kind = kinds[0]
                    item = metric[kind]
                    metrics.append({"kind": kind,
                                    "temporality": _temporality(item.get("aggregationTemporality")),
                                    "data_point_count": len(_objects(item, "dataPoints"))})
    return signal, spans, metrics


def _varint(body: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(body):
            raise ValueError("Truncated protobuf varint")
        byte = body[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("Overflowing protobuf varint")
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("Invalid protobuf varint")


def _fields(body: bytes):
    """Read only protobuf wire framing, never interpret arbitrary attribute data."""
    offset = 0
    while offset < len(body):
        tag, offset = _varint(body, offset)
        field, wire = tag >> 3, tag & 7
        if not 0 < field < 2 ** 29:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value, offset = _varint(body, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, offset = _varint(body, offset)
            else:
                length = 8 if wire == 1 else 4
            end = offset + length
            if end > len(body):
                raise ValueError("Truncated protobuf field")
            value, offset = body[offset:end], end
        else:
            raise ValueError("Unsupported protobuf wire type")
        yield field, wire, value


def _messages(body: bytes, number: int) -> list[bytes]:
    result = []
    for field, wire, value in _fields(body):
        if field == number:
            if wire != 2:
                raise ValueError("Invalid protobuf message wire type")
            result.append(value)
    return result


def _protobuf_projection(body: bytes, path: str) -> tuple[str, int, list[dict]]:
    # The two OTLP export envelopes share wire numbers; the HTTP route selects
    # their schema. This is a bounded projection, not a general OTLP validator.
    routes = [signal for signal in ("traces", "metrics") if signal in path]
    if len(routes) != 1:
        raise ValueError("Unrecognized protobuf signal route")
    signal = routes[0]
    spans = 0
    metrics = []
    for resource in _messages(body, 1):
        for scope in _messages(resource, 2):
            for item in _messages(scope, 2):
                if signal == "traces":
                    list(_fields(item))
                    spans += 1
                    continue
                kinds = [(METRIC_KINDS[field], value) for field, wire, value in _fields(item)
                         if field in METRIC_KINDS and wire == 2]
                if len(kinds) != 1:
                    raise ValueError("Invalid protobuf metric data oneof")
                kind, data = kinds[0]
                if len(metrics) >= MAX_METRIC_OBSERVATIONS:
                    raise _ProjectionLimit
                temporalities = [value for field, wire, value in _fields(data)
                                if field == 2 and wire == 0]
                metrics.append({"kind": kind,
                                "temporality": _temporality(temporalities[-1]
                                                            if temporalities else None),
                                "data_point_count": len(_messages(data, 1))})
    return signal, spans, metrics


def assess_requests(records: list[RequestRecord], protocol: str,
                    urls: dict[str, str]) -> dict:
    """Return metadata-only capability results; absence never counts as support."""
    if protocol not in PROTOCOLS:
        raise AppError("Unsupported diagnostic protocol")
    signals = {"traces": [], "metrics": []}
    metrics = []
    spans = 0
    errors = []
    for record in records:
        try:
            content_type = record.content_type.split(";", 1)[0].strip().lower()
            encoding = next((key for key, value in CONTENT_TYPES.items() if value == content_type), protocol)
            signal, count, items = (_json_projection(record.body) if encoding == "http/json"
                                    else _protobuf_projection(record.body, record.path))
        except _ProjectionLimit:
            errors.append("metric_observation_limit")
            continue
        except (ValueError, UnicodeError, RecursionError):
            errors.append("invalid_otlp_payload")
            continue
        signals[signal].append(record)
        spans += count
        if len(metrics) + len(items) > MAX_METRIC_OBSERVATIONS:
            errors.append("metric_observation_limit")
            items = items[:MAX_METRIC_OBSERVATIONS - len(metrics)]
        metrics.extend(items)

    def measured(items, predicate):
        return ("pass" if all(predicate(item) for item in items) else "fail") if items else "not_observed"

    capabilities = {
        f"{signal}_endpoint_override": measured(items, lambda item: item.path == urlsplit(urls[signal]).path)
        for signal, items in signals.items()
    }
    capabilities.update({
        "authorization_header_decoding": measured(records, lambda item: item.authorization_matches is True),
        "protocol_content_type": measured(
            records, lambda item: item.content_type.split(";", 1)[0].strip().lower() == CONTENT_TYPES[protocol]),
        "traces_export": "pass" if spans else "not_observed",
        "metrics_export": "pass" if any(item["data_point_count"] for item in metrics) else "not_observed",
        "delta_temporality": measured(
            [item for item in metrics if item["kind"] in ("sum", "histogram", "exponentialHistogram")],
            lambda item: type(item["temporality"]) is int and item["temporality"] == 1),
        "exponential_histograms": measured(
            metrics, lambda item: item["kind"] in ("gauge", "sum", "exponentialHistogram"))
            if any(item["kind"] in ("histogram", "exponentialHistogram") and item["data_point_count"]
                   for item in metrics) else
            ("fail" if metrics else "not_observed"),
    })
    if errors or any(capabilities[key] == "not_observed" for key in ("traces_export", "metrics_export")):
        status = "transport_error"
    elif all(value == "pass" for value in capabilities.values()):
        status = "compatible"
    else:
        status = "incompatible"
    return {
        "status": status, "capabilities": capabilities, "azure_ingestion_proven": False,
        "native_metrics_configuration_supported": (
            status != "transport_error" and capabilities["metrics_export"] == capabilities["delta_temporality"]
            == capabilities["exponential_histograms"] == "pass"),
        "request_count": len(records), "payload_bytes": sum(len(item.body) for item in records),
        "span_count": spans, "metric_family_observations": len(metrics), "metrics": metrics,
        "errors": sorted(set(errors)),
        "path_counts": {
            label: sum(record.path == urlsplit(url).path for record in records)
            for label, url in urls.items()
        } | {"unexpected": sum(record.path not in {urlsplit(url).path for url in urls.values()}
                               for record in records)},
    }


class RecordingServer:
    """Single-worker loopback fixture with absolute connection deadlines.

    Limits cover raw on-disk JSON including base64 overhead, not just payload
    bytes. No forwarding, export pipeline, or ingestion validation is provided.
    The context must be closed before reading records/errors for assessment.
    """

    def __init__(self, output: Path, protocol: str, *,
                 max_payload_bytes: int = MAX_PAYLOAD_BYTES,
                 max_evidence_bytes: int = MAX_EVIDENCE_BYTES,
                 max_requests: int = MAX_REQUESTS, request_timeout: float = REQUEST_TIMEOUT):
        if protocol not in PROTOCOLS:
            raise AppError("Unsupported diagnostic protocol")
        for value, ceiling in ((max_payload_bytes, MAX_PAYLOAD_BYTES),
                               (max_evidence_bytes, MAX_EVIDENCE_BYTES),
                               (max_requests, MAX_REQUESTS)):
            if type(value) is not int or not 0 < value <= ceiling:
                raise AppError("Recorder limits must be positive and within fixed ceilings")
        if not 0 < request_timeout <= REQUEST_TIMEOUT:
            raise AppError("Request timeout must be positive and at most two seconds")
        self.output = private_dir(output)
        self.protocol = protocol
        self.max_payload_bytes = max_payload_bytes
        self.max_evidence_bytes = max_evidence_bytes
        self.max_requests = max_requests
        self.request_timeout = request_timeout
        self.records: list[RequestRecord] = []
        self.errors: set[str] = set()
        self.evidence_bytes = 0
        self._attempts = 0
        self._stop = threading.Event()
        self._active: socket.socket | None = None
        self._timer: threading.Timer | None = None
        owner = self

        class Server(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(owner.request_timeout)
                owner._active = connection
                owner._timer = threading.Timer(owner.request_timeout, owner._expire, (connection,))
                owner._timer.daemon = True
                owner._timer.start()
                return connection, address

            def shutdown_request(self, request):
                if owner._timer is not None:
                    owner._timer.cancel()
                    owner._timer.join(timeout=owner.request_timeout)
                owner._active = None
                super().shutdown_request(request)

            def handle_error(self, request, client_address):
                owner.errors.add("handler_error")

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner._receive(self)

            def log_message(self, *args):
                # Never print request headers, payloads, or untrusted paths.
                return

            def log_error(self, format, *args):
                owner.errors.add("request_timeout" if any(isinstance(arg, TimeoutError) for arg in args)
                                 else "invalid_request")

            def send_error(self, code, message=None, explain=None):
                owner.errors.add("invalid_request")
                if self.request_version == "HTTP/0.9":
                    self.request_version = "HTTP/1.0"
                owner._respond(self, code)

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.timeout = 0.05
        self.port = self.server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self._serve, name="native-env-recorder", daemon=True)

    def _expire(self, connection: socket.socket) -> None:
        self.errors.add("request_timeout")
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            self.errors.add("connection_shutdown_error")

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                self.server.handle_request()
        except (OSError, RuntimeError):
            self.errors.add("recorder_worker_failed")
            self._stop.set()

    def _respond(self, handler, status: int) -> None:
        headers = getattr(handler, "headers", {})
        content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type not in CONTENT_TYPES.values():
            content_type = CONTENT_TYPES[self.protocol]
        body = b"{}" if content_type == "application/json" else b""
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
        handler.close_connection = True

    def _receive(self, handler) -> None:
        self._attempts += 1
        if self._attempts > self.max_requests:
            self.errors.add("request_limit")
            self._respond(handler, 413)
            return
        lengths = handler.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or len(lengths[0]) > 10 or "Transfer-Encoding" in handler.headers
                or len(handler.headers.get_all("Content-Type", [])) > 1
                or len(handler.headers.get_all("Content-Encoding", [])) > 1
                or handler.headers.get("Content-Encoding", "identity") != "identity"):
            self.errors.add("invalid_request")
            self._respond(handler, 400)
            return
        length = int(lengths[0])
        if length > self.max_payload_bytes:
            self.errors.add("payload_limit")
            self._respond(handler, 413)
            return
        try:
            body = handler.rfile.read(length)
        except TimeoutError:
            self.errors.add("request_timeout")
            return
        if len(body) != length:
            self.errors.add("truncated_request")
            return
        record = RequestRecord(
            handler.path, handler.headers.get("Content-Type", ""),
            handler.headers.get_all("Authorization", []) == ["Bearer synthetic-probe"], body)
        raw = json.dumps({
            "path": record.path, "content_type": record.content_type,
            "authorization_matches": record.authorization_matches,
            "body_base64": base64.b64encode(body).decode("ascii"),
        }) + "\n"
        size = len(raw.encode("utf-8"))
        if size + self.evidence_bytes > self.max_evidence_bytes:
            self.errors.add("evidence_limit")
            self._respond(handler, 413)
            return
        try:
            write_private(self.output / f"request-{len(self.records) + 1:04d}.json", raw)
        except AppError:
            self.errors.add("evidence_write_error")
            self._respond(handler, 500)
            return
        self.records.append(record)
        self.evidence_bytes += size
        self._respond(handler, 200)

    def __enter__(self):
        try:
            self.thread.start()
        except RuntimeError:
            self.server.server_close()
            raise
        return self

    def close(self) -> None:
        self._stop.set()
        error = None
        try:
            self.server.server_close()
        except OSError as cause:
            error = cause
        finally:
            self.thread.join(timeout=self.request_timeout + 1)
        if error is not None or self.thread.is_alive():
            raise AppError("Loopback recorder cleanup failed") from error

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _scenario(output: Path, protocol: str, token: str, executable: str,
              timeout_seconds: int, evidence_budget: int) -> dict:
    run_id = str(uuid4())
    records_path = output / protocol.split("/")[-1]
    recorder = None
    version = None
    urls = {}
    errors = []
    internal_error = False
    try:
        with tempfile.TemporaryDirectory(prefix=f"native-env-{run_id}-") as temporary:
            home = private_dir(Path(temporary) / "home")
            private_dir(home / ".copilot")
            work = private_dir(Path(temporary) / "work")
            recorder = RecordingServer(records_path, protocol, max_evidence_bytes=evidence_budget)
            with recorder:
                urls = endpoint_urls(recorder.base_url)
                env = probe_environment(home, run_id, token, protocol, recorder.base_url)
                version_env = dict(env, COPILOT_OTEL_ENABLED="false", OTEL_SDK_DISABLED="true")
                try:
                    version_text = run([executable, "--version"], env=version_env,
                                       cwd=work, timeout=15).stdout
                    match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?\b", version_text)
                    if not match:
                        raise AppError("CLI version was not recognized")
                    version = match.group(0)
                except AppError:
                    errors.append("cli_version_unavailable")
                    internal_error = True
                if version is not None:
                    command = run_smoke.command_for("metadata-only", f"SYNTHETIC_AUDIT_{run_id}", work)
                    command[0] = executable
                    try:
                        run_session(command, env=env, cwd=work, timeout=timeout_seconds)
                    except AppError:
                        # CLI errors may contain private model output; report only a code.
                        errors.append("cli_execution_failed")
    except (AppError, OSError, RuntimeError):
        errors.append("scenario_setup_or_cleanup_failed")
        internal_error = True
    result = assess_requests(recorder.records if recorder else [], protocol, urls)
    result["errors"] = sorted(set(result["errors"] + errors) | (recorder.errors if recorder else set()))
    if internal_error or {"evidence_write_error", "recorder_worker_failed"} & set(result["errors"]):
        result["status"] = "internal_error"
    elif result["errors"]:
        result["status"] = "transport_error"
    if result["status"] in ("internal_error", "transport_error"):
        result["native_metrics_configuration_supported"] = False
    result.update(run_id=run_id, protocol=protocol, cli_version=version,
                  records_path=str(records_path), endpoint_urls=urls,
                  evidence_bytes=recorder.evidence_bytes if recorder else 0)
    return result


def execute(*, run_cli: bool = False, timeout_seconds: int = 180) -> dict:
    """Spend exactly two bounded synthetic inferences only with explicit consent.

    Operational failures are persisted as safe error codes, never raw CLI output.
    Raises AppError for invalid consent/options or inability to persist a report.
    """
    if run_cli is not True:
        raise AppError("Explicit --run-cli consent is required; this spends two synthetic CLI "
                       "inferences. The loopback fixture does not prove Azure ingestion.")
    if type(timeout_seconds) is not int or not 0 < timeout_seconds <= 600:
        raise AppError("CLI timeout must be an integer between 1 and 600 seconds per scenario")
    run_id = str(uuid4())
    output = private_dir(LOCAL / "native-probe" / run_id)
    report = {
        "run_id": run_id, "report_path": str(output / "report.json"),
        "started_at": run_smoke.now(), "finished_at": None,
        "purpose": "loopback_protocol_env_diagnostic_only",
        "azure_ingestion_proven": False, "native_metrics_configuration_supported": False,
        "cli_version": None, "status": "running", "exit_code": None,
        "errors": [], "scenarios": [], "request_count": 0, "evidence_bytes": 0,
        "limits": {"max_payload_bytes": MAX_PAYLOAD_BYTES, "max_evidence_bytes": MAX_EVIDENCE_BYTES,
                   "max_report_bytes": MAX_REPORT_BYTES, "max_requests_per_scenario": MAX_REQUESTS,
                   "max_metric_observations_per_scenario": MAX_METRIC_OBSERVATIONS,
                   "request_timeout_seconds": REQUEST_TIMEOUT, "cli_timeout_seconds": timeout_seconds},
    }
    _write_report(report)
    executable = shutil.which("copilot")
    if not executable:
        report["errors"].append("copilot_executable_not_found")
    else:
        try:
            token = run_smoke.authentication_token()
        except AppError:
            report["errors"].append("github_authentication_failed")
        else:
            remaining = MAX_EVIDENCE_BYTES - MAX_REPORT_BYTES
            for protocol in PROTOCOLS:
                if remaining <= 0:
                    report["errors"].append("evidence_budget_exhausted")
                    break
                scenario = _scenario(output, protocol, token, executable, timeout_seconds, remaining)
                report["scenarios"].append(scenario)
                remaining -= scenario["evidence_bytes"]
    scenarios = report["scenarios"]
    if report["errors"] or any(item["status"] == "internal_error" for item in scenarios):
        status = "internal_error"
    elif len(scenarios) != len(PROTOCOLS) or any(item["status"] == "transport_error" for item in scenarios):
        status = "transport_error"
    elif any(item["status"] == "incompatible" for item in scenarios):
        status = "incompatible"
    else:
        status = "compatible"
    versions = {item["cli_version"] for item in scenarios if item["cli_version"] is not None}
    if len(versions) == 1:
        report["cli_version"] = versions.pop()
    elif len(versions) > 1:
        report["errors"].append("cli_version_changed")
        status = "internal_error"
    report.update(
        status=status, exit_code=EXIT_CODES[status], finished_at=run_smoke.now(),
        native_metrics_configuration_supported=(
            status not in ("internal_error", "transport_error") and len(scenarios) == len(PROTOCOLS)
            and all(item["native_metrics_configuration_supported"] for item in scenarios)),
        request_count=sum(item["request_count"] for item in scenarios),
        evidence_bytes=sum(item["evidence_bytes"] for item in scenarios),
    )
    _write_report(report)
    return report


def _write_report(report: dict) -> None:
    text = json.dumps(report, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_REPORT_BYTES:
        raise AppError("Diagnostic report exceeds its reserved evidence budget")
    write_private(Path(report["report_path"]), text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-cli", action="store_true",
                        help="Consent to two paid synthetic CLI inferences (JSON then protobuf)")
    parser.add_argument("--timeout-seconds", type=int, default=180,
                        help="Per-scenario CLI deadline, 1-600 seconds (default: 180)")
    args = parser.parse_args(argv)
    try:
        report = execute(run_cli=args.run_cli, timeout_seconds=args.timeout_seconds)
    except AppError as error:
        print(f"Native environment probe refused or failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
