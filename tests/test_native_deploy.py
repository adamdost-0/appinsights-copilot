from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import native_deploy
from scripts.common import AppError, ROOT, write_json
from tests.test_teardown import child_baseline, child_inventory, saved_search, state as deployment_state


UUID = "00000000-0000-4000-8000-000000000001"


class NativeDeployTests(unittest.TestCase):
    def workspace_readback(self, data):
        return {
            "id": data["workspace_resource_id"], "location": data["location"],
            "tags": {"solution": "copilot-otel-v1", "ownership-marker": data["ownership_marker"]},
            "properties": {"customerId": data["workspace_customer_id"]},
        }

    def deploy_responses(self, validation=None, result=None):
        responses = [
            {"id": UUID, "state": "Enabled", "environmentName": "AzureCloud"},
            *[{"registrationState": "Registered"} for _ in range(3)],
            validation if validation is not None else {"properties": {"provisioningState": "Succeeded"}},
            {"status": "Succeeded", "changes": []},
        ]
        if result is not None:
            responses.append(result)
            properties = result.get("properties")
            if isinstance(properties, dict):
                outputs = properties.get("outputs")
                if isinstance(outputs, dict):
                    output = outputs.get("nativeState")
                    if isinstance(output, dict) and isinstance(output.get("value"), dict):
                        responses.append(self.workspace_readback(output["value"]))
                        responses.extend(child_inventory().values())
        return responses

    def test_region_must_be_ascii_azure_identifier(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(native_deploy, "LOCAL", Path(directory)):
            for region in ("éastus", "", "east us", "../eastus"):
                with self.subTest(region=region), self.assertRaises(AppError):
                    native_deploy.receipt(UUID, region)

    def test_failed_or_malformed_validation_never_reaches_preview_or_apply(self):
        for validation in ({"properties": None}, {"properties": []},
                           {"properties": {"provisioningState": "Failed"}}, []):
            with self.subTest(validation=validation), tempfile.TemporaryDirectory() as directory:
                with patch.object(native_deploy, "LOCAL", Path(directory)), \
                     patch.object(native_deploy, "check_group", return_value=False), \
                     patch.object(native_deploy, "run_json",
                                  side_effect=self.deploy_responses(validation=validation)) as execute:
                    with self.assertRaises(AppError):
                        native_deploy.deploy(UUID, "eastus", True, UUID)
                commands = [call.args[0] for call in execute.call_args_list]
                self.assertFalse(any("create" in command or "what-if" in command for command in commands))

    def test_apply_persists_ownership_before_cloud_and_private_validated_state(self):
        data = deployment_state()
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            receipt = {key: data[key] for key in ("subscription_id", "resource_group", "location", "ownership_marker")}
            write_json(local / "native-deployment.json", receipt)
            result = {"properties": {"provisioningState": "Succeeded", "outputs": {"nativeState": {"value": data}}}}
            replies = iter(self.deploy_responses(result=result))

            def azure(args, **kwargs):
                self.assertEqual(native_deploy.load_receipt(UUID), receipt)
                return next(replies)

            with patch.object(native_deploy, "LOCAL", local), \
                 patch.object(native_deploy, "check_group", return_value=False), \
                 patch.object(native_deploy, "run_json", side_effect=azure) as execute:
                self.assertEqual(native_deploy.deploy(UUID, "eastus", True, UUID)["status"], "deployed")
                self.assertEqual(native_deploy.load_state(receipt), data)
            commands = [call.args[0] for call in execute.call_args_list]
            self.assertEqual([command[3] for command in commands if command[1:3] == ["deployment", "sub"]],
                             ["validate", "what-if", "create"])
            self.assertEqual((local / "native-azure.json").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("application_insights_resource_id", data)
            workspace_get = next(command for command in commands
                                 if command[1] == "rest" and data["workspace_resource_id"] + "?" in " ".join(command))
            self.assertIn(
                "https://management.azure.com" + data["workspace_resource_id"] + "?api-version=2023-09-01",
                workspace_get,
            )
            self.assertEqual(native_deploy.private_state(local / "native-workspace-children.json"),
                             child_baseline(data))

    def test_workspace_customer_binding_must_be_verified_before_state_persistence(self):
        data = deployment_state()
        receipt = {key: data[key] for key in (
            "subscription_id", "resource_group", "location", "ownership_marker")}
        workspace = self.workspace_readback(data)
        for readback in (
            {**workspace, "properties": {"customerId": UUID[:-1] + "3"}},
            {**workspace, "properties": {}},
            {**workspace, "id": data["workspace_resource_id"].replace("rg-copilot-otel-v1", "foreign")},
            {**workspace, "location": "westus"},
            {**workspace, "tags": {}},
            None,
        ):
            with self.subTest(readback=readback), tempfile.TemporaryDirectory() as directory:
                local = Path(directory)
                write_json(local / "native-deployment.json", receipt)
                result = {"properties": {"provisioningState": "Succeeded",
                                         "outputs": {"nativeState": {"value": data}}}}
                responses = self.deploy_responses(result=result)
                responses[-6] = readback
                with patch.object(native_deploy, "LOCAL", local), \
                     patch.object(native_deploy, "check_group", return_value=False), \
                     patch.object(native_deploy, "run_json", side_effect=responses):
                    with self.assertRaises(AppError):
                        native_deploy.deploy(UUID, "eastus", True, UUID)
                self.assertFalse((local / "native-azure.json").exists())

    def test_malformed_apply_output_is_never_persisted_as_state(self):
        for properties in (None, [], {"provisioningState": "Failed"},
                           {"provisioningState": "Succeeded", "outputs": None},
                           {"provisioningState": "Succeeded", "outputs": {"nativeState": None}}):
            with self.subTest(properties=properties), tempfile.TemporaryDirectory() as directory:
                local = Path(directory)
                with patch.object(native_deploy, "LOCAL", local), \
                     patch.object(native_deploy, "check_group", return_value=False), \
                     patch.object(native_deploy, "run_json", side_effect=self.deploy_responses(
                         result={"properties": properties})):
                    with self.assertRaises(AppError):
                        native_deploy.deploy(UUID, "eastus", True, UUID)
                self.assertFalse((local / "native-azure.json").exists())

    def test_redeploy_keeps_old_receipt_when_immutable_identity_changes(self):
        data = deployment_state()
        receipt = {key: data[key] for key in ("subscription_id", "resource_group", "location", "ownership_marker")}
        changed = {**data, "workspace_customer_id": UUID[:-1] + "3"}
        result = {"properties": {"provisioningState": "Succeeded", "outputs": {"nativeState": {"value": changed}}}}
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            write_json(local / "native-deployment.json", receipt)
            write_json(local / "native-azure.json", data)
            write_json(local / "native-workspace-children.json", child_baseline(data))
            with patch.object(native_deploy, "LOCAL", local), \
                 patch.object(native_deploy, "check_group", return_value=True), \
                 patch.object(native_deploy, "run_json", side_effect=self.deploy_responses(result=result)):
                with self.assertRaises(AppError):
                    native_deploy.deploy(UUID, "eastus", True, UUID)
                self.assertEqual(native_deploy.load_state(receipt), data)

    def test_absent_recorded_group_requires_completed_teardown_before_recreation(self):
        for completed in (False, True):
            with self.subTest(completed=completed), tempfile.TemporaryDirectory() as directory:
                local = Path(directory)
                data = deployment_state()
                receipt = {key: data[key] for key in (
                    "subscription_id", "resource_group", "location", "ownership_marker")}
                write_json(local / "native-deployment.json", receipt)
                write_json(local / "native-azure.json", data)
                write_json(local / "native-workspace-children.json", child_baseline(data))
                if completed:
                    write_json(local / "native-teardown.json", {
                        **receipt, "status": "deleted", "managed_resource_group": "ma_monitor_eastus_managed"})
                recreated = {**data, "workspace_customer_id": UUID[:-1] + "3"}
                result = {"properties": {"provisioningState": "Succeeded",
                                        "outputs": {"nativeState": {"value": recreated}}}}
                with patch.object(native_deploy, "LOCAL", local), \
                     patch.object(native_deploy, "check_group", return_value=False), \
                     patch.object(native_deploy, "group_exists", return_value=False), \
                     patch.object(native_deploy, "run_json",
                                  side_effect=self.deploy_responses(result=result)) as execute:
                    if completed:
                        self.assertEqual(native_deploy.deploy(UUID, "eastus", True, UUID)["status"], "deployed")
                        self.assertEqual(native_deploy.private_state(local / "native-workspace-children.json"),
                                         child_baseline(recreated))
                    else:
                        with self.assertRaises(AppError):
                            native_deploy.deploy(UUID, "eastus", True, UUID)
                        self.assertFalse(any("create" in call.args[0] for call in execute.call_args_list))

    def test_missing_recorded_dcr_blocks_redeploy_before_arm_mutation(self):
        data = deployment_state()
        receipt = {key: data[key] for key in (
            "subscription_id", "resource_group", "location", "ownership_marker")}
        tags = {"solution": "copilot-otel-v1", "ownership-marker": data["ownership_marker"]}
        inventory = [
            {"id": data[key], "type": kind, "tags": tags}
            for key, (kind, _) in native_deploy.RESOURCE_TYPES.items() if key != "dcr_resource_id"
        ]
        result = {"properties": {"provisioningState": "Succeeded", "outputs": {"nativeState": {"value": data}}}}
        responses = self.deploy_responses(result=result)
        responses[4:4] = [True, {"id": native_deploy.group_id(UUID), "location": "eastus", "tags": tags}, inventory]
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            write_json(local / "native-deployment.json", receipt)
            write_json(local / "native-azure.json", data)
            write_json(local / "native-workspace-children.json", child_baseline(data))
            with patch.object(native_deploy, "LOCAL", local), \
                 patch.object(native_deploy, "run_json", side_effect=responses) as execute:
                with self.assertRaises(AppError):
                    native_deploy.deploy(UUID, "eastus", True, UUID)
                self.assertFalse(any("create" in call.args[0] for call in execute.call_args_list))

    def test_repeat_apply_preserves_exact_existing_child_baseline(self):
        data = deployment_state()
        receipt = {key: data[key] for key in (
            "subscription_id", "resource_group", "location", "ownership_marker")}
        search = saved_search(data)
        result = {"properties": {"provisioningState": "Succeeded", "outputs": {"nativeState": {"value": data}}}}
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            write_json(local / "native-deployment.json", receipt)
            write_json(local / "native-azure.json", data)
            path = local / "native-workspace-children.json"
            write_json(path, child_baseline(data, [search]))
            before = path.read_bytes()
            responses = self.deploy_responses(result=result)
            responses[-5:] = child_inventory([search]).values()
            with patch.object(native_deploy, "LOCAL", local), \
                 patch.object(native_deploy, "check_group", return_value=True), \
                 patch.object(native_deploy, "run_json", side_effect=responses):
                self.assertEqual(native_deploy.deploy(UUID, "eastus", True, UUID)["status"], "deployed")
            self.assertEqual(path.read_bytes(), before)

    def test_existing_deployment_without_baseline_is_never_silently_adopted(self):
        data = deployment_state()
        receipt = {key: data[key] for key in (
            "subscription_id", "resource_group", "location", "ownership_marker")}
        result = {"properties": {"provisioningState": "Succeeded", "outputs": {"nativeState": {"value": data}}}}
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            write_json(local / "native-deployment.json", receipt)
            write_json(local / "native-azure.json", data)
            with patch.object(native_deploy, "LOCAL", local), \
                 patch.object(native_deploy, "check_group", return_value=True), \
                 patch.object(native_deploy, "run_json", side_effect=self.deploy_responses(result=result)) as execute:
                with self.assertRaises(AppError):
                    native_deploy.deploy(UUID, "eastus", True, UUID)
            self.assertFalse((local / "native-workspace-children.json").exists())
            self.assertFalse(any("create" in call.args[0] for call in execute.call_args_list))

    def test_successful_arm_then_503_has_explicit_reviewed_recovery_and_retry(self):
        for failed_read in ("workspace", "children"):
            with self.subTest(failed_read=failed_read), tempfile.TemporaryDirectory() as directory:
                local = Path(directory)
                data = deployment_state()
                receipt = {key: data[key] for key in (
                    "subscription_id", "resource_group", "location", "ownership_marker")}
                write_json(local / "native-deployment.json", receipt)
                result = {"properties": {"provisioningState": "Succeeded",
                                         "outputs": {"nativeState": {"value": data}}}}
                responses = self.deploy_responses(result=result)
                responses[-6 if failed_read == "workspace" else -5] = AppError("503 ServiceUnavailable")
                with patch.object(native_deploy, "LOCAL", local):
                    with patch.object(native_deploy, "check_group", return_value=False), \
                         patch.object(native_deploy, "run_json", side_effect=responses):
                        with self.assertRaisesRegex(AppError, "503"):
                            native_deploy.deploy(UUID, "eastus", True, UUID)
                    self.assertFalse((local / "native-azure.json").exists())
                    self.assertFalse((local / "native-workspace-children.json").exists())
                    candidate = native_deploy.load_recovery_state(UUID)
                    self.assertEqual(candidate, data)
                    search = saved_search(data)
                    reviewed = child_inventory([search])
                    recovery_responses = [
                        {"id": UUID, "state": "Enabled", "environmentName": "AzureCloud"},
                        self.workspace_readback(data), *reviewed.values(),
                    ]
                    with patch.object(native_deploy, "check_group", return_value=True), \
                         patch.object(native_deploy, "run_json", side_effect=recovery_responses) as execute:
                        recovered = native_deploy.initialize_workspace_child_baseline(candidate, reviewed)
                    self.assertEqual(recovered["status"], "initialized")
                    self.assertEqual(native_deploy.load_state(receipt), data)
                    baseline = (local / "native-workspace-children.json").read_bytes()
                    self.assertFalse(any("create" in call.args[0] or "delete" in call.args[0]
                                         for call in execute.call_args_list))
                    retry_responses = self.deploy_responses(result=result)
                    retry_responses[-5:] = reviewed.values()
                    with patch.object(native_deploy, "check_group", return_value=True), \
                         patch.object(native_deploy, "run_json", side_effect=retry_responses):
                        self.assertEqual(native_deploy.deploy(UUID, "eastus", True, UUID)["status"], "deployed")
                    self.assertEqual((local / "native-workspace-children.json").read_bytes(), baseline)

    def test_recovery_rejects_wrong_scope_marker_and_unsuccessful_saved_results(self):
        data = deployment_state()
        receipt = {key: data[key] for key in (
            "subscription_id", "resource_group", "location", "ownership_marker")}
        for candidate, status, error in (
            ({**data, "resource_group": "foreign"}, "Succeeded", None),
            ({**data, "workspace_resource_id": data["workspace_resource_id"].replace("rg-copilot-otel-v1", "foreign")},
             "Succeeded", None),
            ({**data, "ownership_marker": UUID}, "Succeeded", None),
            (data, "Failed", None),
            (data, "Running", None),
            (data, "Succeeded", {"code": "Failure"}),
        ):
            with self.subTest(candidate=candidate, status=status), tempfile.TemporaryDirectory() as directory:
                local = Path(directory)
                write_json(local / "native-deployment.json", receipt)
                write_json(local / "native-deployment-result.json", {
                    "error": error, "properties": {"provisioningState": status,
                                                   "outputs": {"nativeState": {"value": candidate}}},
                })
                with patch.object(native_deploy, "LOCAL", local), patch.object(native_deploy, "run_json") as execute:
                    with self.assertRaises(AppError):
                        native_deploy.load_recovery_state(UUID)
                    with self.assertRaises(AppError):
                        native_deploy.initialize_workspace_child_baseline(data, child_inventory())
                execute.assert_not_called()
                self.assertFalse((local / "native-azure.json").exists())
                self.assertFalse((local / "native-workspace-children.json").exists())

    def test_recovery_cannot_reset_an_existing_baseline(self):
        data = deployment_state()
        receipt = {key: data[key] for key in (
            "subscription_id", "resource_group", "location", "ownership_marker")}
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory)
            write_json(local / "native-deployment.json", receipt)
            write_json(local / "native-deployment-result.json", {
                "properties": {"provisioningState": "Succeeded", "outputs": {"nativeState": {"value": data}}},
            })
            path = local / "native-workspace-children.json"
            write_json(path, child_baseline(data))
            before = path.read_bytes()
            with patch.object(native_deploy, "LOCAL", local), patch.object(native_deploy, "run_json") as execute:
                with self.assertRaises(AppError):
                    native_deploy.initialize_workspace_child_baseline(data, child_inventory([saved_search(data)]))
            execute.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((local / "native-azure.json").exists())

    def test_native_template_uses_direct_otel_sources_and_no_compute(self):
        text = (ROOT / "infra/resources.bicep").read_text()
        for required in (
            "directDataSources:", "otelMetrics:", "otelTraces:", "otelLogs:",
            "'Microsoft-OTel-Traces-Spans'", "'Microsoft-OTel-Traces-Events'",
            "'Microsoft-OTel-Traces-Resources'", "'Custom-Metrics-Otel'",
            "monitoringAccounts:", "enrichWithResourceAttributes: ['*']", "prometheusQueryEndpoint",
            "3913510d-42f4-4e42-8a64-420c390055eb", "b0d8363b-8ddd-447d-831f-62ca05bff136",
            "73c42c96-874c-492b-b04d-ab87d138a893",
            "Microsoft-OTLP-Traces/otlp/v1/traces", "Microsoft-OTLP-Logs/otlp/v1/logs",
            "retentionInDays: 30", "dailyQuotaGb: 1",
        ):
            self.assertIn(required, text)
        for forbidden in ("Microsoft.Compute", "Microsoft.App/", "containerApps",
                          "connectionString", "InstrumentationKey", "OtlpSupport", "dataSources:",
                          "Microsoft.Insights/components", "application_insights_resource_id",
                          "enrichWithReference", "replaceResourceIdWithReference", "applicationInsights"):
            self.assertNotIn(forbidden, text)

    def test_v1_names_and_resource_scoped_roles(self):
        self.assertEqual(native_deploy.GROUP, "rg-copilot-otel-v1")
        self.assertEqual(native_deploy.DEPLOYMENT, "copilot-otel-v1")
        main = (ROOT / "infra/main.bicep").read_text()
        resources = (ROOT / "infra/resources.bicep").read_text()
        self.assertIn("name: 'rg-copilot-otel-v1'", main)
        self.assertIn("solution: 'copilot-otel-v1'", main)
        self.assertIn("scope: rule", resources)
        self.assertIn("scope: metrics", resources)
        self.assertIn("scope: logs", resources)
        self.assertIn("principalId: operatorPrincipalId", resources)
        self.assertFalse((ROOT / "infra/native-main.bicep").exists())
        self.assertFalse((ROOT / "infra/native-resources.bicep").exists())

    def test_receipt_is_stable_and_cannot_cross_subscriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_deploy, "LOCAL", Path(directory)):
                first = native_deploy.receipt(UUID, "eastus")
                second = native_deploy.receipt(UUID, "eastus")
                self.assertEqual(first, second)
                self.assertEqual(first["resource_group"], "rg-copilot-otel-v1")
                self.assertEqual((Path(directory) / "native-deployment.json").stat().st_mode & 0o777, 0o600)
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID[:-1] + "2", "eastus")
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID, "westus")

    def test_existing_group_requires_exact_ownership(self):
        receipt = {"subscription_id": UUID, "ownership_marker": UUID, "location": "eastus"}
        for group in (
            {"location": "eastus", "tags": {}},
            {"location": "eastus", "tags": {"solution": "copilot-native-otel", "ownership-marker": "wrong"}},
            {"location": "westus", "tags": {"solution": "copilot-native-otel", "ownership-marker": UUID}},
        ):
            with self.subTest(group=group):
                with patch.object(native_deploy, "run_json", side_effect=[True, group]):
                    with self.assertRaises(AppError):
                        native_deploy.check_group(receipt)

    def test_absent_group_is_allowed(self):
        with patch.object(native_deploy, "run_json", return_value=False):
            native_deploy.check_group({"subscription_id": UUID, "ownership_marker": UUID,
                                       "location": "eastus"})

    def test_foreign_resources_refused_even_with_copied_tags(self):
        state = {"subscription_id": UUID, "ownership_marker": UUID, "location": "eastus"}
        tags = {"solution": "copilot-otel-v1", "ownership-marker": UUID}
        group_id = f"/subscriptions/{UUID}/resourceGroups/{native_deploy.GROUP}"
        for kind in ("Microsoft.Insights/components", "Microsoft.Storage/storageAccounts",
                     "Microsoft.AlertsManagement/smartDetectorAlertRules"):
            resource = {"id": f"{group_id}/providers/{kind}/foreign", "type": kind, "tags": tags}
            replies = [True, {"id": group_id, "location": "eastus", "tags": tags}, [resource]]
            with self.subTest(kind=kind), patch.object(native_deploy, "run_json", side_effect=replies):
                with self.assertRaises(AppError):
                    native_deploy.check_group(state)

    def test_owned_visualization_and_exact_deployment_are_preserved(self):
        state = deployment_state()
        tags = {"solution": "copilot-otel-v1", "ownership-marker": state["ownership_marker"]}
        group = native_deploy.group_id(UUID)
        resources = [
            {"id": group + "/providers/Microsoft.Insights/workbooks/" + UUID,
             "type": "Microsoft.Insights/workbooks", "tags": tags},
            {"id": group + "/providers/Microsoft.Resources/deployments/copilot-otel-v1-visualizations",
             "type": "Microsoft.Resources/deployments"},
        ]
        replies = [True, {"id": group, "location": "eastus", "tags": tags}, resources]
        with patch.object(native_deploy, "run_json", side_effect=replies):
            self.assertTrue(native_deploy.check_group(state))

    def test_symlink_parent_receipt_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target").mkdir()
            (root / "link").symlink_to(root / "target", target_is_directory=True)
            with patch.object(native_deploy, "LOCAL", root / "target"):
                native_deploy.receipt(UUID, "eastus")
            with patch.object(native_deploy, "LOCAL", root / "link"), self.assertRaises(AppError):
                native_deploy.receipt(UUID, "eastus")

    def test_corrupt_receipt_fails_before_cloud_access(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_deploy, "LOCAL", Path(directory)):
                write_json(Path(directory) / "native-deployment.json", {"subscription_id": UUID})
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID, "eastus")
                write_json(Path(directory) / "native-deployment.json",
                           {"subscription_id": UUID, "location": "eastus"})
                with self.assertRaises(AppError):
                    native_deploy.receipt(UUID, "eastus")

    def test_parameter_file_contains_no_credentials(self):
        receipt = {"ownership_marker": UUID, "location": "eastus"}
        values = native_deploy.parameters(receipt, UUID, "User")
        self.assertEqual(values["parameters"]["principalId"]["value"], UUID)
        self.assertEqual(values["parameters"]["ownershipMarker"]["value"], UUID)
        with self.assertRaises(AppError):
            native_deploy.parameters(receipt, UUID, "unsupported")
        with self.assertRaises(AppError):
            native_deploy.parameters(receipt, None, "User")
        self.assertNotIn("token", str(values).lower())

    def test_distinct_operator_has_explicit_query_roles(self):
        state = {"ownership_marker": UUID, "location": "eastus"}
        operator = UUID[:-1] + "2"
        values = native_deploy.parameters(state, UUID, "ServicePrincipal", operator, "Group")
        self.assertEqual(values["parameters"]["principalId"]["value"], UUID)
        self.assertEqual(values["parameters"]["operatorPrincipalId"]["value"], operator)
        self.assertEqual(values["parameters"]["operatorPrincipalType"]["value"], "Group")

    def test_what_if_requests_machine_readable_output(self):
        state = {"subscription_id": UUID, "location": "eastus", "ownership_marker": UUID}
        responses = [
            {"id": UUID, "state": "Enabled", "environmentName": "AzureCloud"},
            *[{"registrationState": "Registered"} for _ in range(3)],
            {"properties": {"provisioningState": "Succeeded"}},
            {"status": "Succeeded", "changes": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(native_deploy, "LOCAL", Path(directory)), \
                 patch.object(native_deploy, "receipt", return_value=state), \
                 patch.object(native_deploy, "check_group", return_value=False), \
                 patch.object(native_deploy, "run_json", side_effect=responses) as execute:
                native_deploy.deploy(UUID, "eastus", False, UUID)
        commands = [call.args[0] for call in execute.call_args_list]
        what_if = next(command for command in commands if "what-if" in command)
        self.assertIn("--no-pretty-print", what_if)
        self.assertIn(str(ROOT / "infra" / "main.bicep"), what_if)
        self.assertFalse(any("create" in command or "delete" in command for command in commands))

    def test_failed_or_malformed_preview_is_never_success(self):
        state = {"subscription_id": UUID, "location": "eastus", "ownership_marker": UUID}
        for preview in ({"status": "Failed", "error": {"code": "Denied"}, "changes": []},
                        {"status": "Succeeded"}, {"status": "Succeeded", "changes": [None]},
                        {"status": "Succeeded", "changes": [{"changeType": "Delete", "resourceId": "/foreign"}]},
                        {"status": "Succeeded", "changes": [{"changeType": "Create", "resourceId": "/foreign"}]},
                        {"status": "Succeeded", "changes": [{"changeType": "Unknown", "resourceId": "/foreign"}]}):
            with self.subTest(preview=preview), tempfile.TemporaryDirectory() as directory:
                responses = [
                    {"id": UUID, "state": "Enabled", "environmentName": "AzureCloud"},
                    *[{"registrationState": "Registered"} for _ in range(3)],
                    {"properties": {"provisioningState": "Succeeded"}}, preview,
                ]
                with patch.object(native_deploy, "LOCAL", Path(directory)), \
                     patch.object(native_deploy, "receipt", return_value=state), \
                     patch.object(native_deploy, "check_group", return_value=False), \
                     patch.object(native_deploy, "run_json", side_effect=responses):
                    with self.assertRaises(AppError):
                        native_deploy.deploy(UUID, "eastus", False, UUID)


if __name__ == "__main__":
    unittest.main()
