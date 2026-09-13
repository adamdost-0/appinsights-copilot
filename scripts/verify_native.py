"""Verify native Azure OTLP persistence, separately from CLI content/privacy.

Usage: python3 -m scripts.verify_native --run-id UUID --timeout-seconds 600

Only native OTelSpans/OTelEvents/OTelResources and the native metrics query API
are evidence. No collector, classic App tables, source file, or fixture CLI mode
is supported. Spans/events use the completed manifest's fixed time window.
Resource metadata is restricted to those spans' resource IDs, rather than its
potentially later arrival timestamp. Histogram
count/sum functions operate on dotted BASE metric names and individual snapshots;
cumulative snapshots are never added together. Persisted metrics must also carry
the configured microsoft.appresourceid and microsoft.amwresourceid labels.

Raw backend data is sensitive and remains in private raw-native-query.json.
results.json contains counts and fixed diagnostics. A privacy failure can coexist
with azure_ingestion_proven=true; ignored SDK environment controls do not prove
that Azure rejects explicit or cumulative histograms.

Native schemas: https://learn.microsoft.com/azure/azure-monitor/reference/tables/otelspans
Histogram semantics:
https://learn.microsoft.com/azure/azure-monitor/metrics/prometheus-opentelemetry-best-practices
"""

import argparse
from datetime import timedelta
import json
import math
import os
import re
import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from .common import AppError, LOCAL, ROOT, load_json, run_json, validate_run_id, write_json
from .native_smoke import validate_state as validate_ingest_state
from .verify_ingestion import QueryError, _cli_version, _content, _utc, trusted_path

METRICS = ("gen_ai.client.token.usage", "gen_ai.client.operation.duration",
           "gen_ai.invoke_agent.duration")
TOOL_METRIC = "gen_ai.execute_tool.duration"
TABLES = frozenset({"OTelSpans", "OTelEvents"})
COLUMNS = frozenset({
    "Table", "Name", "Kind", "StatusCode", "Success", "TraceId", "SpanId", "ParentSpanId",
    "Attributes", "ResourceAttributes", "_ResourceId", "TimeGenerated", "EndTime", "DurationMs",
})
MAX_ROWS = 10000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 32
LIMITATIONS = [
    "Backend run labels and the parent's completed CLI manifest establish correlation, not cryptographic producer attestation.",
    "Without independent source capture this verifies required persisted signals, not lossless export of every emitted record.",
    "Privacy is checked on returned native backend attributes, not on CLI memory or pre-ingestion traffic.",
    "Histogram count/sum snapshots prove persistence, not bucket fidelity or SDK temporality/aggregation environment-variable support.",
    "Metrics are evaluated at manifest.finished_at; samples outside the backend's instant-query lookback are not inferred.",
]


