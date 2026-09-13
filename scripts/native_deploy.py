"""Deploy a dedicated collector-free Application Insights native OTLP stack."""

import argparse
import json
import sys
from uuid import uuid4

from .common import (AppError, LOCAL, ROOT, load_json, redact, run_json,
                     validate_run_id, write_json)
from .native_smoke import validate_state

GROUP = "rg-copilot-native-otel"
DEPLOYMENT = "copilot-native-otel"


def receipt(subscription: str, location: str) -> dict:
    validate_run_id(subscription)
    if not location.isalnum() or location != location.lower():
        raise AppError("Specify an Azure region identifier such as eastus")
    path = LOCAL / "native-deployment.json"
    if path.exists() or path.is_symlink():
        value = load_json(path)
        if value.get("subscription_id") != subscription or value.get("location") != location:
            raise AppError("Native receipt belongs to a different subscription or region")
        if not isinstance(value.get("ownership_marker"), str):
            raise AppError("Native receipt is missing its ownership marker")
        validate_run_id(value.get("ownership_marker"))
        return value
    value = {"subscription_id": subscription, "location": location, "ownership_marker": str(uuid4())}
    write_json(path, value)
    return value


def check_group(state: dict) -> None:
    subscription = state["subscription_id"]
    exists = run_json(["az", "group", "exists", "--name", GROUP, "--subscription", subscription])
    if exists is False:
        return
    if exists is not True:
        raise AppError("Azure returned an invalid resource-group existence response")
    group = run_json(["az", "group", "show", "--name", GROUP, "--subscription", subscription])
    tags = group.get("tags") or {}
    if (tags.get("solution") != "copilot-native-otel"
            or tags.get("ownership-marker") != state["ownership_marker"]
            or group.get("location") != state["location"]):
        raise AppError("Refusing native deployment into an unowned or differently located group")
    resources = run_json(["az", "resource", "list", "--resource-group", GROUP,
                          "--subscription", subscription])
    if not isinstance(resources, list):
        raise AppError("Azure returned an invalid resource inventory")
    components = {
        resource["id"].lower(): resource["name"] for resource in resources
        if resource.get("type", "").lower() == "microsoft.insights/components"
        and (resource.get("tags") or {}).get("solution") == "copilot-native-otel"
        and (resource.get("tags") or {}).get("ownership-marker") == state["ownership_marker"]
    }
    for resource in resources:
        if resource.get("type", "").lower() == "microsoft.resources/deployments":
            continue
        owned = resource.get("tags") or {}
        if (owned.get("solution") != "copilot-native-otel"
                or owned.get("ownership-marker") != state["ownership_marker"]):
            if (resource.get("type", "").lower() == "microsoft.alertsmanagement/smartdetectoralertrules"
                    and resource.get("name") in {f"Failure Anomalies - {name}" for name in components.values()}):
                alert = run_json(["az", "resource", "show", "--ids", resource["id"],
                                  "--subscription", subscription])
                properties = alert.get("properties") or {}
                scope = properties.get("scope")
                if (isinstance(scope, list) and len(scope) == 1 and isinstance(scope[0], str)
                        and scope[0].lower() in components
                        and (properties.get("detector") or {}).get("id") == "FailureAnomaliesDetector"):
                    continue  # Azure creates this linked rule asynchronously; never modify or delete it.
            raise AppError("Native resource group contains resources without this deployment's ownership tags")


def parameters(state: dict, principal: str, principal_type: str) -> dict:
    if not isinstance(principal, str):
        raise AppError("Native deployment requires the publisher principal's object UUID")
    validate_run_id(principal)
    if principal_type not in ("User", "ServicePrincipal", "Group"):
        raise AppError("Unsupported native publisher principal type")
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "location": {"value": state["location"]},
            "ownershipMarker": {"value": state["ownership_marker"]},
            "principalId": {"value": principal},
            "principalType": {"value": principal_type},
        },
    }


