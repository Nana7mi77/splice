# Splice — Composable Service Access for Agents

**面向 Agent 的组合式服务访问。**

Agent 按意图组合访问底层服务，无需预制中间业务 Service。可组合逻辑由数据驱动插件表达，通用 Runtime 执行，Gateway 约束权限与状态；目标是省去为每个业务流程手写的专用中间层，底层服务、数据资源与可信执行边界仍承担各自职责。

我们并不预测代码消失；我们研究的是：完整应用与人工预制的业务服务，是否还必须是软件被生产和消费的默认形态。智能成本下降可能改变应用入口、企业内固定流程，以及通用数字劳动与真实业务资源之间的价值分配。这些是待检验的产业假说，不是原型已经证明的事实。

Splice 提出 **cloud-produced, on-device-intent, data-driven-plugin** 的分工：云端高能力模型在 Harness 中生产版本化能力；终端轻量模型理解目标并发现能力；统一 Runtime 根据数据契约组合执行；Gateway 保留数据、授权与不可违反状态转移的可信边界。

**当前交付：思想纲领 + 架构 RFC + 可运行的离线技术原型。** 生产侧与端侧都是明确标注的 Mock；首版通过本地虚构数据和 Runtime 模拟服务访问与组合，尚未接入真实远程底层服务、真实模型或手机部署，也没有规模验证。项目名称为 **Splice**，许可证为 [Apache-2.0](LICENSE)。

先读 [思想纲领 THESIS](docs/THESIS.md)，再看 [架构与边界](ARCHITECTURE.md) 和 [RFC-0001](RFC/0001-intent-driven-software.md)。这三层分别讨论产业假说、可研究的架构命题和本版能检验的行为，不能互相替代。

## 三分钟运行

需要 Python 3.10+，运行依赖只有 `jsonschema`。克隆仓库后运行：

```bash
git clone https://github.com/Nana7mi77/splice.git
cd splice
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
splice demo combined
```

Windows 激活命令为 `.venv\Scripts\activate`。安装依赖后，所有默认演示均可离线运行，无需 API Key、数据库服务或模型下载。若环境已装 `jsonschema`，也可以直接运行：

```bash
PYTHONPATH=src python -m splice demo combined
```

CLI 在当前目录的 `.splice/` 下保存本地 Registry 和 SQLite 草稿。可用 `--state-dir PATH` 选择独立实验目录；重复运行会新增草稿，草稿不会占用预算。虚构数据固定在 **2026-10**，“本月”不跟随系统日期。

## 外卖 × 个人资产：同一个生活目标

外卖消费会影响餐饮预算与可支付现金，而投资持仓估值不等于现金。首版用四个独立 Manifest 把这条联系表达为可观察的数据依赖：

```mermaid
flowchart LR
  H[Mock Harness：模板生产与验证] --> R[版本化 Registry]
  U[自然语言目标] --> I[Mock 端侧 Adapter]
  I --> P[按 x-fact 契约发现与绑定]
  R --> P
  P --> M[外卖菜单：含配送费总价]
  M --> B[预算与现金评估]
  B --> G[条件满足且授权]
  G --> D[受控写入本地草稿]
  P --> A[独立资产概览]
```

这里没有 `order_food_with_budget` 专用 Handler。意图仅表达目标事实、初始约束和执行条件；Planner 从 Manifest 的输入输出 `x-fact` 标注反向找依赖；各插件都通过同一个解释器执行。外卖插件只获得餐品、消费金额和价格上限，金融账户、持仓与原始流水不传给它。

分别运行三个场景：

```bash
splice demo food       # A：30 元内清淡盖饭，含配送费，生成草稿
splice demo assets     # B：现金、投资估值、餐饮支出和预算汇总
splice demo combined   # C：先评估预算与现金，允许时才生成草稿
```

默认输出会展示插件版本、Schema 绑定、逐步输入与产出、Gateway 授权和最终结果。预期结果摘录如下；草稿 ID 每次不同：

| 场景 | 实际虚构数据结果 |
| --- | --- |
| A 外卖 | 青禾食堂香菇鸡肉盖饭，餐价 21 + 配送 3 = **24 元**，`status=draft`；另一个清淡候选含费 28 元，33 元餐品被排除 |
| B 资产 | 现金 **4200**，投资估值 **10500**，总资产 **14700**；十月餐饮已花 **960**，预算剩 **40** |
| C 跨域 | 菜单 → 预算评估 → 条件检查 → 草稿；计划消费 **24**，预计预算余额 **16**，预计现金 **4176** |