def validate_state(state: dict) -> dict:
    validate_ingest_state(state)
    prefix = rf"/subscriptions/{re.escape(state['subscription_id'])}/resourceGroups/[^/]+/providers/"
    for key, kind in (("workspace_resource_id", r"Microsoft\.OperationalInsights/workspaces"),
                      ("azure_monitor_workspace_resource_id", r"Microsoft\.Monitor/accounts")):
        if not isinstance(state.get(key), str) or not re.fullmatch(prefix + kind + r"/[^/]+", state[key], re.I):
            raise AppError("Native query state has an invalid or cross-subscription workspace resource ID")
    try:
        endpoint = state.get("metrics_query_endpoint")
        if not isinstance(endpoint, str):
            raise ValueError
        url = urlsplit(endpoint)
        if (url.scheme != "https" or not url.hostname
                or not url.hostname.endswith(".prometheus.monitor.azure.com")
                or url.username or url.password or url.port not in (None, 443)
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError
    except ValueError as error:
        raise AppError("Native metrics query endpoint must be an HTTPS Azure managed Prometheus origin") from error
    return state


def validate_manifest(manifest: dict, run_id: str, state: dict) -> dict:
    validate_run_id(run_id)
    validate_state(state)
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise AppError("Native manifest run ID does not match the requested run")
    if manifest.get("collector_mode") != "native-azure":
        raise AppError("Only an explicit native-azure CLI run can establish native proof")
    if manifest.get("status") != "awaiting_verification" or type(manifest.get("exit_code")) is not int or manifest["exit_code"]:
        raise AppError("A completed successful native CLI run awaiting verification is required")
    scenario = manifest.get("scenario")
    if scenario not in ("metadata-only", "full-content", "delegated"):
        raise AppError("Invalid native scenario")
    if type(manifest.get("capture_content")) is not bool or manifest["capture_content"] != (scenario != "metadata-only"):
        raise AppError("Native capture_content conflicts with scenario")
    if manifest.get("marker") != f"SYNTHETIC_AUDIT_{run_id}":
        raise AppError("Native manifest requires its run-specific synthetic marker")
    start, finish = _utc(manifest.get("started_at")), _utc(manifest.get("finished_at"))
    if not timedelta(0) < finish - start <= timedelta(hours=6):
        raise AppError("Native manifest must describe a positive window of at most six hours")
    _cli_version(manifest.get("cli_version"))
    for key in ("dcr_resource_id", "application_insights_resource_id"):
        if not isinstance(manifest.get(key), str) or manifest[key].lower() != state[key].lower():
            raise AppError("Native manifest resource identity differs from query state")
    for key in ("traces_endpoint", "metrics_endpoint"):
        if key in manifest and manifest[key] != state[key]:
            raise AppError("Native manifest endpoint differs from query state")
    return manifest


def required_metrics(manifest: dict) -> tuple[str, ...]:
    return METRICS + ((TOOL_METRIC,) if manifest["scenario"] != "metadata-only" else ())


def build_query(manifest: dict, state: dict, *, template: str | None = None) -> str:
    validate_manifest(manifest, manifest.get("run_id"), state)
    if template is None:
        try:
            template = (ROOT / "queries" / "verify_native.kql").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise AppError("Cannot read queries/verify_native.kql") from error
    replacements = {
        "__RUN_ID__": json.dumps(manifest["run_id"]),
        "__RESOURCE_ID__": json.dumps(state["application_insights_resource_id"]),
        "__SCENARIO__": json.dumps(manifest["scenario"]),
        "__VERSION__": json.dumps(_cli_version(manifest["cli_version"])),
        "__START__": _utc(manifest["started_at"]).isoformat(),
        "__FINISH__": _utc(manifest["finished_at"]).isoformat(),
        "__ROW_LIMIT__": str(MAX_ROWS + 1),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def metric_query_url(state: dict, manifest: dict, name: str, statistic: str, *,
                     run_id: str | None = None) -> str:
    validate_manifest(manifest, manifest.get("run_id"), state)
    if name not in required_metrics(manifest) or statistic not in ("count", "sum"):
        raise AppError("Only required native histogram count/sum queries are supported")
    identity = manifest["run_id"] if run_id is None else validate_run_id(run_id)
    labels = {"__name__": name, "copilot.run.id": identity, "service.name": "github-copilot",
              "copilot.audit.scenario": manifest["scenario"]}
    selector = ",".join(
        f'{key if key == "__name__" else json.dumps(key)}={json.dumps(value)}' for key, value in labels.items())
    query = f"histogram_{statistic}({{{selector}}})"
    return state["metrics_query_endpoint"].rstrip("/") + "/api/v1/query?" + urlencode({
        "query": query, "time": manifest["finished_at"],
    })


def query_error(detail: str) -> QueryError:
    lower = detail.lower()
    if re.search(r"\b(401|403)\b|unauthorized|forbidden|aadsts|az login|authentication|authorizationfailed", lower):
        return QueryError("native_query_authorization_failed")
    if "partial" in lower:
        return QueryError("native_query_partial_results")
    table = r"['\"]?(?:otelspans|otelevents|otelresources)['\"]?"
    if (re.search(r"(?:failed to resolve|cannot resolve|unknown) table(?: or column)?"
                  r"(?: expression)?(?: named)?\s+" + table + r"(?:\s|$|[.,])", lower)
            or re.search(r"table\s+" + table + r"\s+does not exist", lower)):
        return QueryError("native_tables_pending", retryable=True)
    retryable = bool(re.search(r"\b(?:429|5\d\d)\b|toomanyrequests|serviceunavailable|internalservererror", lower))
    return QueryError("native_query_transient_failure" if retryable else "native_query_failed", retryable=retryable)


def _object(value, context: str) -> dict:
    if not isinstance(value, dict):
        raise AppError(f"Malformed native {context}: expected object")
    return value


def _array(value, context: str) -> list:
    if not isinstance(value, list):
        raise AppError(f"Malformed native {context}: expected array")
    return value


def _errors(payload: dict) -> None:
    for key in ("error", "errors", "partialError", "partialErrors"):
        if key in payload:
            if key.startswith("partial"):
                raise QueryError("native_query_partial_results")
            raise query_error(json.dumps(payload[key]))
    if payload.get("warnings") or payload.get("infos"):
        raise QueryError("native_query_annotations_prevent_complete_proof")


def _number(value, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AppError(f"Invalid native {context}: expected finite nonnegative number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise AppError(f"Invalid native {context}: expected finite nonnegative number") from error
    if not math.isfinite(number) or number < 0:
        raise AppError(f"Invalid native {context}: expected finite nonnegative number")
    return number


def parse_trace_response(payload: dict) -> list[dict]:
    _errors(_object(payload, "trace response"))
    tables = _array(payload.get("tables"), "trace tables")
    if not tables:
        raise AppError("Native query has no result table")
    result = []
    for table in tables:
        _errors(_object(table, "trace table"))
        names = [_object(column, "column").get("name")
                 for column in _array(table.get("columns"), "columns")]
        if (any(not isinstance(name, str) for name in names) or len(set(names)) != len(names)
                or not COLUMNS.issubset(names)):
            raise AppError("Native query has missing or duplicate columns")
        for values in _array(table.get("rows"), "trace rows"):
            if not isinstance(values, list) or len(values) != len(names):
                raise AppError("Native query has a malformed row")
            row = dict(zip(names, values))
            if not isinstance(row["Table"], str) or row["Table"] not in TABLES:
                raise AppError("Classic or unknown telemetry tables cannot prove native ingestion")
            for key in ("Attributes", "ResourceAttributes"):
                value = row[key]
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (ValueError, RecursionError) as error:
                        raise AppError("Native dynamic attributes contain malformed JSON") from error
                row[key] = _object(value, "attributes")
            result.append(row)
            if len(result) > MAX_ROWS:
                raise AppError("Native query exceeded the bounded row limit")
    return result


def parse_metric_response(payload: dict, manifest: dict, *, run_id: str | None = None) -> list[dict]:
    _errors(_object(payload, "metric response"))
    if payload.get("status") != "success":
        raise QueryError("native_metrics_query_not_successful")
    data = _object(payload.get("data"), "metric data")
    if data.get("resultType") != "vector":
        raise AppError("Native metrics require an instant vector, not cumulative snapshot ranges")
    result = []
    identity = manifest["run_id"] if run_id is None else validate_run_id(run_id)
    expected = {"copilot.run.id": identity, "service.name": "github-copilot",
                "copilot.audit.scenario": manifest["scenario"]}
    for item in _array(data.get("result"), "metric result"):
        item = _object(item, "metric sample")
        labels = _object(item.get("metric"), "metric labels")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()):
            raise AppError("Native metric labels must be strings")
        if any(labels.get(key) != value for key, value in expected.items()):
            raise AppError("Native metric run/service/scenario identity mismatch")
        if _cli_version(labels.get("service.version")) != _cli_version(manifest["cli_version"]):
            raise AppError("Native metric CLI version mismatch")
        value = _array(item.get("value"), "metric value")
        if len(value) != 2:
            raise AppError("Native metric sample needs timestamp and scalar value")
        timestamp = _number(value[0], "metric timestamp")
        if abs(timestamp - _utc(manifest["finished_at"]).timestamp()) > 0.001:
            raise AppError("Native metric query evaluation time differs from manifest.finished_at")
        result.append({"labels": labels, "timestamp": timestamp, "value": _number(value[1], "metric value")})
        if len(result) > MAX_ROWS:
            raise AppError("Native metric query exceeded the bounded series limit")
    return result


def _correlation_id(value, size: int, *, optional: bool = False) -> str:
    if optional and value in (None, "", "0" * size):
        return ""
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-fA-F]{{{size}}}", value) or value == "0" * size:
        raise AppError("Malformed native telemetry correlation ID")
    return value.lower()


def _identity(attributes: dict, manifest: dict) -> None:
    expected = {"copilot.run.id": manifest["run_id"], "copilot.audit.scenario": manifest["scenario"],
                "service.name": "github-copilot"}
    if any(attributes.get(key) != value for key, value in expected.items()):
        raise AppError("Native trace run/service/scenario identity mismatch")
    if _cli_version(attributes.get("service.version")) != _cli_version(manifest["cli_version"]):
        raise AppError("Native trace CLI version mismatch")


def _trace_evidence(rows: list[dict], manifest: dict) -> tuple[dict, list[dict]]:
    by_id, events = {}, []
    start, finish = _utc(manifest["started_at"]), _utc(manifest["finished_at"])
    for original in rows:
        row = dict(_object(original, "trace row"))
        if (not COLUMNS.issubset(row) or not isinstance(row["Table"], str)
                or row["Table"] not in TABLES):
            raise AppError("Only complete native OTel rows establish native trace proof")
        if not isinstance(row["Name"], str) or not row["Name"]:
            raise AppError("Native span/event name is missing")
        if not isinstance(row["_ResourceId"], str) or row["_ResourceId"].lower() != manifest[
                "application_insights_resource_id"].lower():
            raise AppError("Native trace application resource identity mismatch")
        _identity(_object(row["ResourceAttributes"], "resource attributes"), manifest)
        attributes = _object(row["Attributes"], "span/event attributes")
        for key in ("copilot.run.id", "copilot.audit.scenario", "service.name", "service.version"):
            if key in attributes and attributes[key] != row["ResourceAttributes"][key]:
                raise AppError("Native signal attributes conflict with resource identity")
        generated = _utc(row["TimeGenerated"])
        if not start <= generated <= finish:
            raise AppError("Native span/event lies outside the manifest time window")
        row["TraceId"] = _correlation_id(row["TraceId"], 32)
        row["SpanId"] = _correlation_id(row["SpanId"], 16)
        if row["Table"] == "OTelEvents":
            events.append(row)
            continue
        row["ParentSpanId"] = _correlation_id(row["ParentSpanId"], 16, optional=True)
        if (not isinstance(row["Kind"], str) or row["Kind"].lower() not in
                ("unspecified", "internal", "client", "server", "producer", "consumer")
                or not isinstance(row["StatusCode"], str)
                or row["Success"] is not None and type(row["Success"]) is not bool):
            raise AppError("Malformed native span kind/status/success")
        if not generated <= _utc(row["EndTime"]) <= finish:
            raise AppError("Native span end lies outside the manifest time window")
        _number(row["DurationMs"], "span duration")
        key = (row["TraceId"], row["SpanId"])
        if key in by_id and by_id[key] != row:
            raise AppError("Conflicting native span rows share the same correlation ID")
        by_id[key] = row
    return by_id, events


def _ancestry(span: dict, by_id: dict) -> tuple[list[dict], bool]:
    parents = []
    seen = {span["SpanId"]}
    current = span
    while current["ParentSpanId"]:
        parent_id = current["ParentSpanId"]
        if parent_id in seen:
            raise AppError("Cyclic native span parenting")
        seen.add(parent_id)
        parent = by_id.get((span["TraceId"], parent_id))
        if parent is None:
            return parents, False
        parents.append(parent)
        current = parent
    return parents, True


def _metric_series(payload: dict, manifest: dict, state: dict, name: str, statistic: str) -> dict:
    result = {}
    for sample in parse_metric_response(payload, manifest):
        labels = sample["labels"]
        for label, state_key in (("microsoft.appresourceid", "application_insights_resource_id"),
                                 ("microsoft.amwresourceid", "azure_monitor_workspace_resource_id")):
            if labels.get(label, "").lower() != state[state_key].lower():
                raise AppError("Native metric Azure application/workspace resource identity mismatch")
        if "__name__" in labels and labels["__name__"] != name:
            raise AppError("Native metric family identity mismatch")
        if statistic == "count" and not sample["value"].is_integer():
            raise AppError("Native histogram count is not an integer")
        key = tuple(sorted((key, value) for key, value in labels.items() if key != "__name__"))
        if key in result:
            raise AppError("Native instant query contains duplicate series snapshots")
        result[key] = sample
    return result


def assess(manifest: dict, state: dict, rows: list[dict], metrics: dict,
           negative: dict | None, negative_run_id: str) -> dict:
    """Assess real native backend parser outputs; never synthesize source evidence.

    Missing required persistence is pending. Observed invalid identity/errors are
    fatal. Content/privacy failure does not erase independently established trace
    and metric persistence. This helper performs no I/O.
    """
    result = {
        "status": "pending", "azure_ingestion_proven": False,
        "native_cli_content_privacy_verified": False, "proof_scope": "azure-native-backends",
        "checks": {"trace_persistence": False, "metric_persistence": False,
                   "negative_control": False, "content": False, "privacy": False},
        "counts": {"spans": 0, "events": 0, "invoke_agent": 0, "chat": 0, "execute_tool": 0,
                   "delegated_agent_children": 0, "metric_families": 0, "metric_series": 0,
                   "privacy_fields": 0, "content_fields_verified": 0},
        "reasons": [], "limitations": list(LIMITATIONS),
    }
    try:
        validate_manifest(manifest, manifest.get("run_id"), state)
        validate_run_id(negative_run_id)
        if negative_run_id == manifest["run_id"]:
            raise AppError("Negative control must use a never-emitted distinct UUID")
        by_id, events = _trace_evidence(rows, manifest)
        spans = list(by_id.values())
        counts, checks = result["counts"], result["checks"]
        counts.update(spans=len(spans), events=len(events))
        operations = {name: [span for span in spans if span["Attributes"].get("gen_ai.operation.name") == name]
                      for name in ("invoke_agent", "chat", "execute_tool")}
        for name, values in operations.items():
            counts[name] = len(values)
        parents_complete = True
        for span in spans:
            parents, complete = _ancestry(span, by_id)
            parents_complete &= complete
            has_agent_parent = any(parent["Attributes"].get("gen_ai.operation.name") == "invoke_agent"
                                   for parent in parents)
            operation = span["Attributes"].get("gen_ai.operation.name")
            if operation in ("chat", "execute_tool") and not has_agent_parent:
                parents_complete = False
            if operation == "invoke_agent" and has_agent_parent:
                counts["delegated_agent_children"] += 1
        if any((event["TraceId"], event["SpanId"]) not in by_id for event in events):
            parents_complete = False
        tools_required = manifest["scenario"] != "metadata-only"
        checks["trace_persistence"] = bool(
            operations["invoke_agent"] and operations["chat"] and parents_complete
            and (not tools_required or operations["execute_tool"])
            and (manifest["scenario"] != "delegated" or
                 len(operations["invoke_agent"]) >= 2 and counts["delegated_agent_children"] > 0))
        privacy_bags = [item[key] for item in spans + events for key in ("Attributes", "ResourceAttributes")]
        _object(metrics, "metric families")
        for name in required_metrics(manifest):
            family = metrics.get(name)
            if family is None:
                continue
            _object(family, "histogram family")
            if "count" not in family or "sum" not in family:
                continue
            count_series = _metric_series(family["count"], manifest, state, name, "count")
            sum_series = _metric_series(family["sum"], manifest, state, name, "sum")
            privacy_bags.extend(sample["labels"] for sample in [*count_series.values(), *sum_series.values()])
            if count_series.keys() == sum_series.keys() and any(
                    sample["value"] > 0 and sum_series[key]["value"] > 0 for key, sample in count_series.items()):
                counts["metric_families"] += 1
                counts["metric_series"] += len(count_series)
        checks["metric_persistence"] = counts["metric_families"] == len(required_metrics(manifest))
        if negative is not None:
            if parse_metric_response(negative, manifest, run_id=negative_run_id):
                raise AppError("Negative control returned data for a never-emitted UUID")
            checks["negative_control"] = True
        counts["privacy_fields"] = sum(len(_content(bag)) for bag in privacy_bags)
        checks["privacy"] = bool(spans) and counts["privacy_fields"] == 0
        result["native_cli_content_privacy_verified"] = (
            manifest["scenario"] == "metadata-only" and checks["trace_persistence"]
            and checks["metric_persistence"] and checks["privacy"])

        def has_marker(span, field):
            attributes = [span["Attributes"]] + [
                event["Attributes"] for event in events
                if (event["TraceId"], event["SpanId"]) == (span["TraceId"], span["SpanId"])]
            return any(manifest["marker"] in json.dumps(_content(bag).get(field, ""))
                       for bag in attributes)

        if tools_required:
            input_ok = any(has_marker(span, "gen_ai.input.messages")
                           for span in operations["invoke_agent"] + operations["chat"])
            output_ok = any(has_marker(span, "gen_ai.output.messages")
                            for span in operations["invoke_agent"] + operations["chat"])
            tool_ok = any(has_marker(span, "gen_ai.tool.call.arguments")
                          and has_marker(span, "gen_ai.tool.call.result") for span in operations["execute_tool"])
            counts["content_fields_verified"] = int(input_ok) + int(output_ok) + 2 * int(tool_ok)
            checks["content"] = input_ok and output_ok and tool_ok
        else:
            checks["content"] = checks["privacy"]
        result["azure_ingestion_proven"] = all(checks[key] for key in (
            "trace_persistence", "metric_persistence", "negative_control"))
        span_errors = any(span["Success"] is False or span["StatusCode"].lower() == "error" for span in spans)
        if not checks["trace_persistence"]:
            result["reasons"].append("required_native_spans_or_parenting_pending")
        if not checks["metric_persistence"]:
            result["reasons"].append("required_native_histograms_pending")
        if not checks["negative_control"]:
            result["reasons"].append("negative_control_pending")
        if manifest["scenario"] == "metadata-only" and counts["privacy_fields"]:
            result["status"] = "failed"
            result["reasons"].append("metadata_content_fields_present")
        elif span_errors:
            result["status"] = "failed"
            result["reasons"].append("native_spans_record_operation_failure")
        elif not result["azure_ingestion_proven"]:
            result["status"] = "pending"
        elif not checks["content"]:
            result["status"] = "partial"
            result["reasons"].append("required_marker_content_missing")
        else:
            result["status"] = "passed"
    except (AppError, RecursionError) as error:
        result["status"] = "failed"
        result["azure_ingestion_proven"] = False
        result["native_cli_content_privacy_verified"] = False
        result["reasons"] = [str(error) if isinstance(error, AppError) else "native_attribute_nesting_limit"]
    return result


def _bounded_response(payload: dict) -> dict:
    try:
        size = len(json.dumps(payload).encode("utf-8"))
    except (ValueError, TypeError, RecursionError) as error:
        raise AppError("Native query response is not bounded JSON evidence") from error
    if size > MAX_RESPONSE_BYTES:
        raise AppError("Native query response exceeds the private evidence byte limit")
    return _object(payload, "query response")


def query_native(state: dict, manifest: dict, negative_run_id: str, timeout: float) -> dict:
    """Read both Azure backends using core az rest; no extension or token output."""
    validate_manifest(manifest, manifest.get("run_id"), state)
    validate_run_id(negative_run_id)
    if negative_run_id == manifest["run_id"]:
        raise AppError("Negative control must use a never-emitted distinct UUID")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 3600):
        raise AppError("Native query timeout must be finite, positive, and at most 3600")
    deadline = time.monotonic() + timeout
    env = {**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"}

    def request(method, url, audience, body=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("native_query_deadline_exceeded")
        command = ["az", "rest", "--method", method, "--url", url, "--resource", audience,
                   "--subscription", state["subscription_id"], "--output", "json", "--only-show-errors"]
        if body is not None:
            command += ["--headers", "Content-Type=application/json", "--body", json.dumps(body)]
        try:
            payload = run_json(command, env=env, timeout=min(60.0, remaining))
        except AppError as error:
            raise query_error(str(error)) from error
        _errors(_bounded_response(payload))
        return payload

    traces = request(
        "POST", f"https://api.loganalytics.azure.com/v1/workspaces/{state['workspace_customer_id']}/query",
        "https://api.loganalytics.io",
        {"query": build_query(manifest, state)})
    parse_trace_response(traces)
    result = {"traces": traces, "metrics": {}, "negative": None}
    for name in required_metrics(manifest):
        result["metrics"][name] = {}
        for statistic in ("count", "sum"):
            payload = request("GET", metric_query_url(state, manifest, name, statistic),
                              "https://prometheus.monitor.azure.com")
            parse_metric_response(payload, manifest)
            result["metrics"][name][statistic] = payload
            _bounded_response(result)
    result["negative"] = request(
        "GET", metric_query_url(state, manifest, METRICS[0], "count", run_id=negative_run_id),
        "https://prometheus.monitor.azure.com")
    parse_metric_response(result["negative"], manifest, run_id=negative_run_id)
    return _bounded_response(result)


def _pending_ingestion(result: dict) -> bool:
    pending = {"required_native_spans_or_parenting_pending", "required_native_histograms_pending",
               "negative_control_pending"}
    # A known content leak must not stop observation before delayed metrics can
    # establish (or fail to establish) native persistence independently.
    allowed = pending | {"metadata_content_fields_present", "native_spans_record_operation_failure"}
    reasons = set(result["reasons"])
    return bool(reasons & pending) and reasons.issubset(allowed)


def verify(run_id: str, timeout_seconds: float = 600, *, query=None, clock=None, sleep=None) -> dict:
    """Poll completed native runs and persist private results on every handled outcome.

    query/clock/sleep injection is for offline unit tests, never exposed by the CLI.
    Only empty/pending ingestion, missing native tables, 429 and 5xx are retried.
    """
    validate_run_id(run_id)
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600):
        raise AppError("timeout-seconds must be finite, positive, and at most 3600")
    query = query if query is not None else query_native
    clock = clock if clock is not None else time.monotonic
    sleep = sleep if sleep is not None else time.sleep
    deadline = clock() + timeout_seconds
    folder = trusted_path(str(LOCAL / "runs" / run_id), local=LOCAL)
    if not folder.is_dir():
        raise AppError("Native run folder is missing; complete native_smoke first")
    for name in ("results.json", "raw-native-query.json"):
        trusted_path(str(folder / name), local=LOCAL)
    negative_run_id = str(uuid4())
    result = {
        "status": "failed", "azure_ingestion_proven": False,
        "native_cli_content_privacy_verified": False, "proof_scope": "azure-native-backends",
        "counts": {}, "checks": {}, "reasons": [], "limitations": list(LIMITATIONS),
    }
    raw = {"attempts": 0, "negative_run_id": negative_run_id, "response": None}
    attempts = 0
    should_retry = False
    try:
        state = validate_state(load_json(trusted_path(str(LOCAL / "native-azure.json"), local=LOCAL)))
        manifest = validate_manifest(load_json(trusted_path(str(folder / "manifest.json"), local=LOCAL)), run_id, state)
        while clock() < deadline and attempts < MAX_ATTEMPTS:
            attempts += 1
            raw = {"attempts": attempts, "negative_run_id": negative_run_id, "response": None}
            received = False
            try:
                payload = query(state, manifest, negative_run_id, max(0.001, deadline - clock()))
                received = True
                payload = _bounded_response(payload)
                raw["response"] = payload
                rows = parse_trace_response(payload.get("traces"))
                metrics = _object(payload.get("metrics"), "metric families")
                if "negative" not in payload:
                    raise AppError("Native query response lacks its negative control")
                result = assess(manifest, state, rows, metrics, payload["negative"], negative_run_id)
                should_retry = _pending_ingestion(result)
            except AppError as error:
                failure = (error if isinstance(error, QueryError) else
                           QueryError(str(error)) if received else query_error(str(error)))
                result.update(status="pending" if failure.retryable else "failed",
                              azure_ingestion_proven=False, native_cli_content_privacy_verified=False,
                              reasons=[str(failure)])
                raw["query_error"] = str(failure)
                should_retry = failure.retryable
            if not should_retry:
                break
            remaining = deadline - clock()
            if remaining <= 0 or attempts >= MAX_ATTEMPTS:
                break
            sleep(min(30.0, 2.0 ** min(attempts - 1, 5), remaining))
        if should_retry or attempts == 0:
            result["status"] = "partial" if any(result["counts"].values()) else "failed"
            result["reasons"].append("native_verification_deadline_exceeded" if clock() >= deadline
                                     else "native_verification_attempt_limit")
    except AppError as error:
        result.update(status="failed", azure_ingestion_proven=False, reasons=[str(error)])
    result.update(run_id=run_id, attempts=attempts)
    write_json(folder / "raw-native-query.json", raw)
    write_json(folder / "results.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    try:
        result = verify(args.run_id, args.timeout_seconds)
    except AppError:
        print(json.dumps({"status": "failed", "azure_ingestion_proven": False, "counts": {}}))
        return 1
    print(json.dumps({key: result[key] for key in (
        "run_id", "status", "azure_ingestion_proven", "native_cli_content_privacy_verified", "counts", "attempts",
    )}, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
