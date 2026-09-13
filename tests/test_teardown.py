"""Teardown must never delete a group based on its name alone."""

import importlib

from scripts import common
from tests.test_deploy import APP, GROUP, GROUP_ID, LAW, SUB, LifecycleCase, state


class TeardownTests(LifecycleCase):
    def setUp(self):
        super().setUp()
        self.assertTrue((common.ROOT / "scripts/teardown.py").exists(), "Missing teardown implementation")
        self.teardown = importlib.import_module("scripts.teardown")
        self.save_state()
        common.write_private(self.local / "collector.env", "keep-private\n")
        common.write_private(self.local / "unrelated.txt", "keep\n")

    def args(self):
        return ("--subscription", SUB, "--resource-group", GROUP, "--confirm")

    def test_custom_table_omitted_by_generic_inventory_blocks_delete(self):
        self.azure.rest_responses[
            f"https://management.azure.com{LAW}/tables?api-version=2023-09-01"
        ] = {"value": [{"id": LAW + "/tables/Unrelated_CL", "name": "Unrelated_CL",
                       "properties": {"schema": {"name": "Unrelated_CL", "tableType": "CustomLog"}}}]}
        code, output = self.invoke(self.teardown, *self.args())
        self.assertNotEqual(code, 0, output)
        self.assertIn("Automated deletion is disabled", output)
        self.assertFalse(any(a[:2] == ["az", "rest"] for a in self.azure.calls))
        self.assertEqual(self.mutations(), [])

    def test_even_clean_confirmed_group_refuses_automated_delete(self):
        before = {p.name: p.read_bytes() for p in self.local.iterdir()}
        code, output = self.invoke(self.teardown, *self.args())
        self.assertNotEqual(code, 0, output)
        self.assertIn("automated deletion", output.lower())
        self.assertEqual(self.mutations(), [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.local.iterdir()})
        self.assertFalse(any(a[:2] == ["az", "rest"] for a in self.azure.calls))

    def test_requires_confirmation_and_exact_explicit_scope(self):
        for args in [(), ("--subscription", SUB, "--resource-group", GROUP),
                     ("--subscription", SUB, "--confirm"),
                     ("--subscription", SUB, "--resource-group", "other", "--confirm")]:
            self.assertNotEqual(self.invoke(self.teardown, *args)[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_refuses_missing_or_tampered_receipt(self):
        common.AZURE_STATE.unlink()
        self.assertNotEqual(self.invoke(self.teardown, *self.args())[0], 0)
        data = state()
        data["application_insights_resource_id"] = APP.replace(GROUP, "another-group")
        common.write_json(common.AZURE_STATE, data)
        self.assertNotEqual(self.invoke(self.teardown, *self.args())[0], 0)
        self.assertEqual(self.mutations(), [])

    def test_refuses_unowned_group_resource_or_unrelated_inventory(self):
        for attr, value in [
            ("group_tags", {}), ("resource_tags", {}),
            ("extra", [{"id": GROUP_ID + "/providers/Microsoft.Storage/storageAccounts/extra",
                        "type": "Microsoft.Storage/storageAccounts", "tags": self.azure.tags()}]),
            ("extra", [{"id": LAW + "/tables/Custom", "type": "Microsoft.OperationalInsights/workspaces/tables",
                        "tags": self.azure.tags()}]),
        ]:
            with self.subTest(attr=attr):
                old = getattr(self.azure, attr)
                setattr(self.azure, attr, value)
                self.assertNotEqual(self.invoke(self.teardown, *self.args())[0], 0)
                setattr(self.azure, attr, old)
        self.assertEqual(self.mutations(), [])

    def test_missing_group_is_verified_idempotent_noop(self):
        self.azure.exists = False
        code, output = self.invoke(self.teardown, *self.args())
        self.assertEqual(code, 0, output)
        self.assertIn("absent", output.lower())
        self.assertEqual(self.mutations(), [])

    def test_unavailable_child_apis_cannot_change_unconditional_refusal(self):
        self.azure.fail = (lambda a: a[:2] == ["az", "rest"],
                           "AuthorizationFailed ConnectionString=never-print")
        code, output = self.invoke(self.teardown, *self.args())
        self.assertNotEqual(code, 0)
        self.assertIn("Automated deletion is disabled", output)
        self.assertNotIn("never-print", output)
        self.assertTrue(common.AZURE_STATE.exists())
        self.assertEqual(self.mutations(), [])
        self.assertFalse(any(a[:2] == ["az", "rest"] for a in self.azure.calls))

    def test_corrupt_non_utf8_receipt_fails_cleanly(self):
        common.AZURE_STATE.write_bytes(b"\xff\xfe")
        code, output = self.invoke(self.teardown, *self.args())
        self.assertNotEqual(code, 0, output)
        self.assertEqual(self.azure.calls, [])
