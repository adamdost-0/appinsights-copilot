"""Deploy the receipt-owned v1 DCE/DCR stack for direct authenticated Copilot OTLP."""

import argparse
import hashlib
import json
import re
import sys
from uuid import uuid4

from .common import (AppError, LOCAL, ROOT, load_json, redact, run_json,
                     validate_run_id, write_json)
from .native_smoke import validate_state

GROUP = "rg-copilot-otel-v1"
DEPLOYMENT = "copilot-otel-v1"
RESOURCE_TYPES = {
    "workspace_resource_id": ("Microsoft.OperationalInsights/workspaces", "law"),
    "azure_monitor_workspace_resource_id": ("Microsoft.Monitor/accounts", "amw"),
    "dce_resource_id": ("Microsoft.Insights/dataCollectionEndpoints", "dce"),
    "dcr_resource_id": ("Microsoft.Insights/dataCollectionRules", "dcr"),
}
WORKSPACE_CHILDREN = {
    "tables": "2023-09-01",
    "savedSearches": "2020-08-01",
    "dataExports": "2020-08-01",
    "linkedServices": "2020-08-01",
    "linkedStorageAccounts": "2020-08-01",
}
WORKSPACE_BASELINE_BINDINGS = (
    "subscription_id", "resource_group", "workspace_resource_id", "workspace_customer_id", "ownership_marker",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AppError(message)


def object_value(value) -> dict:
    require(isinstance(value, dict), "Azure returned a malformed object")
    return value


def group_id(subscription: str) -> str:
    return f"/subscriptions/{subscription}/resourceGroups/{GROUP}"


def private_state(path) -> dict:
    require(not any(item.is_symlink() for item in (path, *path.parents)),
            "Refusing symlink in private receipt path")
    try:
        value = load_json(path)
    except UnicodeDecodeError as error:
        raise AppError("Private receipt is not valid UTF-8") from error
    require(path.stat().st_mode & 0o077 == 0, "Private receipt permissions must be 0600")
    return value


def load_receipt(subscription: str, location: str | None = None) -> dict:
    validate_run_id(subscription)
    value = private_state(LOCAL / "native-deployment.json")
    require(set(value) == {"subscription_id", "resource_group", "location", "ownership_marker"},
            "Native ownership receipt has unexpected or missing fields")
    require(value["subscription_id"] == subscription and value["resource_group"] == GROUP,
            "Native receipt belongs to a different subscription or resource group")
    require(isinstance(value["location"], str) and re.fullmatch(r"[a-z0-9]+", value["location"])
            and (location is None or value["location"] == location),
            "Native receipt belongs to a different or invalid region")
    require(isinstance(value["ownership_marker"], str), "Native receipt is missing its ownership marker")
    validate_run_id(value["ownership_marker"])
    return value


def load_state(receipt_value: dict) -> dict:
    value = validate_state(private_state(LOCAL / "native-azure.json"))
    require(all(value.get(key) == expected for key, expected in receipt_value.items()),
            "Native Azure state differs from the exact ownership receipt")
    return value


def validated_deployment_state(result: dict, receipt_value: dict) -> dict:
    result = object_value(result)
    properties = object_value(result.get("properties"))
    require(not result.get("error") and properties.get("provisioningState") == "Succeeded",
            "Native deployment did not report Succeeded")
    if "id" in result:
        expected_id = (f"/subscriptions/{receipt_value['subscription_id']}"
                       f"/providers/Microsoft.Resources/deployments/{DEPLOYMENT}")
        require(isinstance(result["id"], str) and result["id"].lower() == expected_id.lower(),
                "Saved ARM deployment result has an unexpected subscription or deployment scope")
    outputs = object_value(properties.get("outputs"))
    value = object_value(outputs.get("nativeState")).get("value")
    require(isinstance(value, dict), "Native deployment did not return its resource receipt")
    validate_state(value)
    require(all(value.get(key) == expected for key, expected in receipt_value.items()),
            "Native deployment returned a mismatched subscription, group, ownership marker, or region")
    return value


def load_recovery_state(subscription: str) -> dict:
    """Load an ownership-validated candidate, not an operational receipt or permission to adopt resources."""
    receipt_value = load_receipt(subscription)
    path = LOCAL / "native-azure.json"
    require(not path.exists() and not path.is_symlink(),
            "Operational Azure state already exists; recovery must not replace it")
    return validated_deployment_state(private_state(LOCAL / "native-deployment-result.json"), receipt_value)


def receipt(subscription: str, location: str) -> dict:
    validate_run_id(subscription)
    if not isinstance(location, str) or not re.fullmatch(r"[a-z0-9]+", location):
        raise AppError("Specify an Azure region identifier such as eastus")
    path = LOCAL / "native-deployment.json"
    if path.exists() or path.is_symlink():
        return load_receipt(subscription, location)
    value = {"subscription_id": subscription, "resource_group": GROUP,
             "location": location, "ownership_marker": str(uuid4())}
    write_json(path, value)
    return value


def check_account(subscription: str) -> None:
    validate_run_id(subscription)
    account = object_value(run_json(["az", "account", "show", "--subscription", subscription]))
    require(account.get("id") == subscription and account.get("state") == "Enabled",
            "The explicitly selected Azure subscription is not enabled")
    require(account.get("environmentName") == "AzureCloud",
            "This native endpoint configuration requires Azure public cloud")


def group_exists(subscription: str, name: str = GROUP) -> bool:
    value = run_json(["az", "group", "exists", "--name", name, "--subscription", subscription])
    require(type(value) is bool, "Azure returned an invalid resource-group existence response")
    return value


def check_group(state: dict, *, require_complete: bool = False) -> bool:
    subscription = state["subscription_id"]
    if not group_exists(subscription):
        return False
    group = object_value(run_json(["az", "group", "show", "--name", GROUP, "--subscription", subscription]))
    tags = group.get("tags") or {}
    if (not isinstance(tags, dict) or tags.get("solution") != DEPLOYMENT
            or tags.get("ownership-marker") != state["ownership_marker"]
            or group.get("location") != state["location"]
            or str(group.get("id")).lower() != group_id(subscription).lower()):
        raise AppError("Refusing native deployment into an unowned or differently located group")
    resources = run_json(["az", "resource", "list", "--resource-group", GROUP,
                          "--subscription", subscription])
    if not isinstance(resources, list):
        raise AppError("Azure returned an invalid resource inventory")
    types = {kind.lower(): (key, prefix) for key, (kind, prefix) in RESOURCE_TYPES.items()}
    seen = set()
    suffixes = set()
    ids = set()
    for resource in resources:
        resource = object_value(resource)
        kind = resource.get("type")
        resource_id = resource.get("id")
        require(isinstance(kind, str) and isinstance(resource_id, str),
                "Inventory entry is missing its resource ID or type")
        kind, resource_id = kind.lower(), resource_id.lower()
        require(resource_id not in ids, "Duplicate resource in native inventory")
        ids.add(resource_id)
        prefix = group_id(subscription).lower() + "/providers/" + kind + "/"
        require(resource_id.startswith(prefix) and "/" not in resource_id[len(prefix):],
                "Inventory resource is outside the dedicated v1 scope")
        if kind == "microsoft.resources/deployments":
            require(resource_id in {prefix + DEPLOYMENT + "-resources",
                                    prefix + DEPLOYMENT + "-visualizations"},
                    "Unrelated deployment in the v1 resource group")
            continue
        owned = resource.get("tags") or {}
        require(isinstance(owned, dict) and owned.get("solution") == DEPLOYMENT
                and owned.get("ownership-marker") == state["ownership_marker"],
                "Native resource group contains resources without this deployment's ownership tags")
        if kind == "microsoft.insights/workbooks":
            validate_run_id(resource_id[len(prefix):])
            continue
        require(kind in types, "Foreign resource type in the dedicated v1 inventory")
        key, name_prefix = types[kind]
        name = resource_id[len(prefix):]
        require(re.fullmatch(name_prefix + r"-copilot-otel-v1-[a-z0-9]{13}", name)
                and key not in seen, "Unexpected or duplicate v1 resource name")
        if key in state:
            require(resource_id == state[key].lower(), "Inventory resource differs from the recorded v1 ID")
        seen.add(key)
        suffixes.add(name.rsplit("-", 1)[-1])
    require(len(suffixes) <= 1, "Native inventory contains resources from different deployments")
    require(not require_complete or seen == set(RESOURCE_TYPES),
            "Recorded resources are missing from the v1 inventory")
    return True


def parameters(state: dict, principal: str, principal_type: str,
               operator_principal: str | None = None, operator_type: str | None = None) -> dict:
    if not isinstance(principal, str):
        raise AppError("Native deployment requires the publisher principal's object UUID")
    validate_run_id(principal)
    if principal_type not in ("User", "ServicePrincipal", "Group"):
        raise AppError("Unsupported native publisher principal type")
    require(operator_principal is not None or operator_type is None,
            "--operator-principal-type requires --operator-principal-id")
    if operator_principal is None:
        operator_principal, operator_type = principal, principal_type
    else:
        require(isinstance(operator_principal, str), "Operator principal must be an object UUID")
        validate_run_id(operator_principal)
        operator_type = operator_type or "User"
    require(operator_type in ("User", "ServicePrincipal", "Group"), "Unsupported operator principal type")
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "location": {"value": state["location"]},
            "ownershipMarker": {"value": state["ownership_marker"]},
            "principalId": {"value": principal},
            "principalType": {"value": principal_type},
            "operatorPrincipalId": {"value": operator_principal},
            "operatorPrincipalType": {"value": operator_type},
        },
    }


