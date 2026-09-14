"""Offline ARM/XML contracts, not an APIM runtime emulator."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


class ApimContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = ROOT / "infra/apim.bicep"
        cls.arm = None
        if cls.template.exists():
            if not shutil.which("az"):
                raise AssertionError("Azure CLI/Bicep required: do not skip compiled ARM validation")
            result = subprocess.run(
                ["az", "bicep", "build", "--file", str(cls.template), "--stdout"],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if result.returncode:
                raise AssertionError(f"Bicep compilation failed:\n{result.stderr}")
            cls.arm = json.loads(result.stdout)

    def compiled(self):
        self.assertTrue(self.template.exists(), "APIM subscription template is missing")
        self.assertIsNotNone(self.arm, "Compiled ARM is required")
        return self.arm

    def module(self, name):
        modules = self.compiled()["resources"]
        return next(r for r in modules if r["type"] == "Microsoft.Resources/deployments"
                    and r["name"] == name)

    def resources(self, module):
        return self.module(module)["properties"]["template"]["resources"]

    def resource(self, module, kind):
        return next(r for r in self.resources(module) if r["type"] == kind)

    def policy(self, filename):
        self.compiled()
        return ET.parse(ROOT / "infra/policies" / filename).getroot()

    def test_independent_group_and_constrained_tier(self):
        arm = self.compiled()
        group = next(r for r in arm["resources"] if r["type"] == "Microsoft.Resources/resourceGroups")
        self.assertEqual(group["name"], "rg-copilot-otel-apim")
        self.assertEqual(arm["variables"]["tags"]["solution"], "copilot-otel-apim")
        self.assertEqual(arm["parameters"]["skuName"]["allowedValues"], ["Developer"])
        service = self.resource("apim-service", "Microsoft.ApiManagement/service")
        self.assertEqual(service["identity"]["type"], "SystemAssigned")
        self.assertEqual(service["sku"]["capacity"], 1)
        self.assertFalse(any("Microsoft.Web" in r["type"] for r in self.resources("apim-service")))

    def test_phases_deny_before_routes_and_activate_last(self):
        for current, previous in (
            ("apim-baseline", "apim-service"),
            ("apim-operations", "apim-baseline"),
            ("apim-activate", "apim-operations"),
        ):
            self.assertIn(previous, json.dumps(self.module(current)["dependsOn"]))
        baseline = self.resource("apim-baseline", "Microsoft.ApiManagement/service/apis/policies")
        global_deny = self.resource("apim-service", "Microsoft.ApiManagement/service/policies")
        for module, policy in (("apim-baseline", baseline), ("apim-service", global_deny)):
            value = policy["properties"]["value"]
            reference = re.fullmatch(r"\[variables\('([^']+)'\)\]", value)
            if reference:
                value = self.module(module)["properties"]["template"]["variables"][reference[1]]
            xml = ET.fromstring(value)
            self.assertEqual(xml.find("inbound/return-response/set-status").get("code"), "503")
            self.assertIsNone(xml.find(".//forward-request"))
        subscription = self.resource("apim-activate", "Microsoft.ApiManagement/service/subscriptions")
        self.assertEqual(subscription["properties"]["state"], "active")
        self.assertIn("policies", json.dumps(subscription["dependsOn"]))
        self.assertIn("Microsoft.ApiManagement/service/apis", subscription["properties"]["scope"])
        self.assertNotIn("primaryKey", subscription["properties"])
        self.assertNotIn("secondaryKey", subscription["properties"])
        self.assertFalse(subscription["properties"]["allowTracing"])

    def test_activation_is_explicit_and_default_deployment_stays_denied(self):
        self.assertIn("activateGateway", self.compiled()["parameters"])
        self.assertFalse(self.compiled()["parameters"]["activateGateway"]["defaultValue"])
        self.assertEqual(self.module("apim-activate")["condition"], "[parameters('activateGateway')]")

    def test_gateway_failures_keep_sanitized_auth_throttle_and_timeout_statuses(self):
        policy = self.policy("apim-api.xml")
        status = policy.find("on-error/set-variable")
        self.assertIsNotNone(status)
        expression = status.get("value")
        for reason, code in (
            ("RateLimitExceeded", 429), ("QuotaExceeded", 403),
            ("SubscriptionKeyNotFound", 401), ("SubscriptionKeyInvalid", 401),
            ("Timeout", 504), ("OperationNotFound", 404),
        ):
            self.assertRegex(expression, rf'"{reason}".*?{code}')
        self.assertNotIn("LastError.Message", ET.tostring(policy, encoding="unicode"))

    def test_https_subscription_header_and_exact_post_operations(self):
        api = self.resource("apim-baseline", "Microsoft.ApiManagement/service/apis")
        self.assertEqual(api["properties"]["protocols"], ["https"])
        self.assertEqual(api["properties"]["path"], "otlp")
        self.assertTrue(api["properties"]["subscriptionRequired"])
        self.assertEqual(api["properties"]["subscriptionKeyParameterNames"],
                         {"header": "X-Copilot-Telemetry-Key", "query": "subscription-key"})
        template = self.module("apim-operations")["properties"]["template"]
        self.assertEqual(template["variables"]["signals"], ["logs", "traces", "metrics"])
        operation = self.resource("apim-operations", "Microsoft.ApiManagement/service/apis/operations")
        self.assertEqual(operation["properties"]["method"], "POST")
        self.assertEqual(operation["properties"]["urlTemplate"],
                         "[format('/v1/{0}', variables('signals')[copyIndex()])]")
        self.assertNotIn("serviceUrl", api["properties"])

    def test_api_allowlist_precedes_subscription_limits(self):
        policy = self.policy("apim-api.xml")
        inbound = list(policy.find("inbound"))
        self.assertIsNone(policy.find("inbound/base"), "Final API deliberately replaces inherited deny")
        self.assertEqual(inbound[0].tag, "choose")
        condition = inbound[0].find("when").get("condition")
        self.assertIn("context.Subscription == null", condition)
        self.assertIn('context.Subscription.Id != "__SUBSCRIPTION_ID__"', condition)
        self.assertEqual(inbound[0].find(".//set-status").get("code"), "403")
        self.assertIsNotNone(policy.find("inbound/rate-limit"))
        quota = policy.find("inbound/quota-by-key")
        self.assertEqual(quota.get("counter-key"), '@("copilot-otel-apim:" + context.Subscription.Id)')
        self.assertIsNone(policy.find(".//quota"), "Product-only quota permits API-scoped bypass")
        self.assertIsNone(policy.find("backend/forward-request"))

    def test_actual_binary_bound_query_and_encoding_rejection(self):
        policy = self.policy("apim-api.xml")
        conditions = [node.get("condition") for node in policy.findall("inbound/choose/when")]
        self.assertTrue(any("OriginalUrl.QueryString" in text for text in conditions))
        self.assertTrue(any("Content-Encoding" in text and "identity" in text for text in conditions))
        size = next(n for n in policy.findall("inbound/set-variable") if n.get("name") == "bodyBytes")
        self.assertIn("Body.As<byte[]>(preserveContent: true).Length", size.get("value"))
        self.assertTrue(any('["bodyBytes"] > 4194304' in text for text in conditions))
        statuses = {node.get("code") for node in policy.findall("inbound/choose/when/return-response/set-status")}
        self.assertTrue({"400", "403", "411", "413", "415"}.issubset(statuses))

    def test_declared_length_and_framing_are_checked_before_body_access(self):
        inbound = list(self.policy("apim-api.xml").find("inbound"))
        body_index = next(index for index, node in enumerate(inbound) if node.get("name") == "bodyBytes")
        before_body = inbound[:body_index]
        conditions = [(node.get("condition"), node.find("return-response/set-status").get("code"))
                      for block in before_body if block.tag == "choose"
                      for node in block.findall("when")]
        self.assertTrue(any('ContainsKey("Transfer-Encoding")' in condition and code == "400"
                            for condition, code in conditions), "Visible transfer framing must be denied")
        self.assertTrue(any('!context.Request.Headers.ContainsKey("Content-Length")' in condition
                            and code == "411" for condition, code in conditions))
        self.assertTrue(any("Regex.IsMatch" in condition and code == "400" for condition, code in conditions))
        self.assertTrue(any("4194304" in condition and "Length > 7" in condition and code == "413"
                            for condition, code in conditions), "Declared oversize must fail before buffering")
        after_body = inbound[body_index + 1:]
        mismatch = next((node for block in after_body if block.tag == "choose"
                         for node in block.findall("when") if "!=" in node.get("condition")), None)
        self.assertIsNotNone(mismatch, "Actual bytes must match the declared length")
        self.assertIn('["bodyBytes"]', mismatch.get("condition"))
        self.assertIn('["declaredBytes"]', mismatch.get("condition"))
        self.assertEqual(mismatch.find("return-response/set-status").get("code"), "400")

    def test_canonical_length_pattern_rejects_ambiguous_header_forms(self):
        conditions = [node.get("condition") for node in self.policy("apim-api.xml").findall("inbound/choose/when")]
        condition = next((text for text in conditions if "Regex.IsMatch" in text), None)
        self.assertIsNotNone(condition, "Canonical Content-Length validation is required")
        pattern = re.search(r'@"([^"]+)"', condition)[1].replace(r"\z", r"\Z")
        for text in ("0", "1", "4194304", "4194305", "999999999999999999999999"):
            with self.subTest(canonical=text):
                self.assertIsNotNone(re.fullmatch(pattern, text))
        for text in ("", "00", "01", "+1", "-1", "1.0", "1e2", " 1", "1 ", "1,1", "1, 1", "1\n"):
            with self.subTest(malformed=text):
                self.assertIsNone(re.fullmatch(pattern, text))

    def test_public_media_type_allowlist_is_protobuf_only(self):
        policy = self.policy("apim-api.xml")
        checks = policy.findall("inbound/choose/when")
        media = next(node for node in checks if 'GetValueOrDefault("Content-Type"' in node.get("condition"))
        self.assertIn('return contentType != "application/x-protobuf";', media.get("condition"))
        self.assertNotIn("application/json", media.get("condition"))
        self.assertEqual(media.find("return-response/set-status").get("code"), "415")
        self.assertEqual(media.find("return-response/set-body").text, "OTLP protobuf is required.")

    def test_empty_declared_or_actual_body_is_denied_before_upstream_authentication(self):
        inbound = list(self.policy("apim-api.xml").find("inbound"))
        body_index = next(index for index, node in enumerate(inbound) if node.get("name") == "bodyBytes")
        for blocks, condition in (
            (inbound[:body_index], '@((string)context.Variables["declaredLength"] == "0")'),
            (inbound[body_index + 1:], '@((int)context.Variables["bodyBytes"] == 0)'),
        ):
            check = next((node for block in blocks if block.tag == "choose"
                          for node in block.findall("when") if node.get("condition") == condition), None)
            self.assertIsNotNone(check, "Both declared and actual empty payloads require explicit denial")
            self.assertEqual(check.find("return-response/set-status").get("code"), "400")
            self.assertEqual(check.find("return-response/set-body").text, "OTLP body must not be empty.")
        operation = list(self.policy("apim-operation.xml").find("inbound"))
        self.assertEqual(operation[0].tag, "base", "API validation must precede operation MI authentication")
        self.assertEqual(operation[-1].tag, "authentication-managed-identity")
        self.assertIsNone(self.policy("apim-api.xml").find(".//authentication-managed-identity"))

    def test_credentials_removed_before_fixed_mi_target(self):
        policy = self.policy("apim-operation.xml")
        inbound = list(policy.find("inbound"))
        self.assertEqual(inbound[0].tag, "base")
        stripped = {node.get("name").lower() for node in policy.findall("inbound/set-header")
                    if node.get("exists-action") == "delete"}
        self.assertTrue({"x-copilot-telemetry-key", "ocp-apim-subscription-key",
                         "authorization", "cookie", "proxy-authorization"}.issubset(stripped))
        query = {n.get("name").lower() for n in policy.findall("inbound/set-query-parameter")}
        self.assertTrue({"subscription-key", "x-copilot-telemetry-key", "ocp-apim-subscription-key"}.issubset(query))
        mi = policy.find("inbound/authentication-managed-identity")
        self.assertEqual(mi.get("resource"), "https://monitor.azure.com/")
        self.assertEqual(mi.get("ignore-error"), "false")
        self.assertNotIn("output-token-variable-name", mi.attrib)
        self.assertEqual(inbound[-1].tag, "authentication-managed-identity")
        rewrite = policy.find("inbound/rewrite-uri")
        self.assertEqual(rewrite.get("copy-unmatched-params"), "false")
        self.assertIn("AbsolutePath", rewrite.get("template"))
        target = policy.find("inbound/set-backend-service").get("base-url")
        self.assertIn("GetLeftPart", target)
        self.assertIn("__ENDPOINT_BASE64__", target)
        compiled = json.dumps(self.module("apim-operations")["properties"]["template"])
        self.assertIn("base64(", compiled)
        for signal in ("logs", "traces", "metrics"):
            self.assertIn(f"{signal}Endpoint", compiled)

    def test_server_endpoint_guard_precedes_mi_and_overrides_client_host(self):
        policy = self.policy("apim-operation.xml")
        condition = policy.find("inbound/choose/when")
        self.assertIsNotNone(condition, "Endpoint provenance preflight also needs a runtime origin guard")
        self.assertIn("Regex.IsMatch", condition.get("condition"))
        self.assertIn("ingest", condition.get("condition"))
        self.assertEqual(condition.find("return-response/set-status").get("code"), "502")
        host = policy.find("inbound/set-header[@name='Host']")
        self.assertIsNotNone(host)
        self.assertEqual(host.get("exists-action"), "override")
        self.assertIn(".Host", host.find("value").text)

    def test_compiled_policy_payloads_are_the_reviewed_xml(self):
        for module, filename in (
            ("apim-service", "apim-deny.xml"), ("apim-baseline", "apim-deny.xml"),
            ("apim-operations", "apim-operation.xml"), ("apim-activate", "apim-api.xml"),
        ):
            expected = (ROOT / "infra/policies" / filename).read_text()
            variables = self.module(module)["properties"]["template"]["variables"]
            self.assertIn(expected, variables.values())
            ET.fromstring(next(value for value in variables.values() if value == expected))

    def test_forward_is_single_no_redirect_and_response_is_sanitized(self):
        policy = self.policy("apim-operation.xml")
        forward = policy.find("backend/forward-request")
        self.assertEqual(forward.get("timeout"), "30")
        self.assertEqual(forward.get("follow-redirects"), "false")
        self.assertEqual(forward.get("buffer-request-body"), "false")
        self.assertIsNone(policy.find(".//retry"))
        for section in ("outbound", "on-error"):
            xml = ET.tostring(policy.find(section), encoding="unicode")
            self.assertNotIn("LastError.Message", xml)
            self.assertNotIn("Response.Body", xml)
        outbound = self.policy("apim-api.xml").find("outbound")
        stripped = {n.get("name").lower() for n in outbound.findall("set-header")}
        self.assertTrue({"location", "www-authenticate", "authorization", "set-cookie"}.issubset(stripped))
        self.assertIn("context.Response.StatusCode >= 300", outbound.find("choose/when").get("condition"))
        self.assertEqual(outbound.find("choose/when/return-response/set-status").get("code"), "502")
        for filename in ("apim-api.xml", "apim-operation.xml"):
            xml = self.policy(filename)
            self.assertIsNone(xml.find(".//trace"))
            self.assertIsNone(xml.find(".//log-to-eventhub"))

    def test_exact_external_publisher_and_nonsecret_receipt(self):
        publisher = self.resource("apim-publisher", "Microsoft.Authorization/roleAssignments")
        self.assertIn("Microsoft.Insights/dataCollectionRules", publisher["scope"])
        self.assertIn("3913510d-42f4-4e42-8a64-420c390055eb",
                      publisher["properties"]["roleDefinitionId"])
        self.assertEqual(len(self.resources("apim-publisher")), 1)
        state = self.compiled()["outputs"]["apimState"]["value"]
        self.assertTrue({"subscription_id", "resource_group", "location", "ownership_marker",
                         "apim_resource_id", "api_resource_id", "api_subscription_resource_id",
                         "publisher_role_assignment_id", "dcr_resource_id", "principal_id",
                         "logs_endpoint", "traces_endpoint", "metrics_endpoint"}.issubset(state))
        self.assertNotIn("subscription_resource_ids", state)
        for field in state:
            self.assertNotIn("key", field.lower())
        serialized = json.dumps(self.compiled()).lower()
        self.assertNotIn("listsecrets", serialized)
        self.assertNotIn("primarykey", serialized)
        self.assertNotIn("secondarykey", serialized)

    def test_publisher_role_name_is_known_before_managed_identity_creation(self):
        publisher = self.resource("apim-publisher", "Microsoft.Authorization/roleAssignments")
        self.assertNotIn("principalId", publisher["name"])
        self.assertEqual(publisher["name"],
                         "[guid(resourceId('Microsoft.Insights/dataCollectionRules', parameters('dcrName')), "
                         "parameters('apimResourceId'), 'copilot-otel-apim-publisher')]")
        self.assertEqual(publisher["scope"],
                         "[resourceId('Microsoft.Insights/dataCollectionRules', parameters('dcrName'))]")
        self.assertEqual(publisher["properties"]["principalId"], "[parameters('principalId')]")
        module_parameters = self.module("apim-publisher")["properties"]["parameters"]
        self.assertEqual(module_parameters["apimResourceId"]["value"], "[variables('apimResourceId')]")
        self.assertIn("reference(", module_parameters["principalId"]["value"])
        variables = self.compiled()["variables"]
        for name in ("apimResourceId", "apimName"):
            self.assertNotIn("reference(", variables[name])
        self.assertIn("resourceId(subscription().subscriptionId", variables["apimResourceId"])
        self.assertEqual(self.module("apim-service")["properties"]["parameters"]["apimName"]["value"],
                         "[variables('apimName')]")
        self.assertEqual(self.resource("apim-service", "Microsoft.ApiManagement/service")["name"],
                         "[parameters('apimName')]")


if __name__ == "__main__":
    unittest.main()
