"""Compare a completed CLI run's collector OTLP JSON-lines with Azure Monitor.

Usage: python3 -m scripts.verify_ingestion --run-id UUID --timeout-seconds 600

The parent must write the manifest only after the CLI and collector have flushed.
Its evidence_path must identify that CLI's collector file, never a standalone
telemetry generator. Service identity, run resource attributes and source/backend
IDs are checked, but telemetry is not a cryptographic attestation of its producer.
Shared collector files are validated in full before selecting the requested run's
resource groups. Selected groups must have service.name github-copilot and
instrumentation scope github.copilot. Resource service.version must match the
manifest CLI version; scope.version is independent, not guessed to be the CLI
version. Bare semantic versions, a leading v, and the GitHub Copilot CLI label
are accepted, including the terminal period in the observed CLI version line.
Unrecognized identity/version formats fail explicitly. The observed CLI resource
scenario key is copilot.audit.scenario; collector privacy transforms must use
that key, not copilot.scenario. Like other string resource attributes, it is
compared against the corresponding Azure Properties value.
Backend proof requires an explicit collector_mode="azure"; missing/local-only
modes cannot become Azure proof even if an injected backend returns matching rows.
check_source_evidence validates local-only capture without changing its mode,
returns source-validated rather than passed, and always denies Azure proof.
The source is POST-transform collector evidence, not the raw CLI file exporter.
An isolated CLI 1.0.84-5 metadata-only diagnostic emitted gen_ai.tool.definitions
despite capture=false. The collector must remove all six gated fields from
spans/events before BOTH evidence and Azure exporters for content-off runs.
A passing downstream absence check therefore proves transport-boundary privacy,
not native CLI capture controls. The raw diagnostic discrepancy stays private.

Supported source format: collector file exporter JSON-lines containing
resourceSpans/scopeSpans and resourceMetrics/scopeMetrics, including mixed lines.
Ordinary span events must export to AppTraces with Message equal to the source
event name and ParentId equal to its span ID. INTERNAL/CLIENT spans must export
to AppDependencies. Metrics must preserve string resource/point attributes.
Lifecycle proof accepts source-emitted lifecycle events, including the observed
CLI 1.0.84-5 github.copilot.mcp.server.lifecycle; no session.shutdown is assumed.
Histograms are compared by individual count/sum snapshots, never by summing
cumulative exports. Each distinct required metric name/resource/datapoint series
needs its own positive matching snapshot. Dimension identities erased by exporter
flattening cannot establish proof. Azure histogram bucket loss is reported.
Full-content and delegated manifests require capture_content=true and source
marker-bearing input messages, output messages, tool arguments and tool results,
all preserved in their matching Azure records. The fixture path itself must
contain the marker to prove real view-tool arguments; older result-only marker
diagnostics do not satisfy this gate. Passing proof does not promise lossless
storage of the entire system prompt or tool catalog: only an exact 8192-character
prefix truncation of oversized system_instructions/tool.definitions is recognized
as documented optional fidelity loss and counted. Other missing/changed content
still fails. See https://learn.microsoft.com/azure/azure-monitor/app/application-insights-faq
for the Application Insights custom property value limit.
The deployed image must enable ordinary span-event export. Map/array span
attributes may require JSON stringification before the Azure exporter; dropped
content never passes. Exception events require a separate AppExceptions proof
and are reported unsupported rather than incorrectly searched in AppTraces.

Mapping references (the parent must confirm its pinned image behaves likewise):
https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/azuremonitorexporter
trace_to_envelope.go, metric_to_envelopes.go and contracts_utils.go.

Public pure helpers: decode_attributes, parse_source, parse_query_response,
validate_manifest, check_source_evidence and compare_evidence. build_query is pure when given template
text; otherwise it loads queries/verify_ingestion.kql. compare_evidence consumes
the two parser outputs and a validated manifest. verify accepts injected query,
monotonic clock and sleep functions for offline tests. Results contain counts and
fixed diagnostics only; raw-query.json contains sensitive Azure data, mode 0600.

Evidence must be an immutable, complete, LF-terminated file. Any <stem>-*<suffix>
rotation sibling (including 0.160.0's timestamp-size suffix) or numbered backup
is rejected, not silently ignored or assembled. Quiesce the collector before
taking an immutable snapshot. Consolidation is only valid if the parent retained
every segment and its preflight provenance baseline; it cannot reconstruct
history already deleted by retention. A surviving well-formed file cannot prove
the absence of deleted records. Stable metadata/LF checks are not fsync, power-loss,
upstream CLI flush or Azure persistence guarantees.
"""

import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from scripts.common import AppError, LOCAL, ROOT, load_json, run_json, validate_run_id, write_json


CONTENT_FIELDS = frozenset({
    "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
    "gen_ai.tool.definitions", "gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
})
OPTIONAL_CONTENT_FIELDS = frozenset({"gen_ai.system_instructions", "gen_ai.tool.definitions"})
AZURE_PROPERTY_VALUE_LIMIT = 8192
TABLES = frozenset({"AppDependencies", "AppTraces", "AppMetrics"})
COLUMNS = frozenset({"Table", "Name", "Id", "OperationId", "ParentId", "Properties",
                     "Sum", "ItemCount"})
