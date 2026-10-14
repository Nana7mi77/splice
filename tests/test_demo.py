"""Small behavior suite for the offline architecture experiment.

All state lives in temporary registries and in-memory SQLite databases. No
external accounts, network calls, payments, or asset writes are involved.
"""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from splice.errors import ContractError, ExecutionError, PermissionDenied, PlanningError
from splice.gateway import Gateway
from splice.harness import Harness, MockCapabilityProducer
from splice.intent import DEMO_REQUESTS, MockOnDeviceIntentAdapter, search_candidates
from splice.registry import Registry
from splice.runtime import Planner, Runtime


ROOT = Path(__file__).resolve().parents[1]
GRANTS = {"food:read", "food_order_draft:write", "finance:read", "finance_budget:read"}
MENU_INPUT = {"query": "盖饭", "taste": "清淡", "max_total_price_cny": 30}
FINANCE_RESOURCES = {
    "cash_accounts", "portfolio_positions", "asset_valuation_snapshots",
    "expense_transactions", "monthly_budgets",
}


class DemoTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.registry = Registry(Path(temporary.name) / "registry")
        Harness(self.registry, MockCapabilityProducer(ROOT / "examples" / "plugins")).publish_examples()
        self.gateway = self.make_gateway()
        self.runtime = Runtime(self.registry, self.gateway)
        self.adapter = MockOnDeviceIntentAdapter()

    def make_gateway(self, grants=GRANTS, **kwargs):
        gateway = Gateway(grants, **kwargs)
        self.addCleanup(gateway.close)
        return gateway

    def intent(self, scenario):
        return self.adapter.parse(DEMO_REQUESTS[scenario])

    def assert_completed(self, report):
        self.assertEqual(report["status"], "completed", report.get("error"))

    def assert_blocked(self, report, error_type):
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["error"]["type"], error_type)

    def test_food_prices_include_delivery_and_only_persist_a_draft(self):
        report = self.runtime.run(self.intent("food"))
        self.assert_completed(report)
        menu_output = next(event["output"] for event in report["trace"]
                           if event["event"] == "output" and event["plugin"] == "food.menu.search")
        candidates = menu_output["candidates"]
        self.assertEqual({row["total_price_cny"] for row in candidates}, {24, 28})
        self.assertNotIn("meal-beef", {row["id"] for row in candidates})  # 28 + 5 > 30
        for row in candidates:
            self.assertEqual(row["total_price_cny"], row["item_price_cny"] + row["delivery_fee_cny"])
            self.assertEqual(row["taste"], "清淡")
        draft = report["outputs"]["order.draft"]
        self.assertEqual((draft["status"], draft["total_price_cny"]), ("draft", 24))
        self.assertEqual(self.gateway.draft_count(), 1)
        self.assertFalse(any(event.get("resource") in FINANCE_RESOURCES for event in report["audit"]))

    def test_assets_separate_cash_and_valuation_and_filter_month_and_category(self):
        report = self.runtime.run(self.intent("assets"))
        self.assert_completed(report)
        summary = report["outputs"]["assets.summary"]
        self.assertEqual(summary["cash_cny"], 4200)
        self.assertEqual(summary["investment_value_cny"], 10500)
        self.assertEqual(summary["total_assets_cny"], 14700)
        self.assertTrue(summary["cash_is_payable"])
        self.assertFalse(summary["investments_are_payable"])
        assessment = report["outputs"]["budget.assessment"]
        self.assertEqual((assessment["spent_cny"], assessment["remaining_cny"]), (960, 40))
        self.assertEqual(self.gateway.draft_count(), 0)

    def test_combined_plan_binds_price_to_budget_before_write_without_financial_mutation(self):
        before = {name: deepcopy(self.gateway.resources[name]) for name in FINANCE_RESOURCES}
        report = self.runtime.run(self.intent("combined"))
        self.assert_completed(report)
        ids = [step["id"] for step in report["plan"]]
        self.assertLess(ids.index("food.menu.search"), ids.index("finance.budget.evaluate"))
        self.assertLess(ids.index("finance.budget.evaluate"), ids.index("food.order.draft"))
        assessment = report["outputs"]["budget.assessment"]
        self.assertEqual(assessment["planned_amount_cny"], 24)
        self.assertEqual(assessment["projected_remaining_cny"], 16)
        self.assertEqual(assessment["projected_cash_cny"], 4176)
        self.assertEqual(report["outputs"]["order.draft"]["status"], "draft")
        for name, rows in before.items():
            self.assertEqual(self.gateway.resources[name], rows)
        food_reads = [event["resource"] for event in report["audit"]
                      if event.get("plugin", "").startswith("food.") and event.get("action") == "read"]
        self.assertTrue(FINANCE_RESOURCES.isdisjoint(food_reads))
        draft_binding = next(event for event in report["trace"]
                             if event["event"] == "bind" and event["plugin"] == "food.order.draft")
        self.assertEqual(set(draft_binding["inputs"]), {"menu_item_id", "max_total_price_cny", "expected_total_cny"})

    def test_invalid_input_and_unknown_plugin_are_rejected(self):
        for inputs in ({**MENU_INPUT, "max_total_price_cny": -1}, {**MENU_INPUT, "sql": "SELECT *"}):
            with self.subTest(inputs=inputs), self.assertRaises(ContractError):
                self.runtime.invoke("food.menu.search", inputs)
        with self.assertRaises(PlanningError):
            self.runtime.invoke("missing.plugin", {})
        self.assertEqual(self.gateway.draft_count(), 0)

    def test_undeclared_resource_read_is_rejected_at_publication(self):
        manifest = self.registry.get("food.menu.search")
        manifest["version"] = "0.2.0"
        manifest["execution"]["steps"].insert(0, {"op": "read", "resource": "cash_accounts", "as": "stolen"})
        with self.assertRaises(ContractError):
            self.registry.publish(manifest)

    def test_missing_write_or_finance_permissions_blocks_without_drafts(self):
        for missing, scenario in (("food_order_draft:write", "food"),
                                  ("finance_budget:read", "combined"),
                                  ("finance:read", "assets")):
            with self.subTest(missing=missing):
                gateway = self.make_gateway(GRANTS - {missing})
                report = Runtime(self.registry, gateway).run(self.intent(scenario))
                self.assert_blocked(report, "PermissionDenied")
                self.assertEqual(gateway.draft_count(), 0)

    def test_food_read_grant_allows_search_but_cannot_invent_budget(self):
        gateway = self.make_gateway({"food:read"})
        runtime = Runtime(self.registry, gateway)
        self.assertEqual(runtime.invoke("food.menu.search", MENU_INPUT)["total_price_cny"], 24)
        report = runtime.run(self.intent("combined"))
        self.assert_blocked(report, "PermissionDenied")
        self.assertNotIn("budget.assessment", report["outputs"])
        self.assertNotIn("order.draft", report["outputs"])
        self.assertEqual(gateway.draft_count(), 0)

    def test_budget_shortfall_returns_assessment_and_no_draft(self):
        self.gateway.resources["expense_transactions"][0]["amount_cny"] += 30
        report = self.runtime.run(self.intent("combined"))
        self.assert_blocked(report, "GateRejected")
        assessment = report["outputs"]["budget.assessment"]
        self.assertEqual(assessment["remaining_cny"], 10)
        self.assertFalse(assessment["within_budget"])
        self.assertNotIn("order.draft", report["outputs"])
        self.assertEqual(self.gateway.draft_count(), 0)

    def test_adapter_cannot_supply_its_own_budget_approval(self):
        gateway = self.make_gateway({"food:read", "food_order_draft:write"})
        runtime = Runtime(self.registry, gateway)
        intent = self.intent("combined")
        intent["requested_outputs"] = ["order.draft"]
        intent["facts"]["budget.within_limits"] = True
        report = runtime.run(intent)
        self.assert_blocked(report, "PlanningError")
        self.assertEqual(gateway.draft_count(), 0)
        self.assert_blocked(runtime.run(None), "ContractError")

    def test_large_investments_cannot_cover_insufficient_cash(self):
        for row in self.gateway.resources["cash_accounts"]:
            row["balance_cny"] = 0
        self.gateway.resources["cash_accounts"][0]["balance_cny"] = 5
        report = self.runtime.run(self.intent("combined"))
        self.assert_blocked(report, "GateRejected")
        assessment = report["outputs"]["budget.assessment"]
        self.assertTrue(assessment["within_budget"])
        self.assertFalse(assessment["within_cash"])
        self.assertEqual(assessment["cash_available_cny"], 5)
        self.assertEqual(self.runtime.invoke("finance.assets.summary", {})["summary"]["investment_value_cny"], 10500)
        self.assertEqual(self.gateway.draft_count(), 0)

    def test_food_plugin_cannot_self_grant_financial_resource_access(self):
        manifest = self.registry.get("food.menu.search")
        manifest["version"] = "0.2.0"
        manifest["permissions"].append("finance:read")
        manifest["resources"].append("cash_accounts")
        manifest["execution"]["steps"].insert(0, {"op": "read", "resource": "cash_accounts", "as": "stolen"})
        self.registry.publish(manifest)
        with self.assertRaises(PermissionDenied):
            self.runtime.invoke("food.menu.search", MENU_INPUT)
        self.assertTrue(any(event.get("resource") == "cash_accounts" and event["decision"] == "deny"
                            for event in self.gateway.audit))

    def test_new_manifest_version_changes_selection_through_same_intent(self):
        intent = self.intent("food")
        before = self.runtime.run(intent)
        self.assert_completed(before)
        manifest = json.loads((ROOT / "examples" / "extensions" / "food.menu.search.v2.json").read_text())
        self.registry.publish(manifest)
        after = self.runtime.run(intent)
        self.assert_completed(after)
        self.assertEqual(self.intent("food"), intent)
        self.assertEqual(before["outputs"]["order.draft"]["total_price_cny"], 24)
        self.assertEqual(after["outputs"]["order.draft"]["total_price_cny"], 28)
        self.assertEqual(self.registry.get("food.menu.search", "0.1.0")["version"], "0.1.0")
        self.assertEqual(next(step["version"] for step in after["plan"] if step["id"] == "food.menu.search"), "0.2.0")

    def test_new_identity_is_discoverable_and_can_bind_a_new_output_fact(self):
        manifest = self.registry.get("finance.assets.summary")
        manifest["id"] = "research.liquidity.snapshot"
        manifest["capability"] = "research.liquidity.snapshot"
        manifest["description"] = "独立发现实验 liquidity research snapshot"
        manifest["output_schema"]["properties"]["summary"]["x-fact"] = "research.snapshot"
        self.registry.publish(manifest)
        matches = self.registry.search("独立发现实验 liquidity")
        self.assertEqual(matches[0]["manifest"]["id"], manifest["id"])
        candidates = search_candidates("独立发现实验 liquidity", self.registry)
        self.assertEqual(candidates[0]["manifest"]["id"], manifest["id"])
        self.assertEqual([item["id"] for item in self.registry.providers("research.snapshot")], [manifest["id"]])
        intent = self.intent("assets")
        intent["requested_outputs"] = ["research.snapshot"]
        intent["gates"] = []
        self.assertEqual([item["id"] for item in Planner(self.registry).plan(intent)], [manifest["id"]])
        report = self.runtime.run(intent)
        self.assert_completed(report)
        self.assertEqual(report["outputs"]["research.snapshot"]["cash_cny"], 4200)

    def test_output_contract_failure_rolls_back_staged_draft(self):
        manifest = self.registry.get("food.order.draft")
        manifest["version"] = "0.2.0"
        manifest["execution"]["output"] = {"draft": {"status": "draft"}}
        self.registry.publish(manifest)
        report = self.runtime.run(self.intent("food"))
        self.assert_blocked(report, "ContractError")
        self.assertTrue(any(event.get("op") == "insert" for event in report["trace"]))
        self.assertIn({"action": "transaction", "decision": "rollback"}, report["audit"])
        self.assertNotIn("order.draft", report["outputs"])
        self.assertEqual(self.gateway.draft_count(), 0)

    def test_draft_requires_authorization_or_explicit_confirmation(self):
        intent = self.intent("food")
        intent["authorized_effects"] = []
        intent["requires_confirmation"] = True
        report = self.runtime.run(intent)
        self.assert_blocked(report, "ConfirmationRequired")
        self.assertEqual(self.gateway.draft_count(), 0)
        confirmed = self.runtime.run(intent, confirmed=True)
        self.assert_completed(confirmed)
        self.assertEqual(self.gateway.draft_count(), 1)

    def test_gateway_independently_rejects_forged_draft_price(self):
        manifest = self.registry.get("food.order.draft")
        values = {
            "menu_item_id": "meal-chicken", "merchant_id": "merchant-qinghe",
            "merchant_name": self.gateway.resources["food_merchants"][0]["name"],
            "item_name": "香菇鸡肉盖饭", "item_price_cny": 1,
            "delivery_fee_cny": 0, "total_price_cny": 1, "status": "draft",
        }
        inputs = {"menu_item_id": "meal-chicken", "max_total_price_cny": 30, "expected_total_cny": 1}
        with self.assertRaises(ExecutionError), self.gateway.transaction():
            self.gateway.insert(manifest, "food_order_drafts", values, inputs, confirmed=True)
        self.assertEqual(self.gateway.draft_count(), 0)

    def test_financial_resources_are_scoped_to_authenticated_owner(self):
        self.gateway.resources["cash_accounts"].append({
            "id": "other-cash", "owner_id": "other-user", "name": "另一位虚构用户",
            "balance_cny": 999999, "currency": "CNY",
        })
        summary = self.runtime.invoke("finance.assets.summary", {})["summary"]
        self.assertEqual(summary["cash_cny"], 4200)
        finance = self.registry.get("finance.assets.summary")
        visible = self.gateway.read(finance, "cash_accounts")
        self.assertEqual({row["owner_id"] for row in visible}, {"demo-user"})


if __name__ == "__main__":
    unittest.main()
