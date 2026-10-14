"""Capability production is separate from per-request intent consumption."""

from copy import deepcopy
import json
from pathlib import Path
from typing import Protocol

from .contracts import validate_manifest
from .errors import ContractError, PlanningError
from .registry import Registry


class CapabilityProducer(Protocol):
    def produce(self, requirement: str) -> dict:
        """Produce a candidate manifest; publication still requires validation."""
        ...


class MockCapabilityProducer:
    """Offline controlled templates, not an implemented language model.

    A requirement must name a template stem, its ID, or its capability. This
    producer neither invents a new plan nor edits source code on demand.
    """

    def __init__(self, template_dir: Path):
        self.template_dir = Path(template_dir)

    def _templates(self) -> list[tuple[str, dict]]:
        templates = []
        for path in sorted(self.template_dir.glob("*.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ContractError(f"Cannot read mock template {path.name}: {exc}") from exc
            validate_manifest(manifest)
            templates.append((path.stem, manifest))
        if not templates:
            raise PlanningError(f"No controlled mock templates in {self.template_dir}")
        return templates

    def requirements(self) -> list[str]:
        return [stem for stem, _ in self._templates()]

    def produce(self, requirement: str) -> dict:
        normalized = requirement.strip().lower()
        matches = []
        for stem, manifest in self._templates():
            names = {stem.lower(), manifest["id"].lower(), manifest["capability"].lower()}
            if any(name == normalized or name in normalized for name in names):
                matches.append(manifest)
        if len(matches) != 1:
            raise PlanningError("Mock producer requires exactly one known template stem, ID, or capability")
        return deepcopy(matches[0])


class LLMCapabilityProducer:
    """Extension point only; no API client or model generation is implemented.

    A future backend should produce a candidate manifest against the published
    schema, run contract/behavior checks, and support review before publication.
    It must not turn model output into arbitrary executable code.
    """

    def produce(self, requirement: str) -> dict:
        raise NotImplementedError("LLM capability production is a research extension; use MockCapabilityProducer offline")


class Harness:
    def __init__(self, registry: Registry, producer: CapabilityProducer):
        self.registry = registry
        self.producer = producer

    def publish(self, requirement: str) -> dict:
        manifest = self.producer.produce(requirement)
        validate_manifest(manifest)
        return self.registry.publish(manifest)

    def publish_examples(self) -> list[dict]:
        if not isinstance(self.producer, MockCapabilityProducer):
            raise PlanningError("publish_examples is available for controlled mock templates only")
        return [self.publish(requirement) for requirement in self.producer.requirements()]
