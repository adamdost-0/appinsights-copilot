"""Shared synthetic-only session isolation for native OTLP runs and diagnostics.

This module has no standalone runner or ingestion proof mode. The optional
loopback environment diagnostic reuses exactly the same synthetic isolation.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from .common import AppError, run, validate_run_id

SCENARIOS = ("metadata-only", "full-content", "delegated")


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
    if not isinstance(token, str) or not token.strip() or any(char.isspace() for char in token):
        raise AppError("A supported GitHub authentication token is required")
    source = os.environ if inherited is None else inherited
    env = {key: source[key] for key in ("PATH", "LANG", "LC_ALL") if key in source}
    env.update({
        "HOME": str(home),
        "COPILOT_HOME": str(home / ".copilot"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "COPILOT_GITHUB_TOKEN": token,
        "COPILOT_AUTO_UPDATE": "false",
        "COPILOT_OTEL_ENABLED": "true",
        "COPILOT_OTEL_EXPORTER_TYPE": "otlp-http",
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
