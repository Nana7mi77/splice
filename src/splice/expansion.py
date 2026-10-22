"""Verified local capability publication with independently supplied acceptance data.

The manager has no domain-specific dispatch or executable generated code. An
experiment declares contracts, facts, permissions and expected fixture results.
Only public contracts and the capability gap are sent to the cloud producer.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import uuid

from .contracts import validate_manifest, validate_intent
from .errors import ContractError, PlanningError, SpliceError
from .gateway import Gateway
from .harness import LLMCapabilityProducer
from .learning import atomic_json, manifest_hash, validate_pack
from .registry import Registry
from .runtime import Planner, Runtime


def _semantic_schema(value):
    if isinstance(value, dict):
        return {k: _semantic_schema(v) for k, v in value.items() if k not in {"description", "title", "examples"}}
    if isinstance(value, list):
        return [_semantic_schema(v) for v in value]
    return value


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class ExpansionManager:
    def __init__(self, registry, learning_store, state_dir, client=None):
        self.registry = registry
        self.learning_store = learning_store
        self.state_dir = Path(state_dir)
        self.client = client

    def stage(self, bundle, experiment):
        """Reverify an exported bundle offline; this does not call a producer."""
        bundle = deepcopy(bundle) if isinstance(bundle, dict) else _read(bundle)
        experiment = self._experiment(experiment)
        verification = self.verify(bundle, experiment)
        directory = self.state_dir / "candidates" / uuid.uuid4().hex
        report = {
            "status": "verified", "producer": "imported_bundle", "candidate_dir": str(directory.resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(), "cloud_calls": [],
            "manifest_sha256": manifest_hash(bundle["manifest"]), "bundle_sha256": manifest_hash(bundle),
            "experiment_sha256": manifest_hash(experiment), "verification": verification,
            "adaptation_mode": "dynamic_context", "weights_updated": False,
        }
        for name, value in (("bundle", bundle), ("experiment", experiment), ("manifest", bundle["manifest"]),
                            ("learning-pack", bundle["learning_pack"]), ("report", report)):
            atomic_json(directory / f"{name}.json", value)
        return report

    @staticmethod
    def _experiment(value):
        experiment = deepcopy(value) if isinstance(value, dict) else _read(value)
        required = {"schema_version", "target_contract", "base_intent", "grants", "cases"}
        if not isinstance(experiment, dict) or not required.issubset(experiment):
            raise ContractError("An independent experiment must declare contracts, intent, grants, and cases")
        if experiment["schema_version"] != "1.0" or not isinstance(experiment["cases"], list) or not 3 <= len(experiment["cases"]) <= 30:
            raise ContractError("An experiment needs 3–30 independent cases")
        validate_intent(experiment["base_intent"])
        target = experiment["target_contract"]
        for key in ("domain", "input_facts", "output_facts", "allowed_resources", "allowed_permissions", "required_gates"):
            if key not in target:
                raise ContractError(f"Experiment target_contract missing {key}")
        if not target["output_facts"] or not target["required_gates"]:
            raise ContractError("An expansion must declare its new boolean output and required gates")
        for case in experiment["cases"]:
            if not isinstance(case, dict) or not {"name", "facts", "expected_status", "expected_drafts"}.issubset(case):
                raise ContractError("Acceptance cases need name, facts, expected status and draft count")
        return experiment

    def verify(self, bundle, experiment):
        """Run both generated examples and independent cases in isolated stores."""
        experiment = self._experiment(experiment)
        if not isinstance(bundle, dict) or set(bundle) != {"manifest", "rationale", "learning_pack", "behavior_examples"}:
            raise ContractError("Candidate bundle must contain exactly manifest, rationale, learning_pack, behavior_examples")
        candidate = bundle["manifest"]
        validate_manifest(candidate)
        target = experiment["target_contract"]
        if candidate["domain"] != target["domain"] or candidate["effects"]:
            raise ContractError("Expansion must stay in the accepted domain and be read-only")
        if len(candidate["execution"]["steps"]) > 100:
            raise ContractError("Candidate exceeds the bounded execution size")
        if not set(candidate["resources"]).issubset(target["allowed_resources"]) or not set(candidate["permissions"]).issubset(target["allowed_permissions"]):
            raise ContractError("Candidate requests resources or permissions outside the experiment scope")
        for direction, expected in (("input_schema", target["input_facts"]), ("output_schema", target["output_facts"])):
            contract = candidate[direction]
            actual = {v["x-fact"]: v for v in contract["properties"].values()}
            if len(actual) != len(contract["properties"]) or _semantic_schema(actual) != _semantic_schema(expected):
                raise ContractError(f"Candidate {direction} does not match the independently specified facts")
            if set(contract.get("required", [])) != set(contract["properties"]):
                raise ContractError("Every candidate input/output field must be required")
        for fact, prop in target["output_facts"].items():
            if prop.get("type") != "boolean":
                raise ContractError("This expansion protocol currently verifies boolean guard outputs only")
            if any(m["id"] != candidate["id"] for m in self.registry.providers(fact)):
                raise ContractError(f"Conflicting provider for {fact}")
        prior = [m for m in self.registry.all() if m["id"] == candidate["id"]]
        if prior and prior[0] != candidate:
            raise ContractError("This initial protocol requires a new capability ID; version updates need separate migration review")
        pack = bundle["learning_pack"]
        validate_pack(pack, candidate, self.registry)
        examples = bundle["behavior_examples"]
        if not isinstance(examples, list) or not 3 <= len(examples) <= 20:
            raise ContractError("Candidate needs 3–20 declarative behavior examples")
        required_gates = target["required_gates"]
        for intent in [experiment["base_intent"], *[e["intent"] for e in pack["examples"]]]:
            if any(gate not in intent["gates"] for gate in required_gates):
                raise ContractError("Teaching and acceptance intents must preserve every required gate")
        for expected in target.get("teaching_cases", []):
            if not any(e["intent"]["facts"] == expected["facts"] and
                       set(expected["requested_outputs"]).issubset(e["intent"]["requested_outputs"])
                       for e in pack["examples"]):
                raise ContractError("Learning pack is missing an independently specified teaching case")
        results = []
        with tempfile.TemporaryDirectory(prefix="splice-verify-") as directory:
            trial = Registry(Path(directory) / "registry")
            for manifest in self.registry.all():
                trial.publish(manifest)
            trial.publish(candidate)
            for example in pack["examples"]:
                plan = Planner(trial).plan(example["intent"])
                if candidate["id"] not in {m["id"] for m in plan}:
                    raise ContractError("Teaching intent does not actually bind the new component")
            for index, example in enumerate(examples):
                if not isinstance(example, dict) or set(example) != {"input", "expected_output"}:
                    raise ContractError("Behavior examples need exactly input and expected_output")
                gateway = Gateway(set(candidate["permissions"]))
                try:
                    actual = Runtime(trial, gateway).invoke(candidate["id"], example["input"], candidate["version"])
                    if actual != example["expected_output"] or gateway.draft_count():
                        raise ContractError(f"Generated behavior example {index} failed")
                finally:
                    gateway.close()
            for case in experiment["cases"]:
                intent = deepcopy(experiment["base_intent"])
                intent["facts"].update(case["facts"])
                gateway = Gateway(case.get("grants", experiment["grants"]))
                try:
                    for resource, updates in case.get("resource_updates", {}).items():
                        if resource not in gateway.resources or not isinstance(updates, dict):
                            raise ContractError("Acceptance resource update is invalid")
                        for row in gateway.resources[resource]:
                            row.update(deepcopy(updates))
                    report = Runtime(trial, gateway).run(intent, confirmed=True)
                    item = {"name": case["name"], "status": report["status"], "draft_count": gateway.draft_count(),
                            "error_type": report.get("error", {}).get("type"),
                            "gates": [e for e in report["trace"] if e["event"] == "gate"]}
                    ok = (item["status"] == case["expected_status"] and item["draft_count"] == case["expected_drafts"]
                          and (not case.get("expected_error") or item["error_type"] == case["expected_error"]))
                    item["passed"] = ok
                    results.append(item)
                    if not ok:
                        # No runtime outputs or source rows enter a repair request.
                        raise ContractError(f"Independent acceptance failed: {json.dumps(item, ensure_ascii=False)}")
                finally:
                    gateway.close()
        return {"passed": True, "schema": True, "permissions": True, "provider_conflicts": False,
                "teaching_plans": len(pack["examples"]), "generated_examples": len(examples), "cases": results,
                "scope": "declared contracts and finite synthetic fixture cases; not a proof of arbitrary semantics"}

    def propose(self, requirement, failure, experiment, approve=False, *, repair=None, max_attempts=2):
        if self.client is None:
            raise PlanningError("Cloud producer is required to propose an expansion")
        if max_attempts not in (1, 2):
            raise ContractError("Expansion allows one generation and at most one repair")
        experiment = self._experiment(experiment)
        directory = self.state_dir / "candidates" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        atomic_json(directory / "experiment.json", experiment)
        report = {"status": "draft", "candidate_dir": str(directory.resolve()), "requirement": requirement,
                  "created_at": datetime.now(timezone.utc).isoformat(), "adaptation_mode": "dynamic_context",
                  "weights_updated": False, "attempts": [], "cloud_calls": []}
        atomic_json(directory / "report.json", report)
        producer = LLMCapabilityProducer(self.client)
        # At most one repair. Transport/auth failures are never automatically retried.
        for index in range(max_attempts):
            try:
                bundle = producer.produce_bundle(requirement, failure, self.registry.all(), experiment["target_contract"], repair)
            except SpliceError as exc:
                report.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)}, cloud_calls=self.client.metadata)
                atomic_json(directory / "report.json", report)
                return report
            bundle["learning_pack"]["manifest_sha256"] = manifest_hash(bundle["manifest"])
            atomic_json(directory / f"attempt-{index + 1}.json", bundle)
            try:
                verification = self.verify(bundle, experiment)
            except (SpliceError, KeyError, TypeError, ValueError) as exc:
                finding = {"type": type(exc).__name__, "message": str(exc)}
                report["attempts"].append({"number": index + 1, "valid": False, "error": finding})
                report["cloud_calls"] = self.client.metadata
                report["status"] = "rejected" if index == max_attempts - 1 else "draft"
                atomic_json(directory / "report.json", report)
                repair = {"candidate": bundle, "validation_error": finding}
                continue
            report["attempts"].append({"number": index + 1, "valid": True})
            report.update(status="verified", manifest_sha256=manifest_hash(bundle["manifest"]),
                          bundle_sha256=manifest_hash(bundle), experiment_sha256=manifest_hash(experiment),
                          verification=verification, cloud_calls=self.client.metadata)
            atomic_json(directory / "bundle.json", bundle)
            atomic_json(directory / "manifest.json", bundle["manifest"])
            atomic_json(directory / "learning-pack.json", bundle["learning_pack"])
            atomic_json(directory / "report.json", report)
            return self.approve(directory) if approve else report
        return report

    def approve(self, candidate_dir):
        directory = Path(candidate_dir).resolve()
        root = (self.state_dir / "candidates").resolve()
        if not directory.is_relative_to(root):
            raise ContractError("Candidate must belong to this local state directory")
        report = _read(directory / "report.json")
        if report.get("status") not in {"verified", "published"}:
            raise ContractError("Only a verified candidate can be explicitly approved")
        bundle = _read(directory / "bundle.json")
        experiment = _read(directory / "experiment.json")
        if manifest_hash(bundle) != report.get("bundle_sha256") or manifest_hash(experiment) != report.get("experiment_sha256"):
            raise ContractError("Candidate or acceptance specification changed after verification")
        verification = self.verify(bundle, experiment)
        manifest = self.registry.publish(bundle["manifest"])
        teaching = self.learning_store.install(bundle["learning_pack"], self.registry)
        report.update(status="published", publication_approved=True, verification=verification,
                      published={"id": manifest["id"], "version": manifest["version"]}, teaching=teaching)
        atomic_json(directory / "report.json", report)
        return report
