"""Dedicated, receipt-owned Azure monitoring deployment; no implicit login or grants."""

import argparse
import math
import re
import sys
from uuid import UUID, uuid4

from scripts import common

GROUP = "rg-copilot-otel-audit"
DEPLOYMENT = "copilot-otel-audit"
ARM = "https://management.azure.com"
STATE_FIELDS = {
    "subscription_id", "resource_group", "application_insights_resource_id",
    "workspace_resource_id", "workspace_customer_id", "deployment_name",
    "ownership_marker", "location",
}
RESOURCE_TYPES = {
    "workspace_resource_id": "Microsoft.OperationalInsights/workspaces",
    "application_insights_resource_id": "Microsoft.Insights/components",
}


def require(condition, message):
    if not condition:
        raise common.AppError(message)


def object_value(value):
    require(isinstance(value, dict), "Azure returned an invalid object; refusing to proceed.")
    return value


def same_id(value, expected):
    return isinstance(value, str) and value.lower() == expected.lower()


def uuid_value(value, label):
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise common.AppError(f"{label} must be a canonical UUID.") from error
    require(canonical == value, f"{label} must be a canonical lowercase UUID.")
    return value


def location_value(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-z0-9]+", value),
            "Location must be an Azure region name, for example eastus.")
    return value


def azure(args, subscription, *, timeout=120, expect_json=True):
    command = ["az", *args, "--subscription", subscription, "--only-show-errors",
               "--output", "json" if expect_json else "none"]
    return checked_command(command, timeout=timeout, expect_json=expect_json)


def checked_command(command, *, timeout=120, expect_json=True):
    try:
        if expect_json:
            return common.run_json(command, timeout=timeout)
        common.run(command, timeout=timeout)
        return None
    except common.AppError as error:
        # ARM responses can contain connection strings in unexpected formats.
        # Classify errors without ever displaying a raw response or command URL.
        text = str(error).lower()
        if any(word in text for word in ("authorization", "forbidden", "permission", "policy", "denied")):
            reason = "permission or policy denied the operation; ask an administrator to review access/policy"
        elif "not found" in text and "executable" in text:
            reason = "Azure CLI is not installed"
        elif any(word in text for word in ("login", "token", "credential")):
            reason = "authentication failed; arrange an authorized Azure CLI session separately"
        elif "timed out" in text:
            reason = "timed out; inspect Azure state before retrying"
        else:
            reason = "command failed or returned invalid JSON; inspect Azure diagnostics privately"
        raise common.AppError(f"Azure {command[1]}: {reason}.") from None


def check_account(subscription):
    uuid_value(subscription, "Subscription")
    account = object_value(azure(["account", "show"], subscription))
    require(same_id(account.get("id"), subscription) and account.get("state") == "Enabled",
            "The explicit subscription is not the enabled account returned by Azure CLI.")
    cloud = object_value(checked_command(["az", "cloud", "show", "--only-show-errors", "--output", "json"]))
    endpoint = object_value(cloud.get("endpoints")).get("resourceManager")
    require(account.get("environmentName") == "AzureCloud"
            and cloud.get("name") == "AzureCloud" and cloud.get("isActive") is True
            and isinstance(endpoint, str) and endpoint.rstrip("/") == ARM,
            "Only the active public AzureCloud is supported; refusing a cloud/account mismatch.")


def check_providers_and_location(subscription, location):
    response = object_value(azure(
        ["rest", "--method", "get", "--url",
         f"{ARM}/subscriptions/{subscription}/locations?api-version=2022-12-01"], subscription))
    locations = response.get("value")
    require(isinstance(locations, list), "Azure returned invalid subscription locations.")
    matches = [object_value(item) for item in locations if object_value(item).get("name") == location]
    require(len(matches) == 1 and isinstance(matches[0].get("displayName"), str),
            "Location is unavailable in the explicit subscription.")
    accepted = {location, matches[0]["displayName"].replace(" ", "").lower()}
    for resource_type in RESOURCE_TYPES.values():
        namespace, kind = resource_type.split("/")
        provider = object_value(azure(["provider", "show", "--namespace", namespace], subscription))
        require(provider.get("registrationState") == "Registered",
                f"{namespace} is not Registered; request registration separately. No automatic registration.")
        types = provider.get("resourceTypes")
        require(isinstance(types, list), f"{namespace} returned invalid resource types.")
        candidates = [object_value(item) for item in types
                      if same_id(object_value(item).get("resourceType"), kind)]
        require(len(candidates) == 1 and isinstance(candidates[0].get("locations"), list),
                f"{namespace}/{kind} has no explicit region availability.")
        supported = candidates[0]["locations"]
        require(all(isinstance(item, str) for item in supported)
                and any(item.replace(" ", "").lower() in accepted for item in supported),
                f"{namespace}/{kind} is unavailable in the requested location.")


def group_id(subscription):
    return f"/subscriptions/{subscription}/resourceGroups/{GROUP}"


