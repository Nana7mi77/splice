"""API correctness checks only: tiny random weights are not latency evidence."""

from __future__ import annotations

import unittest

import torch
from transformers import Qwen3Config, Qwen3ForCausalLM

from experiments.cache_benchmark.prefix_cache import PrefixCache, cached_generation_inputs


IDENTITY = {
    "model_revision": "tiny-random-test-weights",
    "tokenizer_revision": "integer-token-fixture-v1",
    "chat_template": "fixture-v1",
    "backend": "transformers-cpu",
    "dtype": "float32",
    "catalog_hash": "fixture-catalog-v1",
    "learning_hash": "fixture-learning-v1",
}


class PrefixCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        torch.set_num_threads(1)
        torch.manual_seed(7)
        config = Qwen3Config(
            vocab_size=64,
            hidden_size=32,
            intermediate_size=64,
            num_hidden_layers=2,
            num_attention_heads=4,
            num_key_value_heads=2,
            head_dim=8,
            max_position_embeddings=128,
            attention_dropout=0.0,
            bos_token_id=1,
            eos_token_id=2,
            pad_token_id=0,
        )
        cls.model = Qwen3ForCausalLM(config).eval()
        cls.prefix = torch.tensor([[1, 4, 5, 6]], dtype=torch.long)
        cls.full = torch.tensor([[1, 4, 5, 6, 7, 8]], dtype=torch.long)

    def test_generation_tokens_match_and_only_suffix_is_forwarded(self) -> None:
        holder = PrefixCache(self.model, IDENTITY)
        options = dict(max_new_tokens=6, min_new_tokens=6, do_sample=False, pad_token_id=0)
        with torch.inference_mode():
            uncached = self.model.generate(
                input_ids=self.full,
                attention_mask=torch.ones_like(self.full),
                **options,
            )
        cloned, first = holder.get_or_build(self.prefix)
        pristine_tensors = [
            (layer.keys.clone(), layer.values.clone()) for layer in cloned.layers
        ]
        lengths: list[int] = []

        def record_forward(module, args, kwargs):
            lengths.append(kwargs["input_ids"].shape[1])

        handle = self.model.register_forward_pre_hook(record_forward, with_kwargs=True)
        try:
            with torch.inference_mode():
                cached = self.model.generate(
                    **cached_generation_inputs(self.full, self.prefix, cloned), **options
                )
        finally:
            handle.remove()
        self.assertTrue(torch.equal(uncached, cached))
        self.assertEqual(lengths, [2, 1, 1, 1, 1, 1])
        self.assertEqual(holder.stored_lengths, {first["key"]: 4})
        self.assertGreater(cloned.get_seq_length(), 4)
        second_clone, second = holder.get_or_build(self.prefix)
        self.assertFalse(first["hit"])
        self.assertTrue(second["hit"])
        self.assertEqual(second["build_seconds"], 0.0)
        self.assertEqual(second_clone.get_seq_length(), 4)
        self.assertNotEqual(cloned.layers[0].keys.data_ptr(), second_clone.layers[0].keys.data_ptr())
        for layer, (expected_keys, expected_values) in zip(second_clone.layers, pristine_tensors):
            self.assertTrue(torch.equal(layer.keys, expected_keys))
            self.assertTrue(torch.equal(layer.values, expected_values))
        self.assertEqual(first["cache_bytes"], 2 * 2 * 1 * 2 * 4 * 8 * 4)

    def test_prefix_and_each_identity_field_change_miss(self) -> None:
        holder = PrefixCache(self.model, IDENTITY, max_entries=2)
        _, base = holder.get_or_build(self.prefix)
        changed_prefix = self.prefix.clone()
        changed_prefix[0, -1] = 9
        _, other = holder.get_or_build(changed_prefix)
        self.assertFalse(other["hit"])
        self.assertNotEqual(base["key"], other["key"])
        for field in IDENTITY:
            with self.subTest(field=field):
                changed_identity = dict(IDENTITY, **{field: IDENTITY[field] + "-changed"})
                _, result = holder.get_or_build(self.prefix, identity=changed_identity)
                self.assertFalse(result["hit"])
                self.assertNotEqual(base["key"], result["key"])
                self.assertLessEqual(result["retained_entries"], 2)

    def test_lru_bound_and_eviction(self) -> None:
        holder = PrefixCache(self.model, IDENTITY, max_entries=1)
        _, first = holder.get_or_build(self.prefix)
        _, second = holder.get_or_build(self.full)
        self.assertEqual(second["evicted_key"], first["key"])
        self.assertEqual(holder.stored_lengths, {second["key"]: 6})
        _, again = holder.get_or_build(self.prefix)
        self.assertFalse(again["hit"])

    def test_invalid_and_mismatched_inputs_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            PrefixCache(self.model, {})
        with self.assertRaises(ValueError):
            PrefixCache(self.model, dict(IDENTITY, dtype=""))
        with self.assertRaises(ValueError):
            PrefixCache(self.model, IDENTITY, max_entries=0)
        holder = PrefixCache(self.model, IDENTITY)
        for invalid in (torch.empty((1, 0), dtype=torch.long), self.prefix.float(), self.prefix.repeat(2, 1)):
            with self.assertRaises(ValueError):
                holder.get_or_build(invalid)
        cache, _ = holder.get_or_build(self.prefix)
        mismatch = self.full.clone()
        mismatch[0, 1] = 9
        with self.assertRaises(ValueError):
            cached_generation_inputs(mismatch, self.prefix, cache)
        with self.assertRaises(ValueError):
            cached_generation_inputs(self.prefix, self.prefix, cache)
        with self.assertRaises(ValueError):
            cached_generation_inputs(self.full, self.prefix[:, :-1], cache)

    def test_identity_snapshot_is_not_mutated_by_caller(self) -> None:
        caller_identity = dict(IDENTITY)
        holder = PrefixCache(self.model, caller_identity)
        _, first = holder.get_or_build(self.prefix)
        caller_identity["catalog_hash"] = "changed"
        _, second = holder.get_or_build(self.prefix)
        self.assertTrue(second["hit"])
        self.assertEqual(first["key"], second["key"])
        _, third = holder.get_or_build(self.prefix, identity=caller_identity)
        self.assertFalse(third["hit"])


if __name__ == "__main__":
    unittest.main()
