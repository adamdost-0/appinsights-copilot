"""Offline lifecycle tests; only the process boundary is mocked."""

import contextlib
import copy
import importlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import common


SUB = "11111111-2222-3333-4444-555555555555"
CUSTOMER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
MARKER = "12345678-1234-4234-8234-123456789abc"
GROUP = "rg-copilot-otel-audit"
GROUP_ID = f"/subscriptions/{SUB}/resourceGroups/{GROUP}"
LAW = GROUP_ID + "/providers/Microsoft.OperationalInsights/workspaces/law-copilot-otel-test"
APP = GROUP_ID + "/providers/Microsoft.Insights/components/ai-copilot-otel-test"
LOCATIONS = f"https://management.azure.com/subscriptions/{SUB}/locations?api-version=2022-12-01"
SECRET = "InstrumentationKey=super-private;IngestionEndpoint=https://eastus.example/;ApplicationId=private"
FIELDS = {
    "subscription_id", "resource_group", "application_insights_resource_id",
    "workspace_resource_id", "workspace_customer_id", "deployment_name",
    "ownership_marker", "location",
}


def state():
    return dict(subscription_id=SUB, resource_group=GROUP,
                application_insights_resource_id=APP, workspace_resource_id=LAW,
                workspace_customer_id=CUSTOMER, deployment_name="copilot-otel-audit",
                ownership_marker=MARKER, location="eastus")


