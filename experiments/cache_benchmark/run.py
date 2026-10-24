"""Offline, controlled experiment; does not change Splice's production path.

Run from the repository root with ``python -m experiments.cache_benchmark.run``.
Only an already-downloaded model snapshot is accepted. No cloud API is used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cpu_stat():
    path = Path('/sys/fs/cgroup/cpu.stat')
    return {k: int(v) for k, v in (line.split() for line in path.read_text().splitlines())} if path.exists() else {}


def rss():
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1]) * 1024
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from transformers.generation.streamers import BaseStreamer
    imports_s = time.perf_counter() - started
    from splice.registry import Registry
    from splice.learning import LearningStore
    from splice.harness import Harness, MockCapabilityProducer
    from splice.local_intent import LocalOnDeviceIntentAdapter
    from .prefix_cache import PrefixCache, cached_generation_inputs

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(0)
    repo = Path(__file__).resolve().parents[2]
    state = args.output / 'state'
    registry = Registry(state / 'registry')
    Harness(registry, MockCapabilityProducer(repo / 'examples/plugins')).publish_examples()
    registry.publish(json.loads((repo / 'docs/experiments/2026-10-15/manifest.json').read_text()))
    learning = LearningStore(state / 'learning')
    learning.install(json.loads((repo / 'docs/experiments/2026-10-15/learning-pack.json').read_text()), registry)
    adapter = LocalOnDeviceIntentAdapter(registry, learning, None)
    catalog = registry.all()
    base = '给我挑份清淡的盖饭，送到手最多30元。先核对本月餐饮预算和能用的现金，这餐之后预算至少留{reserve}元，符合才建订单草稿，不要真正下单，不要支付。'
    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(str(args.snapshot), local_files_only=True, trust_remote_code=False)
    tokenizer_load_s = time.perf_counter() - started
    started = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        str(args.snapshot), local_files_only=True, trust_remote_code=False,
        torch_dtype=torch.bfloat16, low_cpu_mem_usage=True,
    ).to('cpu').eval()
    model_load_s = time.perf_counter() - started

    requests = {}
    for reserve in (15, 20, 50, 16):
        utterance = base.format(reserve=reserve)
        packs = learning.relevant(utterance, registry)
        messages = adapter._messages(utterance, catalog, packs)
        started = time.perf_counter()
        full_text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        # Qwen3 changes the final assistant example when templating a truncated
        # conversation (it can insert <think> tags). Extract from the ACTUAL full
        # rendering, at a complete turn boundary, never re-render the prefix.
        user_marker = '<|im_start|>user\n'
        prefix_text, separator, last_turn = full_text.rpartition(user_marker)
        assert separator and last_turn.startswith(utterance + '<|im_end|>')
        full_ids = tokenizer(full_text, return_tensors='pt')['input_ids']
        prefix_ids = tokenizer(prefix_text, return_tensors='pt')['input_ids']
        assert torch.equal(full_ids[:, :prefix_ids.shape[1]], prefix_ids)
        requests[reserve] = dict(utterance=utterance, messages=messages, packs=packs,
                                 full_ids=full_ids, prefix_ids=prefix_ids,
                                 tokenization_seconds=time.perf_counter() - started)
    first = requests[15]
    assert all(torch.equal(first['prefix_ids'], r['prefix_ids']) for r in requests.values())
    identity = dict(model_revision=args.snapshot.name, tokenizer_revision=args.snapshot.name,
                    chat_template=digest(tokenizer.chat_template), backend='transformers-cpu',
                    dtype=str(model.dtype), catalog_hash=digest(catalog),
                    learning_hash=digest(first['packs']),
                    attention_implementation=model.config._attn_implementation,
                    transformers=transformers.__version__, torch=torch.__version__,
                    default_month='2026-10', enable_thinking=False)
    cache = PrefixCache(model, identity, max_entries=1)
    report = dict(status='running', timestamp_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  scope='Resident CPU model; cross-request prefix KV reuse only; not production CLI timing',
                  prefix_extraction='Actual rendered full prompt before final Qwen user turn, exact token prefix asserted',
                  git_head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
                  cloud_calls=[], offline=True, model='Qwen/Qwen3-1.7B', identity=identity,
                  environment=dict(cpu_count=os.cpu_count(), affinity=sorted(os.sched_getaffinity(0)),
                      cpu_quota=Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
                      memory_limit_bytes=Path('/sys/fs/cgroup/memory.max').read_text().strip(),
                      threads=torch.get_num_threads(), interop_threads=torch.get_num_interop_threads()),
                  startup=dict(imports_seconds=imports_s, tokenizer_load_seconds=tokenizer_load_s,
                      model_load_seconds=model_load_s, rss_after_load_bytes=rss()),
                  requests={str(k): dict(utterance=v['utterance'], messages=v['messages'],
                      full_token_ids=v['full_ids'][0].tolist(), prefix_token_ids=v['prefix_ids'][0].tolist(),
                      prompt_tokens=v['full_ids'].shape[1], prefix_tokens=v['prefix_ids'].shape[1],
                      suffix_tokens=v['full_ids'].shape[1]-v['prefix_ids'].shape[1],
                      tokenization_seconds=v['tokenization_seconds']) for k, v in requests.items()}, runs=[])

    def save():
        target = args.output / 'results.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(target)

    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    class FirstTokenClock(BaseStreamer):
        def __init__(self):
            self.prompt_seen = False
            self.first_token_at = None
        def put(self, value):
            if not self.prompt_seen:
                self.prompt_seen = True
            elif self.first_token_at is None:
                self.first_token_at = time.perf_counter()
        def end(self):
            pass

    # Untimed-for-comparison warmup, separately recorded; no reusable KV remains.
    emit(dict(stage='loaded', startup=report['startup'], prefix_tokens=first['prefix_ids'].shape[1]))
    save()
    warm_ids = tokenizer('A short local CPU warmup.', return_tensors='pt')
    started = time.perf_counter()
    with torch.inference_mode():
        model.generate(**warm_ids, max_new_tokens=2, min_new_tokens=2, do_sample=False,
                       use_cache=True, pad_token_id=tokenizer.eos_token_id)
    report['warmup_seconds'] = time.perf_counter() - started
    started = time.perf_counter()
    initial_cache, build = cache.get_or_build(first['prefix_ids'])
    report['cache_initialization'] = dict(**build, lookup_build_clone_seconds=time.perf_counter()-started,
                                        rss_with_template_and_clone_bytes=rss())
    del initial_cache
    report['cache_initialization']['rss_after_discarding_initial_clone_bytes'] = rss()
    emit(dict(stage='cache_built', metadata=report['cache_initialization']))
    save()

    def run(name, reserve, mode, full):
        req = requests[reserve]
        forward_times, input_lengths, stack = [], [], []
        def pre(module, positional, keyword):
            stack.append(time.perf_counter())
            ids = keyword.get('input_ids')
            input_lengths.append(int(ids.shape[1]) if ids is not None else None)
        def post(module, positional, keyword, output):
            forward_times.append(time.perf_counter()-stack.pop())
        h1 = model.register_forward_pre_hook(pre, with_kwargs=True)
        h2 = model.register_forward_hook(post, with_kwargs=True)
        clock = FirstTokenClock()
        before = cpu_stat()
        rss_before = rss()
        emit(dict(stage='run_start', name=name, mode=mode, reserve=reserve, full=full))
        started = time.perf_counter()
        metadata = None
        if mode == 'prefix_hit':
            fresh, metadata = cache.get_or_build(req['prefix_ids'])
            assert metadata['hit']
            kwargs = cached_generation_inputs(req['full_ids'], req['prefix_ids'], fresh)
        else:
            kwargs = dict(input_ids=req['full_ids'], attention_mask=torch.ones_like(req['full_ids']), use_cache=True)
        generation_started = time.perf_counter()
        with torch.inference_mode():
            output = model.generate(**kwargs, max_new_tokens=384 if full else 16,
                                    min_new_tokens=0 if full else 16, do_sample=False,
                                    pad_token_id=tokenizer.eos_token_id, streamer=clock)
        ended = time.perf_counter()
        h1.remove()
        h2.remove()
        after = cpu_stat()
        ids = output[0, req['full_ids'].shape[1]:].tolist()
        assert input_lengths[0] == (req['full_ids'].shape[1]-req['prefix_ids'].shape[1] if mode=='prefix_hit' else req['full_ids'].shape[1])
        assert all(length == req['prefix_ids'].shape[1] for length in cache.stored_lengths.values())
        row = dict(name=name, reserve=reserve, mode=mode, full_completion=full,
                   elapsed_seconds=ended-started, generate_seconds=ended-generation_started,
                   first_token_seconds=clock.first_token_at-started,
                   first_forward_seconds=forward_times[0], first_forward_input_tokens=input_lengths[0],
                   decode_forward_seconds=sum(forward_times[1:]), decode_forward_count=len(forward_times)-1,
                   mean_decode_forward_seconds=sum(forward_times[1:])/(len(forward_times)-1),
                   generated_tokens=len(ids), output_token_ids=ids,
                   output_text=tokenizer.decode(ids, skip_special_tokens=True),
                   ended_with_eos=ids[-1] == tokenizer.eos_token_id,
                   cache=metadata, stored_cache_lengths=cache.stored_lengths,
                   rss_before_bytes=rss_before, rss_after_bytes=rss(),
                   process_cumulative_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                   cpu_stat_delta={k:after[k]-before[k] for k in before})
        report['runs'].append(row)
        save()
        emit({k: row[k] for k in ('name','elapsed_seconds','first_token_seconds','generated_tokens','first_forward_input_tokens','ended_with_eos')})
        if full:
            from .recheck import recheck_completion
            check_started = time.perf_counter()
            row['correctness'] = recheck_completion(row['output_text'], req['utterance'], registry, learning)
            row['correctness_recheck_seconds'] = time.perf_counter() - check_started
            save()
            emit(dict(stage='correctness', name=name, result=row['correctness']))
        return row

    try:
        for i, reserve in enumerate((15, 20, 50, 16)):
            modes = ('no_prefix_reuse', 'prefix_hit') if i % 2 == 0 else ('prefix_hit', 'no_prefix_reuse')
            for mode in modes:
                run(f'short_{i+1}_{mode}', reserve, mode, False)
        for i, reserve in enumerate((15, 20)):
            modes = ('no_prefix_reuse', 'prefix_hit') if i % 2 == 0 else ('prefix_hit', 'no_prefix_reuse')
            for mode in modes:
                run(f'full_{reserve}_{mode}', reserve, mode, True)
        report['comparisons'] = []
        for full, reserves in ((False, (15,20,50,16)), (True, (15,20))):
            for reserve in reserves:
                a, b = (next(r for r in report['runs'] if r['full_completion']==full and r['reserve']==reserve and r['mode']==mode)
                        for mode in ('no_prefix_reuse','prefix_hit'))
                ai, bi = a['output_token_ids'], b['output_token_ids']
                first_difference = next((i for i, (x, y) in enumerate(zip(ai, bi)) if x!=y), None)
                if first_difference is None and len(ai)!=len(bi):
                    first_difference=min(len(ai),len(bi))
                report['comparisons'].append(dict(full_completion=full, reserve=reserve, exact_token_match=ai==bi,
                    first_differing_token_index=first_difference,
                    elapsed_seconds_saved=a['elapsed_seconds']-b['elapsed_seconds'],
                    elapsed_reduction_fraction=1-b['elapsed_seconds']/a['elapsed_seconds'],
                    first_token_reduction_fraction=1-b['first_token_seconds']/a['first_token_seconds']))
        report['status']='completed'
    except Exception as exc:
        report['status']='failed'
        report['error']=dict(type=type(exc).__name__, message=str(exc))
        save()
        raise
    save()
    emit(dict(stage='completed', comparisons=report['comparisons']))


if __name__ == '__main__':
    main()
