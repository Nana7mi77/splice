"""Protocol boundary tests with synthetic producers and model outputs.

These fixtures exercise verification, never constitute evidence of a real LLM
expansion, and cannot be loaded by the production bootstrap. No network, model
download, inference, or real credential is used by this suite.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from splice.cloud import CloudError, DeepSeekClient
from splice.config import ConfigStore
from splice.errors import ContractError, PlanningError
from splice.expansion import ExpansionManager
from splice.harness import Harness, LLMCapabilityProducer, MockCapabilityProducer
from splice.intent import DEMO_REQUESTS, MockOnDeviceIntentAdapter
from splice.learning import LearningStore, manifest_hash, validate_pack
from splice.local_intent import LocalIntentError, LocalOnDeviceIntentAdapter
from splice.registry import Registry


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "examples" / "experiments" / "budget-reserve.json"
TEST_CREDENTIAL = "unit-test-credential-never-valid-at-a-provider"
MESSAGES = [{"role": "user", "content": 'Return {"ready":true} as JSON.'}]


def provider_response(content='{"ready":true}', **overrides):
    response = {
        "id": "test-response-1",
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
    }
    response.update(overrides)
    return json.dumps(response)


def synthetic_bundle(experiment):
    """An explicitly handwritten test double; not an example implementation."""
    target = experiment["target_contract"]
    manifest = {
        "id": "test.reserve.guard", "version": "0.1.0", "domain": "finance",
        "description": "Synthetic test-only budget threshold guard",
        "examples": ["Test a parameterized budget threshold"],
        "capability": "test.reserve.guard",
        "permissions": ["finance_budget:read"], "resources": ["monthly_budgets"],
        "preconditions": [], "effects": [],
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "assessment": deepcopy(target["input_facts"]["budget.assessment"]),
                "minimum": deepcopy(target["input_facts"]["budget.min_remaining_cny"]),
            },
            "required": ["assessment", "minimum"],
        },
        "output_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"allowed": deepcopy(target["output_facts"]["budget.reserve_preserved"])},
            "required": ["allowed"],
        },
        "execution": {
            "steps": [{"op": "compare", "as": "allowed", "value": {
                "op": "gte", "args": ["$input.assessment.projected_remaining_cny", "$input.minimum"],
            }}],
            "output": {"allowed": "$allowed"},
        },
    }
    examples = []
    wordings = (
        "清淡盖饭含配送最多30元，只留草稿，吃完本月餐饮预算至少保留{minimum}元。",
        "帮找清淡盖饭，算上配送费不要超过30元；只生成草稿，本月餐饮结余不能低于{minimum}元。",
        "预算里还想留{minimum}元，先查本月预算和现金，再选含配送费至多30元的清淡盖饭做草稿。",
    )
    for teaching_case, wording in zip(target["teaching_cases"], wordings):
        minimum = teaching_case["facts"]["budget.min_remaining_cny"]
        utterance = wording.format(minimum=minimum)
        intent = deepcopy(experiment["base_intent"])
        intent["utterance"] = utterance
        intent["facts"] = deepcopy(teaching_case["facts"])
        intent["requested_outputs"] = list(teaching_case["requested_outputs"])
        examples.append({"utterance": utterance, "intent": intent})
    pack = {
        "schema_version": "1.0", "plugin_id": manifest["id"], "plugin_version": "0.1.0",
        "manifest_sha256": manifest_hash(manifest), "domain": "finance",
        "description": "餐饮消费后保留预算的测试教学资料", "keywords": ["预算", "保留", "至少"],
        "examples": examples,
        "negative_examples": [
            {"utterance": "直接支付订单", "reason": "Real payments are unsupported"},
            {"utterance": "显示其他用户账户", "reason": "Private accounts are inaccessible"},
        ],
        "fact_dependencies": {"budget.reserve_preserved": ["budget.assessment", "budget.min_remaining_cny"]},
        "required_permissions": list(manifest["permissions"]),
        "supported_manifest_versions": ["0.1.0"], "adaptation_mode": "dynamic_context",
    }
    assessment = {
        "month": "2026-10", "budget_limit_cny": 1000, "spent_cny": 960,
        "remaining_cny": 40, "planned_amount_cny": 24, "projected_remaining_cny": 16,
        "cash_available_cny": 4200, "projected_cash_cny": 4176,
        "within_budget": True, "within_cash": True,
    }
    return {
        "manifest": manifest, "rationale": "Test double, not model-generated evidence", "learning_pack": pack,
        "behavior_examples": [
            {"input": {"assessment": deepcopy(assessment), "minimum": minimum},
             "expected_output": {"allowed": minimum <= 16}} for minimum in (15, 20, 50)
        ],
    }


class ConfigAndCloudTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = ConfigStore(Path(temporary.name) / "private")
        self.store.set_api_key(TEST_CREDENTIAL)

    def test_configuration_hides_credentials_and_restricts_file_permissions(self):
        self.assertEqual(self.store.get_api_key(), TEST_CREDENTIAL)
        self.assertEqual(self.store.show()["credentials"]["deepseek"], "configured")
        self.assertNotIn(TEST_CREDENTIAL, json.dumps(self.store.show()))
        self.assertNotIn(TEST_CREDENTIAL, self.store.settings_path.read_text())
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.credentials_path.stat().st_mode & 0o777, 0o600)
        self.store.clear_api_key()
        self.assertEqual(self.store.show()["credentials"]["deepseek"], "missing")

    def test_settings_reject_secret_fields_and_unapproved_provider(self):
        for values in ({"api_key": TEST_CREDENTIAL}, {"base_url": "https://unapproved.example"}):
            with self.subTest(values=values), self.assertRaises(ContractError):
                self.store.update("deepseek", values)
        self.assertEqual(self.store.load()["deepseek"]["base_url"], "https://api.deepseek.com")

    def test_offline_fails_before_configuration_credentials_or_transport(self):
        store = Mock()
        client = DeepSeekClient(store, offline=True)
        with patch("splice.cloud._post_with_deadline") as transport, self.assertRaises(CloudError):
            client.complete_json(MESSAGES)
        transport.assert_not_called()
        store.load.assert_not_called()
        store.get_api_key.assert_not_called()
        self.assertEqual(client.metadata, [])

    def test_failed_attempt_consumes_budget_without_retry_or_secret_echo(self):
        self.store.update("deepseek", {"max_calls": 1})
        client = DeepSeekClient(self.store)
        with patch("splice.cloud._post_with_deadline", side_effect=OSError(TEST_CREDENTIAL)) as transport:
            with self.assertRaises(CloudError) as failed:
                client.complete_json(MESSAGES)
            with self.assertRaises(CloudError):
                client.complete_json(MESSAGES)
        self.assertEqual(transport.call_count, 1)
        self.assertNotIn(TEST_CREDENTIAL, str(failed.exception))
        self.assertNotIn(TEST_CREDENTIAL, json.dumps(client.metadata))
        self.assertEqual(client.metadata[0]["status"], "failed")

    def test_credential_echo_and_duplicate_json_keys_are_rejected(self):
        for content in (json.dumps({"echo": TEST_CREDENTIAL}), '{"ready":true,"ready":false}'):
            with self.subTest(content=content):
                client = DeepSeekClient(self.store)
                with patch("splice.cloud._post_with_deadline", return_value=provider_response(content)):
                    with self.assertRaises(CloudError) as failed:
                        client.complete_json(MESSAGES)
                self.assertNotIn(TEST_CREDENTIAL, str(failed.exception))
                self.assertNotIn(TEST_CREDENTIAL, json.dumps(client.metadata))
                self.assertEqual(client.metadata[0]["status"], "failed")

    def test_response_metadata_is_filtered_and_is_a_copy(self):
        client = DeepSeekClient(self.store)
        response = provider_response(id=TEST_CREDENTIAL, usage={
            "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
            "private_debug": TEST_CREDENTIAL, "prompt_cache_hit_tokens": True,
        })
        with patch("splice.cloud._post_with_deadline", return_value=response):
            self.assertEqual(client.complete_json(MESSAGES), {"ready": True})
        self.assertIsNone(client.metadata[0]["response_id"])
        self.assertEqual(client.usage, {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        metadata = client.metadata
        metadata[0]["usage"]["prompt_tokens"] = 999
        self.assertEqual(client.usage["prompt_tokens"], 7)


class ExpansionAndLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.registry = Registry(self.directory / "registry")
        Harness(self.registry, MockCapabilityProducer(ROOT / "examples" / "plugins")).publish_examples()
        self.learning = LearningStore(self.directory / "learning")
        self.experiment = json.loads(EXPERIMENT.read_text())
        self.bundle = synthetic_bundle(self.experiment)
        self.client = Mock(metadata=[])
        self.client.complete_json.side_effect = lambda *args, **kwargs: deepcopy(self.bundle)
        self.manager = ExpansionManager(self.registry, self.learning, self.directory, self.client)

    def propose(self):
        return self.manager.propose(self.experiment["requirement"], {"reason": "missing_capability"}, self.experiment)

    def test_independent_cases_cover_threshold_cash_and_permission_boundaries(self):
        report = self.manager.verify(self.bundle, self.experiment)
        cases = {case["name"]: case for case in report["cases"]}
        self.assertEqual(len(cases), 10)
        self.assertTrue(all(case["passed"] for case in cases.values()))
        self.assertEqual((cases["reserve_15"]["status"], cases["reserve_15"]["draft_count"]), ("completed", 1))
        for name in ("reserve_20", "reserve_50", "cash_insufficient", "smaller_budget_still_solvent"):
            self.assertEqual((cases[name]["error_type"], cases[name]["draft_count"]), ("GateRejected", 0))
        self.assertEqual((cases["larger_budget_same_guard"]["status"],
                          cases["larger_budget_same_guard"]["draft_count"]), ("completed", 1))
        self.assertEqual((cases["missing_finance_permission"]["error_type"],
                          cases["missing_finance_permission"]["draft_count"]), ("PermissionDenied", 0))
        self.assertEqual(self.registry.providers("budget.reserve_preserved"), [])

    def test_model_authored_examples_cannot_approve_a_broken_always_true_guard(self):
        bundle = deepcopy(self.bundle)
        bundle["manifest"]["execution"]["steps"][0]["value"] = True
        bundle["learning_pack"]["manifest_sha256"] = manifest_hash(bundle["manifest"])
        for example in bundle["behavior_examples"]:
            example["expected_output"]["allowed"] = True
        with self.assertRaisesRegex(ContractError, "Independent acceptance failed"):
            self.manager.verify(bundle, self.experiment)

    def test_varying_budget_cases_reject_a_guard_hardcoded_to_fixture_balance(self):
        bundle = deepcopy(self.bundle)
        bundle["manifest"]["execution"]["steps"][0]["value"]["args"][0] = 16
        bundle["learning_pack"]["manifest_sha256"] = manifest_hash(bundle["manifest"])
        with self.assertRaisesRegex(ContractError, "larger_budget_same_guard"):
            self.manager.verify(bundle, self.experiment)

    def test_teaching_examples_must_cover_independently_required_parameter_cases(self):
        bundle = deepcopy(self.bundle)
        example = bundle["learning_pack"]["examples"][-1]
        example["intent"]["facts"]["budget.min_remaining_cny"] = 60
        example["utterance"] = example["utterance"].replace("50元", "60元")
        example["intent"]["utterance"] = example["utterance"]
        with self.assertRaisesRegex(ContractError, "independently specified teaching case"):
            self.manager.verify(bundle, self.experiment)

    def test_verified_candidate_does_not_publish_until_explicit_approval(self):
        report = self.propose()
        self.assertEqual(report["status"], "verified")
        self.assertEqual(self.registry.providers("budget.reserve_preserved"), [])
        self.assertEqual(self.learning.all(self.registry), [])
        published = self.manager.approve(report["candidate_dir"])
        self.assertEqual(published["status"], "published")
        self.assertTrue(published["publication_approved"])
        self.assertEqual(len(self.registry.providers("budget.reserve_preserved")), 1)
        self.assertEqual(len(self.learning.all(self.registry)), 1)
        self.assertEqual(published["teaching"]["adaptation_mode"], "dynamic_context")
        self.assertFalse(published["teaching"]["weights_updated"])

    def test_modified_candidate_or_acceptance_is_not_approved(self):
        for filename in ("bundle.json", "experiment.json"):
            with self.subTest(filename=filename):
                report = self.propose()
                path = Path(report["candidate_dir"]) / filename
                data = json.loads(path.read_text())
                data["rationale" if filename == "bundle.json" else "description"] = "changed after verification"
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ContractError, "changed after verification"):
                    self.manager.approve(report["candidate_dir"])
                self.assertEqual(self.registry.providers("budget.reserve_preserved"), [])

    def test_approval_rechecks_provider_conflicts_added_since_verification(self):
        report = self.propose()
        conflicting = deepcopy(self.bundle["manifest"])
        conflicting["id"] = "test.competing.guard"
        self.registry.publish(conflicting)
        with self.assertRaisesRegex(ContractError, "Conflicting provider"):
            self.manager.approve(report["candidate_dir"])
        self.assertEqual([m["id"] for m in self.registry.providers("budget.reserve_preserved")],
                         [conflicting["id"]])
        self.assertEqual(self.learning.all(self.registry), [])

    def test_invalid_candidate_gets_at_most_one_repair_and_no_publication(self):
        self.bundle["manifest"]["execution"]["output"]["allowed"] = True
        report = self.propose()
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(self.client.complete_json.call_count, 2)
        self.assertEqual(self.registry.providers("budget.reserve_preserved"), [])
        repair_prompt = json.loads(self.client.complete_json.call_args_list[1].args[0][1]["content"])
        self.assertIn("validation_error", repair_prompt["repair"])

    def test_transport_failure_is_not_retried(self):
        self.client.complete_json.side_effect = CloudError("synthetic timeout", "timeout")
        report = self.propose()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(self.client.complete_json.call_count, 1)
        self.assertEqual(self.registry.providers("budget.reserve_preserved"), [])

    def test_learning_pack_cannot_change_hash_version_permissions_or_authorization(self):
        original = self.bundle["learning_pack"]
        mutations = {
            "hash": lambda pack: pack.update(manifest_sha256="0" * 64),
            "version": lambda pack: pack.update(plugin_version="9.0.0"),
            "supported_version": lambda pack: pack.update(supported_manifest_versions=["9.0.0"]),
            "permission": lambda pack: pack.update(required_permissions=["finance:read"]),
            "authorization": lambda pack: pack["examples"][0]["intent"].update(authorized_effects=["draft.create"]),
            "fabricated_gate": lambda pack: pack["examples"][0]["intent"]["facts"].update({"budget.reserve_preserved": True}),
            "fabricated_price": lambda pack: pack["examples"][0]["intent"]["facts"].update({"spending.amount_cny": 0}),
            "negative_reserve": lambda pack: pack["examples"][0]["intent"]["facts"].update({"budget.min_remaining_cny": -1}),
            "invalid_month": lambda pack: pack["examples"][0]["intent"]["facts"].update({"budget.month": "2026-99"}),
            "wrong_amount_type": lambda pack: pack["examples"][0]["intent"]["facts"].update({"spending.max_cny": "30"}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                pack = deepcopy(original)
                mutate(pack)
                with self.assertRaises(ContractError):
                    validate_pack(pack, self.bundle["manifest"], self.registry)

    def test_learning_pack_rejects_missing_guards_even_when_requested_as_outputs(self):
        for omitted in ("budget.reserve_preserved", "budget.within_limits"):
            with self.subTest(omitted=omitted):
                pack = deepcopy(self.bundle["learning_pack"])
                intent = pack["examples"][0]["intent"]
                intent["gates"] = [gate for gate in intent["gates"] if gate["fact"] != omitted]
                intent["requested_outputs"].append(omitted)
                with self.assertRaisesRegex(ContractError, "Planned boolean guard"):
                    validate_pack(pack, self.bundle["manifest"], self.registry)

    def test_learning_pack_rejects_an_unclosed_dependency_plan(self):
        pack = deepcopy(self.bundle["learning_pack"])
        del pack["examples"][0]["intent"]["facts"]["budget.min_remaining_cny"]
        with self.assertRaises(PlanningError):
            validate_pack(pack, self.bundle["manifest"], self.registry)

    def test_learning_pack_needs_published_active_version_and_is_immutable(self):
        pack = self.bundle["learning_pack"]
        with self.assertRaises(PlanningError):
            self.learning.install(pack, self.registry)
        self.registry.publish(self.bundle["manifest"])
        self.learning.install(pack, self.registry)
        changed = deepcopy(pack)
        changed["description"] = "Different teaching material for the same version"
        with self.assertRaises(ContractError):
            self.learning.install(changed, self.registry)
        newer = deepcopy(self.bundle["manifest"])
        newer["version"] = "0.2.0"
        self.registry.publish(newer)
        self.assertEqual(self.learning.all(self.registry), [])
        with self.assertRaises(ContractError):
            self.learning.install(pack, self.registry)

    def test_cloud_receives_gap_contracts_without_runtime_financial_records(self):
        producer = LLMCapabilityProducer(self.client)
        private_marker = "private-row-must-not-enter-cloud-context"
        producer.produce_bundle("Add a guard", {
            "reason": "missing_capability", "trace": [{"account": private_marker}],
            "outputs": {"assets.summary": private_marker}, "raw_accounts": [private_marker],
        }, self.registry.all(), self.experiment["target_contract"])
        messages = self.client.complete_json.call_args.args[0]
        self.assertNotIn(private_marker, json.dumps(messages))
        context = json.loads(messages[1]["content"])
        self.assertEqual(context["failure"], {"reason": "missing_capability"})
        self.assertNotIn("execution", context["registry_contracts"][0])
        with self.assertRaises(PlanningError):
            Harness(self.registry, producer).publish("Bypass verified expansion")


class LocalIntentBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.registry = Registry(directory / "registry")
        Harness(self.registry, MockCapabilityProducer(ROOT / "examples" / "plugins")).publish_examples()
        self.learning = LearningStore(directory / "learning")
        self.model = Mock()
        self.adapter = LocalOnDeviceIntentAdapter(self.registry, self.learning, self.model)

    def model_returns(self, intent, unresolved=None):
        self.model.generate.return_value = {
            "model": "synthetic-test-model", "backend": "test-double",
            "text": json.dumps({"intent": intent, "unresolved_constraints": unresolved or []}, ensure_ascii=False),
        }

    def test_model_cannot_grant_write_authorization_and_always_runs_offline(self):
        utterance = DEMO_REQUESTS["combined"]
        intent = MockOnDeviceIntentAdapter().parse(utterance)
        self.model_returns(intent)
        result = self.adapter.parse(utterance)
        self.assertEqual(result["authorized_effects"], [])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["adapter"], "local")
        self.assertTrue(self.model.generate.call_args.kwargs["offline"])

    def test_semantic_only_response_gets_trusted_metadata_and_confirmation(self):
        utterance = DEMO_REQUESTS["combined"]
        full = MockOnDeviceIntentAdapter().parse(utterance)
        semantics = {key: full[key] for key in ("goal", "requested_outputs", "facts", "gates")}
        self.model_returns(semantics)
        result = self.adapter.parse(utterance)
        self.assertEqual(result["facts"], full["facts"])
        self.assertEqual(result["utterance"], utterance)
        self.assertEqual(result["authorized_effects"], [])
        self.assertEqual(result["constraints"], {"place_order": False, "make_payment": False})
        self.assertTrue(result["requires_confirmation"])

    def test_installed_teaching_survives_reopening_stores_and_enters_local_context(self):
        experiment = json.loads(EXPERIMENT.read_text())
        bundle = synthetic_bundle(experiment)
        self.registry.publish(bundle["manifest"])
        self.learning.install(bundle["learning_pack"], self.registry)
        reopened_registry = Registry(self.registry.directory)
        reopened_learning = LearningStore(self.learning.directory)
        adapter = LocalOnDeviceIntentAdapter(reopened_registry, reopened_learning, self.model)
        example = bundle["learning_pack"]["examples"][0]
        self.model_returns(example["intent"])
        with patch("splice.cloud.DeepSeekClient.complete_json") as cloud:
            intent = adapter.parse(example["utterance"])
        cloud.assert_not_called()
        self.assertEqual(intent["facts"]["budget.min_remaining_cny"], 15)
        self.assertEqual(adapter.last_report["learning_pack_versions"], [{
            "plugin_id": bundle["manifest"]["id"], "plugin_version": "0.1.0",
            "manifest_sha256": manifest_hash(bundle["manifest"]),
        }])
        messages = self.model.generate.call_args.args[0]
        taught = [message["content"] for message in messages if message["role"] == "assistant"]
        self.assertTrue(any("budget.reserve_preserved" in content for content in taught))
        self.assertTrue(self.model.generate.call_args.kwargs["offline"])

    def test_ignored_request_for_real_payment_is_still_rejected(self):
        intent = MockOnDeviceIntentAdapter().parse(DEMO_REQUESTS["combined"])
        self.model_returns(intent)
        with self.assertRaises(LocalIntentError) as failure:
            self.adapter.parse(DEMO_REQUESTS["combined"] + "最后直接支付。")
        self.assertTrue(any("unsupported_effect" in item["reason"]
                            for item in failure.exception.report["unresolved_constraints"]))

    def test_silently_dropped_monetary_constraint_blocks_execution(self):
        utterance = DEMO_REQUESTS["combined"] + "消费后预算至少保留20元。"
        intent = MockOnDeviceIntentAdapter().parse(DEMO_REQUESTS["combined"])
        self.model_returns(intent)
        with self.assertRaises(LocalIntentError) as failure:
            self.adapter.parse(utterance)
        self.assertEqual(failure.exception.report["reason"], "unresolved_constraint")
        self.assertTrue(any("20元" in item["text"] for item in failure.exception.report["unresolved_constraints"]))

    def test_unknown_input_and_model_asserted_computed_fact_are_rejected(self):
        for fact, value in (("unregistered.guard", 20), ("budget.within_limits", True),
                            ("spending.amount_cny", 0)):
            with self.subTest(fact=fact):
                intent = MockOnDeviceIntentAdapter().parse(DEMO_REQUESTS["combined"])
                intent["facts"][fact] = value
                self.model_returns(intent)
                with self.assertRaises(LocalIntentError):
                    self.adapter.parse(DEMO_REQUESTS["combined"])

    def test_local_model_cannot_replace_guards_with_requested_outputs(self):
        experiment = json.loads(EXPERIMENT.read_text())
        bundle = synthetic_bundle(experiment)
        self.registry.publish(bundle["manifest"])
        self.learning.install(bundle["learning_pack"], self.registry)
        for omitted in ("budget.reserve_preserved", "budget.within_limits"):
            with self.subTest(omitted=omitted):
                intent = deepcopy(bundle["learning_pack"]["examples"][0]["intent"])
                utterance = intent["utterance"]
                intent["gates"] = [gate for gate in intent["gates"] if gate["fact"] != omitted]
                intent["requested_outputs"].append(omitted)
                self.model_returns(intent)
                with self.assertRaises(LocalIntentError):
                    self.adapter.parse(utterance)

    def test_local_model_fact_values_must_match_registered_input_schema(self):
        intent = MockOnDeviceIntentAdapter().parse(DEMO_REQUESTS["combined"])
        intent["facts"]["budget.month"] = "2026-99"
        self.model_returns(intent)
        with self.assertRaises(LocalIntentError):
            self.adapter.parse(DEMO_REQUESTS["combined"])

    def test_inference_failure_does_not_fall_back_to_mock_or_cloud(self):
        self.model.generate.side_effect = ContractError("Synthetic unavailable local model")
        with patch("splice.harness.LLMCapabilityProducer.produce_bundle") as cloud, \
                patch("splice.intent.MockOnDeviceIntentAdapter.parse") as mock_adapter:
            with self.assertRaises(LocalIntentError) as failure:
                self.adapter.parse(DEMO_REQUESTS["combined"])
        self.assertEqual(failure.exception.report["reason"], "local_inference_or_contract_error")
        cloud.assert_not_called()
        mock_adapter.assert_not_called()
        self.assertEqual(self.model.generate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