def verify_workspace_binding(state: dict) -> None:
    workspace = object_value(run_json([
        "az", "rest", "--method", "get",
        "--url", "https://management.azure.com" + state["workspace_resource_id"] + "?api-version=2023-09-01",
        "--subscription", state["subscription_id"], "--output", "json",
    ]))
    resource_id = workspace.get("id")
    require(isinstance(resource_id, str) and resource_id.lower() == state["workspace_resource_id"].lower()
            and workspace.get("location") == state["location"],
            "Workspace ARM readback returned an unexpected resource ID or location")
    tags = object_value(workspace.get("tags"))
    require(tags.get("solution") == DEPLOYMENT and tags.get("ownership-marker") == state["ownership_marker"],
            "Workspace ARM readback does not match the deployment ownership")
    properties = object_value(workspace.get("properties"))
    require(properties.get("customerId") == state["workspace_customer_id"],
            "Workspace customer ID is not bound to the recorded ARM workspace resource")


def read_workspace_children(state: dict) -> dict:
    return {
        child: run_json([
            "az", "rest", "--method", "get",
            "--url", f"https://management.azure.com{state['workspace_resource_id']}/{child}?api-version={api}",
            "--subscription", state["subscription_id"], "--output", "json",
        ])
        for child, api in WORKSPACE_CHILDREN.items()
    }


