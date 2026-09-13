"""Deploy and read back the Log Analytics workspace session workbook."""

import argparse
import json
import sys
from urllib.parse import quote

from .common import AppError, LOCAL, ROOT, load_json, redact, run_json, validate_run_id, write_json
from .native_deploy import GROUP, check_group
from .native_smoke import validate_state
from .session_workbook import build_workbook, validate_queries

DEPLOYMENT = "copilot-otel-v1-visualizations"
API = "2023-06-01"


def parameters(state: dict, model: dict) -> dict:
    validate_state(state)
    if state.get("resource_group") != GROUP:
        raise AppError("Visualization deployment must use the dedicated native resource group")
    marker = state.get("ownership_marker")
    if not isinstance(marker, str):
        raise AppError("Native state lacks its ownership marker")
    validate_run_id(marker)
    expected_workspace_prefix = (
        f"/subscriptions/{state['subscription_id']}/resourceGroups/{GROUP}/providers/"
        "Microsoft.OperationalInsights/workspaces/"
    )
    if (not isinstance(state.get("workspace_resource_id"), str)
            or not state["workspace_resource_id"].lower().startswith(expected_workspace_prefix.lower())):
        raise AppError("Visualization workspace is outside the dedicated native resource scope")
    if (not isinstance(model, dict) or model.get("version") != "Notebook/1.0"
            or not isinstance(model.get("items"), list) or not model["items"]):
        raise AppError("Session workbook must contain a nonempty Notebook/1.0 definition")
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "location": {"value": state["location"]},
            "workspaceResourceId": {"value": state["workspace_resource_id"]},
            "ownershipMarker": {"value": marker},
            "workbookData": {"value": json.dumps(model, separators=(",", ":"))},
        },
    }


def check_preview(preview: dict) -> None:
    if (not isinstance(preview, dict) or preview.get("status") != "Succeeded"
            or preview.get("error") or not isinstance(preview.get("changes"), list)):
        raise AppError("Visualization what-if did not return a successful changes list")
    for change in preview["changes"]:
        if (not isinstance(change, dict) or not isinstance(change.get("changeType"), str)
                or not isinstance(change.get("resourceId"), str)):
            raise AppError("Visualization what-if returned a malformed change")
        if change["changeType"] == "Delete":
            raise AppError("Refusing visualization deployment containing a deletion")


def verify_readback(state: dict, model: dict, resource: dict, expected_id: str) -> None:
    if not isinstance(resource, dict) or resource.get("id", "").lower() != expected_id.lower():
        raise AppError("Workbook readback returned an unexpected resource identity")
    properties = resource.get("properties") or {}
    tags = resource.get("tags") or {}
    if (resource.get("kind") != "shared" or properties.get("category") != "workbook"
            or properties.get("sourceId", "").lower() != state["workspace_resource_id"].lower()
            or tags.get("solution") != "copilot-otel-v1"
            or tags.get("ownership-marker") != state["ownership_marker"]):
        raise AppError("Workbook readback did not preserve ownership or Log Analytics workspace association")
    try:
        stored = json.loads(properties["serializedData"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise AppError("Workbook readback did not include valid serialized content") from error
    if stored != model:
        raise AppError("Azure's saved workbook definition differs from the validated definition")


def deploy(apply: bool) -> dict:
    state = load_json(LOCAL / "native-azure.json")
    model = build_workbook(state)
    parameter_data = parameters(state, model)
    if check_group(state, require_complete=True) is not True:
        raise AppError("Visualization deployment requires an existing complete native foundation")
    queries = validate_queries(state)
    write_json(LOCAL / "visualization-query-validation.json", queries)
    if queries.get("ok") is not True:
        raise AppError("Workbook panel queries failed live validation; deployment was not attempted")
    parameter_path = LOCAL / "visualization.parameters.json"
    write_json(parameter_path, parameter_data)
    write_json(LOCAL / "native-session-workbook.json", model)
    common = [
        "--subscription", state["subscription_id"], "--resource-group", GROUP,
        "--name", DEPLOYMENT, "--template-file", str(ROOT / "infra/visualizations.bicep"),
        "--parameters", "@" + str(parameter_path), "--output", "json",
    ]
    validation = run_json(["az", "deployment", "group", "validate", *common], timeout=300)
    write_json(LOCAL / "visualization-arm-validation.json", validation)
    if validation.get("error") or validation.get("properties", {}).get("provisioningState") != "Succeeded":
        raise AppError("Visualization ARM validation did not succeed")
    preview = run_json(["az", "deployment", "group", "what-if", "--no-pretty-print", *common], timeout=300)
    write_json(LOCAL / "visualization-what-if.json", preview)
    check_preview(preview)
    if not apply:
        return {"status": "previewed", "panel_queries_verified": True,
                "changes": [{"type": c["changeType"], "resource_id": c["resourceId"]}
                            for c in preview["changes"]]}
    result = run_json(["az", "deployment", "group", "create", *common], timeout=600)
    write_json(LOCAL / "visualization-deployment-result.json", result)
    if result.get("properties", {}).get("provisioningState") != "Succeeded":
        raise AppError("Visualization deployment did not succeed")
    resource_id = result.get("properties", {}).get("outputs", {}).get("workbookResourceId", {}).get("value")
    prefix = f"/subscriptions/{state['subscription_id']}/resourceGroups/{GROUP}/providers/Microsoft.Insights/workbooks/"
    if not isinstance(resource_id, str) or not resource_id.lower().startswith(prefix.lower()):
        raise AppError("Visualization deployment returned an unexpected workbook resource")
    validate_run_id(resource_id.rsplit("/", 1)[-1])
    resource = run_json([
        "az", "rest", "--method", "get",
        "--url", f"https://management.azure.com{resource_id}?api-version={API}&canFetchContent=true",
        "--subscription", state["subscription_id"], "--output", "json",
    ], timeout=60)
    write_json(LOCAL / "visualization-readback.json", resource)
    verify_readback(state, model, resource, resource_id)
    receipt = {
        "status": "deployed", "workbook_resource_id": resource_id,
        "workbook_name": "Copilot CLI - Session Explorer",
        "workspace_resource_id": state["workspace_resource_id"],
        "dcr_resource_id": state["dcr_resource_id"],
        "portal_url": "https://portal.azure.com/#resource" + quote(resource_id, safe="/"),
        "panel_queries_verified": True, "saved_definition_verified": True,
        "portal_render_verified": False,
    }
    write_json(LOCAL / "visualizations.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--what-if", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = deploy(args.apply)
    except AppError as error:
        print(f"Visualization deployment failed: {redact(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
