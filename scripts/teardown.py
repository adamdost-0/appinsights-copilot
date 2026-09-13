"""Audit recorded teardown scope; refuse automated deletion of an existing group."""

import argparse
import sys

from scripts import common
from scripts.deploy import (
    GROUP, check_account, check_local_paths, group_exists, load_state,
    require, uuid_value, verify_inventory,
)


def teardown(subscription, resource_group):
    uuid_value(subscription, "Subscription")
    require(resource_group == GROUP, "Only the exact recorded dedicated resource group can be deleted.")
    check_local_paths()
    data = load_state(subscription)
    require(data["resource_group"] == resource_group, "Resource group differs from the ownership receipt.")
    check_account(subscription)
    if not group_exists(subscription):
        print("Recorded group is already absent. Local files were preserved.")
        return
    verify_inventory(data)
    raise common.AppError(
        "Automated deletion is disabled: generic ARM inventory cannot "
        "prove absence of all provider/proxy resources, including other users' private AI artifacts. "
        "An administrator must review the remaining scope and perform teardown separately. "
        "No cloud resources or local files were deleted.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--confirm", required=True, action="store_true",
                        help="Confirm the recorded-scope audit; this does not override automated deletion refusal.")
    args = parser.parse_args(argv)
    try:
        teardown(args.subscription, args.resource_group)
    except common.AppError as error:
        print(f"Teardown refused/failed: {common.redact(str(error))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
