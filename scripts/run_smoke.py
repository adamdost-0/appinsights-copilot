"""Emit actual CLI telemetry from temporary, synthetic-only sessions."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

from .common import (AppError, LOCAL, load_json, private_dir, redact, run, run_session,
                     validate_run_id, write_json, write_private)

SCENARIOS = ("metadata-only", "full-content", "delegated")
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authentication_token() -> str:
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        token = run(["gh", "auth", "token", "--hostname", "github.com"], timeout=15).stdout.strip()
    except AppError as error:
        raise AppError("GitHub authentication is required: use gh auth login or a supported "
                       "Copilot token environment variable; tokens are never saved by this harness") from error
    if not token:
        raise AppError("GitHub CLI returned an empty authentication token")
    return token


def child_environment(home: Path, run_id: str, scenario: str, token: str,
                      inherited: dict[str, str] | None = None) -> dict[str, str]:
    validate_run_id(run_id)
    if scenario not in SCENARIOS:
        raise AppError(f"Unknown synthetic scenario: {scenario}")
    source = os.environ if inherited is None else inherited
    env = {key: source[key] for key in ("PATH", "LANG", "LC_ALL") if key in source}
    env.update({
        "HOME": str(home),
        "COPILOT_HOME": str(home / ".copilot"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "COPILOT_GITHUB_TOKEN": token,
        "COPILOT_AUTO_UPDATE": "false",
        "COPILOT_OTEL_ENABLED": "true",
        "COPILOT_OTEL_EXPORTER_TYPE": "otlp-http",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL": "http/protobuf",
        "OTEL_SERVICE_NAME": "github-copilot",
        "OTEL_RESOURCE_ATTRIBUTES": f"copilot.run.id={run_id},copilot.audit.scenario={scenario}",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": str(scenario != "metadata-only").lower(),
        "NO_COLOR": "1",
    })
    return env


def command_for(scenario: str, marker: str, work: Path) -> list[str]:
    if scenario not in SCENARIOS:
        raise AppError(f"Unknown synthetic scenario: {scenario}")
    command = [
        "copilot", "--no-auto-update", "--no-custom-instructions", "--disable-builtin-mcps",
        "--no-ask-user", "--disallow-temp-dir", "--deny-tool=shell", "--deny-tool=write",
        "--deny-tool=url", "--secret-env-vars=COPILOT_GITHUB_TOKEN",
    ]
    if scenario == "metadata-only":
        command += ["--available-tools"]
        prompt = f"Reply exactly {marker}. Do not use any tools."
    elif scenario == "full-content":
        command += ["--available-tools=view"]
        prompt = (
            f"Use the view tool once to read {work / f'{marker}.txt'}. "
            f"The file contains only SYNTHETIC_AUDIT_FIXTURE {marker}. "
            f"Then reply exactly {marker}. Read no other paths. Do not use other tools."
        )
    else:
        command += ["--available-tools", "task", "view"]
        prompt = (
            f"Use the task tool exactly once for a bounded explore subagent. Ask it to read only "
            f"{work / f'{marker}.txt'} with view and return its SYNTHETIC_AUDIT_FIXTURE {marker} "
            f"text. It must not delegate, use shell, browse, write, or read other files. "
            f"After it returns, reply exactly {marker}."
        )
    return [*command, "-p", prompt]


def evidence_file(state: dict) -> Path:
    value = state.get("evidence_path")
    if not isinstance(value, str):
        raise AppError("Collector state lacks evidence_path; start the owned collector first")
    path = Path(value)
    if not path.is_absolute() or not path.resolve().is_relative_to(LOCAL.resolve()):
        raise AppError("Collector evidence must be inside this repository's private .local directory")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise AppError("Collector evidence must not use symlinks")
    if not path.is_file():
        raise AppError("Collector evidence file is not available")
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise AppError("Collector evidence exceeds 64 MiB; archive the owned run before continuing")
    return path


def execute(scenario: str, timeout: int = 180) -> dict:
    if timeout <= 0:
        raise AppError("Scenario timeout must be positive")
    state = load_json(LOCAL / "collector.json")
    mode = state.get("mode")
    if mode not in ("azure", "local-only"):
        raise AppError("Collector state requires an explicit azure or local-only mode")
    # The status command checks ownership, liveness and readiness before inference.
    run([sys.executable, "-m", "scripts.collector", "status"], timeout=30)
    evidence = evidence_file(state)
    token = authentication_token()
    run_id = str(uuid4())
    marker = f"SYNTHETIC_AUDIT_{run_id}"
    output = private_dir(LOCAL / "runs" / run_id)
    manifest = {
        "run_id": run_id, "scenario": scenario, "capture_content": scenario != "metadata-only",
        "marker": marker, "started_at": now(), "finished_at": None, "exit_code": None,
        "cli_version": run(["copilot", "--version"], timeout=30).stdout.strip().splitlines()[0],
        "evidence_path": str(evidence), "collector_mode": mode,
        "status": "running",
    }
    write_json(output / "manifest.json", manifest)
    try:
        with tempfile.TemporaryDirectory(prefix=f"copilot-otel-{run_id}-") as temporary:
            base = Path(temporary)
            work = private_dir(base / "work")
            home = private_dir(base / "home")
            private_dir(home / ".copilot")
            write_private(work / f"{marker}.txt", f"SYNTHETIC_AUDIT_FIXTURE {marker}\n")
            env = child_environment(home, run_id, scenario, token)
            command = command_for(scenario, marker, work)
            executable = shutil.which("copilot")
            if not executable:
                raise AppError("Required executable not found: copilot")
            command[0] = executable
            result = run_session(command, env=env, timeout=timeout, cwd=work)
            write_private(output / "cli-stdout.txt", redact(result.stdout, env))
            write_private(output / "cli-stderr.txt", redact(result.stderr, env))
            manifest.update(exit_code=result.returncode, status="awaiting_verification")
    except AppError as error:
        manifest.update(status="failed", error=redact(str(error), {"COPILOT_GITHUB_TOKEN": token}))
        raise
    finally:
        manifest["finished_at"] = now()
        write_json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    try:
        manifest = execute(args.scenario, args.timeout_seconds)
    except AppError as error:
        print(f"Smoke run failed: {redact(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps({key: manifest[key] for key in ("run_id", "scenario", "status", "collector_mode")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