def deploy(subscription: str, location: str, apply: bool,
           principal: str | None = None, principal_type: str = "User") -> dict:
    state = receipt(subscription, location)
    account = run_json(["az", "account", "show", "--subscription", subscription])
    if account.get("id") != subscription or account.get("state") != "Enabled":
        raise AppError("The explicitly selected Azure subscription is not enabled")
    if account.get("environmentName") != "AzureCloud":
        raise AppError("This native endpoint configuration requires Azure public cloud")
    for namespace in ("Microsoft.Insights", "Microsoft.OperationalInsights", "Microsoft.Monitor"):
        provider = run_json(["az", "provider", "show", "--namespace", namespace,
                             "--subscription", subscription])
        if provider.get("registrationState") != "Registered":
            raise AppError(f"Provider {namespace} must be registered by an authorized administrator")
    check_group(state)
    if principal is None:
        if principal_type != "User":
            raise AppError("--principal-id is required for a non-user principal")
        principal = run_json(["az", "ad", "signed-in-user", "show"]).get("id")
    parameter_path = LOCAL / "native-deployment.parameters.json"
    write_json(parameter_path, parameters(state, principal, principal_type))
    base = [
        "--subscription", subscription, "--location", location, "--name", DEPLOYMENT,
        "--template-file", str(ROOT / "infra" / "native-main.bicep"),
        "--parameters", "@" + str(parameter_path), "--output", "json",
    ]
    validation = run_json(["az", "deployment", "sub", "validate", *base], timeout=300)
    write_json(LOCAL / "native-validation.json", validation)
    if (not isinstance(validation, dict) or validation.get("error")
            or validation.get("properties", {}).get("provisioningState") != "Succeeded"):
        raise AppError("Native ARM validation did not succeed; inspect private native-validation.json")
    preview = run_json(["az", "deployment", "sub", "what-if", "--no-pretty-print", *base], timeout=300)
    write_json(LOCAL / "native-what-if.json", preview)
    if (not isinstance(preview, dict) or preview.get("error") or preview.get("status") != "Succeeded"
            or not isinstance(preview.get("changes"), list)
            or any(not isinstance(change, dict) or not isinstance(change.get("changeType"), str)
                   or not isinstance(change.get("resourceId"), str) for change in preview["changes"])):
        raise AppError("Native what-if returned failed or malformed results; inspect private native-what-if.json")
    changes = [{"type": change.get("changeType"), "resource_id": change.get("resourceId")}
               for change in preview["changes"]]
    if any(change["type"] == "Delete" for change in changes):
        raise AppError("Native preview unexpectedly includes a deletion")
    if not apply:
        return {"status": "previewed", "changes": changes}
    result = run_json(["az", "deployment", "sub", "create", *base], timeout=900)
    write_json(LOCAL / "native-deployment-result.json", result)
    if result.get("properties", {}).get("provisioningState") != "Succeeded":
        raise AppError("Native deployment did not report Succeeded")
    outputs = result.get("properties", {}).get("outputs", {})
    value = outputs.get("nativeState", {}).get("value")
    if not isinstance(value, dict):
        raise AppError("Native deployment did not return its resource receipt")
    validate_state(value)
    if value["subscription_id"] != subscription or value.get("resource_group") != GROUP:
        raise AppError("Native deployment returned unexpected resource scope")
    value.update(ownership_marker=state["ownership_marker"], deployment_name=DEPLOYMENT)
    write_json(LOCAL / "native-azure.json", value)
    return {"status": "deployed", "resource_group": GROUP,
            "traces_endpoint": value["traces_endpoint"], "metrics_endpoint": value["metrics_endpoint"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--location", default="eastus")
    parser.add_argument("--principal-id")
    parser.add_argument("--principal-type", choices=("User", "ServicePrincipal", "Group"), default="User")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--what-if", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = deploy(args.subscription, args.location, args.apply, args.principal_id, args.principal_type)
    except AppError as error:
        print(f"Native deployment failed: {redact(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
