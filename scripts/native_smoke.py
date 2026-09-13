"""Run isolated synthetic CLI sessions directly against Azure native OTLP endpoints."""

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

from .common import (AppError, LOCAL, cli_version, load_json, private_dir, redact, run, run_json,
                     run_session, validate_run_id, write_json, write_private)
from .synthetic_session import SCENARIOS, authentication_token, child_environment, command_for, now

PRIVACY_POLICIES = ("metadata-only-v1", "strict-absence")


def validate_state(state: dict) -> dict:
    required = (
        "subscription_id", "resource_group", "location", "ownership_marker",
        "dcr_resource_id", "dce_resource_id", "dcr_immutable_id", "workspace_customer_id",
        "workspace_resource_id", "azure_monitor_workspace_resource_id",
        "metrics_query_endpoint", "traces_endpoint", "metrics_endpoint", "logs_endpoint",
    )
    if not isinstance(state, dict) or any(
            not isinstance(state.get(key), str) or not state[key] for key in required):
        raise AppError("Native Azure state is missing required string fields")
    if "application_insights_resource_id" in state:
        raise AppError("Application Insights state is not supported by DCE-only v1")
    for key in ("subscription_id", "workspace_customer_id"):
        validate_run_id(state[key])
    immutable = state["dcr_immutable_id"]
    if not re.fullmatch(r"dcr-[a-f0-9]{32}", immutable):
        raise AppError("Native state has an invalid DCR immutable ID")
    if not re.fullmatch(r"[A-Za-z0-9_.()-]+", state["resource_group"]):
        raise AppError("Native state has an invalid resource group")
    prefix = (rf"/subscriptions/{re.escape(state['subscription_id'])}/resourceGroups/"
              rf"{re.escape(state['resource_group'])}/providers/")
    for key, kind in (
            ("dcr_resource_id", r"Microsoft\.Insights/dataCollectionRules"),
            ("dce_resource_id", r"Microsoft\.Insights/dataCollectionEndpoints"),
            ("workspace_resource_id", r"Microsoft\.OperationalInsights/workspaces"),
            ("azure_monitor_workspace_resource_id", r"Microsoft\.Monitor/accounts")):
        if not re.fullmatch(prefix + kind + r"/[^/]+", state[key], re.I):
            raise AppError(f"Native state has an invalid or cross-subscription {key}")
    for signal in ("traces", "metrics", "logs"):
        try:
            url = urlsplit(state[f"{signal}_endpoint"])
            valid_host = (
                url.scheme == "https" and url.hostname
                and url.hostname.endswith(".ingest.monitor.azure.com")
                and url.port in (None, 443) and not url.username and not url.password
                and not url.query and not url.fragment
            )
        except ValueError as error:
            raise AppError(f"Invalid native {signal} endpoint URL") from error
        stream = {"traces": "Microsoft-OTLP-Traces", "logs": "Microsoft-OTLP-Logs",
                  "metrics": r"Custom-Metrics-[A-Za-z][A-Za-z0-9_-]*"}[signal]
        path = rf"/datacollectionRules/{immutable}/streams/{stream}/otlp/v1/{signal}"
        if not valid_host or not re.fullmatch(path, url.path):
            raise AppError(f"Native {signal} endpoint must be an HTTPS Azure Monitor DCR OTLP URL")
    return state


def monitor_token(subscription: str, session_timeout: int) -> str:
    validate_run_id(subscription)
    result = run_json([
        "az", "account", "get-access-token", "--subscription", subscription,
        "--resource", "https://monitor.azure.com/", "--output", "json",
    ], timeout=30)
    if not isinstance(result, dict) or not isinstance(result.get("accessToken"), str) or not result["accessToken"]:
        raise AppError("Azure CLI did not return a Monitor access token; authenticate with az login")
    try:
        expires = int(result["expires_on"])
    except (KeyError, TypeError, ValueError) as error:
        raise AppError("Azure token response lacks a valid expires_on timestamp") from error
    if expires - time.time() < session_timeout + 120:
        raise AppError("Azure token expires too soon for this session; refresh Azure login before retrying")
    return result["accessToken"]


def native_environment(home: Path, run_id: str, scenario: str, github_token: str,
                       azure_token: str, state: dict) -> dict[str, str]:
    validate_state(state)
    if not isinstance(azure_token, str) or not azure_token.strip() or any(
            char.isspace() for char in azure_token):
        raise AppError("A nonempty Azure Monitor authentication token is required")
    env = child_environment(home, run_id, scenario, github_token)
    env.update({
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": state["traces_endpoint"],
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": state["metrics_endpoint"],
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=" + quote("Bearer " + azure_token, safe=""),
    })
    return env