LIMITATIONS = [
    "Source provenance depends on the parent's exclusive CLI collector evidence; "
    "OTLP alone cannot attest producer identity.",
    "Content privacy is checked only after collector transforms and in Azure, not in raw CLI output. "
    "CLI 1.0.84-5 has emitted gen_ai.tool.definitions with capture disabled; "
    "downstream filtering is the privacy boundary, not native CLI behavior.",
    "Source completeness depends on the parent's retained preflight provenance and quiesced snapshot; "
    "deleted historical records cannot be detected from surviving JSON-lines alone.",
    "Histogram bucket boundaries/counts are lost in Azure AppMetrics conversion; "
    "only individual sum/count snapshots are compared, not cumulative totals.",
]


class QueryError(AppError):
    """A sanitized query failure with an explicit retry policy."""

    def __init__(self, message, *, retryable=False, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _object(value, context):
    if not isinstance(value, dict):
        raise AppError(f"Expected object in {context}")
    return value


def _array(value, context):
    if not isinstance(value, list):
        raise AppError(f"Expected array in {context}")
    return value


def _number(value, context):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AppError(f"Expected finite number in {context}")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise AppError(f"Expected finite number in {context}") from error
    if not math.isfinite(number):
        raise AppError(f"Expected finite number in {context}")
    return number


def _value(value):
    value = _object(value, "OTLP attribute")
    if len(value) != 1:
        raise AppError("OTLP AnyValue must have exactly one typed value")
    kind, data = next(iter(value.items()))
    if kind in ("stringValue", "bytesValue") and isinstance(data, str):
        return data
    if kind == "boolValue" and isinstance(data, bool):
        return data
    if kind == "intValue" and not isinstance(data, bool):
        if isinstance(data, int) or (isinstance(data, str) and re.fullmatch(r"-?\d+", data)):
            return int(data)
    if kind == "doubleValue":
        return _number(data, "OTLP doubleValue")
    if kind == "arrayValue":
        return [_value(item) for item in _array(_object(data, "arrayValue").get("values", []),
                                                "arrayValue.values")]
    if kind == "kvlistValue":
        return decode_attributes(_object(data, "kvlistValue").get("values", []))
    raise AppError("Unsupported or malformed OTLP typed attribute")


def decode_attributes(attributes):
    """Decode typed attributes; collapse identical encodings, reject conflicting duplicates."""
    result, encodings = {}, {}
    for attribute in _array(attributes, "attributes"):
        attribute = _object(attribute, "attribute")
        key = attribute.get("key")
        if not isinstance(key, str) or not key:
            raise AppError("Missing OTLP attribute key")
        raw = attribute.get("value")
        value = _value(raw)
        encoding = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        if key in result:
            if encodings[key] != encoding:
                raise AppError("Conflicting duplicate OTLP attribute key")
            continue
        result[key] = value
        encodings[key] = encoding
    return result


def _id(value, size, *, optional=False):
    if optional and (value is None or value == "" or value == "0" * size):
        return ""
    if not isinstance(value, str):
        raise AppError("Malformed telemetry correlation ID")
    if value.startswith("|"):
        parts = value[1:].rstrip(".").split(".")
        value = parts[0] if size == 32 else parts[-1]
    if re.fullmatch(r"[0-9a-fA-F]{" + str(size) + "}", value):
        normalized = value.lower()
    else:
        try:
            normalized = base64.b64decode(value, validate=True).hex()
        except (ValueError, base64.binascii.Error) as error:
            raise AppError("Malformed telemetry correlation ID") from error
        if len(normalized) != size:
            raise AppError("Malformed telemetry correlation ID")
    if normalized == "0" * size:
        if optional:
            return ""
        raise AppError("Zero telemetry correlation ID")
    return normalized


def _resource(group):
    resource = decode_attributes(_object(group.get("resource"), "resource").get("attributes", []))
    if not isinstance(resource.get("copilot.run.id"), str):
        raise AppError("Source resource copilot.run.id must be a string UUID")
    validate_run_id(resource["copilot.run.id"])
    return resource


def _scope(group):
    scope = _object(group.get("scope", {}), "instrumentation scope")
    name, version = scope.get("name", ""), scope.get("version", "")
    if not isinstance(name, str) or not isinstance(version, str):
        raise AppError("Malformed instrumentation scope name/version")
    decode_attributes(scope.get("attributes", []))
    return {"name": name, "version": version}


def parse_source(text, run_id):
    """Validate complete shared collector JSON-lines, then select one run's groups."""
    validate_run_id(run_id)
    spans, events, metrics = [], [], []
    seen = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            document = _object(json.loads(line), "collector line")
        except json.JSONDecodeError as error:
            raise AppError(f"Invalid or truncated collector JSON on line {line_number}") from error
        if not any(key in document for key in ("resourceSpans", "resourceMetrics")):
            raise AppError("Unsupported collector envelope; expected resourceSpans/resourceMetrics JSON-lines")
        for group in _array(document.get("resourceSpans", []), "resourceSpans"):
            group = _object(group, "resourceSpans entry")
            resource = _resource(group)
            for scope in _array(group.get("scopeSpans", []), "scopeSpans"):
                scope = _object(scope, "scopeSpans entry")
                identity = _scope(scope)
                for raw in _array(scope.get("spans", []), "spans"):
                    raw = _object(raw, "span")
                    name = raw.get("name")
                    if not isinstance(name, str) or not name:
                        raise AppError("Source span has no name")
                    span = {
                        "name": name, "trace_id": _id(raw.get("traceId"), 32),
                        "span_id": _id(raw.get("spanId"), 16),
                        "parent_id": _id(raw.get("parentSpanId"), 16, optional=True),
                        "kind": raw.get("kind", 0), "resource": resource, "scope": identity,
                        "attributes": decode_attributes(raw.get("attributes", [])),
                    }
                    key = (resource["copilot.run.id"], span["trace_id"], span["span_id"])
                    record = (resource, identity, raw)
                    if key in seen:
                        if seen[key] != record:
                            raise AppError("Conflicting duplicate source span IDs")
                        continue
                    seen[key] = record
                    spans.append(span)
                    for event in _array(raw.get("events", []), "span events"):
                        event = _object(event, "span event")
                        if not isinstance(event.get("name"), str) or not event["name"]:
                            raise AppError("Source span event has no name")
                        events.append({
                            "name": event["name"], "trace_id": span["trace_id"],
                            "parent_id": span["span_id"], "resource": resource, "scope": identity,
                            "attributes": decode_attributes(event.get("attributes", [])),
                        })
        for group in _array(document.get("resourceMetrics", []), "resourceMetrics"):
            group = _object(group, "resourceMetrics entry")
            resource = _resource(group)
            for scope in _array(group.get("scopeMetrics", []), "scopeMetrics"):
                scope = _object(scope, "scopeMetrics entry")
                identity = _scope(scope)
                for metric in _array(scope.get("metrics", []), "metrics"):
                    metric = _object(metric, "metric")
                    name = metric.get("name")
                    if not isinstance(name, str) or not name:
                        raise AppError("Source metric has no name")
                    kinds = [key for key in ("histogram", "exponentialHistogram", "sum", "gauge")
                             if key in metric]
                    if len(kinds) != 1:
                        raise AppError("Unsupported metric format; expected histogram, sum or gauge")
                    kind = kinds[0]
                    data = _object(metric[kind], "metric data")
                    for point in _array(data.get("dataPoints", []), "metric dataPoints"):
                        point = _object(point, "metric point")
                        if kind in ("histogram", "exponentialHistogram"):
                            count = _number(point.get("count"), "histogram count")
                            total = _number(point.get("sum"), "histogram sum")
                            if count < 0 or not count.is_integer():
                                raise AppError("Invalid histogram count")
                        else:
                            count = 1
                            total = _number(point.get("asDouble", point.get("asInt")), "metric value")
                        metrics.append({
                            "name": name, "resource": resource, "scope": identity,
                            "attributes": decode_attributes(point.get("attributes", [])),
                            "count": count, "sum": total, "kind": kind,
                            "temporality": data.get("aggregationTemporality"),
                        })
    for item in spans + events + metrics:
        if ("copilot.run.id" in item["attributes"]
                and item["attributes"]["copilot.run.id"] != item["resource"]["copilot.run.id"]):
            raise AppError("Source signal copilot.run.id conflicts with its resource")
    spans, events, metrics = (
        [item for item in items if item["resource"]["copilot.run.id"] == run_id]
        for items in (spans, events, metrics))
    if not spans and not metrics:
        raise AppError("Collector evidence contains no spans or metrics for the requested run")
    for item in spans + events + metrics:
        if item["resource"].get("service.name") != "github-copilot":
            raise AppError("Source service.name must identify the actual github-copilot CLI")
        if item["scope"]["name"] != "github.copilot":
            raise AppError("Source instrumentation scope must identify the actual github.copilot CLI")
    return {"spans": spans, "events": events, "metrics": metrics}


def _cli_version(value):
    if not isinstance(value, str):
        raise AppError("CLI manifest and source service.version must contain a supported CLI version")
    match = re.fullmatch(
        r"(?:GitHub Copilot CLI\s+)?v?(\d+\.\d+\.\d+"
        r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)\.?", value.strip())
    if match is None:
        raise AppError("CLI manifest or source service.version has an unrecognized version format")
    return match.group(1)


def _utc(value):
    if not isinstance(value, str):
        raise AppError("Manifest timestamps must be UTC ISO strings")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise AppError("Manifest timestamps must be UTC ISO strings") from error
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise AppError("Manifest timestamps must include UTC timezone")
    return parsed


def validate_manifest(manifest, run_id, *, require_azure=True):
    """Validate the exact required parent/CLI manifest contract; allow extra keys."""
    _object(manifest, "manifest")
    validate_run_id(run_id)
    if manifest.get("run_id") != run_id:
        raise AppError("Manifest run_id does not match requested run")
    if manifest.get("collector_mode") not in ("azure", "local-only"):
        raise AppError("Manifest collector_mode must explicitly be azure or local-only")
    if require_azure and manifest["collector_mode"] != "azure":
        raise AppError("Manifest collector_mode must explicitly be azure; local-only or unknown runs are ineligible")
    scenario = manifest.get("scenario")
    if scenario not in ("metadata-only", "full-content", "delegated"):
        raise AppError("Manifest scenario must be metadata-only, full-content or delegated")
    capture = manifest.get("capture_content")
    if not isinstance(capture, bool):
        raise AppError("Manifest capture_content must be boolean")
    if (scenario == "metadata-only" and capture) or (
            scenario in ("full-content", "delegated") and not capture):
        raise AppError("Manifest capture_content conflicts with scenario")
    marker = manifest.get("marker")
    if not isinstance(marker, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", marker):
        raise AppError("Manifest marker must be a harmless alphanumeric audit marker")
    if type(manifest.get("exit_code")) is not int:
        raise AppError("Manifest exit_code must be an integer")
    if not isinstance(manifest.get("cli_version"), str) or not manifest["cli_version"].strip():
        raise AppError("Manifest cli_version must be nonempty")
    _cli_version(manifest["cli_version"])
    if _utc(manifest.get("finished_at")) < _utc(manifest.get("started_at")):
        raise AppError("Manifest finished_at precedes started_at")
    if not isinstance(manifest.get("evidence_path"), str) or not Path(manifest["evidence_path"]).is_absolute():
        raise AppError("Manifest evidence_path must be absolute")
    return manifest


def trusted_path(value, local=None):
    """Accept only absolute, symlink-free paths strictly below private local state."""
    local = Path(local if local is not None else ROOT / ".local").absolute()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(local) or path == local:
        raise AppError("Evidence/state path must remain within ROOT/.local")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise AppError("Refusing symlink in evidence/state path")
    return path


def _evidence_signature(path):
    trusted_path(str(path))
    backups = re.compile(re.escape(path.stem) + r"-.+" + re.escape(path.suffix) + r"(?:\.gz)?")
    numbered = re.compile(re.escape(path.name) + r"\.\d+(?:\.gz)?")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise AppError("Manifest collector evidence is not a regular file")
        if any(backups.fullmatch(item.name) or numbered.fullmatch(item.name)
               for item in path.parent.iterdir()):
            raise AppError("Collector evidence is rotated and potentially truncated; "
                           "consolidate every segment into a new immutable evidence_path")
    except OSError as error:
        raise AppError("Cannot inspect collector evidence for rotation or truncation") from error
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _require_unchanged_evidence(path, signature):
    if _evidence_signature(path) != signature:
        raise AppError("Collector evidence changed during verification; it may be rotated or truncated")


def build_query(manifest, resource_id, *, template=None):
    """Render JSON-escaped KQL; passing template text makes this helper pure."""
    validate_manifest(manifest, manifest.get("run_id"))
    if not isinstance(resource_id, str) or not resource_id.startswith("/subscriptions/"):
        raise AppError("Azure state application_insights_resource_id is missing or invalid")
    if template is None:
        try:
            template = (ROOT / "queries" / "verify_ingestion.kql").read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise AppError("Cannot read queries/verify_ingestion.kql; restore the owned KQL template") from error
    values = {
        "__RUN_ID__": json.dumps(manifest["run_id"]),
        "__START__": _utc(manifest["started_at"]).isoformat(),
        "__FINISH__": _utc(manifest["finished_at"]).isoformat(),
        "__RESOURCE_ID__": json.dumps(resource_id),
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def _query_error(detail):
    lower = detail.lower()
    if re.search(r"\b(401|403)\b|unauthorized|forbidden|aadsts|az login|authentication|authorizationfailed", lower):
        return QueryError("Azure query authentication/authorization failed; verify the existing session and permissions")
    if "partial" in lower:
        return QueryError("Azure returned partial query results; full proof is unavailable")
    if re.search(r"(failed to resolve|cannot resolve|does not exist|unknown table)", lower) and any(
            table.lower() in lower for table in TABLES):
        return QueryError("Azure telemetry tables are not available yet", retryable=True)
    retryable = bool(re.search(
        r"\b(408|429|500|502|503|504)\b|toomanyrequests|throttl|timeout|timed out|"
        r"serviceunavailable|gatewaytimeout|internalservererror|temporarily unavailable|connection reset",
        lower))
    delay = None
    match = re.search(r"retry-after[\"']?\s*[:=]\s*[\"']?([^\r\n\"']+)", detail, re.I)
    if match:
        text = match.group(1).strip()
        try:
            delay = max(0.0, float(text))
        except ValueError:
            try:
                date = parsedate_to_datetime(text)
                delay = max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                delay = None
        if delay is not None and not math.isfinite(delay):
            delay = None
    return QueryError("Azure query transient failure" if retryable else "Azure query failed",
                      retryable=retryable, retry_after=delay)


def parse_query_response(payload):
    """Parse named Azure columns, rejecting partial errors and malformed results."""
    payload = _object(payload, "Azure response")
    for key in ("error", "errors", "partialError", "partialErrors"):
        if key in payload:
            raise _query_error(json.dumps(payload[key]))
    tables = _array(payload.get("tables"), "Azure tables")
    if not tables:
        raise AppError("Azure response has no result table")
    rows = []
    for table in tables:
        table = _object(table, "Azure table")
        if "error" in table:
            raise _query_error(json.dumps(table["error"]))
        columns = _array(table.get("columns"), "Azure columns")
        names = [_object(column, "Azure column").get("name") for column in columns]
        if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
            raise AppError("Azure response has invalid or duplicate named columns")
        if not COLUMNS.issubset(names):
            raise AppError("Azure response is missing required proof columns")
        for values in _array(table.get("rows"), "Azure rows"):
            if not isinstance(values, list) or len(values) != len(names):
                raise AppError("Azure response has a malformed row")
            row = dict(zip(names, values))
            if row["Table"] not in TABLES or not isinstance(row["Name"], str) or not row["Name"]:
                raise AppError("Azure response has an invalid telemetry table/name")
            properties = row["Properties"]
            if isinstance(properties, str):
                try:
                    properties = json.loads(properties)
                except json.JSONDecodeError as error:
                    raise AppError("Azure Properties is not valid JSON") from error
            row["Properties"] = _object(properties, "Azure Properties")
            for key in ("Id", "OperationId", "ParentId"):
                if row[key] is None:
                    row[key] = ""
                if not isinstance(row[key], str):
                    raise AppError("Azure response has a malformed correlation column")
            if row["Table"] == "AppMetrics":
                row["Sum"] = _number(row["Sum"], "Azure metric Sum")
                row["ItemCount"] = _number(row["ItemCount"], "Azure metric ItemCount")
                if row["ItemCount"] < 0 or not row["ItemCount"].is_integer():
                    raise AppError("Azure metric ItemCount must be a nonnegative integer")
            rows.append(row)
    return rows


def _content_key(key):
    return any(key == field or key.startswith(field + ".") for field in CONTENT_FIELDS)


def _canonical(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _content(attributes, prefix="", *, canonical=True):
    result = {}
    for key, raw_value in attributes.items():
        path = prefix + key
        value = _canonical(raw_value)
        if _content_key(path):
            result[path] = value if canonical else raw_value
        elif isinstance(value, dict):
            result.update(_content(value, path + ".", canonical=canonical))
    return result


def _compare_content(source_attributes, properties):
    expected, actual = _content(source_attributes), _content(properties)
    if expected.keys() != actual.keys():
        return False, 0
    source_raw = _content(source_attributes, canonical=False)
    actual_raw = _content(properties, canonical=False)
    truncated = 0
    for key, value in expected.items():
        if value == actual[key]:
            continue
        original, exported = source_raw[key], actual_raw[key]
        if (key not in OPTIONAL_CONTENT_FIELDS or not isinstance(original, str)
                or not isinstance(exported, str)
                or len(original.encode("utf-8")) <= AZURE_PROPERTY_VALUE_LIMIT
                or exported != original.encode("utf-8")[:AZURE_PROPERTY_VALUE_LIMIT].decode("utf-8", errors="ignore")):
            return False, 0
        truncated += 1
    return True, truncated


def _record_optional_truncation(result, checks):
    count = max((count for _, count in checks), default=0)
    result["counts"]["optional_content_truncations"] += count
    note = ("Observed optional system/tool-definition truncation at the exporter's UTF-8 byte "
            "property-value "
            "limit; content transport is not lossless.")
    if count and note not in result["limitations"]:
        result["limitations"].append(note)


def _properties_match(item, row):
    expected = {key: value for key, value in item["resource"].items() if isinstance(value, str)}
    expected.update({key: value for key, value in item["attributes"].items()
                     if isinstance(value, str) and not _content_key(key)})
    expected["instrumentationlibrary.name"] = item["scope"]["name"]
    if item["scope"]["version"]:
        expected["instrumentationlibrary.version"] = item["scope"]["version"]
    return all(row["Properties"].get(key) == value for key, value in expected.items())


def _parent_matches(span, row):
    parent = row["ParentId"]
    # Application Insights can populate a root's empty parent with its operation ID.
    if isinstance(parent, str) and parent.lower() == span["trace_id"]:
        return not span["parent_id"]
    return _id(parent, 16, optional=True) == span["parent_id"]


def _lifecycle(event):
    return bool(re.search(
        r"(?:^|[._])lifecycle(?:$|[._])|"
        r"(session|turn|agent|chat|operation).*(start|end|complet|finish|creat)",
        event["name"], re.I))


def _duration(name):
    return name.startswith("gen_ai.") and (name.endswith(".duration") or ".duration." in name)


def _metric_dimensions_match(point, row, dimension_keys):
    expected = {**point["resource"], **point["attributes"]}
    actual = row["Properties"]
    for key in dimension_keys:
        if key not in expected:
            if key in actual:
                return False
            continue
        if key not in actual:
            return False
        value = expected[key]
        candidate = actual[key] if isinstance(value, str) else _canonical(actual[key])
        if isinstance(value, bool) != isinstance(candidate, bool) or candidate != value:
            return False
    return True


def compare_evidence(manifest, source, rows):
    """Return sanitized passed/pending/failed/unsupported source-vs-backend proof.

    Missing Azure signals are pending; impossible source proof and observed
    correlation/content mismatches fail immediately. Absence is never success.
    """
    return _evaluate_evidence(manifest, source, rows, source_only=False)


def check_source_evidence(manifest, source):
    """Check source identity, privacy, markers, metrics and parenting without Azure proof."""
    return _evaluate_evidence(manifest, source, [], source_only=True)


def _evaluate_evidence(manifest, source, rows, *, source_only):
    result = {
        "status": "pending", "reasons": [], "limitations": list(LIMITATIONS),
        "proof_scope": ("post-transform-collector-source-only" if source_only
                        else "post-transform-collector-to-azure"),
        "azure_ingestion_proven": False,
        "native_cli_content_privacy_verified": False,
        "counts": {"source_spans": len(source["spans"]), "source_events": len(source["events"]),
                   "source_metric_points": len(source["metrics"]), "azure_rows": 0,
                   "matched_spans": 0, "matched_events": 0, "matched_metrics": 0,
                   "optional_content_truncations": 0},
    }

    def stop(status, reason):
        result["status"] = status
        result["reasons"].append(reason)
        return result

    try:
        validate_manifest(manifest, manifest.get("run_id"), require_azure=not source_only)
    except AppError as error:
        return stop("failed", str(error))
    if manifest["exit_code"] != 0:
        return stop("failed", "CLI process exited unsuccessfully")
    spans, events, metrics = source["spans"], source["events"], source["metrics"]
    try:
        version = _cli_version(manifest["cli_version"])
        if any(_cli_version(item["resource"].get("service.version")) != version
               for item in spans + events + metrics):
            return stop("failed", "Source service.version does not match the manifest CLI version")
    except AppError as error:
        return stop("failed", str(error))
    operations = {span["attributes"].get("gen_ai.operation.name") for span in spans}
    if not {"invoke_agent", "chat"}.issubset(operations):
        return stop("failed", "Source CLI invoke_agent and chat spans are required")
    if not any(_lifecycle(event) for event in events):
        return stop("failed", "Source has no identifiable lifecycle span event; mandatory full proof "
                    "is unavailable (confirm CLI/collector event output)")
    if any(event["name"] == "exception" for event in events):
        return stop("unsupported", "Source exception events require AppExceptions proof, "
                    "outside the supported ordinary AppTraces lifecycle mapping")
    series, dimension_keys, projected_identities = {}, {}, {}
    for point in metrics:
        name = point["name"]
        if name != "gen_ai.client.token.usage" and not _duration(name):
            continue
        key = json.dumps([name, point["resource"], point["attributes"]], sort_keys=True)
        projected = json.dumps([name, {**point["resource"], **point["attributes"]}], sort_keys=True)
        if projected in projected_identities and projected_identities[projected] != key:
            return stop("failed", "Distinct source metric series collapse to the same flattened "
                        "resource/datapoint dimensions; backend identity cannot be proven")
        projected_identities[projected] = key
        series.setdefault(key, []).append(point)
        dimension_keys.setdefault(name, set()).update(point["resource"])
        dimension_keys[name].update(point["attributes"])
    required_metrics = {points[0]["name"] for points in series.values()}
    if "gen_ai.client.token.usage" not in required_metrics or not any(map(_duration, required_metrics)):
        return stop("failed", "Source token usage and duration metrics with nonempty points are required")
    if any(not any(point["count"] >= 1 and point["sum"] > 0 for point in points)
           for points in series.values()):
        return stop("failed", "Each required source metric series needs a positive numeric snapshot")
    result["counts"]["required_metric_series"] = len(series)
    by_id = {(span["trace_id"], span["span_id"]): span for span in spans}
    for span in spans:
        visited = {span["span_id"]}
        current = span
        while current["parent_id"]:
            parent = by_id.get((current["trace_id"], current["parent_id"]))
            if parent is None or parent["span_id"] in visited:
                return stop("failed", "Source span parenting is broken, cyclic or outside the run")
            visited.add(parent["span_id"])
            current = parent
    if manifest["scenario"] == "delegated":
        delegated = False
        for span in spans:
            if span["attributes"].get("gen_ai.operation.name") != "invoke_agent":
                continue
            parent_id = span["parent_id"]
            while parent_id:
                parent = by_id[(span["trace_id"], parent_id)]
                if parent["attributes"].get("gen_ai.operation.name") == "invoke_agent":
                    delegated = True
                parent_id = parent["parent_id"]
        if not delegated:
            return stop("unsupported", "Delegated execution has no source parent/child agent evidence")
    items = spans + events + metrics
    if not manifest["capture_content"] and any(
            _content(item["attributes"]) or _content(item["resource"]) for item in items):
        return stop("failed", "Content-off source contains gated content fields")
    if manifest["capture_content"]:
        for field in ("gen_ai.input.messages", "gen_ai.output.messages"):
            if not any(manifest["marker"] in json.dumps(_content(span["attributes"]).get(field, ""))
                       for span in spans
                       if span["attributes"].get("gen_ai.operation.name") in ("invoke_agent", "chat")):
                return stop("failed", "Content-on source requires marker-bearing input and output messages")
        tools = [span for span in spans if span["attributes"].get("gen_ai.operation.name") == "execute_tool"]
        if not any(all(manifest["marker"] in json.dumps(_content(span["attributes"]).get(key, ""))
                       for key in ("gen_ai.tool.call.arguments", "gen_ai.tool.call.result"))
                   for span in tools):
            return stop("failed", "Content-on source requires execute_tool arguments and result with audit marker")
        if not any(_content(item["attributes"]) for item in items):
            return stop("failed", "Content-on source has no captured content")
    if source_only:
        result["status"] = "source-validated"
        return result
    rows = [row for row in rows if row["Properties"].get("copilot.run.id") == manifest["run_id"]]
    result["counts"]["azure_rows"] = len(rows)
    if not rows:
        return stop("pending", "No Azure rows match the run; content absence is not proof")
    if not manifest["capture_content"] and any(_content(row["Properties"]) for row in rows):
        return stop("failed", "Content-off Azure telemetry contains gated content fields")
    pending = []
    for span in spans:
        if span["kind"] not in (0, 1, 3, "SPAN_KIND_UNSPECIFIED", "SPAN_KIND_INTERNAL",
                                "SPAN_KIND_CLIENT", "UNSPECIFIED", "INTERNAL", "CLIENT"):
            return stop("failed", "Unsupported source span kind; cannot assert AppDependencies mapping")
        candidates = [row for row in rows if row["Table"] == "AppDependencies"
                      and _id(row["Id"], 16) == span["span_id"]]
        if not candidates:
            pending.append("Source spans are missing from Azure AppDependencies")
            continue
        for row in candidates:
            if (row["Name"] != span["name"] or _id(row["OperationId"], 32) != span["trace_id"]
                    or not _parent_matches(span, row)):
                return stop("failed", "Azure span name/OperationId/ParentId differs from source")
        content_checks = [_compare_content({**span["resource"], **span["attributes"]}, row["Properties"])
                          for row in candidates]
        if any(not matched for matched, _ in content_checks):
            return stop("failed", "Azure captured span content differs from source")
        if not any(_properties_match(span, row) for row in candidates):
            pending.append("Azure span properties do not preserve source attributes")
            continue
        _record_optional_truncation(result, content_checks)
        result["counts"]["matched_spans"] += 1
    for event in events:
        candidates = [row for row in rows if row["Table"] == "AppTraces" and row["Name"] == event["name"]
                      and _id(row["OperationId"], 32) == event["trace_id"]
                      and _id(row["ParentId"], 16, optional=True) == event["parent_id"]]
        if not candidates:
            pending.append("Source span events are missing or uncorrelated in Azure AppTraces")
            continue
        content_checks = [_compare_content({**event["resource"], **event["attributes"]}, row["Properties"])
                          for row in candidates]
        if any(not matched for matched, _ in content_checks):
            return stop("failed", "Azure captured event content differs from source")
        if not any(_properties_match(event, row) for row in candidates):
            pending.append("Azure event properties do not preserve source attributes")
            continue
        _record_optional_truncation(result, content_checks)
        result["counts"]["matched_events"] += 1
    for snapshots in series.values():
        name = snapshots[0]["name"]
        points = [point for point in snapshots if point["count"] >= 1 and point["sum"] > 0]
        matched = any(
            row["Table"] == "AppMetrics" and row["Name"] == name
            and _properties_match(point, row)
            and _metric_dimensions_match(point, row, dimension_keys[name])
            and _content(row["Properties"]) == _content({**point["resource"], **point["attributes"]})
            and math.isclose(_number(row["Sum"], "metric Sum"), point["sum"], rel_tol=1e-6, abs_tol=1e-9)
            and _number(row["ItemCount"], "metric ItemCount") == point["count"]
            for point in points for row in rows)
        if matched:
            result["counts"]["matched_metrics"] += 1
        else:
            pending.append("Azure metrics lack a matching positive snapshot for each source metric series "
                           "(name, resource and datapoint dimensions)")
    if pending:
        result["reasons"] = sorted(set(pending))
        return result
    result["status"] = "passed"
    result["azure_ingestion_proven"] = True
    return result


def query_azure(state, manifest, timeout):
    """Execute one noninteractive, explicitly scoped az rest request."""
    subscription = state.get("subscription_id")
    workspace = state.get("workspace_customer_id")
    if not isinstance(subscription, str) or not isinstance(workspace, str):
        raise AppError("Azure state requires subscription_id and workspace_customer_id UUIDs")
    validate_run_id(subscription)
    validate_run_id(workspace)
    body = {"query": build_query(manifest, state.get("application_insights_resource_id")),
            "timespan": f'{manifest["started_at"]}/{manifest["finished_at"]}'}
    args = ["az", "rest", "--method", "POST", "--url",
            f"https://api.loganalytics.azure.com/v1/workspaces/{workspace}/query",
            "--resource", "https://api.loganalytics.io", "--subscription", subscription,
            "--headers", "Content-Type=application/json", "--body", json.dumps(body),
            "--output", "json", "--only-show-errors"]
    env = {**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"}
    return run_json(args, env=env, timeout=timeout)


def verify(run_id, timeout_seconds=600, *, query=None, clock=None, sleep=None):
    """Poll until complete proof or deadline; persist private artifacts on every outcome."""
    validate_run_id(run_id)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise AppError("timeout-seconds must be a finite positive number")
    query = query if query is not None else query_azure
    clock = clock if clock is not None else time.monotonic
    sleep = sleep if sleep is not None else time.sleep
    deadline = clock() + timeout_seconds
    folder = trusted_path(str(LOCAL / "runs" / run_id))
    if not folder.is_dir():
        raise AppError("Run folder is missing; parent must finish the CLI run first")
    for name in ("results.json", "raw-query.json"):
        trusted_path(str(folder / name))
    result = {"status": "failed", "counts": {}, "reasons": [], "limitations": list(LIMITATIONS)}
    raw = {"attempts": 0, "response": None}
    attempts = 0
    try:
        manifest = validate_manifest(load_json(trusted_path(str(folder / "manifest.json"))), run_id)
        if manifest["exit_code"] != 0:
            raise AppError("CLI process exited unsuccessfully")
        path = trusted_path(manifest["evidence_path"])
        signature = _evidence_signature(path)
        try:
            text = path.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                raise AppError("Collector evidence has an incomplete or truncated final JSON-line; "
                               "quiesce the exporter and require its terminating LF")
            source = parse_source(text, run_id)
        except (OSError, UnicodeError) as error:
            raise AppError("Cannot read collector evidence as UTF-8 JSON-lines") from error
        _require_unchanged_evidence(path, signature)
        result = compare_evidence(manifest, source, [])
        if result["status"] not in ("failed", "unsupported"):
            state = load_json(trusted_path(str(LOCAL / "azure.json")))
            while clock() < deadline:
                _require_unchanged_evidence(path, signature)
                attempts += 1
                retry_after = None
                raw = {"attempts": attempts, "response": None}
                received_response = False
                try:
                    payload = query(state, manifest, max(0.001, deadline - clock()))
                    received_response = True
                    raw = {"attempts": attempts, "response": payload}
                    rows = parse_query_response(payload)
                    result = compare_evidence(manifest, source, rows)
                except AppError as error:
                    if isinstance(error, QueryError):
                        failure = error
                    elif received_response:
                        failure = QueryError(f"Azure evidence validation failed: {error}")
                    else:
                        failure = _query_error(str(error))
                    result["status"] = "pending" if failure.retryable else "failed"
                    result["reasons"] = [str(failure)]
                    raw["attempts"] = attempts
                    raw["query_error"] = str(failure)
                    retry_after = failure.retry_after
                _require_unchanged_evidence(path, signature)
                if clock() >= deadline:
                    break
                if result["status"] in ("passed", "failed", "unsupported"):
                    break
                delay = min(30.0, 2.0 ** min(attempts - 1, 5))
                if retry_after is not None:
                    delay = max(delay, retry_after)
                sleep(min(delay, max(0.0, deadline - clock())))
            if clock() >= deadline and result["status"] not in ("failed", "unsupported"):
                result["status"] = "failed"
                result["reasons"].append("Ingestion verification deadline exceeded before full proof")
    except AppError as error:
        result["status"] = "failed"
        result["reasons"] = [str(error)]
    result["run_id"] = run_id
    result["attempts"] = attempts
    result["proof_scope"] = "post-transform-collector-to-azure"
    result["azure_ingestion_proven"] = result["status"] == "passed"
    result["native_cli_content_privacy_verified"] = False
    write_json(folder / "raw-query.json", raw)
    write_json(folder / "results.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    try:
        result = verify(args.run_id, args.timeout_seconds)
    except AppError as error:
        print(json.dumps({"status": "failed", "reasons": [str(error)]}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
