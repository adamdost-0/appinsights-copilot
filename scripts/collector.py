"""Private, explicitly owned, local Docker bridge; no cloud provisioning.

Usage: python3 -m scripts.collector validate|start|status|stop
start requires .local/collector.env, owned by this user, mode 0600, containing
only APPLICATIONINSIGHTS_CONNECTION_STRING=<full unquoted connection string>.
Standard validate uses a synthetic key with networking disabled; no Azure state
or credentials are required. Docker must be local and able to bind host paths.

Diagnostic fallback: start --local-only and validate --local-only select
collector/otelcol-local.yaml, with only file export and health, no Azure exporter
and no credential injection (not even a synthetic key). This is never automatic:
standard start still requires the deployer's env file. Both modes accept OTLP
HTTP JSON and protobuf. All output has azure_ingestion_proven=false.
State and start/status/stop output identify mode="local-only" or mode="azure";
ownership labels also bind the mode, preventing local state from being relabelled
as cloud verification. Missing or unknown state modes are rejected, never inferred.
Use a fresh copilot.run.id when switching modes; both share the evidence file.
The parent may start --local-only after validation for its live CLI capture,
then stop it explicitly. Azure remains unverified in this diagnostic fallback.

Privacy boundary: the real CLI was observed emitting nonempty
gen_ai.tool.definitions (841 serialized characters) on chat/invoke spans even
with metadata-only capture flags false. Other gated keys and the token were
absent in that observation; it is NOT proof of upstream CLI privacy.
Both configurations run transform/privacy before trace export, deleting these
six exact keys from span AND span-event attributes by default:
gen_ai.input.messages, gen_ai.output.messages, gen_ai.system_instructions,
gen_ai.tool.definitions, gen_ai.tool.call.arguments, gen_ai.tool.call.result.
Only explicit resource copilot.audit.scenario="full-content" or "delegated" bypasses
this filter; use those opt-ins only for synthetic full-content captures.
Missing/unknown scenarios remain filtered, and span/event-local scenario values
cannot opt out. There is no copilot.scenario alias: it never opts out or overrides
the audit scenario. OTTL error_mode=propagate rejects processing failures.
The evidence file is post-transform: it demonstrates the collector's outgoing
boundary, not what left the CLI. This exact-key filter is not general secret
redaction and does not sanitize resource attributes, metrics, or arbitrary keys.
Restart an existing collector to load config changes; they are not hot-reloaded.

Reader contract: .local/collector.json has container_id, ownership_marker,
started_at (UTC), and evidence_path (absolute). evidence_path is a JSONL file,
NOT one JSON document: each complete line is an OTLP resourceSpans or
resourceMetrics envelope. Read as the invoking host user, tolerate an unfinished
final line while running, and select the caller-supplied copilot.run.id resource
attribute. The collector preserves that attribute; it does not invent run IDs.
All runs share a precreated 0600 file in a 0700 directory, including restarts.
v0.160.0 rotation creates replacement/backup files with mode 0644; the 0700
directory remains the access boundary. Never loosen directory permissions.
stop restores all retained evidence files to 0600 after the writer has exited.
No Azure credentials, connection string, or telemetry are printed by this CLI.

Run status before AND after a smoke run and read evidence promptly. At 8 MiB,
or if rotation has occurred, start/status fail closed: stop, securely archive
the evidence (including otel-*.json backups), and clear it explicitly before
another smoke. Nothing is silently truncated by this CLI. Exporter rotation
bounds retained payloads to 10 MiB each, two backups plus the active file
(cleanup is asynchronous; transient disk usage may exceed 30 MiB).
The single-file reader must not claim completeness after rotation.

Health and file evidence prove only local acceptance, never Azure persistence.
Verify Azure ingestion separately by run ID. Docker operators can inspect
container environment variables; this is not a defense against Docker admins.
There is no restart policy or permanent host service. stop allows 30 seconds,
longer than the exporter's 10-second shutdown timeout, and retains evidence.
It reports clean_shutdown=true only after observing Running=false, Status=exited,
ExitCode=0 and OOMKilled=false. A successful Docker stop command alone is not
flush evidence: Docker can escalate to SIGKILL and still return success.
Abnormal or incomplete exit status raises an error, retaining the container,
collector.json and evidence unchanged. Do not use that capture as flushed proof.
Operator recovery: inspect the exact saved container_id with
docker inspect --format '{{json .State}}' <container_id> (do not dump environment),
verify ownership labels against saved state, and archive all suspect evidence.
After diagnosis, an operator may explicitly docker rm <container_id> for the
stopped owned container, then run this CLI's stop to clear stale state. That
recovery returns clean_shutdown=false; it never upgrades an abnormal capture.
Clean shutdown is not an fsync durability or Azure-ingestion guarantee.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from uuid import UUID, uuid4

from scripts.common import (AppError, LOCAL, ROOT, load_json, private_dir, redact,
                            run, write_json)


PINNED_IMAGE = ("otel/opentelemetry-collector-contrib:0.160.0@sha256:"
                "799dc6cf12c96192af37b5bdba804da8c10b3bc563b43cb90c3f3c58d9572ad6")
IMAGE_FILE = ROOT / "collector/image.txt"
CONFIG_FILE = ROOT / "collector/otelcol.yaml"
LOCAL_CONFIG_FILE = ROOT / "collector/otelcol-local.yaml"
CONNECTION_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"
SYNTHETIC_CONNECTION = ("InstrumentationKey=00000000-0000-0000-0000-000000000001;"
                        "IngestionEndpoint=http://127.0.0.1:9/")
OWNER_LABEL = "io.appinsights-copilot.collector.owner"
PROJECT_LABEL = "io.appinsights-copilot.collector.project"
MODE_LABEL = "io.appinsights-copilot.collector.mode"
HEALTH_TIMEOUT = 20
EVIDENCE_PREFLIGHT_BYTES = 8 * 1024 * 1024
INSPECT_FORMAT = ('{"Id":{{json .Id}},"Name":{{json .Name}},'
                  '"Labels":{{json .Config.Labels}},"State":{{json .State}},'
                  '"Ports":{{json .NetworkSettings.Ports}}}')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_health(port: int) -> bool:
    """Probe on the host, bypassing proxies; scratch images have no curl."""
    url = f"http://127.0.0.1:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(url, timeout=2) as response:
            return response.status == 200 and response.url == url
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def _json_command(args: list[str]) -> dict:
    try:
        value = json.loads(run(args).stdout)
    except json.JSONDecodeError as error:
        raise AppError("Docker returned invalid JSON") from error
    if not isinstance(value, dict):
        raise AppError("Docker returned a non-object response")
    return value


def _owned_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise AppError(f"Refusing symlink private file: {path}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise AppError(f"Expected a regular private file with no hardlinks: {path}")
    if info.st_uid != os.getuid():
        raise AppError(f"Private file must be owned by this user: {path}")


def _private_file(path: Path) -> None:
    _owned_regular_file(path)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise AppError(f"{path} must be owned by this user with mode 0600 (chmod 600)")


class Collector:
    def __init__(self, *, local: Path = LOCAL, otlp_port: int = 4318,
                 health_port: int = 13133):
        self.local = Path(local).absolute()
        self.state_path = self.local / "collector.json"
        self.env_path = self.local / "collector.env"
        self.evidence_dir = self.local / "collector-evidence"
        self.evidence_path = self.evidence_dir / "otel.json"
        self.project = hashlib.sha256(str(self.local).encode()).hexdigest()[:16]
        self.name = "appinsights-copilot-collector-" + self.project
        self.otlp_port = otlp_port
        self.health_port = health_port
        for port in (otlp_port, health_port):
            if type(port) is not int or not 0 <= port <= 65535:
                raise AppError("Collector port must be between 0 and 65535")

    @contextmanager
    def _lock(self):
        private_dir(self.local)
        private_dir(self.evidence_dir)
        # Lock the directory itself: no extra state file or stale PID lock.
        descriptor = os.open(self.evidence_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise AppError("Another collector command is in progress") from error
            yield
        finally:
            os.close(descriptor)

    def _image(self) -> str:
        image = IMAGE_FILE.read_text(encoding="utf-8").strip()
        if image != PINNED_IMAGE:
            raise AppError("Collector image pin does not match the approved tag and digest")
        return image

    def _base_create(self, name: str, marker: str, *, local_only: bool = False) -> list[str]:
        config = (LOCAL_CONFIG_FILE if local_only else CONFIG_FILE).absolute()
        if not config.is_file():
            raise AppError(f"Collector configuration missing: {config}")
        if "," in str(config) or "," in str(self.evidence_dir):
            raise AppError("Docker bind mount paths must not contain commas")
        return [
            "docker", "create", "--name", name,
            "--label", f"{OWNER_LABEL}={marker}",
            "--label", f"{PROJECT_LABEL}={self.project}",
            "--label", f"{MODE_LABEL}={'local-only' if local_only else 'azure'}",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--restart", "no", "--log-driver", "none",
            "--mount", f"type=bind,src={config},dst=/etc/otelcol-contrib/config.yaml,readonly",
        ]

    def _connection(self) -> None:
        if not self.env_path.exists() and not self.env_path.is_symlink():
            raise AppError(f"Missing {self.env_path}; deployer must supply it with chmod 600")
        _private_file(self.env_path)
        lines = [line for line in self.env_path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
        if len(lines) != 1:
            raise AppError("collector.env must contain only one connection string assignment")
        key, separator, value = lines[0].partition("=")
        if key != CONNECTION_ENV or not separator:
            raise AppError(f"collector.env must contain only {CONNECTION_ENV}")
        fields = {}
        for part in value.split(";"):
            if part:
                name, equals, item = part.partition("=")
                if not equals or name.lower() in fields:
                    raise AppError("Invalid connection string in collector.env")
                fields[name.lower()] = item
        try:
            UUID(fields.get("instrumentationkey", ""))
        except ValueError as error:
            raise AppError("Invalid connection string InstrumentationKey in collector.env") from error
        if value != value.strip() or any(char in value for char in ('"', "'", "\x00")):
            raise AppError("Use a full unquoted connection string in collector.env")

    def _evidence(self, *, create: bool = False) -> None:
        if create and not self.evidence_path.exists() and not self.evidence_path.is_symlink():
            descriptor = os.open(self.evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        if not self.evidence_path.exists() and not self.evidence_path.is_symlink():
            raise AppError("Collector evidence file is missing")
        if any(self.evidence_dir.glob("otel-*.json")):
            raise AppError("Evidence rotation detected; stop and archive all evidence before smoke")
        _private_file(self.evidence_path)
        if self.evidence_path.stat().st_size >= EVIDENCE_PREFLIGHT_BYTES:
            raise AppError("Evidence size bound reached (8 MiB); stop and archive before smoke")

    def _secure_stopped_evidence(self) -> None:
        paths = [self.evidence_path, *self.evidence_dir.glob("otel-*.json")]
        for path in paths:
            if path.exists() or path.is_symlink():
                _owned_regular_file(path)
                path.chmod(0o600)

    def _state(self) -> dict:
        if not self.state_path.exists() and not self.state_path.is_symlink():
            raise AppError("Collector state missing; collector has not started")
        _private_file(self.state_path)
        state = load_json(self.state_path)
        cid = state.get("container_id")
        marker = state.get("ownership_marker")
        started = state.get("started_at")
        if not isinstance(cid, str) or not re.fullmatch("[0-9a-f]{64}", cid):
            raise AppError("Invalid collector state container_id")
        try:
            if not isinstance(marker, str) or str(UUID(marker)) != marker:
                raise ValueError
            if not isinstance(started, str) or not started.endswith("Z"):
                raise ValueError
            datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError as error:
            raise AppError("Invalid collector state ownership_marker or started_at") from error
        if state.get("evidence_path") != str(self.evidence_path):
            raise AppError("Invalid collector state evidence_path")
        port = state.get("health_port", 13133)
        if type(port) is not int or not 1 <= port <= 65535:
            raise AppError("Invalid collector state health_port")
        if state.get("mode") not in ("azure", "local-only"):
            raise AppError("Invalid collector state mode; expected azure or local-only")
        return state

    def _exists(self, cid: str) -> bool:
        ids = run(["docker", "ps", "--all", "--quiet", "--no-trunc",
                   "--filter", f"id={cid}"]).stdout.split()
        return cid in ids

    def _inspect_owned(self, state: dict) -> dict | None:
        cid = state["container_id"]
        if not self._exists(cid):
            return None
        info = _json_command(["docker", "inspect", "--format", INSPECT_FORMAT, cid])
        labels = info.get("Labels") or {}
        if (info.get("Id") != cid or not isinstance(labels, dict) or
                labels.get(OWNER_LABEL) != state["ownership_marker"] or
                labels.get(PROJECT_LABEL) != self.project):
            raise AppError("Collector ownership mismatch; refusing to adopt or modify container")
        if labels.get(MODE_LABEL) != state["mode"]:
            raise AppError("Collector mode ownership mismatch; refusing to misidentify capture mode")
        if not isinstance(info.get("State"), dict):
            raise AppError("Docker returned invalid container process state")
        return info

    def _remove_owned(self, state: dict, *, require_clean_exit: bool = False) -> bool:
        info = self._inspect_owned(state)
        if info is None:
            return False
        cid = state["container_id"]
        if info["State"].get("Running"):
            run(["docker", "stop", "--time", "30", cid], timeout=60)
            info = self._inspect_owned(state)
            if info is not None and info["State"].get("Running"):
                raise AppError("Collector is still running after graceful stop; state retained")
        if require_clean_exit:
            if info is None:
                raise AppError("Collector disappeared during stop; cannot confirm clean shutdown; "
                               "state and evidence retained")
            process = info["State"]
            if (process.get("Running") is not False or process.get("Status") != "exited" or
                    type(process.get("ExitCode")) is not int or process["ExitCode"] != 0 or
                    process.get("OOMKilled") is not False):
                raise AppError(
                    f"Collector clean shutdown not confirmed: status={process.get('Status')}, "
                    f"exit_code={process.get('ExitCode')}, OOMKilled={process.get('OOMKilled')}; "
                    f"container {cid}, state and evidence retained. Inspect the owned container "
                    "and archive evidence before explicit operator cleanup; flush is unverified")
        if info is not None:
            run(["docker", "rm", cid])
        return True

    def validate(self, *, local_only: bool = False) -> dict:
        image = self._image()
        marker = str(uuid4())
        mode = "local-only" if local_only else "azure"
        args = self._base_create(self.name + "-validate-" + marker, marker, local_only=local_only)
        args += ["--network", "none"]
        env = None
        if not local_only:
            args += ["--env", CONNECTION_ENV]
            env = dict(os.environ, **{CONNECTION_ENV: SYNTHETIC_CONNECTION})
        args += [image, "validate", "--config=/etc/otelcol-contrib/config.yaml"]
        cid = run(args, env=env).stdout.strip()
        if not re.fullmatch("[0-9a-f]{64}", cid):
            raise AppError("Docker create returned invalid container ID during validation")
        state = {"container_id": cid, "ownership_marker": marker, "mode": mode}
        try:
            run(["docker", "start", "-a", cid], env=env, timeout=60)
            info = self._inspect_owned(state)
            if (info is None or info["State"].get("Running") is not False or
                    info["State"].get("ExitCode") != 0):
                raise AppError("Collector configuration validation failed")
        finally:
            try:
                self._remove_owned(state)
            except AppError as error:
                raise AppError(f"Validation cleanup failed for owned container {cid} "
                               f"(ownership_marker={marker}): {error}") from error
        return {"validated": True, "image": image, "mode": mode,
                "synthetic_connection_string": not local_only,
                "azure_ingestion_proven": False}

    def _status(self, state: dict, *, wait: bool = False) -> dict:
        deadline = time.monotonic() + (HEALTH_TIMEOUT if wait else 0)
        while True:
            info = self._inspect_owned(state)
            if info is None:
                raise AppError("Collector state is stale: container missing; use stop to clear state")
            if info["State"].get("Running") is not True:
                raise AppError(f"Collector is not running (exit code {info['State'].get('ExitCode')})")
            if probe_health(state.get("health_port", 13133)):
                # Recheck after the probe: a different service answering is not process proof.
                info = self._inspect_owned(state)
                if info is None or info["State"].get("Running") is not True:
                    raise AppError("Collector exited during health check")
                self._evidence()
                return dict(state, running=True, healthy=True, azure_ingestion_proven=False)
            if time.monotonic() >= deadline:
                raise AppError("Collector host-side health check failed at /health")
            time.sleep(0.2)

    def start(self, *, local_only: bool = False) -> dict:
        with self._lock():
            if self.state_path.exists() or self.state_path.is_symlink():
                raise AppError("Collector state already exists; use status or stop before start")
            image = self._image()
            if not local_only:
                self._connection()
            self._evidence(create=True)
            collision = run(["docker", "ps", "--all", "--quiet", "--no-trunc",
                             "--filter", f"name=^/{self.name}$"]).stdout.strip()
            if collision:
                raise AppError("Collector name collision without state; refusing to adopt container")
            marker = str(uuid4())
            args = self._base_create(self.name, marker, local_only=local_only)
            if not local_only:
                args += ["--env-file", str(self.env_path)]
            args += [
                "--publish", f"127.0.0.1:{self.otlp_port}:4318",
                "--publish", f"127.0.0.1:{self.health_port}:13133",
                "--mount", f"type=bind,src={self.evidence_dir},dst=/evidence",
                image, "--config=/etc/otelcol-contrib/config.yaml",
            ]
            cid = run(args).stdout.strip()
            if not re.fullmatch("[0-9a-f]{64}", cid):
                raise AppError("Docker create returned invalid container ID")
            state = {
                "container_id": cid, "ownership_marker": marker,
                "evidence_path": str(self.evidence_path),
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "health_port": self.health_port,
                "mode": "local-only" if local_only else "azure",
            }
            try:
                write_json(self.state_path, state)
                run(["docker", "start", cid])
                if self.health_port == 0:
                    ports = _json_command(["docker", "inspect", "--format",
                                           "{{json .NetworkSettings.Ports}}", cid])
                    bindings = ports.get("13133/tcp")
                    if (not isinstance(bindings, list) or len(bindings) != 1 or
                            bindings[0].get("HostIp") != "127.0.0.1"):
                        raise AppError("Docker did not publish a loopback health port")
                    state["health_port"] = int(bindings[0]["HostPort"])
                    write_json(self.state_path, state)
                return self._status(state, wait=True)
            except (AppError, OSError, ValueError, KeyboardInterrupt) as error:
                try:
                    self._remove_owned(state)
                except AppError as cleanup_error:
                    raise AppError(f"Collector start failed: {error}; cleanup failed: {cleanup_error}; "
                                   f"state retained at {self.state_path}") from cleanup_error
                self.state_path.unlink(missing_ok=True)
                raise

    def status(self) -> dict:
        with self._lock():
            return self._status(self._state())

    def stop(self) -> dict:
        with self._lock():
            state = self._state()
            removed = self._remove_owned(state, require_clean_exit=True)
            self._secure_stopped_evidence()
            self.state_path.unlink()
            return {"stopped": True, "stale": not removed, "clean_shutdown": removed,
                    "mode": state["mode"],
                    "container_id": state["container_id"],
                    "evidence_path": state["evidence_path"], "azure_ingestion_proven": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "start", "status", "stop"))
    parser.add_argument("--local-only", action="store_true",
                        help="diagnostic file-only capture/validation; Azure remains unverified")
    arguments = parser.parse_args(argv)
    if arguments.local_only and arguments.command not in ("start", "validate"):
        parser.error("--local-only is only supported for start or validate; status reads stored mode")
    try:
        command = getattr(Collector(), arguments.command)
        result = command(local_only=True) if arguments.local_only else command()
        print(json.dumps(result, indent=2))
        return 0
    except AppError as error:
        print(f"Collector error: {redact(str(error))}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, ValueError) as error:
        detail = error.strerror if isinstance(error, OSError) else type(error).__name__
        print(f"Collector error: cannot process local runtime data: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
