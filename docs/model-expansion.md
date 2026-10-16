# 真实模型与动态能力实验

Splice 保留原有离线 Mock 演示，同时提供真实 DeepSeek 能力生产与本地小模型意图解析。业务数据始终使用仓库内的虚构快照；云端生成与本地推理是真实调用，不代表接入了真实外卖、账户或支付服务。

本轮研究的是：端侧遇到一个已有契约无法表达的条件后，能否生成可验证的新声明，明确授权发布，再通过持久化教学资料让本地模型在离线的新进程中使用它。0.2.0 实现这一流程的各个接口；候选验证、教学包安装和模型正确使用新能力需要分别证明。

## 配置与模型准备

先按 [README](../README.md#三分钟运行) 安装基础包。默认命令无需 API Key；只有云端测试和能力生成会访问 DeepSeek。

DeepSeek 配置默认保存在 `~/.config/splice/`，可通过 `SPLICE_CONFIG_DIR` 指向仓库之外的私有目录。目录权限为 `0700`，配置与凭据文件权限为 `0600`，凭据和可显示设置分开存放。`config deepseek` 使用 `getpass` 隐藏输入，命令行不接受明文 `--api-key`；`config show` 只显示是否已配置。环境变量 `DEEPSEEK_API_KEY` 优先于文件，清除本地文件不会清除当前 shell 的环境变量。

```bash
splice config init
splice config deepseek
splice config show
```

裸 `config deepseek` 会隐藏输入并保存或替换密钥。已经配置密钥后，可单独调整调用限制；以下命令不发起 API 请求：

```bash
splice config deepseek --model deepseek-flash --timeout 90 --max-tokens 6000 --max-calls 3
```

`splice cloud test` 是可选的真实联网探针，会消耗一次 API 调用。清除文件中保存的密钥使用 `splice config clear deepseek`。不要把真实密钥写进示例、Git 或 shell 命令参数。

云端默认使用 `https://api.deepseek.com` 和 `deepseek-flash`，以 JSON 模式请求声明，关闭额外思考输出。默认每个客户端实例最多 3 次调用、单次总超时 90 秒、最多 6000 输出 tokens；生成最多修复一次，网络或鉴权失败不会自动重试。调用额度是本次流程的上限，不是跨进程的月度账户预算。离线模式拒绝任何云调用。

模型配置记录实际下载的 Hub revision、缓存位置、推理后端和依赖版本。`model setup` 联网下载，推理阶段只加载本地缓存，`trust_remote_code=false`；模型推理失败会报告错误，不会悄悄换成 Mock 或云端解析。

| 用途 | 后端与默认模型 | 下载估算 | 实测边界 |
| --- | --- | --- | --- |
| Apple Silicon 首选 | `mlx-vlm` / `mlx-community/Qwen3.5-2B-4bit` | 约 1.8 GB | 实现了路径，尚无 M1 8GB 实测 |
| Apple Silicon 备用 | `mlx-lm` / `mlx-community/Qwen3-1.7B-4bit` | 约 1.2 GB | 需用户显式选择，没有自动切换 |
| Linux CPU 开发验证 | `transformers` / `Qwen/Qwen3-1.7B`，BF16 | 约 4.1 GB | 真实 CPU 推理，不能替代 Mac 验收 |

首选模型的模型卡使用 `mlx-vlm 0.3.12`，安装对应依赖组：

```bash
python -m pip install -e '.[local]'
splice model setup --backend mlx-vlm --yes
splice model status
splice model test
```

如需在 Apple Silicon 上试备用模型，明确选择：

```bash
splice model setup --backend mlx-lm --yes
```

Linux CPU 验证使用独立依赖组。先安装 CPU 版 PyTorch 可避免下载不需要的 CUDA 包：

```bash
python -m pip install --index-url https://download.pytorch.org/whl/cpu 'torch>=2.6,<3'
python -m pip install -e '.[local-cpu]'
splice model setup --backend transformers --yes
splice model status
```

`--yes` 仅确认模型下载。缓存目录默认遵从 Hugging Face 配置；在 home 只读的开发环境中，可事先将 `HF_HOME` 设置为可写路径。`model setup --backend auto` 只在 Apple Silicon 选择首选 MLX 后端，在 Linux 会要求显式选择 CPU 后端。

## 验收目标与扩展边界

实验条件是“吃完后，本月餐饮预算至少保留 N 元”。默认虚构数据预算剩余 40 元，含配送费的最便宜清淡盖饭为 24 元，拟消费后剩 16 元。因此 15 元和 16 元应允许草稿，20 元、50 元和 16.01 元应阻止草稿。

独立规范 [budget-reserve.json](../examples/experiments/budget-reserve.json) 指定能力接口与期望行为，但不包含最终执行 DSL。生成组件消费 `budget.assessment` 和 `budget.min_remaining_cny`，输出 `budget.reserve_preserved`；原有 `budget.within_limits` 条件必须保留，不能用保留预算判断替换现金与预算检查。

| 独立用例 | 预期执行结果 | 新增草稿 |
| --- | --- | --- |
| 保留 15 / 16 / 0 元 | `completed` | 每例 1 |
| 保留 20 / 50 / 16.01 元 | `blocked`，`GateRejected` | 0 |
| 无金融预算权限 | `blocked`，`PermissionDenied` | 0 |
| 现金不足、仍持有投资 | `blocked`，`GateRejected` | 0 |
| 月预算改为 1100 元、消费后要求保留 50 元 | `completed`，预计剩 116 元 | 1 |
| 月预算改为 990 元、消费后要求保留 15 元 | `blocked`，预计剩 6 元但预算与现金仍足够支付 | 0 |

共十个独立用例，后两例改变上游数据，以检查生成逻辑是否使用实际评估结果，而不是记住默认余额 16。教学标签另外要求三种完整请求：含配送费上限都为 30 元，分别保留 15、20、50 元，且保留原有预算与现金条件。

当前协议要求人工提供独立验收契约和样本，只允许新增 ID 的只读布尔输出能力。生成代码不能新增 Runtime Handler、执行任意 Python/JS/SQL、获得写权限或改写已有插件。通用 Planner 仍负责依赖、提供者歧义和环检测；同一 Runtime 执行原有与新生成声明。

`--confirm` 授权本次本地草稿，`--approve-expansion` 授权通过验收的候选发布到本地 Registry。两者是不同操作；发布候选不授权草稿，也不会把组件上传到公共市场。没有发布授权时，候选保留在 `verified`，可检查文件后单独批准。

## 从缺口到离线重试

先选择一个全新状态目录，避免已安装能力影响缺口实验。配置与模型缓存可以继续使用，不需要再次下载：

```bash
SPLICE_LOOP_STATE=$(mktemp -d)
SPLICE_REQUIREMENT='帮我找一份清淡盖饭，含配送费不超过30元，检查本月餐饮预算和可用现金，只生成订单草稿，不真正下单或支付；吃完这顿饭后，这个月至少还要剩20元餐饮预算。'
splice init --state-dir "$SPLICE_LOOP_STATE"
splice run "$SPLICE_REQUIREMENT" --adapter local --offline --profile combined --confirm --experiment examples/experiments/budget-reserve.json --state-dir "$SPLICE_LOOP_STATE" --json
```

初始目录缺少保留预算能力，这次请求应在写入之前被拒绝。错误 JSON 区分缺少能力、未解决条件与模型契约错误。自动扩展只处理可识别的能力缺口；JSON 格式错误、权限拒绝、条件不成立，以及模型完全遗漏必需条件，都不会触发云端生成。

若要执行一次真实云端生成，可用同一个请求启用自动扩展。此命令包含对本地发布和草稿写入的明确预授权；验证通过才发布，发布后只重试一次本地解析：

```bash
splice run "$SPLICE_REQUIREMENT" --adapter local --auto-expand --approve-expansion --profile combined --confirm --experiment examples/experiments/budget-reserve.json --state-dir "$SPLICE_LOOP_STATE" --json
```

省略 `--approve-expansion` 时，候选停留在 `verified`，不会执行请求。检查输出的 `candidate_dir` 中的声明、教学包与报告后，使用 `splice expand approve CANDIDATE_DIR --state-dir "$SPLICE_LOOP_STATE"` 发布并安装教学包。发布时会重新检查内容哈希与独立用例。

也可以把生成与本地解析分开。以下命令是人工提出扩展需求，不应在证据中冒充本地模型自动发现了缺口；不加 `--approve` 会保留候选供检查：

```bash
splice expand propose --requirement "$SPLICE_REQUIREMENT" --experiment examples/experiments/budget-reserve.json --state-dir "$SPLICE_LOOP_STATE" --json
```

为了不重复消耗 API，可在另一个全新状态目录中复用仓库保存的真实生成包，重新验收、授权发布和安装教学包：

```bash
SPLICE_REPLAY_STATE=$(mktemp -d)
splice expand verify docs/experiments/2026-10-15/bundle.json --experiment examples/experiments/budget-reserve.json --approve --state-dir "$SPLICE_REPLAY_STATE" --json
splice learn list --state-dir "$SPLICE_REPLAY_STATE"
```

此路径报告 `producer=imported_bundle`、`cloud_calls=[]`，只复验已有产物，没有重新调用云端。若需要先检查候选，省略 `--approve`，再用 `expand approve`。单独导入教学包可用 `splice learn install PATH --state-dir STATE`，但对应插件必须已经存在，且版本与哈希必须匹配。

安装后，用新进程、不同措辞和 15 元条件重试。以下命令使用复验目录；若刚执行的是云端生成路径，请把状态目录换成 `"$SPLICE_LOOP_STATE"`：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 splice run '给我挑份清淡的盖饭，送到手最多30元。先核对本月餐饮预算和能用的现金，这餐之后预算至少留15元，符合才建订单草稿，不要真正下单，不要支付。' --adapter local --offline --profile combined --confirm --experiment examples/experiments/budget-reserve.json --state-dir "$SPLICE_REPLAY_STATE" --json
```

完整意图解析正确时，默认数据应得到 24 元草稿、预计预算剩余 16 元、两个条件均通过，且 `cloud_calls=[]`。将保留金额改为 20 或 50 元，应由预算保留条件阻止写入。模型错误会明确报告，不能把契约拒绝计为该行为用例通过。`--offline` 禁止扩展流程请求云端；本地推理始终只使用缓存模型。它不是操作系统级断网开关。

## 教学资料如何生效

云端同时生成 Manifest、理由、声明式行为样本与 Learning Pack。包中包含插件 ID、版本、描述、关键词、正反例、事实依赖、权限及支持版本。Manifest SHA-256 由本地计算封装，避免依赖模型计算哈希。教学标签必须通过意图契约检查，并实际绑定新能力；例句不能替用户授权，也不能把计算结果冒充初始事实。

发布前会在隔离 Registry 与内存 SQLite 中执行生成样本和独立验收。通过后才允许本地发布并安装教学包，安装时检查版本与哈希；新进程读取时再次核对当前 Registry。相关教学资料通过词法匹配进入提示上下文，不是 embedding 索引。

报告中 `adaptation_mode=dynamic_context`、`weights_updated=false` 的含义是更新持久化上下文资料，没有训练、RL、LoRA 或模型权重更新。安装成功只说明资料可供读取；是否正确处理新措辞、新数值以及多个条件，必须用实际本地模型另外验证。

已报告的未知约束和模型契约错误会在任何能力执行前拒绝。`run --experiment` 在所有模式下都会另行核对规范中的全部 `required_gates`，包括离线运行和未启用自动扩展的运行。它能发现已指定实验条件的整体遗漏；普通不带验收文件的本地请求仍没有完整语义保证。正常的 `GateRejected` 或 `PermissionDenied` 表示现有规则正在生效，不能把它们当作能力缺口再生成组件绕开。离线重试只能使用已安装能力与缓存模型，不能请求云端补齐。

## 2026-10-15 至 16 日真实证据

本次机器为 Linux x86_64 CPU，使用 `Qwen/Qwen3-1.7B` BF16，revision 为 `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`。没有 Apple Silicon 或手机设备。CPU 端首次三个请求均因输出契约错误而拒绝，不能记为通过；原始模型输出和错误保留在 [local-before.json](experiments/2026-10-15/local-before.json)。

| 首轮本地请求 | 结果 | 推理报告延迟 | 进程峰值内存 |
| --- | --- | --- | --- |
| 已有跨域请求 | 意图把自然语言写进事实标识，拒绝 | 305.963 秒 | 约 4.16 GiB |
| 未知预算保留条件 | 缺少必需 `facts` 字段，拒绝 | 159.417 秒 | 约 4.27 GiB |
| 已有外卖草稿请求 | 缺少确认等字段，拒绝 | 223.040 秒 | 约 4.27 GiB |

以上是各次调用的实测值，不是基准均值；峰值来自进程累计最大 RSS，首个调用包含模型加载。它们证明执行了真实模型并安全拒绝错误输出，不证明理解能力已达到目标。

首轮真实 DeepSeek 生成得到 `finance.budget.reserve_check@0.1.0`，组件通过当时的八个独立用例。后续教学审查发现例句错误，包括把计算事实作为输入、无依据改变月份及遗漏草稿输出；组件验收通过不能代替教学质量验收。在一次性实验状态中撤回首轮产物、恢复四个基线能力后再验证修复版；常规 Registry 仍禁止覆写同一版本。这是实验清理，没有新增生产撤销接口。首轮候选的历史发布状态已撤销，原始材料保存在 [attempt-1](experiments/2026-10-15/attempt-1/cloud-generation.json)。

加强教学标签检查并加入两组上游预算变化用例后，唯一一次真实修复生成了最终组件与教学包，通过十个独立用例、三条生成行为样本及三组教学计划检查。三条例句同时表达 30 元含配送费上限、15 / 20 / 50 元保留金额、预算与现金条件和草稿目标。最终 Manifest SHA-256 为 `5c7ab506e4a2705fb4d377070d682c96af5092b493c6a476694f5fed7f178cdb`；仓库版本 0.2.0 与生成插件版本 0.1.0 分别标识软件和组件。

| 云端调用 | 状态 | API 延迟 | 输入 / 输出 tokens | 已知总 tokens |
| --- | --- | --- | --- | --- |
| 连通性探针 | 环境重启中断，未取得响应 | 未知 | 未知 | 未知 |
| 首次生成 | 返回成功，教学包后续审查撤销 | 11.513 秒 | 9544 / 2666 | 12210 |
| 唯一一次修复 | 返回成功，最终候选通过加强后的验收并发布 | 9.600 秒 | 12257 / 2409 | 14666 |

共尝试三次调用，两次取得响应；已知 token 合计 **26876**，不包含探针的未知用量。API 没有返回费用金额，不能把中断探针计费记为零，也不能把 token 数直接当作实际账单。最终 [云端验收报告](experiments/2026-10-15/cloud-generation.json)、[生成包](experiments/2026-10-15/bundle.json)、[教学包](experiments/2026-10-15/learning-pack.json) 与 [调用台账](experiments/2026-10-15/cloud-call-ledger.json) 可分别核对。

压缩提示词并加载教学包后，15 / 20 元新措辞均输出了正确的参数与两道 gate，但都多生成一个右花括号，严格解析仍拒绝；原始记录见 [格式失败记录](experiments/2026-10-15/local-after-format-failure.json)。适配器随后增加狭窄的格式处理：只允许完整 JSON 对象之后恰好多一个 `}`，记录 `format_corrections` 并保留原始输出；额外对象、说明文字、重复键、非有限数值、缺失字段和不完整对象仍拒绝，不修改任何语义值。

最终使用全新 CLI 进程加载缓存模型与持久化教学包，`--offline --experiment ... --confirm` 下，15 元新措辞真实推理成功：生成 **24 元草稿**、预计预算剩 **16 元**、现金剩 **4176 元**，两道 gate 均通过，`cloud_calls=[]`。推理报告为 **218.685 秒**、峰值 RSS **4296663040 字节（约 4.00 GiB）**、1844 输入 tokens / 126 输出 tokens，记录了一次上述纯格式纠正。完整原文、计划和授权轨迹见 [真实离线 CLI 结果](experiments/2026-10-15/local-offline-cli.json)。

另将已经保存的真实 15 / 20 元模型输出交给修复后的解析器复核，分别得到完成一份草稿、`GateRejected` 且零草稿；这一步是 [历史输出重放](experiments/2026-10-15/recorded-output-recheck.json)，没有重新进行模型推理，不将其重复计作新推理。20 / 50 元、现金不足及权限缺失的组件行为仍由十项独立用例验证。

同一最终提示协议、同一 15 元请求，在只有四个基线组件且没有学习包的新进程对照中仍被拒绝：模型用 `budget.remaining_cny >= 15` 冒充事实标识，83.191 秒后触发 Schema 错误，未执行能力。见 [未安装能力的真实对照](experiments/2026-10-15/local-control-without-capability.json)。它没有正确输出可自动扩展的结构化缺口，因此这次真实云生产是人工触发的分阶段实验；CLI 自动扩展入口通过合成边界测试，尚未在该本地模型上实证自动完成整条路径。

本次证明人工验收规范指导下的真实云端生产、持久化教学和一次未见措辞的本地离线消费可运行；没有完成开放域准确率评估、M1 8GB 真机验收，也没有证明无需人工规范的自动缺口建模。

## 隐私与可复现性

云端生成请求只包含需求、公开 Schema、声明目录及筛选后的失败诊断，不上传 Runtime 输出、账户、持仓或原始消费流水。仓库中的记录全部使用虚构数据。凭据不写入 Git、模型提示词、打印输出或异常详情；清理时使用配置命令，不将密钥写进 shell 参数。若密钥曾在聊天或其他公开可见文本中出现，测试完成后应在提供方控制台轮换。

模型源与接口依据：

- [DeepSeek 当前模型与定价](https://api-docs.deepseek.com/quick_start/pricing)、[JSON 模式](https://api-docs.deepseek.com/guides/json_mode)。
- [Qwen3.5-2B-4bit / mlx-vlm 模型卡](https://huggingface.co/mlx-community/Qwen3.5-2B-4bit)。
- [Qwen3-1.7B-4bit / mlx-lm 模型卡](https://huggingface.co/mlx-community/Qwen3-1.7B-4bit)。
- [Qwen3-1.7B CPU 模型源](https://huggingface.co/Qwen/Qwen3-1.7B/tree/70d244cc86ccca08cf5af4e1e306ecf908b1ad5e)。