def validate_state(data, subscription, location=None):
    require(isinstance(data, dict) and set(data) == STATE_FIELDS,
            "Azure receipt must contain exactly the documented safe snake_case fields.")
    require(all(isinstance(value, str) and value for value in data.values()),
            "Azure receipt fields must be nonempty strings.")
    require(data["subscription_id"] == subscription and data["resource_group"] == GROUP
            and data["deployment_name"] == DEPLOYMENT,
            "Azure receipt does not match the exact subscription/group/deployment.")
    uuid_value(data["ownership_marker"], "Ownership marker")
    uuid_value(data["workspace_customer_id"], "Workspace customer ID")
    location_value(data["location"])
    require(location is None or data["location"] == location,
            "Location differs from the recorded deployment; relocation is refused.")
    for key, kind in RESOURCE_TYPES.items():
        prefix = group_id(subscription) + "/providers/" + kind + "/"
        resource_id = data[key]
        require(resource_id.lower().startswith(prefix.lower())
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{2,62}", resource_id[len(prefix):]),
                "Recorded resource ID is not a supported resource in the exact dedicated group.")
    return data


def load_state(subscription, location=None):
    try:
        data = common.load_json(common.AZURE_STATE)
    except UnicodeDecodeError as error:
        raise common.AppError("Azure receipt is not valid UTF-8; refusing to proceed.") from error
    return validate_state(data, subscription, location)


def group_exists(subscription):
    exists = azure(["group", "exists", "--name", GROUP], subscription)
    require(type(exists) is bool, "Azure returned an invalid group existence response.")
    return exists


def check_local_paths():
    for path in (common.LOCAL, common.AZURE_STATE, common.LOCAL / "collector.env"):
        require(not any(item.is_symlink() for item in (path, *path.parents)),
                "Symlink in local state paths; refusing deployment/teardown.")
        if path.exists():
            require(path.is_dir() if path == common.LOCAL else path.is_file(),
                    "Local state destination has an unexpected file type.")


def owned(resource, marker):
    tags = object_value(resource).get("tags")
    require(isinstance(tags, dict) and tags.get("solution") == DEPLOYMENT
            and tags.get("ownership-marker") == marker,
            "Ownership tags are missing or mismatched; refusing to adopt or delete resources.")


def verify_inventory(data):
    """Verify recorded parents and generic inventory, not provider-only children."""
    subscription = data["subscription_id"]
    group = object_value(azure(["group", "show", "--name", GROUP], subscription))
    require(same_id(group.get("id"), group_id(subscription))
            and group.get("location") == data["location"],
            "Resource group ID/location does not match the receipt.")
    owned(group, data["ownership_marker"])
    inventory = azure(["resource", "list", "--resource-group", GROUP], subscription)
    require(isinstance(inventory, list), "Azure returned invalid resource inventory.")
    expected = {data[key].lower(): kind.lower() for key, kind in RESOURCE_TYPES.items()}
    seen = set()
    for item in inventory:
        item = object_value(item)
        resource_id = item.get("id")
        kind = item.get("type")
        require(isinstance(resource_id, str) and isinstance(kind, str),
                "Inventory entry lacks an explicit resource ID/type.")
        resource_id = resource_id.lower()
        require(resource_id in expected and kind.lower() == expected[resource_id] and resource_id not in seen,
                "Unrelated, duplicate, or unexpected resource found; dedicated-group operation refused.")
        owned(item, data["ownership_marker"])
        seen.add(resource_id)
    require(seen == set(expected), "Recorded resources are missing; refusing an incomplete ownership inventory.")


def read_resource(data, key, api_version):
    resource = object_value(azure(
        ["rest", "--method", "get", "--url", f"{ARM}{data[key]}?api-version={api_version}"],
        data["subscription_id"]))
    require(same_id(resource.get("id"), data[key])
            and resource.get("location") == data["location"], "ARM readback ID/location mismatch.")
    owned(resource, data["ownership_marker"])
    return resource