以上余额是拟消费后的模拟值。账户、持仓、消费流水和预算均不修改；唯一写入是本地未提交草稿，没有真实订单、支付或投资操作。

也可提交受支持的自然语言样本或查看完整结构化轨迹：

```bash
splice run '帮我找今晚30元以内的盖饭，算上配送费，口味清淡，先生成订单草稿，不要真正下单。'
splice run '找30元以内的盖饭' --profile search
splice demo combined --json
```

Mock Adapter 支持三个演示句式族及只读外卖搜索变体，允许有限的金额、月份和清淡偏好替换；陌生从句与复杂限制会明确拒绝。完整样本文本见 [intent.py](src/splice/intent.py)。自然语言跨域请求需加 `--profile combined`，资产请求需加 `--profile assets`；普通 `run` 默认采用 `food` 权限配置。

## 五个可复现实验

使用独立状态目录可避免以前发布的版本影响结果：

```bash
SPLICE_EXPERIMENT_STATE=$(mktemp -d)
splice init --state-dir "$SPLICE_EXPERIMENT_STATE"
```

**1. 发现与分发。** 生产侧 Mock 从受控模板生成声明，经验证后发布；描述搜索返回匹配词、分数和版本。它不声称真的用大模型生成了新业务能力。

```bash
splice produce food.menu.search --state-dir "$SPLICE_EXPERIMENT_STATE"
splice search '盖饭 配送费' --state-dir "$SPLICE_EXPERIMENT_STATE"
```

**2. 契约约束。** 以下命令应退出 `2`，分别拒绝负金额和不存在的插件：

```bash
splice invoke finance.budget.evaluate --input '{"amount_cny":-1,"month":"2026-10"}' --profile assets --state-dir "$SPLICE_EXPERIMENT_STATE"
splice invoke missing.plugin --input '{}' --state-dir "$SPLICE_EXPERIMENT_STATE"
```

**3. 独立权限。** 下面分别撤去草稿写权限、金融预算权限和资产概览权限。每条都应拒绝，无新增草稿；金融数据未知时不会声称预算充足。

```bash
splice demo food --grants 'food:read' --state-dir "$SPLICE_EXPERIMENT_STATE"
splice demo combined --profile food --state-dir "$SPLICE_EXPERIMENT_STATE"
splice demo assets --grants 'finance_budget:read' --state-dir "$SPLICE_EXPERIMENT_STATE"
```

**4. 跨域组合与声明替换。** 同一个意图、同一条执行路径，仅发布新版本的菜单排序声明，选择从 24 元变为 28 元，预计餐饮预算余额从 16 变为 12 元：

```bash
splice demo combined --state-dir "$SPLICE_EXPERIMENT_STATE"
splice publish examples/extensions/food.menu.search.v2.json --state-dir "$SPLICE_EXPERIMENT_STATE"
splice demo combined --state-dir "$SPLICE_EXPERIMENT_STATE"
```

扩展文件把合格候选改成价格降序，用于展示声明改变如何影响选择，不代表更优的推荐策略。旧版本仍保留；每个 ID 默认选择最高数字版本。测试还发布一个全新 ID 与输出事实，验证描述检索、事实发现及同一运行时绑定。

