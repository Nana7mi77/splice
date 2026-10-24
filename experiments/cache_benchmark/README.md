# 本地前缀 KV 缓存实验

实验脚本独立于 `src/splice`，不会为生产 CLI 启用缓存，也不会调用云端模型。
使用已下载的 Qwen3-1.7B、CPU BF16、Transformers 4.57.6 / PyTorch 2.14.1+cpu。
详细结果见 [实验报告](../../docs/experiments/2026-10-23-cache/report.md)。

从仓库根目录运行，`--snapshot` 必须是本地 Hugging Face snapshot 目录：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv/bin/python -m experiments.cache_benchmark.run \
  --snapshot /path/to/models--Qwen--Qwen3-1.7B/snapshots/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e \
  --output /tmp/splice-prefix-cache-experiment --threads 2

.venv/bin/python -m experiments.cache_benchmark.summarize \
  /tmp/splice-prefix-cache-experiment/results.json \
  --output /tmp/splice-prefix-cache-experiment/summary.json

HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv/bin/python -m unittest experiments.cache_benchmark.test_prefix_cache -v
```

需要数 GB 内存及数分钟至十几分钟。输出目录内的 Registry、教学包和内存 Runtime 使用仓库已有的虚构业务数据；会生成实验用草稿，不连接真实业务资源。输出包含完整 prompt、原始生成 token、模型文本、计时和正确性证据。

`run.py` 固定模型、线程、贪心解码和 prompt，先预热并单独测量建缓存成本，再按 AB/BA/AB/BA 测四个不同金额的 16-token 配对，最后测 15/20 元两个完整输出配对。A 和 B 均保留请求内 KV；B 额外复用跨请求固定前缀。完整输出上限为 384 tokens，必须核对是否自然结束及语义是否正确。

首 token 计时取 streamer 收到首个生成 token 的时刻，包含查键和克隆；生成请求耗时截止 `generate()` 返回，不含预先 tokenize、最终文本解码、解析和 Runtime。`recheck.py` 把本次真实模型输出原样交给生产解析器，再执行默认、现金不足、无财务权限三个虚构场景。重放不会新增模型推理，格式修正与失败均记录。

`prefix_cache.py` 只适用于此实验的单进程、单调用者、CPU、无 padding、batch=1、完整注意力缓存。固定前缀从实际完整 Qwen3 模板文本中提取；逐 token 断言确保准确。每次返回深拷贝以隔离生成时的原地修改。键包含精确 token IDs、模型与 tokenizer revision、模板、后端、dtype、目录/教学包哈希等；保留项上限为 1。它没有实现多用户隔离、并发控制、持久化或生产生命周期。

`test_prefix_cache.py` 使用微型随机权重，仅验证 API 与隔离/失效机制，不能作为真实模型的性能证据。报告中的真实性能由 `run.py` 提供。