def readback(data):
    workspace = read_resource(data, "workspace_resource_id", "2023-09-01")
    props = object_value(workspace.get("properties"))
    require(props.get("customerId") == data["workspace_customer_id"], "Workspace customer ID mismatch.")
    require(object_value(props.get("features")).get("enableLogAccessUsingOnlyResourcePermissions") is False,
            "Workspace resource-permission access is not explicitly disabled.")
    require(object_value(props.get("sku")).get("name") == "PerGB2018", "Workspace SKU is not PerGB2018.")
    retention = props.get("retentionInDays")
    cap = object_value(props.get("workspaceCapping")).get("dailyQuotaGb")
    require(type(retention) is int and retention > 0 and type(cap) in (int, float) and math.isfinite(cap),
            "Workspace readback lacks valid actual retention/daily-cap values.")
    insights = read_resource(data, "application_insights_resource_id", "2020-02-02")
    app = object_value(insights.get("properties"))
    require(isinstance(app.get("WorkspaceResourceId"), str)
            and app["WorkspaceResourceId"].lower() == data["workspace_resource_id"].lower(),
            "Application Insights workspace linkage mismatch.")
    require(insights.get("kind") == "web" and app.get("Application_Type") == "web"
            and app.get("DisableLocalAuth") is False,
            "Application Insights web/local-auth configuration mismatch.")
    for properties in (props, app):
        require(properties.get("publicNetworkAccessForIngestion") == "Enabled"
                and properties.get("publicNetworkAccessForQuery") == "Enabled",
                "Public ingestion/query access is not explicitly enabled.")
    connection = app.get("ConnectionString")
    require(isinstance(connection, str) and connection
            and all(32 <= ord(char) < 127 for char in connection)
            and re.search(r"(?:^|;)InstrumentationKey=[^;]+(?:;|$)", connection),
            "ARM returned an empty or unsafe Application Insights connection string.")
    return connection, retention, cap


def deployment_command(action, subscription, location, marker, previous):
    args = ["deployment", "sub", action, "--name", DEPLOYMENT, "--location", location,
            "--template-file", str(common.ROOT / "infra" / "main.bicep"),
            "--parameters", f"location={location}", f"ownershipMarker={marker}"]
    if previous:
        args += ["workspaceName=" + previous["workspace_resource_id"].rsplit("/", 1)[1],
                 "applicationInsightsName=" + previous["application_insights_resource_id"].rsplit("/", 1)[1]]
    if action == "create":
        args += ["--query", "properties.outputs"]
    if action == "what-if":
        args += ["--no-pretty-print"]
    return azure(args, subscription, timeout=1800)


def deploy(subscription, location, apply):
    uuid_value(subscription, "Subscription")
    location_value(location)
    check_local_paths()
    previous = None
    if common.AZURE_STATE.exists() or common.AZURE_STATE.is_symlink():
        previous = load_state(subscription, location)
    check_account(subscription)
    check_providers_and_location(subscription, location)
    exists = group_exists(subscription)
    if exists:
        require(previous is not None,
                "Existing group has no matching local ownership receipt; existing-group adoption is refused.")
        verify_inventory(previous)
    else:
        require(previous is None, "Recorded group is absent; archive the receipt manually before a fresh deployment.")
    marker = previous["ownership_marker"] if previous else str(uuid4())
    validation = object_value(deployment_command("validate", subscription, location, marker, previous))
    require(not validation.get("error"),
            "Azure validation failed; review permissions and policy privately before retrying.")
    preview = object_value(deployment_command("what-if", subscription, location, marker, previous))
    require(preview.get("status") == "Succeeded" and isinstance(preview.get("changes"), list),
            "What-if did not return an explicit successful change report.")
    counts = {}
    for change in preview["changes"]:
        kind = object_value(change).get("changeType")
        require(isinstance(kind, str) and kind in ("Create", "Modify", "NoChange", "Deploy"),
                "What-if contains deletion, ignored, unsupported, or unknown changes; apply is refused.")
        counts[kind] = counts.get(kind, 0) + 1
    summary = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items()))
    print(f"What-if succeeded: {len(preview['changes'])} change entries ({summary}; raw payload withheld).")
    if not apply:
        return
    # Repeat ownership checks immediately before the first mutating command.
    require(group_exists(subscription) == exists, "Resource group changed during preview; rerun from the start.")
    if previous:
        verify_inventory(previous)
    outputs = object_value(deployment_command("create", subscription, location, marker, previous))
    data = dict(subscription_id=subscription, resource_group=GROUP, deployment_name=DEPLOYMENT,
                ownership_marker=marker, location=location)
    for field, output in [
        ("application_insights_resource_id", "applicationInsightsResourceId"),
        ("workspace_resource_id", "workspaceResourceId"), ("workspace_customer_id", "workspaceCustomerId"),
    ]:
        data[field] = object_value(outputs.get(output)).get("value")
    validate_state(data, subscription, location)
    if previous:
        require(data == previous, "Deployment outputs changed recorded resource identity; refusing local updates.")
    verify_inventory(data)
    # Retain the verified ownership receipt even if subsequent service readback fails.
    common.write_json(common.AZURE_STATE, data)
    connection, retention, cap = readback(data)
    common.write_private(common.LOCAL / "collector.env",
                         f"APPLICATIONINSIGHTS_CONNECTION_STRING={connection}\n")
    print(f"Deployment verified. Actual workspace retention: {retention} days; daily cap: {cap} GB.")
    print("Table-specific retention can differ; workspace retention does not delete all App tables after 30 days.")
    print("Private collector.env and safe azure.json written; no connection string is displayed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--location", default="eastus")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--what-if", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        deploy(args.subscription, args.location, args.apply)
    except common.AppError as error:
        print(f"Deployment refused/failed: {common.redact(str(error))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
