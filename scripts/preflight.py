"""Read-only readiness checks; never signs in, installs or deploys."""

import argparse
import json
import shutil
import sys

from .common import AppError, LOCAL, redact, run, run_json, write_private


def tool_check(name: str, args: list[str]) -> dict:
    if not shutil.which(name):
        return {"name": name, "status": "blocked", "detail": f"Install required tool: {name}"}
    try:
        result = run([name, *args], timeout=30)
    except AppError as error:
        return {"name": name, "status": "blocked", "detail": str(error)}
    return {"name": name, "status": "passed", "detail": redact(result.stdout.strip())}


def azure_check() -> dict:
    try:
        cloud = run_json(["az", "cloud", "show", "--output", "json"])
        if cloud.get("name") != "AzureCloud":
            raise AppError("This configuration requires AzureCloud (public Azure)")
        account = run_json(["az", "account", "show", "--output", "json"])
        if account.get("state") != "Enabled" or not account.get("id"):
            raise AppError("Selected Azure subscription is not enabled")
    except AppError as error:
        return {"name": "azure-account", "status": "blocked", "detail": str(error)}
    return {"name": "azure-account", "status": "passed",
            "detail": "Authenticated; deployment still requires explicit --subscription"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    checks = [
        tool_check("az", ["version", "--output", "json"]),
        tool_check("az", ["bicep", "version"]),
        tool_check("copilot", ["--version"]),
        azure_check(),
    ]
    try:
        for topic in ("monitoring", "environment", "permissions"):
            text = run(["copilot", "help", topic], timeout=30).stdout
            write_private(LOCAL / "versions" / f"copilot-{topic}.txt", text)
    except AppError as error:
        checks.append({"name": "cli-help", "status": "blocked", "detail": str(error)})
    blocked = any(check["status"] != "passed" for check in checks)
    print(json.dumps({"status": "blocked" if blocked else "passed", "checks": checks}, indent=2))
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