def workspace_child_fingerprints(state: dict, inventory: dict) -> dict:
    require(isinstance(inventory, dict) and set(inventory) == set(WORKSPACE_CHILDREN),
            "Workspace child inventory must include every expected child type")
    workspace = state["workspace_resource_id"]
    search_prefix = f"LogManagement({workspace.rsplit('/', 1)[-1]})_"
    fingerprints = {}
    for child, response in inventory.items():
        require(isinstance(response, dict) and set(response) <= {"value", "nextLink"}
                and isinstance(response.get("value"), list) and response.get("nextLink") in (None, ""),
                f"Workspace {child} inventory is malformed or incomplete")
        seen = set()
        for entry in response["value"]:
            require(isinstance(entry, dict), f"Workspace {child} inventory contains a malformed entry")
            resource_id, name = entry.get("id"), entry.get("name")
            require(isinstance(resource_id, str) and isinstance(name, str) and name and "/" not in name
                    and resource_id.lower() == f"{workspace}/{child}/{name}".lower()
                    and resource_id.lower() not in seen,
                    f"Workspace {child} inventory contains an invalid, foreign, or duplicate resource ID")
            seen.add(resource_id.lower())
            properties = entry.get("properties")
            if child == "tables":
                schema = properties.get("schema") if isinstance(properties, dict) else None
                require(isinstance(schema, dict) and schema.get("tableType") == "Microsoft",
                        "Workspace tables contains a custom or unrecognized table")
            elif child == "savedSearches":
                require(
                    set(entry) == {"id", "name", "type", "properties"}
                    and entry.get("type") == "Microsoft.OperationalInsights/savedSearches"
                    and re.fullmatch(re.escape(search_prefix) + r"[A-Za-z][A-Za-z0-9_|]*", name)
                    and isinstance(properties, dict)
                    and set(properties) == {"category", "displayName", "query", "version"}
                    and properties["category"] in ("Log Management", "General Exploration")
                    and type(properties["version"]) is int and properties["version"] == 2
                    and all(isinstance(properties[key], str) and properties[key].strip()
                            for key in ("displayName", "query")),
                    "Workspace savedSearches contains an entry outside the known Azure default shape",
                )
                # Shape is not ownership: these hashes are trusted only after fresh creation or explicit review.
                fingerprints[resource_id] = hashlib.sha256(json.dumps(
                    entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
            else:
                raise AppError(f"Workspace {child} contains a foreign child resource")
    return fingerprints


def load_workspace_child_baseline(state: dict) -> dict:
    path = LOCAL / "native-workspace-children.json"
    require(path.exists() or path.is_symlink(),
            "Workspace child baseline is missing; an existing deployment requires explicit reviewed initialization")
    baseline = private_state(path)
    require(set(baseline) == {"schema_version", "saved_searches", *WORKSPACE_BASELINE_BINDINGS}
            and type(baseline.get("schema_version")) is int and baseline["schema_version"] == 1
            and all(baseline.get(key) == state[key] for key in WORKSPACE_BASELINE_BINDINGS),
            "Workspace child baseline does not match this exact workspace/customer ID/ownership receipt")
    fingerprints = baseline.get("saved_searches")
    require(isinstance(fingerprints, dict), "Workspace savedSearches baseline is malformed")
    prefix = state["workspace_resource_id"] + "/savedSearches/"
    require(all(isinstance(resource_id, str) and resource_id.startswith(prefix)
                and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest)
                for resource_id, digest in fingerprints.items()),
            "Workspace savedSearches baseline contains an invalid scope or SHA-256 digest")
    return baseline


def verify_workspace_child_baseline(state: dict, inventory: dict) -> None:
    baseline = load_workspace_child_baseline(state)
    current = workspace_child_fingerprints(state, inventory)
    require(current == baseline["saved_searches"],
            "Workspace savedSearches differs from the captured baseline (new, modified, or missing entries)")


def _write_workspace_child_baseline(state: dict, fingerprints: dict) -> None:
    write_json(LOCAL / "native-workspace-children.json", {
        "schema_version": 1,
        **{key: state[key] for key in WORKSPACE_BASELINE_BINDINGS},
        "saved_searches": fingerprints,
    })


def initialize_workspace_child_baseline(state: dict, reviewed_inventory: dict) -> dict:
    """One-time, explicit migration for an existing stack whose full inventory an operator reviewed."""
    validate_state(state)
    path = LOCAL / "native-workspace-children.json"
    require(not path.exists() and not path.is_symlink(), "Workspace child baseline already exists; never overwrite it")
    state_path = LOCAL / "native-azure.json"
    recovering = not (state_path.exists() or state_path.is_symlink())
    candidate = (load_recovery_state(state["subscription_id"]) if recovering
                 else load_state(load_receipt(state["subscription_id"])))
    require(candidate == state,
            "Reviewed initialization requires the exact operational receipt or saved successful ARM candidate")
    reviewed = workspace_child_fingerprints(state, reviewed_inventory)
    check_account(state["subscription_id"])
    require(check_group(state, require_complete=True), "Reviewed workspace resource group is absent")
    verify_workspace_binding(state)
    current = workspace_child_fingerprints(state, read_workspace_children(state))
    require(current == reviewed, "Workspace savedSearches changed since the explicitly reviewed inventory")
    _write_workspace_child_baseline(state, current)
    if recovering:
        write_json(state_path, state)
    return {"status": "initialized", "workspace_resource_id": state["workspace_resource_id"],
            "saved_search_count": len(current)}


def deploy(subscription: str, location: str, apply: bool,
           principal: str | None = None, principal_type: str = "User",
           operator_principal: str | None = None, operator_type: str | None = None) -> dict:
    state = receipt(subscription, location)
    previous = load_state(state) if (LOCAL / "native-azure.json").exists() else None
    check_account(subscription)
    for namespace in ("Microsoft.Insights", "Microsoft.OperationalInsights", "Microsoft.Monitor"):
        provider = object_value(run_json(["az", "provider", "show", "--namespace", namespace,
                                         "--subscription", subscription]))
        if provider.get("registrationState") != "Registered":
            raise AppError(f"Provider {namespace} must be registered by an authorized administrator")
    exists = check_group(previous or state, require_complete=previous is not None)
    recreated = False
    if previous and not exists:
        audit = private_state(LOCAL / "native-teardown.json")
        require(audit.get("status") == "deleted"
                and all(audit.get(key) == state[key] for key in (
                    "subscription_id", "resource_group", "ownership_marker")),
                "Recreating a recorded group requires a completed matching v1 teardown receipt")
        managed_group = audit.get("managed_resource_group")
        require(isinstance(managed_group, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.()-]*", managed_group)
                and managed_group.lower() != GROUP.lower(),
                "Completed teardown receipt has an unsafe managed resource group")
        require(not group_exists(subscription, managed_group),
                "The previous AMW managed group still exists; refusing recreation")
        previous = None
        recreated = True
    baseline_path = LOCAL / "native-workspace-children.json"
    if exists:
        require(baseline_path.exists() or baseline_path.is_symlink(),
                "Workspace child baseline is missing; an existing deployment requires explicit reviewed initialization")
        if previous:
            load_workspace_child_baseline(previous)
    else:
        require(recreated or not (baseline_path.exists() or baseline_path.is_symlink()),
                "Replacing a workspace child baseline requires a verified teardown and recreation")
    if principal is None:
        if principal_type != "User":
            raise AppError("--principal-id is required for a non-user principal")
        principal = object_value(run_json(["az", "ad", "signed-in-user", "show"])).get("id")
    parameter_path = LOCAL / "native-deployment.parameters.json"
    write_json(parameter_path, parameters(state, principal, principal_type, operator_principal, operator_type))
    base = [
        "--subscription", subscription, "--location", location, "--name", DEPLOYMENT,
        "--template-file", str(ROOT / "infra" / "main.bicep"),
        "--parameters", "@" + str(parameter_path), "--output", "json",
    ]
    validation = run_json(["az", "deployment", "sub", "validate", *base], timeout=300)
    write_json(LOCAL / "native-validation.json", validation)
    if (not isinstance(validation, dict) or validation.get("error")
            or not isinstance(validation.get("properties"), dict)
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
    scope = group_id(subscription).lower()
    for change in changes:
        require(change["type"] in {"Create", "Modify", "NoChange", "Deploy", "Ignore"},
                "Native what-if contains an unsupported change type")
        resource_id = change["resource_id"].lower()
        require(resource_id == scope or resource_id.startswith(scope + "/providers/"),
                "Native what-if contains a change outside the exact v1 resource group")
    if not apply:
        return {"status": "previewed", "changes": changes}
    result = object_value(run_json(["az", "deployment", "sub", "create", *base], timeout=900))
    write_json(LOCAL / "native-deployment-result.json", result)
    value = validated_deployment_state(result, state)
    if previous:
        require(all(value.get(key) == previous.get(key) for key in (
            *RESOURCE_TYPES, "workspace_customer_id", "dcr_immutable_id", "traces_endpoint",
            "metrics_endpoint", "logs_endpoint", "metrics_query_endpoint")),
            "Repeat deployment changed a recorded stable resource ID or endpoint")
    verify_workspace_binding(value)
    inventory = read_workspace_children(value)
    if exists:
        verify_workspace_child_baseline(value, inventory)
    else:
        _write_workspace_child_baseline(value, workspace_child_fingerprints(value, inventory))
    write_json(LOCAL / "native-azure.json", value)
    return {"status": "deployed", "resource_group": GROUP,
            "traces_endpoint": value["traces_endpoint"], "metrics_endpoint": value["metrics_endpoint"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--location", default="eastus")
    parser.add_argument("--principal-id")
    parser.add_argument("--principal-type", choices=("User", "ServicePrincipal", "Group"), default="User")
    parser.add_argument("--operator-principal-id",
                        help="Query operator object UUID; defaults to the publisher identity")
    parser.add_argument("--operator-principal-type", choices=("User", "ServicePrincipal", "Group"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--what-if", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = deploy(args.subscription, args.location, args.apply, args.principal_id, args.principal_type,
                        args.operator_principal_id, args.operator_principal_type)
    except AppError as error:
        print(f"Native deployment failed: {redact(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