class Azure:
    def __init__(self):
        self.calls = []
        self.exists = False
        self.marker = MARKER
        self.account = dict(id=SUB, state="Enabled", environmentName="AzureCloud")
        self.cloud = dict(name="AzureCloud", isActive=True,
                          endpoints={"resourceManager": "https://management.azure.com/"})
        self.provider_state = "Registered"
        self.locations = ["East US", "West Europe"]
        self.extra = []
        self.group_tags = None
        self.resource_tags = None
        self.app_changes = {}
        self.law_changes = {}
        self.fail = None
        self.responses = {}
        self.raw_response = None
        self.existence_checks = []
        self.rest_responses = {}

    def tags(self):
        return {"solution": "copilot-otel-audit", "ownership-marker": self.marker}

    def run(self, args, **kwargs):
        assert isinstance(args, list) and all(isinstance(x, str) for x in args)
        assert kwargs["capture_output"] and not kwargs["check"]
        self.calls.append(args)
        if self.fail and self.fail[0](args):
            return subprocess.CompletedProcess(args, 1, "", self.fail[1])
        if self.raw_response and self.raw_response[0](args):
            return subprocess.CompletedProcess(args, 0, self.raw_response[1], "")
        value = self.response(args)
        return subprocess.CompletedProcess(args, 0, "" if value is None else json.dumps(value), "")

    def response(self, a):
        for prefix, value in self.responses.items():
            if tuple(a[:len(prefix)]) == prefix:
                return value
        if a[:3] == ["az", "account", "show"]:
            return self.account
        if a[:3] == ["az", "cloud", "show"]:
            return self.cloud
        if a[:3] == ["az", "account", "list-locations"]:
            return [{"name": "eastus", "displayName": "East US"},
                    {"name": "westeurope", "displayName": "West Europe"}]
        if a[:3] == ["az", "provider", "show"]:
            namespace = a[a.index("--namespace") + 1]
            kind = "workspaces" if namespace == "Microsoft.OperationalInsights" else "components"
            return {"registrationState": self.provider_state,
                    "resourceTypes": [{"resourceType": kind, "locations": self.locations}]}
        if a[:3] == ["az", "group", "exists"]:
            if self.existence_checks:
                return self.existence_checks.pop(0)
            return self.exists
        if a[:3] == ["az", "group", "show"]:
            return {"id": GROUP_ID, "location": "eastus",
                    "tags": self.group_tags if self.group_tags is not None else self.tags()}
        if a[:3] == ["az", "resource", "list"]:
            tags = self.resource_tags if self.resource_tags is not None else self.tags()
            return [{"id": LAW, "type": "Microsoft.OperationalInsights/workspaces", "tags": tags},
                    {"id": APP, "type": "Microsoft.Insights/components", "tags": tags}] + self.extra
        if a[:3] == ["az", "deployment", "sub"]:
            for item in a:
                if item.startswith("ownershipMarker="):
                    self.marker = item.split("=", 1)[1]
            if a[3] == "create":
                self.exists = True
                return {k: {"type": "String", "value": v} for k, v in {
                    "applicationInsightsResourceId": APP, "workspaceResourceId": LAW,
                    "workspaceCustomerId": CUSTOMER}.items()}
            if a[3] == "what-if":
                return {"status": "Succeeded", "changes": [{"changeType": "Create"}]}
            return {}
        if a[:2] == ["az", "rest"]:
            url = a[a.index("--url") + 1]
            if url in self.rest_responses:
                return self.rest_responses[url]
            if url == LOCATIONS:
                return {"value": [{"name": "eastus", "displayName": "East US"},
                                  {"name": "westeurope", "displayName": "West Europe"}]}
            if url == f"https://management.azure.com{APP}?api-version=2020-02-02":
                props = dict(WorkspaceResourceId=LAW, DisableLocalAuth=False,
                             Application_Type="web", publicNetworkAccessForIngestion="Enabled",
                             publicNetworkAccessForQuery="Enabled", ConnectionString=SECRET)
                props.update(self.app_changes)
                return {"id": APP, "kind": "web", "location": "eastus",
                        "tags": self.tags(), "properties": props}
            assert url == f"https://management.azure.com{LAW}?api-version=2023-09-01", url
            props = dict(customerId=CUSTOMER, retentionInDays=30, sku={"name": "PerGB2018"},
                         workspaceCapping={"dailyQuotaGb": 1},
                         features={"enableLogAccessUsingOnlyResourcePermissions": False},
                         publicNetworkAccessForIngestion="Enabled", publicNetworkAccessForQuery="Enabled")
            props.update(self.law_changes)
            return {"id": LAW, "location": "eastus", "tags": self.tags(), "properties": props}
        if a[:3] == ["az", "group", "delete"]:
            self.exists = False
            return None
        raise AssertionError(f"Unexpected command: {a}")


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self.assertTrue((common.ROOT / "scripts/deploy.py").is_file(),
                        "Missing deployment implementation")
        self.deploy = importlib.import_module("scripts.deploy")
        infra = common.ROOT / "infra"
        infra.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix=".test-", dir=infra)
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name)
        self.azure = Azure()
        for target, value in [
            ("LOCAL", self.local), ("AZURE_STATE", self.local / "azure.json"),
        ]:
            patcher = patch.object(common, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("scripts.common.subprocess.run", side_effect=self.azure.run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, module=None, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = (module or self.deploy).main(list(args))
            except SystemExit as error:
                code = error.code
        self.assertNotIn("super-private", output.getvalue())
        self.assertNotIn("ApplicationId=private", output.getvalue())
        return code, output.getvalue()

    def save_state(self):
        common.write_json(common.AZURE_STATE, state())
        self.azure.exists = True

    def deploy_args(self, mode="--apply"):
        return ("--subscription", SUB, "--location", "eastus", mode)

    def mutations(self):
        return [a for a in self.azure.calls
                if a[:4] == ["az", "deployment", "sub", "create"]
                or a[:3] == ["az", "group", "delete"]]


class DeployTests(LifecycleCase):
    def test_location_discovery_does_not_use_unsupported_account_subscription_flag(self):
        self.azure.fail = (
            lambda a: a[:3] == ["az", "account", "list-locations"] and "--subscription" in a,
            "unrecognized arguments: --subscription")
        code, output = self.invoke(None, *self.deploy_args("--what-if"))
        self.assertEqual(code, 0, output)
        calls = [a for a in self.azure.calls if LOCATIONS in a]
        self.assertEqual(len(calls), 1)
        self.assertIn(SUB, calls[0])

    def test_favorites_permission_failure_cannot_strand_fresh_deployment(self):
        self.azure.fail = (
            lambda a: a[:2] == ["az", "rest"] and "/favorites?" in a[a.index("--url") + 1],
            "AuthorizationFailed " + SECRET)
        code, output = self.invoke(None, *self.deploy_args())
        self.assertEqual(len(self.mutations()), 1)
        self.assertTrue(self.azure.exists)
        self.assertTrue(common.AZURE_STATE.exists(), "Successful create stranded the group without its receipt")
        self.assertEqual(code, 0, output)
        before = common.load_json(common.AZURE_STATE)
        code, output = self.invoke(None, *self.deploy_args())
        self.assertEqual(code, 0, output)
        self.assertEqual(common.load_json(common.AZURE_STATE), before)
        urls = {a[a.index("--url") + 1] for a in self.azure.calls if a[:2] == ["az", "rest"]}
        self.assertEqual(urls, {
            LOCATIONS,
            f"https://management.azure.com{LAW}?api-version=2023-09-01",
            f"https://management.azure.com{APP}?api-version=2020-02-02",
        })

    def test_owned_preview_has_no_ancillary_api_dependencies(self):
        self.save_state()
        self.azure.fail = (lambda a: a[:2] == ["az", "rest"] and LOCATIONS not in a,
                           "404 UnsupportedApiVersion " + SECRET)
        code, output = self.invoke(None, *self.deploy_args("--what-if"))
        self.assertEqual(code, 0, output)
        self.assertFalse(any(a[:2] == ["az", "rest"] and LOCATIONS not in a for a in self.azure.calls))
        self.assertEqual(self.mutations(), [])

    def test_parent_readback_failure_keeps_receipt_and_allows_retry(self):
        parent_url = f"https://management.azure.com{APP}?api-version=2020-02-02"
        self.azure.fail = (lambda a: a[:2] == ["az", "rest"] and parent_url in a,
                           "AuthorizationFailed " + SECRET)
        original = self.azure.run

        def process(args, **kwargs):
            if args[:2] == ["az", "rest"] and args[args.index("--url") + 1] in (
                parent_url, f"https://management.azure.com{LAW}?api-version=2023-09-01"
            ):
                self.assertTrue(common.AZURE_STATE.exists(), "Receipt must precede parent readback")
                self.assertEqual(set(common.load_json(common.AZURE_STATE)), FIELDS)
            return original(args, **kwargs)

        with patch("scripts.common.subprocess.run", side_effect=process):
            code, output = self.invoke(None, *self.deploy_args())
        self.assertNotEqual(code, 0, output)
        self.assertTrue(common.AZURE_STATE.exists())
        before = common.load_json(common.AZURE_STATE)
        self.assertFalse((self.local / "collector.env").exists())
        self.azure.fail = None
        code, output = self.invoke(None, *self.deploy_args())
        self.assertEqual(code, 0, output)
        self.assertEqual(common.load_json(common.AZURE_STATE), before)
        self.assertEqual((self.local / "collector.env").read_text(),
                         f"APPLICATIONINSIGHTS_CONNECTION_STRING={SECRET}\n")

    def test_apply_contract_private_secret_and_order(self):
        code, output = self.invoke(None, *self.deploy_args())
        self.assertEqual(code, 0, output)
        data = common.load_json(common.AZURE_STATE)
        self.assertEqual(set(data), FIELDS)
        self.assertEqual(data["workspace_customer_id"], CUSTOMER)
        self.assertNotIn(SECRET, common.AZURE_STATE.read_text())
        env = self.local / "collector.env"
        self.assertEqual(env.read_text(), f"APPLICATIONINSIGHTS_CONNECTION_STRING={SECRET}\n")
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(common.AZURE_STATE.stat().st_mode & 0o777, 0o600)
        self.assertIn("30", output)
        self.assertIn("1", output)
        commands = [a[3] for a in self.azure.calls if a[:3] == ["az", "deployment", "sub"]]
        self.assertEqual(commands, ["validate", "what-if", "create"])
        for a in self.azure.calls:
            if a[:3] != ["az", "cloud", "show"]:
                self.assertIn("--subscription", a)
                self.assertEqual(a[a.index("--subscription") + 1], SUB)
            self.assertNotIn("login", a)
            self.assertNotIn("register", a)
        create = self.mutations()[0]
        self.assertEqual(create[create.index("--name") + 1], "copilot-otel-audit")
        self.assertIn(str(common.ROOT / "infra/main.bicep"), create)

    def test_what_if_never_mutates_or_writes(self):
        code, output = self.invoke(None, *self.deploy_args("--what-if"))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.mutations(), [])
        self.assertEqual(list(self.local.iterdir()), [])
        self.assertFalse(any(a[:2] == ["az", "rest"] and LOCATIONS not in a for a in self.azure.calls))

    def test_repeat_preserves_names_marker_and_deployment(self):
        self.assertEqual(self.invoke(None, *self.deploy_args())[0], 0)
        before = common.load_json(common.AZURE_STATE)
        self.assertEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(common.load_json(common.AZURE_STATE), before)
        for command in self.mutations():
            self.assertIn("ownershipMarker=" + before["ownership_marker"], command)
        self.assertIn("workspaceName=law-copilot-otel-test", self.mutations()[1])
        self.assertIn("applicationInsightsName=ai-copilot-otel-test", self.mutations()[1])

    def test_requires_explicit_subscription_and_one_mode(self):
        for args in [[], ["--what-if"], ["--subscription", SUB],
                     ["--subscription", SUB, "--apply", "--what-if"],
                     ["--subscription", "not-an-id", "--apply"]]:
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(None, *args)[0], 0)
        self.assertEqual(self.azure.calls, [])

    def test_account_cloud_provider_and_region_fail_closed(self):
        cases = [("account", {"id": SUB, "state": "Disabled", "environmentName": "AzureCloud"}),
                 ("account", {"id": CUSTOMER, "state": "Enabled", "environmentName": "AzureCloud"}),
                 ("cloud", {"name": "AzureUSGovernment", "isActive": True}),
                 ("provider_state", "NotRegistered"), ("locations", ["West Europe"])]
        for attr, value in cases:
            with self.subTest(attr=attr, value=value):
                original = copy.deepcopy(getattr(self.azure, attr))
                setattr(self.azure, attr, value)
                self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
                setattr(self.azure, attr, original)
        self.assertEqual(self.mutations(), [])

    def test_other_supported_region_not_hard_coded(self):
        code, output = self.invoke(None, "--subscription", SUB, "--location", "westeurope", "--what-if")
        self.assertEqual(code, 0, output)
        self.assertIn("location=westeurope", self.azure.calls[-1])

    def test_existing_group_requires_receipt_and_exact_inventory(self):
        self.azure.exists = True
        self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.save_state()
        self.azure.group_tags = {}
        self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.azure.group_tags = None
        self.azure.extra = [{"id": GROUP_ID + "/providers/Microsoft.Storage/storageAccounts/unrelated",
                             "type": "Microsoft.Storage/storageAccounts", "tags": self.azure.tags()}]
        self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.azure.extra = []
        self.azure.resource_tags = {"solution": "copilot-otel-audit", "ownership-marker": "wrong"}
        self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_receipt_scope_and_schema_are_strict(self):
        for key, value in [("subscription_id", CUSTOMER), ("resource_group", "other"),
                           ("workspace_resource_id", LAW.replace(SUB, CUSTOMER)),
                           ("ownership_marker", ""), ("deployment_name", "other"),
                           ("location", "westeurope"), ("connection_string", SECRET)]:
            with self.subTest(key=key):
                data = state()
                data[key] = value
                common.write_json(common.AZURE_STATE, data)
                self.azure.exists = True
                self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_permission_policy_and_secret_errors_stop_before_apply(self):
        for phase in ["validate", "what-if"]:
            self.azure.fail = (lambda a, p=phase: a[:4] == ["az", "deployment", "sub", p],
                               "RequestDisallowedByPolicy AuthorizationFailed " + SECRET)
            code, output = self.invoke(None, *self.deploy_args())
            self.assertNotEqual(code, 0)
            self.assertIn("policy", output.lower())
            self.assertEqual(list(self.local.iterdir()), [])
        self.assertEqual(self.mutations(), [])

    def test_secret_retrieval_failure_never_leaks_raw_error(self):
        self.azure.fail = (lambda a: a[:2] == ["az", "rest"] and LOCATIONS not in a,
                           "ConnectionString=arbitrary-secret-without-key")
        code, output = self.invoke(None, *self.deploy_args())
        self.assertNotEqual(code, 0)
        self.assertNotIn("arbitrary-secret", output)
        self.assertFalse((self.local / "collector.env").exists())

    def test_readback_requires_explicit_linkage_auth_and_network(self):
        cases = [("WorkspaceResourceId", APP), ("DisableLocalAuth", True),
                 ("DisableLocalAuth", None), ("publicNetworkAccessForQuery", "Disabled"),
                 ("publicNetworkAccessForIngestion", None), ("Application_Type", "other"),
                 ("ConnectionString", ""), ("ConnectionString", SECRET + "\nEVIL=yes")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.azure.exists = False
                self.azure.app_changes = {key: value}
                mutations_before = len(self.mutations())
                self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
                self.assertFalse((self.local / "collector.env").exists())
                self.assertEqual(len(self.mutations()), mutations_before + 1)
                if common.AZURE_STATE.exists():
                    common.AZURE_STATE.unlink()

    def test_readback_reports_actual_cap_retention_not_silent_defaults(self):
        self.azure.law_changes = {"retentionInDays": 90, "workspaceCapping": {"dailyQuotaGb": 2.5}}
        code, output = self.invoke(None, *self.deploy_args())
        self.assertEqual(code, 0, output)
        self.assertIn("90", output)
        self.assertIn("2.5", output)
        self.assertIn("table", output.lower())

    def test_missing_cap_or_resource_permission_mode_is_failure(self):
        for change in [{"workspaceCapping": {}}, {"retentionInDays": None},
                       {"features": {"enableLogAccessUsingOnlyResourcePermissions": True}},
                       {"customerId": "invalid"}]:
            self.azure.exists = False
            self.azure.law_changes = change
            mutations_before = len(self.mutations())
            self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
            self.assertEqual(len(self.mutations()), mutations_before + 1)
            self.assertEqual(set(common.load_json(common.AZURE_STATE)), FIELDS)
            self.assertFalse((self.local / "collector.env").exists())
            common.AZURE_STATE.unlink()

    def test_null_account_and_cloud_fields_fail_without_traceback(self):
        for attr, value in [
            ("account", {"id": None, "state": "Enabled", "environmentName": "AzureCloud"}),
            ("cloud", {"name": "AzureCloud", "isActive": True,
                       "endpoints": {"resourceManager": None}}),
        ]:
            with self.subTest(attr=attr):
                old = getattr(self.azure, attr)
                setattr(self.azure, attr, value)
                self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
                setattr(self.azure, attr, old)
        self.assertEqual(self.mutations(), [])

    def test_symlink_secret_destination_refused_before_mutation(self):
        target = self.local / "unrelated"
        target.write_text("untouched")
        (self.local / "collector.env").symlink_to(target)
        code, output = self.invoke(None, *self.deploy_args())
        self.assertNotEqual(code, 0, output)
        self.assertEqual(target.read_text(), "untouched")
        self.assertEqual(self.mutations(), [])

    def test_invalid_json_never_leaks_response(self):
        self.azure.raw_response = (lambda a: a[:3] == ["az", "account", "show"], SECRET)
        self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_invalid_inventory_and_outputs_fail_closed(self):
        for response in [None, {}, [], [{"id": LAW, "type": "Microsoft.OperationalInsights/workspaces",
                                        "tags": self.azure.tags()}]]:
            with self.subTest(response=response):
                self.save_state()
                self.azure.responses[("az", "resource", "list")] = response
                self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_null_inventory_group_id_fails_cleanly(self):
        self.save_state()
        self.azure.responses[("az", "group", "show")] = {
            "id": None, "location": "eastus", "tags": self.azure.tags()}
        self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_what_if_failure_or_unevaluated_changes_block_apply(self):
        for value in [
            {}, {"status": "Failed", "changes": []},
            {"status": "Succeeded", "changes": [{"changeType": "Unsupported"}]},
            {"status": "Succeeded", "changes": [{"changeType": "Ignore"}]},
            {"status": "Succeeded", "changes": [{"changeType": "Delete"}]},
            {"status": "Succeeded", "changes": [{"changeType": "unknown"}]},
        ]:
            with self.subTest(value=value):
                self.azure.responses[("az", "deployment", "sub", "what-if")] = value
                self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_missing_group_receipt_not_silently_recreated(self):
        self.save_state()
        self.azure.exists = False
        self.assertNotEqual(self.invoke(None, *self.deploy_args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_failed_readback_still_preserves_safe_ownership_receipt(self):
        self.azure.app_changes = {"DisableLocalAuth": True}
        code, output = self.invoke(None, *self.deploy_args())
        self.assertNotEqual(code, 0, output)
        self.assertTrue(common.AZURE_STATE.exists())
        self.assertEqual(set(common.load_json(common.AZURE_STATE)), FIELDS)
        self.assertFalse((self.local / "collector.env").exists())

    def test_validation_error_payload_even_with_exit_zero_blocks_apply(self):
        self.azure.responses[("az", "deployment", "sub", "validate")] = {
            "error": {"code": "RequestDisallowedByPolicy", "message": SECRET}}
        code, output = self.invoke(None, *self.deploy_args())
        self.assertNotEqual(code, 0, output)
        self.assertIn("policy", output.lower())
        self.assertEqual(self.mutations(), [])

    def test_corrupt_non_utf8_receipt_fails_cleanly(self):
        common.AZURE_STATE.write_bytes(b"\xff\xfe")
        code, output = self.invoke(None, *self.deploy_args())
        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.azure.calls, [])

    def test_group_appearing_during_preview_refuses_apply(self):
        self.azure.existence_checks = [False, True]
        code, output = self.invoke(None, *self.deploy_args())
        self.assertNotEqual(code, 0, output)
        self.assertIn("changed", output)
        self.assertEqual(self.mutations(), [])

    def test_what_if_never_prints_raw_resource_properties(self):
        self.azure.responses[("az", "deployment", "sub", "what-if")] = {
            "status": "Succeeded", "changes": [{"changeType": "Modify",
                                               "after": {"ConnectionString": SECRET}}]}
        code, output = self.invoke(None, *self.deploy_args("--what-if"))
        self.assertEqual(code, 0, output)
        self.assertIn("Modify: 1", output)
        self.assertEqual(self.mutations(), [])


class InfrastructureTests(unittest.TestCase):
    def test_required_bicep_contract(self):
        root = common.ROOT / "infra"
        self.assertTrue((root / "main.bicep").exists(), "Missing subscription Bicep")
        main = (root / "main.bicep").read_text()
        resources = (root / "resources.bicep").read_text()
        self.assertIn("targetScope = 'subscription'", main)
        self.assertIn("rg-copilot-otel-audit", main)
        self.assertIn("uniqueString(", main)
        for token in ["@2023-09-01", "@2020-02-02", "dailyQuotaGb: 1",
                      "retentionInDays: 30", "enableLogAccessUsingOnlyResourcePermissions: false",
                      "DisableLocalAuth: false", "WorkspaceResourceId: workspace.id",
                      "Application_Type: 'web'", "kind: 'web'", "name: 'PerGB2018'"]:
            self.assertIn(token, resources)
        for text in [main, resources]:
            self.assertNotIn("ConnectionString", text)
            self.assertNotIn("connectionString", text)
        self.assertTrue((root / "main.bicepparam").exists())


if __name__ == "__main__":
    unittest.main()
