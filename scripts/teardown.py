"""Delete only the explicitly confirmed, receipt-owned v1 resource group."""

import argparse
import json
import re
import sys

from .common import AppError, LOCAL, redact, run, run_json, write_json
from .native_deploy import (
    GROUP, check_account, check_group, group_exists, load_receipt, load_state,
    object_value, private_state, read_workspace_children, require,
    verify_workspace_binding, verify_workspace_child_baseline,
)

ARM = "https://management.azure.com"


def read_resource(resource_id: str, subscription: str, api: str) -> dict:
    value = object_value(run_json([
        "az", "rest", "--method", "get", "--url", f"{ARM}{resource_id}?api-version={api}",
        "--subscription", subscription, "--output", "json",
    ]))
    require(str(value.get("id")).lower() == resource_id.lower(), "ARM resource readback ID mismatch")
    return value


def verify_workspace_children(state: dict) -> None:
    """Generic ARM inventory omits provider-managed workspace child resources."""
    verify_workspace_binding(state)
    verify_workspace_child_baseline(state, read_workspace_children(state))


def verify_managed_group(state: dict) -> str:
    """Follow both AMW-generated ingestion links and its resource group's managedBy."""
    subscription = state["subscription_id"]
    amw_id = state["azure_monitor_workspace_resource_id"]
    amw = read_resource(amw_id, subscription, "2025-10-03")
    settings = object_value(object_value(amw.get("properties")).get("defaultIngestionSettings"))
    expected = {}
    groups = set()
    for field, kind in (
        ("dataCollectionRuleResourceId", "Microsoft.Insights/dataCollectionRules"),
        ("dataCollectionEndpointResourceId", "Microsoft.Insights/dataCollectionEndpoints"),
    ):
        resource_id = settings.get(field)
        require(isinstance(resource_id, str), "AMW is missing its service-managed ingestion linkage")
        match = re.fullmatch(
            rf"(/subscriptions/{re.escape(subscription)}/resourceGroups/([^/]+))/providers/{re.escape(kind)}/[^/]+",
            resource_id, re.I,
        )
        require(match is not None and match[2].lower() != GROUP.lower(),
                "AMW ingestion linkage escapes its subscription or points to the project group")
        groups.add(match[1].lower())
        expected[resource_id.lower()] = kind.lower()
    require(len(groups) == 1, "AMW service-managed ingestion resources are in different groups")
    managed_id = groups.pop()
    managed_name = managed_id.rsplit("/", 1)[-1]
    group = object_value(run_json([
        "az", "group", "show", "--name", managed_name, "--subscription", subscription, "--output", "json",
    ]))
    require(str(group.get("id")).lower() == managed_id
            and str(group.get("managedBy")).lower() == amw_id.lower(),
            "Service-managed resource group does not link to this exact owned AMW")
    resources = run_json([
        "az", "resource", "list", "--resource-group", managed_name,
        "--subscription", subscription, "--output", "json",
    ])
    require(isinstance(resources, list), "Invalid AMW managed resource inventory")
    seen = set()
    for resource in resources:
        resource = object_value(resource)
        resource_id, kind = resource.get("id"), resource.get("type")
        require(isinstance(resource_id, str) and isinstance(kind, str),
                "Malformed AMW managed resource inventory")
        resource_id = resource_id.lower()
        require(resource_id in expected and kind.lower() == expected[resource_id] and resource_id not in seen,
                "Foreign or duplicate resource found in AMW managed resource group")
        seen.add(resource_id)
    require(seen == set(expected), "AMW managed resource inventory is incomplete")
    return managed_name


def teardown(subscription: str, resource_group: str) -> dict:
    require(resource_group == GROUP, "Only the exact rg-copilot-otel-v1 group can be deleted")
    receipt = load_receipt(subscription)
    state = load_state(receipt)
    check_account(subscription)
    if not check_group(state, require_complete=True):
        audit_path = LOCAL / "native-teardown.json"
        if audit_path.exists() or audit_path.is_symlink():
            audit = private_state(audit_path)
            require(all(audit.get(key) == state[key] for key in (
                "subscription_id", "resource_group", "ownership_marker")),
                "Teardown audit differs from the current v1 ownership receipt")
            managed = audit.get("managed_resource_group")
            require(isinstance(managed, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.()-]*", managed)
                    and managed.lower() != GROUP.lower(),
                    "Teardown audit contains an unsafe managed resource group")
            require(not group_exists(subscription, managed),
                    "Azure service-managed AMW cleanup is still incomplete; do not force-delete its group")
            audit["status"] = "deleted"
            write_json(audit_path, audit)
        return {"status": "already_absent", "resource_group": GROUP,
                "detail": "Recorded v1 group is absent. Local receipts were preserved."}
    verify_workspace_children(state)
    managed_group = verify_managed_group(state)
    audit = {
        "subscription_id": subscription, "resource_group": GROUP,
        "ownership_marker": state["ownership_marker"], "managed_resource_group": managed_group,
        "status": "verified",
    }
    write_json(LOCAL / "native-teardown.json", audit)
    require(check_group(state, require_complete=True),
            "V1 group disappeared during ownership verification; inspect Azure state")
    # Deleting the owning AMW lets Azure clean up its managed group; never delete it directly.
    run(["az", "group", "delete", "--name", GROUP, "--subscription", subscription,
         "--yes", "--output", "none"], timeout=1800)
    require(not group_exists(subscription), "V1 group deletion is incomplete; local receipts were preserved")
    require(not group_exists(subscription, managed_group),
            "Azure service-managed AMW cleanup is incomplete; inspect the recorded managed group, do not force-delete it")
    audit["status"] = "deleted"
    write_json(LOCAL / "native-teardown.json", audit)
    return audit


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True, choices=(GROUP,))
    parser.add_argument("--confirm", required=True, action="store_true",
                        help="Confirm irreversible deletion of the verified v1 group and its telemetry")
    args = parser.parse_args(argv)
    try:
        result = teardown(args.subscription, args.resource_group)
    except AppError as error:
        print(f"Teardown refused/failed: {redact(str(error))}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
