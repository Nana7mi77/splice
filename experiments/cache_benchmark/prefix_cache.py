"""Bounded, isolated prefix KV cache for the local Qwen3 benchmark only.

This deliberately has no connection to the production request path. It is a
single-process, single-caller experiment for unpadded batch-size-one prompts.
Only a stable prompt prefix is cached; request facts and execution results are
not cached. The caller must identify every model, tokenizer and prompt revision.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

import torch
from transformers import DynamicCache


REQUIRED_IDENTITY_FIELDS = frozenset(
    {
        "model_revision",
        "tokenizer_revision",
        "chat_template",
        "backend",
        "dtype",
        "catalog_hash",
        "learning_hash",
    }
)


def _identity_json(identity: Mapping[str, Any]) -> str:
    if not isinstance(identity, Mapping):
        raise TypeError("identity must be a mapping")
    missing = REQUIRED_IDENTITY_FIELDS - identity.keys()
    if missing:
        raise ValueError(f"identity missing fields: {', '.join(sorted(missing))}")
    for field in REQUIRED_IDENTITY_FIELDS:
        if not isinstance(identity[field], str) or not identity[field].strip():
            raise ValueError(f"identity field {field!r} must be a nonempty string")
    if not all(isinstance(key, str) for key in identity):
        raise ValueError("identity keys must be strings")
    # Round-trip serialization also disconnects the key from caller mutation.
    return json.dumps(dict(identity), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_ids(ids: torch.Tensor, label: str) -> None:
    if not isinstance(ids, torch.Tensor) or ids.dtype != torch.long:
        raise ValueError(f"{label} must be a torch.long tensor")
    if ids.ndim != 2 or ids.shape[0] != 1 or ids.shape[1] == 0:
        raise ValueError(f"{label} must be a nonempty unpadded [1, tokens] tensor")
    if ids.device.type != "cpu":
        raise ValueError("this benchmark helper only supports CPU tensors")


def cache_tensor_bytes(cache: DynamicCache) -> int:
    """Logical K/V tensor bytes, excluding Python overhead and allocator slack."""
    return sum(
        tensor.numel() * tensor.element_size()
        for layer in cache.layers
        for tensor in (layer.keys, layer.values)
        if tensor is not None
    )


class PrefixCache:
    """Retain pristine prefix caches and return a deep copy for each request.

    ``identity`` may be replaced per lookup when catalog/learning state changes.
    Model/backend/dtype/template changes also produce misses. Model identity is
    supplied by the caller; changing weights in place is outside this experiment.
    """

    def __init__(
        self,
        model: Any,
        identity: Mapping[str, Any],
        max_entries: int = 1,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if model.training:
            raise ValueError("prefix caching requires model.eval()")
        self.model = model
        self.max_entries = max_entries
        self._identity = _identity_json(identity)
        self._entries: OrderedDict[str, DynamicCache] = OrderedDict()
        self._supports_logits_to_keep = "logits_to_keep" in inspect.signature(model.forward).parameters

    @property
    def stored_lengths(self) -> dict[str, int]:
        """Lengths of the pristine templates, for contamination checks."""
        return {key: int(cache.get_seq_length()) for key, cache in self._entries.items()}

    def get_or_build(
        self,
        prefix_ids: torch.Tensor,
        identity: Mapping[str, Any] | None = None,
    ) -> tuple[DynamicCache, dict[str, Any]]:
        _validate_ids(prefix_ids, "prefix_ids")
        if self.model.training:
            raise ValueError("prefix caching requires model.eval()")
        identity_json = self._identity if identity is None else _identity_json(identity)
        identity_hash = hashlib.sha256(identity_json.encode()).hexdigest()
        # Explicit dtype, shape and bytes make the exact token sequence part of
        # the key. CPU int64 is a precondition above, not an implicit conversion.
        digest = hashlib.sha256()
        digest.update(identity_json.encode())
        digest.update(b"\x00torch.int64\x00")
        digest.update(json.dumps(list(prefix_ids.shape)).encode())
        digest.update(b"\x00")
        digest.update(prefix_ids.contiguous().numpy().tobytes())
        key = digest.hexdigest()
        hit = key in self._entries
        build_seconds = 0.0
        evicted_key = None
        if hit:
            self._entries.move_to_end(key)
            template = self._entries[key]
        else:
            # Evict before allocating a new template to bound live template
            # storage. The request's returned clone is additional memory.
            if len(self._entries) >= self.max_entries:
                evicted_key, evicted_cache = self._entries.popitem(last=False)
                del evicted_cache
            kwargs: dict[str, Any] = {
                "input_ids": prefix_ids,
                "attention_mask": torch.ones_like(prefix_ids),
                "past_key_values": DynamicCache(config=self.model.config),
                "use_cache": True,
            }
            if self._supports_logits_to_keep:
                kwargs["logits_to_keep"] = 1
            started = time.perf_counter()
            with torch.inference_mode():
                output = self.model(**kwargs)
            template = output.past_key_values
            del output
            build_seconds = time.perf_counter() - started
            if not isinstance(template, DynamicCache):
                raise TypeError("expected a Transformers DynamicCache")
            if int(template.get_seq_length()) != prefix_ids.shape[1]:
                raise ValueError("only full-attention prefix caches are supported")
            if any(getattr(layer, "is_sliding", False) for layer in template.layers):
                raise ValueError("sliding-window cache is outside this experiment")
            self._entries[key] = template
        started = time.perf_counter()
        # HF generation mutates DynamicCache. A shallow copy would share layers
        # and contaminate subsequent requests, so isolate every tensor as well.
        with torch.inference_mode():
            fresh_cache = copy.deepcopy(template)
        clone_seconds = time.perf_counter() - started
        return fresh_cache, {
            "key": key,
            "identity_hash": identity_hash,
            "hit": hit,
            "prefix_tokens": int(prefix_ids.shape[1]),
            "build_seconds": build_seconds,
            "clone_seconds": clone_seconds,
            "cache_bytes": cache_tensor_bytes(template),
            "retained_entries": len(self._entries),
            "evicted_key": evicted_key,
        }


def cached_generation_inputs(
    full_ids: torch.Tensor,
    prefix_ids: torch.Tensor,
    cache: DynamicCache,
) -> dict[str, Any]:
    """Prepare full prompt IDs while HF forwards only uncached suffix tokens.

    Transformers 4.57.6 generation/utils.py retains explicit cache_position in
    _get_initial_cache_position, then slices input_ids by those positions in
    _cache_dependant_input_preparation. The full-length attention mask is needed
    to attend over both the stored prefix and the new suffix.
    """
    _validate_ids(full_ids, "full_ids")
    _validate_ids(prefix_ids, "prefix_ids")
    prefix_length = prefix_ids.shape[1]
    if full_ids.shape[1] <= prefix_length:
        raise ValueError("generation requires at least one uncached suffix token")
    if not torch.equal(full_ids[:, :prefix_length], prefix_ids):
        raise ValueError("full_ids does not start with the exact cached prefix")
    if not isinstance(cache, DynamicCache) or int(cache.get_seq_length()) != prefix_length:
        raise ValueError("cache length does not match prefix_ids")
    return {
        "input_ids": full_ids,
        "attention_mask": torch.ones_like(full_ids),
        "past_key_values": cache,
        "cache_position": torch.arange(prefix_length, full_ids.shape[1], device=full_ids.device),
        "use_cache": True,
    }
