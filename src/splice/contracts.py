"""JSON Schema validation with local schemas and no remote references."""

import json
import math
import re
from functools import lru_cache

from jsonschema import Draft202012Validator, SchemaError

from .errors import ContractError
from .paths import project_data


@lru_cache
def schema(name):
    return json.loads((project_data() / "specs" / f"{name}.schema.json").read_text())


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("NaN / Infinity are not valid contract values")
    if isinstance(value, dict):
        for child in value.values():
            _finite(child)
    elif isinstance(value, list):
        for child in value:
            _finite(child)


def _local_schema(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and not str(child).startswith("#"):
                raise ContractError("Schema references must be local; remote fetching is disabled")
            _local_schema(child)
    elif isinstance(value, list):
        for child in value:
            _local_schema(child)


@lru_cache(maxsize=128)
def _validator(encoded):
    contract = json.loads(encoded)
    _local_schema(contract)
    Draft202012Validator.check_schema(contract)
    return Draft202012Validator(contract)


def validate_data(value, schema, label="data"):
    _finite(value)
    try:
        validator = _validator(json.dumps(schema, sort_keys=True, allow_nan=False))
        error = next(validator.iter_errors(value), None)
    except SchemaError as exc:
        raise ContractError(f"{label}: invalid JSON Schema: {exc.message}") from exc
    if error:
        path = ".".join(map(str, error.absolute_path)) or "$"
        raise ContractError(f"{label} at {path}: {error.message}")


def validate_manifest(manifest):
    validate_data(manifest, schema("plugin"), "manifest")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", manifest["id"]):
        raise ContractError("Unsafe plugin ID")
    for direction in ("input_schema", "output_schema"):
        contract = manifest[direction]
        try:
            _validator(json.dumps(contract, sort_keys=True, allow_nan=False))
        except SchemaError as exc:
            raise ContractError(f"{direction}: {exc.message}") from exc
        if contract.get("type") != "object" or contract.get("additionalProperties") is not False:
            raise ContractError(f"{direction} must be a closed object schema")
        for name, prop in contract.get("properties", {}).items():
            if not isinstance(prop.get("x-fact"), str) or not prop["x-fact"]:
                raise ContractError(f"{direction}.{name} requires an x-fact binding")
    outputs = [p["x-fact"] for p in manifest["output_schema"]["properties"].values()]
    if len(set(outputs)) != len(outputs):
        raise ContractError("A plugin may not produce the same fact twice")
    for step in manifest["execution"]["steps"]:
        if step["op"] in {"read", "insert"} and step["resource"] not in manifest["resources"]:
            raise ContractError(f"Undeclared resource: {step['resource']}")
        if step["op"] == "insert" and not manifest["effects"]:
            raise ContractError("An insert must declare its effect")


def validate_intent(intent):
    validate_data(intent, schema("intent"), "intent")
