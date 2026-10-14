"""Local, versioned capabilities and explicitly lexical discovery.

Search uses word tokens and Chinese bigrams. It is not an embedding service or
a claim of semantic understanding. Readers revalidate the persisted manifests.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .contracts import validate_manifest
from .errors import ContractError, PlanningError


def _version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ContractError("This prototype supports numeric major.minor.patch versions only")
    return tuple(int(part) for part in value.split("."))


def _tokens(value: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9]+", value.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        if len(run) == 1:
            result.add(run)
        else:
            result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


class Registry:
    """Immutable versions on disk; latest numeric version is active per ID."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ContractError(f"Cannot read registry manifest {path.name}: {exc}") from exc
        validate_manifest(manifest)
        _version(manifest["version"])
        return manifest

    def _records(self) -> list[dict]:
        records = {}
        for path in sorted(self.directory.rglob("*.json")):
            manifest = self._load(path)
            key = (manifest["id"], manifest["version"])
            if key in records and records[key] != manifest:
                raise ContractError(f"Conflicting persisted versions for {key[0]}@{key[1]}")
            records[key] = manifest
        return list(records.values())

    def publish(self, manifest: dict) -> dict:
        """Validate and publish atomically without overwriting an existing version."""
        validate_manifest(manifest)
        _version(manifest["version"])
        # Hashing the identity also makes storage independent of ID path syntax.
        identity = hashlib.sha256(manifest["id"].encode("utf-8")).hexdigest()
        destination = self.directory / identity / f"{manifest['version']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if self._load(destination) != manifest:
                raise ContractError(f"Version already published: {manifest['id']}@{manifest['version']}")
            return deepcopy(manifest)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".publish-", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Unlike replace(), link() cannot race into replacing another
                # publisher's immutable version. Both paths share a filesystem.
                os.link(temporary, destination)
            except FileExistsError:
                if self._load(destination) != manifest:
                    raise ContractError(f"Version already published: {manifest['id']}@{manifest['version']}")
        finally:
            temporary.unlink(missing_ok=True)
        return deepcopy(manifest)

    def get(self, plugin_id: str, version: str | None = None) -> dict:
        candidates = [item for item in self._records() if item["id"] == plugin_id]
        if version is not None:
            candidates = [item for item in candidates if item["version"] == version]
        if not candidates:
            suffix = f"@{version}" if version is not None else ""
            raise PlanningError(f"Capability not found in registry: {plugin_id}{suffix}")
        return deepcopy(max(candidates, key=lambda item: _version(item["version"])))

    def all(self) -> list[dict]:
        latest = {}
        for manifest in self._records():
            plugin_id = manifest["id"]
            if plugin_id not in latest or _version(manifest["version"]) > _version(latest[plugin_id]["version"]):
                latest[plugin_id] = manifest
        return [deepcopy(latest[key]) for key in sorted(latest)]

    def search(self, query: str) -> list[dict]:
        """Return explanations alongside unmodified latest-version manifests."""
        query_terms = _tokens(query)
        results = []
        for manifest in self.all():
            fields = (
                (manifest["id"], 4),
                (manifest["capability"], 4),
                (manifest["description"], 2),
                (" ".join(manifest["examples"]), 1),
            )
            score = 0
            matched = set()
            for value, weight in fields:
                overlap = query_terms & _tokens(value)
                matched.update(overlap)
                score += len(overlap) * weight
            if matched:
                results.append({
                    "manifest": manifest,
                    "score": float(score),
                    "matched_terms": sorted(matched),
                    "method": "lexical-words-and-chinese-bigrams",
                })
        return sorted(results, key=lambda item: (-item["score"], item["manifest"]["id"]))

    def providers(self, fact: str) -> list[dict]:
        return [manifest for manifest in self.all() if any(
            prop.get("x-fact") == fact
            for prop in manifest["output_schema"]["properties"].values()
        )]