**5. 预算不足与现金语义。** 测试把虚构剩余预算降到 10 元，验证返回评估但不生成草稿；再保留 10500 元持仓、将现金降到 5 元，验证不会把持仓视作可支付现金。手动修改临时样本并复现的完整命令见 [RFC 实验](RFC/0001-intent-driven-software.md#6-可复现实验与反证条件)。

运行全部关键行为验证：

```bash
python -m unittest discover -s tests -v
```

测试还覆盖外卖插件越界读取金融数据、未声明资源、伪造草稿价格、Adapter 伪造预算批准、用户授权、所有者隔离，以及草稿写入后输出 Schema 失败时的事务回滚。所有测试使用临时 Registry 和内存 SQLite，不接触外部账号。实际运行记录见 [验证记录](docs/verification.md)。

## 原型的边界

| 机制 | 已实现 | 尚未实现 / 限制 |
| --- | --- | --- |
| 能力生产 | `CapabilityProducer` 接口、Mock 模板、Schema 检查、不可覆盖的版本发布 | `LLMCapabilityProducer` 仅扩展占位；没有真实模型生成和自主验证 |
| 意图消费 | `OnDeviceIntentAdapter`、受限确定性中文样本解析 | 无手机部署、开放域 NLU 或端侧推理实测 |
| 能力发现 | 英文词和中文双字词的描述检索；按 `x-fact` 精确查生产者 | 描述搜索不决定可执行性；新事实语义需要约定，多提供者歧义拒绝 |
| 规划与执行 | 受限无环依赖、参数与输出校验、8 种通用步骤操作 | 无任意分支/循环、未知目标分解、补偿或兼容版本求解 |
| 数据与权限 | 9 类资源、领域/声明/用户授权交集、所有者过滤、本地事务草稿 | 本地信任的 Manifest 和固定用户；无第三方签名、撤销或生产认证 |
| 资产语义 | 现金、持仓、估值、流水、月预算各自建模 | 无实时行情、真实账户、预算预占；重复草稿可能使用同一份预算 |

通用步骤为 `read / filter / project / aggregate / compare / sort / assert / insert`；关联通过 `lookup` 表达式完成。插件不执行 Python/JS/SQL。Gateway 的可信 [资源策略](specs/resources.json) 仍需预置领域数据语义、草稿状态和价格不变量；这部分维护成本必须计入实验。底层 `invoke` 只做单插件契约实验，不会自动添加 `run` 中的跨域预算条件。

本版证明的是：在已约定数据语义与原语的有限范围内，声明可被发现、组合和受控执行。它不能证明任意新领域无需人工工作，也不能证明 SaaS 消失、岗位减少或生产关系必然改变。高频且稳定的任务未来可缓存或编译为确定性执行单元，何时值得这么做应测量决定。

与 App/API/MCP/Harness 的联系、差异和基础规范参考见 [comparison](docs/comparison.md)。插件、Schema、网关与 Agent 都已有先例；项目关注其分工组合。未经核实的 Muse、DeepSeek Harness 内部实现不作为论据。

## Roadmap

- 接入可选真实 LLM 生产者，检验陌生需求产生的 Manifest 是否正确，并记录人工修改成本。
- 接入真实端侧小模型，测量未见插件的意图结构化、检索、填参与跨域组合能力。
- 研究复杂组合、来源认证、兼容版本、缓存/自动编译与真实业务执行边界。
- **领域 LoRA / Adapter 热插拔**：作为后续明确研究方向，增强专业意图解析、Query Rewrite、能力召回/重排与专业知识检索。采用领域级适配，研究加载前路由、多领域协调、基座兼容和手机内存/延迟；不做每插件一个执行模型，不替代 Registry 或权限层，不是首版训练依赖。

完整路线见 [roadmap](docs/roadmap.md)，失败情景与开放研究问题见 [open-questions](docs/open-questions.md)。

## 文件与贡献

```text
docs/THESIS.md        思想纲领：历史限制、产业情景与反例
ARCHITECTURE.md       架构图、事实绑定、执行与权限边界
RFC/                 可检验提案和复现实验
specs/               插件/意图 JSON Schema、可信资源策略
examples/            外卖与资产虚构数据、四插件和扩展版本
src/splice/          Harness、Registry、Adapter、Planner/Runtime、Gateway、CLI
tests/test_demo.py   三条演示及关键边界验证
```

GitHub 仓库：[Nana7mi77/splice](https://github.com/Nana7mi77/splice)。欢迎通过 Issue 提出可复现问题，通过 Pull Request 提交改进；开发时使用可编辑安装，提交前运行 `python -m unittest discover -s tests -v`，并同步相关契约与文档。

`.gitignore` 排除本地状态、数据库、虚拟环境与环境变量文件。示例全部使用虚构数据，贡献中请继续使用虚构样本。

## English summary

**Splice — Composable Service Access for Agents.** Agents compose access to underlying services by intent, without a predefined intermediate business service layer.

Splice explores a cloud-produced, on-device-intent, data-driven-plugin architecture. A cloud-side harness produces versioned capabilities; an on-device agent interface expresses goals and constraints; a governed runtime discovers and composes capabilities through data contracts. The aim is to remove the need to hand-author a dedicated intermediate business service for each workflow. Underlying services, data resources, authorization, and state constraints remain necessary.

This repository is an architectural proposal with an offline Python prototype. Both model interfaces are explicitly mocked. Service access and composition are simulated through a local runtime and fictional data; real remote underlying services are not connected. Four declarative plugins connect food delivery with personal asset management: total price including delivery flows into a budget and cash assessment, and an authorized local draft is created only when the intent's conditions pass. Investment valuations are distinct from spendable cash. No real ordering, payment, account access, or trading occurs.

Run `splice demo combined` after `pip install -e .`. The prototype demonstrates bounded contract discovery and composition, not autonomous software production, general natural-language understanding, or a proven economic transition. Domain LoRA / Adapter hot-swapping remains a future research direction for intent parsing and capability search, not a runtime prerequisite.