def execute(scenario: str, timeout: int = 180, *, privacy_policy: str = "metadata-only-v1") -> dict:
    if scenario not in SCENARIOS or type(timeout) is not int or not 0 < timeout <= 3600:
        raise AppError("Choose a supported synthetic scenario and an integer timeout of 1-3600 seconds")
    if privacy_policy not in PRIVACY_POLICIES:
        raise AppError("Choose an explicit supported metadata privacy policy")
    state = validate_state(load_json(LOCAL / "native-azure.json"))
    azure_token = monitor_token(state["subscription_id"], timeout)
    github_token = authentication_token()
    secrets = {"AZURE_TOKEN": azure_token, "COPILOT_GITHUB_TOKEN": github_token}
    run_id = str(uuid4())
    marker = f"SYNTHETIC_AUDIT_{run_id}"
    output = private_dir(LOCAL / "runs" / run_id)
    manifest = {
        "run_id": run_id, "scenario": scenario, "capture_content": scenario != "metadata-only",
        "marker": marker, "transport": "native-azure", "privacy_policy": privacy_policy,
        "started_at": now(), "finished_at": None,
        "exit_code": None, "status": "running", "azure_ingestion_proven": False,
        "cli_version": None,
        "dcr_resource_id": state["dcr_resource_id"],
        "dce_resource_id": state["dce_resource_id"],
        "traces_endpoint": state["traces_endpoint"], "metrics_endpoint": state["metrics_endpoint"],
        "native_cli_content_privacy_verified": False,
        "native_metrics_compatibility": "unverified",
        "metric_export_limitations": (
            "CLI 1.0.84-5 was observed emitting cumulative explicit histograms despite SDK "
            "delta/exponential preferences. No environment setting here claims to change that."),
    }
    write_json(output / "manifest.json", manifest)
    try:
        with tempfile.TemporaryDirectory(prefix=f"copilot-native-{run_id}-") as temporary:
            base = Path(temporary)
            home = private_dir(base / "home")
            work = private_dir(base / "work")
            write_private(work / f"{marker}.txt", f"SYNTHETIC_AUDIT_FIXTURE {marker}\n")
            env = native_environment(home, run_id, scenario, github_token, azure_token, state)
            version_env = dict(env, COPILOT_OTEL_ENABLED="false", OTEL_SDK_DISABLED="true")
            version = run(["copilot", "--version"], env=version_env, cwd=work, timeout=30).stdout.strip()
            if not version:
                raise AppError("CLI version output was empty")
            manifest["cli_version"] = cli_version(version.splitlines()[0])
            write_json(output / "manifest.json", manifest)
            command = command_for(scenario, marker, work)
            command[command.index("--secret-env-vars=COPILOT_GITHUB_TOKEN")] = (
                "--secret-env-vars=COPILOT_GITHUB_TOKEN,OTEL_EXPORTER_OTLP_HEADERS"
            )
            try:
                result = run_session(command, env=env, timeout=timeout, cwd=work)
                write_private(output / "cli-stdout.txt", redact(result.stdout, secrets))
                write_private(output / "cli-stderr.txt", redact(result.stderr, secrets))
                manifest.update(exit_code=result.returncode, status="awaiting_verification")
            finally:
                logs = home / ".copilot" / "logs"
                for path in logs.glob("*.log"):
                    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
                        raise AppError("CLI diagnostic log is unsafe or exceeds 8 MiB")
                    write_private(output / "cli-logs" / path.name,
                                  redact(path.read_text(encoding="utf-8"), secrets))
    except AppError as error:
        manifest.update(status="failed", error=redact(str(error), secrets))
        raise AppError(manifest["error"]) from error
    finally:
        manifest["finished_at"] = now()
        write_json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--privacy-policy", choices=PRIVACY_POLICIES, default="metadata-only-v1",
                        help="v1 allows exact tool name/type metadata; strict-absence forbids tool definitions")
    args = parser.parse_args()
    try:
        manifest = execute(args.scenario, args.timeout_seconds, privacy_policy=args.privacy_policy)
    except AppError as error:
        print(f"Native smoke run failed: {redact(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps({key: manifest[key] for key in (
        "run_id", "scenario", "status", "transport", "privacy_policy", "azure_ingestion_proven",
    )}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
