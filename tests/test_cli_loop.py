"""CLI integration checks using synthetic model outputs and cloud responses only."""

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from splice.cli import main
from splice.harness import Harness, MockCapabilityProducer
from splice.intent import DEMO_REQUESTS, MockOnDeviceIntentAdapter
from splice.learning import LearningStore
from splice.registry import Registry

from test_model_loop import EXPERIMENT, ROOT, synthetic_bundle


class CliModelLoopTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "state"
        self.config = self.directory / "isolated-test-config"
        self.model = Mock()
        self.experiment = json.loads(EXPERIMENT.read_text())
        self.bundle = synthetic_bundle(self.experiment)

    def model_returns(self, intent):
        self.model.generate.return_value = {
            "model": "synthetic-cli-model", "backend": "test-double",
            "text": json.dumps({"intent": intent, "unresolved_constraints": []}, ensure_ascii=False),
        }

    def invoke(self, utterance, *options):
        output = StringIO()
        with patch("splice.cli.ModelManager", return_value=self.model), redirect_stdout(output):
            code = main(["run", utterance, "--adapter", "local", "--profile", "combined",
                         "--state-dir", str(self.state), "--config-dir", str(self.config), "--json", *options])
        return code, json.loads(output.getvalue())

    def seed_expansion(self):
        registry = Registry(self.state / "registry")
        Harness(registry, MockCapabilityProducer(ROOT / "examples" / "plugins")).publish_examples()
        registry.publish(self.bundle["manifest"])
        LearningStore(self.state / "learning").install(self.bundle["learning_pack"], registry)

    def draft_count(self):
        path = self.state / "drafts.sqlite3"
        if not path.exists():
            return 0
        with sqlite3.connect(path) as connection:
            return connection.execute("SELECT COUNT(*) FROM records WHERE resource='food_order_drafts'").fetchone()[0]

    def test_offline_local_execution_requires_explicit_confirmation_without_cloud(self):
        utterance = DEMO_REQUESTS["combined"]
        self.model_returns(MockOnDeviceIntentAdapter().parse(utterance))
        with patch("splice.cli.DeepSeekClient") as cloud:
            blocked_code, blocked = self.invoke(utterance, "--offline")
            completed_code, completed = self.invoke(utterance, "--offline", "--confirm")
        cloud.assert_not_called()
        self.assertEqual(blocked_code, 2)
        self.assertEqual(blocked["error"]["type"], "ConfirmationRequired")
        self.assertEqual(completed_code, 0)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["cloud_calls"], [])
        self.assertEqual(completed["intent"]["authorized_effects"], [])
        self.assertEqual(completed["intent_adapter"], "local")
        self.assertFalse(completed["mock"])
        self.assertTrue(completed["fictional_data"])
        self.assertEqual(self.draft_count(), 1)

    def test_offline_unknown_constraint_does_not_create_cloud_client_or_candidate(self):
        self.model_returns(self.experiment["base_intent"])
        with patch("splice.cli.DeepSeekClient") as cloud:
            code, report = self.invoke(self.experiment["requirement"], "--offline", "--auto-expand",
                                       "--experiment", str(EXPERIMENT), "--approve-expansion", "--confirm")
        cloud.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(report["local_model_metadata"]["reason"], "unresolved_constraint")
        self.assertEqual(report["cloud_calls"], [])
        self.assertFalse((self.state / "candidates").exists())
        self.assertEqual(self.draft_count(), 0)

    def test_verified_auto_expansion_stays_unpublished_without_approval(self):
        self.model_returns(self.experiment["base_intent"])
        client = Mock(metadata=[])
        client.complete_json.return_value = deepcopy(self.bundle)
        with patch("splice.cli.DeepSeekClient", return_value=client) as cloud:
            code, report = self.invoke(self.experiment["requirement"], "--auto-expand",
                                       "--experiment", str(EXPERIMENT), "--confirm")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["initial_local_failure"]["reason"], "unresolved_constraint")
        cloud.assert_called_once()
        self.assertEqual(client.complete_json.call_count, 1)
        self.assertEqual(self.model.generate.call_count, 1)
        registry = Registry(self.state / "registry")
        self.assertEqual(registry.providers("budget.reserve_preserved"), [])
        self.assertEqual(LearningStore(self.state / "learning").all(registry), [])
        self.assertEqual(self.draft_count(), 0)

    def test_explicit_experiment_rejects_omitted_guard_before_opening_gateway(self):
        utterance = DEMO_REQUESTS["combined"]
        self.model_returns(MockOnDeviceIntentAdapter().parse(utterance))
        with patch("splice.cli.DeepSeekClient") as cloud, patch("splice.cli.Gateway") as gateway:
            code, report = self.invoke(utterance, "--offline", "--confirm", "--experiment", str(EXPERIMENT))
        cloud.assert_not_called()
        gateway.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["type"], "ContractError")
        self.assertIn("omitted required experiment gates", report["error"]["message"])

    def test_gate_rejection_does_not_trigger_cloud_expansion(self):
        self.seed_expansion()
        example = self.bundle["learning_pack"]["examples"][1]
        self.model_returns(example["intent"])
        with patch("splice.cli.DeepSeekClient") as cloud:
            code, report = self.invoke(example["utterance"], "--auto-expand", "--approve-expansion",
                                       "--experiment", str(EXPERIMENT), "--confirm")
        cloud.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["type"], "GateRejected")
        self.assertEqual(report["cloud_calls"], [])
        self.assertEqual(self.draft_count(), 0)


if __name__ == "__main__":
    unittest.main()
