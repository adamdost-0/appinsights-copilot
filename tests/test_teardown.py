"""Only the exact receipt-owned v1 stack may be deleted."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import common, native_deploy, teardown

SUB = "00000000-0000-4000-8000-000000000001"
GROUP = "rg-copilot-otel-v1"
GROUP_ID = f"/subscriptions/{SUB}/resourceGroups/{GROUP}"
MANAGED_GROUP = f"/subscriptions/{SUB}/resourceGroups/MA_monitor_eastus_managed"
MARKER = "00000000-0000-4000-8000-000000000002"
TAGS = {"solution": "copilot-otel-v1", "ownership-marker": MARKER}


def state():
    immutable = "dcr-" + "a" * 32
    return {
        "subscription_id": SUB, "resource_group": GROUP, "location": "eastus",
        "ownership_marker": MARKER, "workspace_customer_id": SUB,
        "workspace_resource_id": GROUP_ID + "/providers/Microsoft.OperationalInsights/workspaces/law-copilot-otel-v1-aaaaaaaaaaaaa",
        "azure_monitor_workspace_resource_id": GROUP_ID + "/providers/Microsoft.Monitor/accounts/amw-copilot-otel-v1-aaaaaaaaaaaaa",
        "dce_resource_id": GROUP_ID + "/providers/Microsoft.Insights/dataCollectionEndpoints/dce-copilot-otel-v1-aaaaaaaaaaaaa",
        "dcr_resource_id": GROUP_ID + "/providers/Microsoft.Insights/dataCollectionRules/dcr-copilot-otel-v1-aaaaaaaaaaaaa",
        "dcr_immutable_id": immutable,
        "metrics_query_endpoint": "https://example.eastus.prometheus.monitor.azure.com",
        "traces_endpoint": f"https://example.eastus-1.ingest.monitor.azure.com/datacollectionRules/{immutable}/streams/Microsoft-OTLP-Traces/otlp/v1/traces",
        "metrics_endpoint": f"https://example.eastus-1.metrics.ingest.monitor.azure.com/datacollectionRules/{immutable}/streams/Custom-Metrics-Otel/otlp/v1/metrics",
        "logs_endpoint": f"https://example.eastus-1.ingest.monitor.azure.com/datacollectionRules/{immutable}/streams/Microsoft-OTLP-Logs/otlp/v1/logs",
    }


def saved_search(data, suffix="LogManagement|AllEvents", query="Event | take 1"):
    name = f"LogManagement({data['workspace_resource_id'].rsplit('/', 1)[-1]})_{suffix}"
    return {
        "id": data["workspace_resource_id"] + "/savedSearches/" + name,
        "name": name, "type": "Microsoft.OperationalInsights/savedSearches",
        "properties": {"category": "Log Management", "displayName": "All Events", "query": query, "version": 2},
    }


def child_inventory(searches=()):
    return {kind: {"value": list(searches) if kind == "savedSearches" else []}
            for kind in ("tables", "savedSearches", "dataExports", "linkedServices", "linkedStorageAccounts")}


def child_baseline(data, searches=()):
    return {
        "schema_version": 1,
        **{key: data[key] for key in (
            "subscription_id", "resource_group", "workspace_resource_id", "workspace_customer_id", "ownership_marker")},
        "saved_searches": {
            entry["id"]: hashlib.sha256(json.dumps(
                entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
            for entry in searches
        },
    }


class TeardownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name)
        self.data = state()
        self.receipt = {key: self.data[key] for key in (
            "subscription_id", "resource_group", "location", "ownership_marker")}
        common.write_json(self.local / "native-deployment.json", self.receipt)
        common.write_json(self.local / "native-azure.json", self.data)
        common.write_json(self.local / "native-workspace-children.json", child_baseline(self.data))
        common.write_private(self.local / "unrelated.txt", "preserve")
        for module in (native_deploy, teardown):
            patcher = patch.object(module, "LOCAL", self.local, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        self.exists = True
        self.extra = []
        self.managed_extra = []
        self.managed_by = self.data["azure_monitor_workspace_resource_id"]
        self.managed_rule = MANAGED_GROUP + "/providers/Microsoft.Insights/dataCollectionRules/default"
        self.managed_endpoint = MANAGED_GROUP + "/providers/Microsoft.Insights/dataCollectionEndpoints/default"
        self.children = {}
        self.live_customer_id = self.data["workspace_customer_id"]
        self.managed_present_after_delete = False
        self.deleted = False
        self.inventory = [
            {"id": self.data[key], "type": kind, "tags": dict(TAGS)}
            for key, kind in (
                ("workspace_resource_id", "Microsoft.OperationalInsights/workspaces"),
                ("azure_monitor_workspace_resource_id", "Microsoft.Monitor/accounts"),
                ("dce_resource_id", "Microsoft.Insights/dataCollectionEndpoints"),
                ("dcr_resource_id", "Microsoft.Insights/dataCollectionRules"),
            )
        ]
        for module in (native_deploy, teardown):
            for name in ("run_json", "run"):
                patcher = patch.object(module, name, side_effect=self.azure, create=True)
                patcher.start()
                self.addCleanup(patcher.stop)

    def azure(self, args, **kwargs):
        self.calls.append(args)
        if args[1:3] == ["account", "show"]:
            return {"id": SUB, "state": "Enabled", "environmentName": "AzureCloud"}
        if args[1:3] == ["group", "exists"]:
            if MANAGED_GROUP.rsplit("/", 1)[-1].lower() in [arg.lower() for arg in args]:
                return self.managed_present_after_delete if self.deleted else True
            return self.exists and not self.deleted
        if args[1:3] == ["group", "show"]:
            if GROUP in args:
                return {"id": GROUP_ID, "location": "eastus", "tags": TAGS}
            return {"id": MANAGED_GROUP, "managedBy": self.managed_by}
        if args[1:3] == ["resource", "list"]:
            if GROUP in args:
                return self.inventory + self.extra
            return [
                {"id": self.managed_rule, "type": "Microsoft.Insights/dataCollectionRules"},
                {"id": self.managed_endpoint, "type": "Microsoft.Insights/dataCollectionEndpoints"},
            ] + self.managed_extra
        if args[1] == "rest":
            url = args[args.index("--url") + 1]
            if url.endswith(self.data["workspace_resource_id"] + "?api-version=2023-09-01"):
                return {"id": self.data["workspace_resource_id"], "location": "eastus", "tags": TAGS,
                        "properties": {"customerId": self.live_customer_id}}
            if url.endswith(self.data["azure_monitor_workspace_resource_id"] + "?api-version=2025-10-03"):
                return {"id": self.data["azure_monitor_workspace_resource_id"], "properties": {
                    "defaultIngestionSettings": {
                        "dataCollectionRuleResourceId": self.managed_rule,
                        "dataCollectionEndpointResourceId": self.managed_endpoint,
                    }}}
            return {"value": self.children.get(url.split("?")[0].rsplit("/", 1)[-1], [])}
        if args[1:3] == ["group", "delete"]:
            self.deleted = True
            return None
        self.fail(f"Unexpected Azure command: {args}")

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return teardown.main(list(args or (
                    "--subscription", SUB, "--resource-group", GROUP, "--confirm")))
            except SystemExit as error:
                return error.code

    def mutations(self):
        return [args for args in self.calls if "delete" in args]

    def test_owned_v1_only_uses_azure_managed_cleanup(self):
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(len(self.mutations()), 1)
        command = self.mutations()[0]
        self.assertEqual(command[command.index("--name") + 1], GROUP)
        self.assertEqual(command[command.index("--subscription") + 1], SUB)
        self.assertNotIn("--force-deletion-types", command)
        self.assertEqual((self.local / "unrelated.txt").read_text(), "preserve")
        self.assertTrue((self.local / "native-azure.json").exists())

    def test_requires_confirmation_and_exact_scope(self):
        for args in (("--subscription", SUB, "--resource-group", GROUP),
                     ("--subscription", SUB, "--resource-group", "rg-copilot-native-otel", "--confirm"),
                     ("--subscription", SUB[:-1] + "3", "--resource-group", GROUP, "--confirm")):
            self.assertNotEqual(self.invoke(*args), 0)
        self.assertEqual(self.mutations(), [])

    def test_missing_receipt_never_creates_ownership(self):
        (self.local / "native-deployment.json").unlink()
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.local / "native-deployment.json").exists())

    def test_mismatched_marker_or_resource_scope_is_refused_before_cloud(self):
        for field, value in (("ownership_marker", SUB), ("resource_group", "foreign"),
                             ("dcr_resource_id", self.data["dcr_resource_id"].replace(GROUP, "foreign"))):
            with self.subTest(field=field):
                common.write_json(self.local / "native-azure.json", {**self.data, field: value})
                self.assertNotEqual(self.invoke(), 0)
                self.assertEqual(self.calls, [])

    def test_absent_group_is_idempotent(self):
        self.exists = False
        self.assertEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_foreign_inventory_even_with_owned_tags_is_refused(self):
        self.extra = [{"id": GROUP_ID + "/providers/Microsoft.Storage/storageAccounts/foreign",
                       "type": "Microsoft.Storage/storageAccounts", "tags": TAGS}]
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_missing_or_duplicate_recorded_resource_is_refused(self):
        self.inventory.pop()
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_managed_group_must_link_to_exact_owned_amw(self):
        self.managed_by = "/subscriptions/foreign"
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_managed_links_cannot_escape_subscription(self):
        self.managed_rule = self.managed_rule.replace(SUB, SUB[:-1] + "3")
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_managed_provider_name_is_exact(self):
        self.managed_rule = self.managed_rule.replace("Microsoft.Insights", "MicrosoftXInsights")
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_managed_group_refuses_foreign_resources(self):
        self.managed_extra = [{"id": MANAGED_GROUP + "/providers/Microsoft.Storage/storageAccounts/foreign",
                               "type": "Microsoft.Storage/storageAccounts"}]
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_provider_only_custom_children_are_refused(self):
        for child in ("tables", "savedSearches", "dataExports", "linkedServices", "linkedStorageAccounts"):
            self.children = {child: [{"id": "/foreign", "properties": {
                "schema": {"name": "Foreign_CL", "tableType": "CustomLog"}}}]}
            self.assertNotEqual(self.invoke(), 0)
            self.assertEqual(self.mutations(), [])

    def test_system_tables_are_not_foreign(self):
        self.children = {"tables": [{"id": self.data["workspace_resource_id"] + "/tables/OTelSpans",
                                    "name": "OTelSpans", "properties": {
                                        "schema": {"name": "OTelSpans", "tableType": "Microsoft"}}}]}
        self.assertEqual(self.invoke(), 0)

    def test_exact_baselined_azure_default_saved_searches_are_allowed(self):
        search = saved_search(self.data)
        self.children = {"savedSearches": [search]}
        common.write_json(self.local / "native-workspace-children.json", child_baseline(self.data, [search]))
        self.assertEqual(self.invoke(), 0)

    def test_replaced_workspace_customer_id_blocks_deletion_despite_identical_defaults(self):
        search = saved_search(self.data)
        self.children = {"savedSearches": [search]}
        common.write_json(self.local / "native-workspace-children.json", child_baseline(self.data, [search]))
        self.live_customer_id = MARKER
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(any("/savedSearches?" in " ".join(args) for args in self.calls))

    def test_new_modified_or_missing_default_searches_are_not_adopted(self):
        search = saved_search(self.data)
        common.write_json(self.local / "native-workspace-children.json", child_baseline(self.data, [search]))
        for searches in (
            [saved_search(self.data, query="Event | take 999")],
            [search, saved_search(self.data, suffix="LogManagement|Foreign")],
            [{**search, "etag": "new-custom-version"}],
            [],
        ):
            with self.subTest(searches=searches):
                self.children = {"savedSearches": searches}
                with self.assertRaisesRegex(common.AppError, "savedSearches"):
                    teardown.verify_workspace_children(self.data)
                self.assertEqual(self.mutations(), [])

    def test_missing_or_cross_workspace_baseline_blocks_teardown(self):
        path = self.local / "native-workspace-children.json"
        path.unlink()
        self.assertNotEqual(self.invoke(), 0)
        self.assertFalse(path.exists())
        common.write_json(path, {**child_baseline(self.data), "workspace_customer_id": MARKER})
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_reviewed_initializer_binds_current_exact_defaults_without_deletion(self):
        path = self.local / "native-workspace-children.json"
        path.unlink()
        search = saved_search(self.data)
        self.children = {"savedSearches": [search]}
        result = native_deploy.initialize_workspace_child_baseline(self.data, child_inventory([search]))
        self.assertEqual(result["status"], "initialized")
        self.assertEqual(common.load_json(path), child_baseline(self.data, [search]))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.mutations(), [])

    def test_reviewed_initializer_never_overwrites_or_accepts_unreviewed_changes(self):
        path = self.local / "native-workspace-children.json"
        with self.assertRaises(common.AppError):
            native_deploy.initialize_workspace_child_baseline(self.data, child_inventory())
        path.unlink()
        search = saved_search(self.data)
        self.children = {"savedSearches": [saved_search(self.data, query="Event | take 999")]}
        with self.assertRaisesRegex(common.AppError, "savedSearches"):
            native_deploy.initialize_workspace_child_baseline(self.data, child_inventory([search]))
        self.assertFalse(path.exists())
        self.assertEqual(self.mutations(), [])

    def test_reviewed_initializer_rejects_custom_shape_and_incomplete_review(self):
        path = self.local / "native-workspace-children.json"
        path.unlink()
        search = saved_search(self.data)
        for reviewed in (
            child_inventory([{**search, "etag": "custom"}]),
            child_inventory([{**search, "properties": {**search["properties"], "version": 1}}]),
            {**child_inventory([search]), "savedSearches": {"value": [search], "nextLink": "https://example.invalid"}},
            {**child_inventory([search]), "savedSearches": {"value": [], "error": {"code": "Incomplete"}}},
            {"savedSearches": {"value": [search]}},
        ):
            with self.subTest(reviewed=reviewed), self.assertRaises(common.AppError):
                native_deploy.initialize_workspace_child_baseline(self.data, reviewed)
        self.assertFalse(path.exists())
        self.assertEqual(self.mutations(), [])

    def test_incomplete_managed_cleanup_is_not_reported_success(self):
        self.managed_present_after_delete = True
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(len(self.mutations()), 1)

    def test_retry_does_not_hide_incomplete_managed_cleanup(self):
        self.managed_present_after_delete = True
        self.assertNotEqual(self.invoke(), 0)
        self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(len(self.mutations()), 1)

    def test_provider_failure_never_reaches_deletion(self):
        with patch.object(teardown, "run_json", side_effect=common.AppError("Forbidden")):
            self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])

    def test_incomplete_child_pagination_blocks_deletion(self):
        original = self.azure

        def azure(args, **kwargs):
            if args[1] == "rest" and "/tables?" in args[args.index("--url") + 1]:
                return {"value": [], "nextLink": "https://management.azure.com/other"}
            return original(args, **kwargs)

        with patch.object(native_deploy, "run_json", side_effect=azure):
            self.assertNotEqual(self.invoke(), 0)
        self.assertEqual(self.mutations(), [])


if __name__ == "__main__":
    unittest.main()
