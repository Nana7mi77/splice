"""Versioned, schema-bound examples for local in-context teaching, not training."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .contracts import schema, validate_data, validate_intent, validate_effect_guards
from .errors import ContractError, PlanningError
from .registry import _tokens
from .runtime import Planner


def manifest_hash(manifest):
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".splice-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_pack(pack, manifest, registry):
    validate_data(pack, schema("learning-pack"), "learning pack")
    if (pack["plugin_id"], pack["plugin_version"], pack["domain"]) != (
        manifest["id"], manifest["version"], manifest["domain"]
    ):
        raise ContractError("Learning pack identity/domain does not match its manifest")
    if pack["manifest_sha256"] != manifest_hash(manifest):
        raise ContractError("Learning pack manifest hash mismatch")
    if pack["supported_manifest_versions"] != [manifest["version"]]:
        raise ContractError("This learning-pack version must name exactly its verified manifest version")
    if set(pack["required_permissions"]) != set(manifest["permissions"]):
        raise ContractError("Learning pack cannot change the capability's permission requirements")
    outputs = {p["x-fact"] for p in manifest["output_schema"]["properties"].values()}
    inputs = {p["x-fact"] for p in manifest["input_schema"]["properties"].values()}
    if set(pack["fact_dependencies"]) != outputs or any(set(v) != inputs for v in pack["fact_dependencies"].values()):
        raise ContractError("Learning pack dependency labels must match the manifest's facts")
    known = [m for m in registry.all() if m["id"] != manifest["id"]] + [manifest]
    provided = {p["x-fact"] for m in known for p in m["output_schema"]["properties"].values()}
    consumed = {p["x-fact"] for m in known for p in m["input_schema"]["properties"].values()}
    for example in pack["examples"]:
        intent = example["intent"]
        validate_intent(intent)
        if intent["adapter"] != "local" or intent["authorized_effects"]:
            raise ContractError("Teaching examples must be local and may not grant effects")
        if intent["utterance"] != example["utterance"]:
            raise ContractError("Teaching labels must correspond to the example utterance")
        if not set(intent["facts"]).issubset(consumed - provided):
            raise ContractError("Learning examples may contain only user input facts, never computed facts")
        for fact, value in intent["facts"].items():
            for component in known:
                for prop in component["input_schema"]["properties"].values():
                    if prop["x-fact"] == fact:
                        validate_data(value, prop, f"teaching input {fact}")
        wanted = set(intent["requested_outputs"]) | {g["fact"] for g in intent["gates"]}
        if not wanted.issubset(provided) or not (wanted & outputs):
            raise ContractError("Teaching examples must request a real new capability output")
        if any(g["fact"] in intent["facts"] for g in intent["gates"]):
            raise ContractError("Teaching examples may not fabricate gate approvals")
        class Catalog:
            def providers(self, fact):
                return [m for m in known if any(p["x-fact"] == fact for p in m["output_schema"]["properties"].values())]
        plan = Planner(Catalog()).plan(intent)
        validate_effect_guards(intent, plan)


class LearningStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def install(self, pack, registry):
        manifest = registry.get(pack["plugin_id"], pack["plugin_version"])
        validate_pack(pack, manifest, registry)
        active = registry.get(pack["plugin_id"])
        if active["version"] != pack["plugin_version"]:
            raise ContractError("Cannot teach an inactive manifest version")
        key = hashlib.sha256((pack["plugin_id"] + "@" + pack["plugin_version"]).encode()).hexdigest()
        destination = self.directory / (key + ".json")
        if destination.exists() and json.loads(destination.read_text()) != pack:
            raise ContractError("Learning pack versions are immutable; publish a new manifest version")
        atomic_json(destination, pack)
        return {"plugin_id": pack["plugin_id"], "plugin_version": pack["plugin_version"],
                "manifest_sha256": pack["manifest_sha256"], "path": str(destination),
                "adaptation_mode": "dynamic_context", "weights_updated": False}

    def all(self, registry):
        packs = []
        for path in sorted(self.directory.glob("*.json")):
            pack = json.loads(path.read_text())
            try:
                active = registry.get(pack["plugin_id"])
            except PlanningError:
                continue
            if active["version"] != pack["plugin_version"]:
                continue
            validate_pack(pack, active, registry)
            packs.append(pack)
        return packs

    def relevant(self, utterance, registry):
        query = _tokens(utterance)
        ranked = []
        for pack in self.all(registry):
            text = " ".join([pack["description"], *pack["keywords"], *[e["utterance"] for e in pack["examples"]]])
            score = len(query & _tokens(text))
            if score:
                ranked.append((score, pack))
        ranked.sort(key=lambda item: (-item[0], item[1]["plugin_id"]))
        return [deepcopy(pack) for _, pack in ranked[:3]]
