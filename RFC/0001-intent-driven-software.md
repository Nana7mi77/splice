# RFC-0001：Splice 的组合式服务访问、能力生产与执行

- 状态：实验性提案，附本地可运行原型；不代表生产系统或行业共识。
- 语言：中文为主。项目英文摘要见 README。
- 范围：虚构外卖与个人资产数据、单用户、本地草稿。
- 相关文档：[THESIS](../docs/THESIS.md)、[ARCHITECTURE](../ARCHITECTURE.md)、[开放问题](../docs/open-questions.md)。

## 1. 问题与可检验命题

Splice — Composable Service Access for Agents，研究面向 Agent 的组合式服务访问：Agent 按意图组合访问底层服务，无需预制中间业务 Service。传统软件通常由人提前把需求组装成 App、界面与专用服务。本提案检验：当数据契约与受控执行能力已经具备时，新业务目标能否通过发布能力声明进入系统，由现有终端意图接口发现、绑定和执行，而无需重建应用或编写专用中间业务 Handler？

提案省去的是人工预编写的专用中间业务层；底层服务、资源适配与可信 Gateway 仍保留各自职责。我们允许生成更多代码，允许复杂业务保留确定性服务，允许稳定组合被缓存或编译。关键变量是新增目标所需的人工专用代码和持续维护成本。

产业层面关于应用入口、企业协调成本与价值分配的推测见 THESIS。它们需要独立的经济和组织证据，不能由此 RFC 的技术演示证明。

## 2. 提案

采用四个责任边界：

1. **Cloud Harness** 生产能力定义、数据契约和受限执行计划，做基础验证后发布版本。
2. **Registry** 保存身份、版本、语义描述、Schema、权限、资源和效果，支持能力检索。
3. **On-device Intent Adapter** 将需求结构化为初始事实、目标事实、条件和允许效果；利用实际 Registry 发现候选。
4. **Runtime + Gateway** 根据契约组合能力，解释通用数据操作，在独立授权与状态约束下执行。

首版中的生产侧和端侧均为 Mock：生产者使用受控模板，适配器仅支持三个演示句式族及只读外卖搜索变体，允许有限的金额、月份和清淡偏好替换，未知复杂约束拒绝；不存在已部署的云端大模型或手机模型。Runtime 通过本地虚构资源模拟服务访问与组合，尚未接入真实远程底层服务。数据为单用户 `demo-user` 的虚构快照，默认“本月”固定指 `2026-10`。Mock 不改变接口分工，但也不能作为自主软件生产能力的证据。

## 3. 契约与受限计划

Plugin Manifest 至少包含 `id`、`version`、`description`、`examples`、`capability`、输入输出 Schema、`permissions`、`resources`、`preconditions`、`effects` 与 `execution`。输入输出属性使用 `x-fact` 标注语义事实；完整语法以 [plugin.schema.json](../specs/plugin.schema.json) 为准。

Structured Intent 的核心字段是 `requested_outputs`、`facts`、`gates` 和 `authorized_effects`；完整语法以 [intent.schema.json](../specs/intent.schema.json) 为准。Intent 不等于已授权的插件调用，也不提供执行任意脚本的权力。

Planner 从目标及条件需要的事实反向查找生产者，把输出事实绑定到所需输入，形成受限依赖计划。执行前和产出后分别做 Schema 校验。`x-fact` 解决已约定语义下的连接，不自行推断单位、作用域、时间和经济含义。

当前 Registry 的描述搜索是英文词与中文双字词重叠排序；计划依赖来自 `x-fact` 精确检索，不来自自然语言排序的猜测。每个 ID 激活最高数字版本；不同 ID 对同一事实有多个提供者、没有提供者或出现依赖环时拒绝规划。每个插件版本在单次计划最多执行一次，只读步骤和条件不能依赖尚未执行的写入。

Runtime 采用通用的读取、筛选、投影、聚合、比较、排序、断言和插入操作，通过 `lookup` 表达式关联资源，表达价格合计、数据汇总与条件约束。Manifest 不包含任意 Python、JavaScript 或 SQL。首版不提供任意分支、循环、长事务或补偿语言。

## 4. 核心实验：外卖 × 资产

四类能力独立分发：`food.menu.search`、`food.order.draft`、`finance.assets.summary` 与 `finance.budget.evaluate`。

场景 A 按餐品、口味和含配送费总价筛选，授权后生成本地草稿。场景 B 独立展示现金、投资持仓估值、总资产与当月餐饮预算。场景 C 将外卖候选总价传给预算能力，预算和现金条件满足且草稿写入已授权时，才生成草稿并展示预计余额。

场景 C 的公共路径不能是一个专用 `order_food_with_budget` Handler。菜单输出与预算输入通过事实契约绑定，条件由计划统一检查，草稿能力只获得它所需的餐品、金额和上限。资产账户与原始流水保留在金融能力内部。

数据统一使用人民币，来自虚构样本。投资估值使用固定快照，不能计入可支付现金。草稿不预占预算、不扣减现金，也不产生支付流水；显示的是计划消费后的预测。预算不足、缺少必要权限或契约错误时，不写入草稿。

## 5. 授权与状态边界

用户授予的权限、插件请求的权限和可信资源策略必须同时允许资源访问。`food:read`、`finance:read`、`finance_budget:read` 与 `food_order_draft:write` 分开处理。插件自报领域不能成为任意第三方代码的信任证明。

Gateway 允许读取授权虚构资源与写入本地草稿。草稿资源策略限制 `status=draft`，从可信菜单与配送数据核验价格和上限。写入与产出校验处于事务边界内，失败不留下半成品。

跨域预算条件由 `Runtime.run` 处理意图时检查；单插件的 `Runtime.invoke` 契约接口不自动附加预算工作流。因此普通外卖草稿和要求预算约束的草稿具有不同的数据权限需求，这种差异必须在调用入口和执行轨迹中保持可见。

首版没有真实下单、付款、转账、证券买卖、资产划拨或真实账户接入原语。不能把本地单用户授权演示外推为多租户生产隔离。Gateway 中预置的资源语义与不变量也必须计入新增领域的工程成本。

## 6. 可复现实验与反证条件

在仓库根目录按 [README](../README.md) 安装后，可运行以下命令。它们只创建新的本地 Registry 与虚构草稿；所有 CLI 命令都会通过 Mock Harness 幂等发布四个默认 Manifest。`demo` 与 `run` 的 `--json` 报告包含意图、候选版本、事实绑定、授权判断、插件产出，以及请求且成功生成的草稿结果；其他子命令输出对应操作的 JSON 结果。

```bash
SPLICE_RFC_STATE=$(mktemp -d)
splice demo food --state-dir "$SPLICE_RFC_STATE" --json
splice demo assets --state-dir "$SPLICE_RFC_STATE" --json
splice demo combined --state-dir "$SPLICE_RFC_STATE" --json
splice search '盖饭 配送费' --state-dir "$SPLICE_RFC_STATE"
splice publish examples/extensions/food.menu.search.v2.json --state-dir "$SPLICE_RFC_STATE"
splice demo combined --state-dir "$SPLICE_RFC_STATE" --json
```

基线外卖草稿总价 24 元，现金 4200 元，投资估值 10500 元；当月餐饮预算原剩余 40 元，拟消费后预算 16 元、现金 4176 元。扩展声明将菜单排序改为降序并发布同 ID 的 `0.2.0` 版本：公共路径不变，选择变成 28 元，预计预算余额变成 12 元。该实验改变 Manifest，不修改 Adapter、Planner 或 Gateway。

以下是预期退出码为 `2` 的拒绝实验，可逐条运行观察；它们不会新增草稿：

```bash
splice demo food --grants 'food:read' --state-dir "$SPLICE_RFC_STATE" --json
splice demo combined --profile food --state-dir "$SPLICE_RFC_STATE" --json
splice invoke finance.budget.evaluate --input '{"amount_cny":-1,"month":"2026-10"}' --profile assets --state-dir "$SPLICE_RFC_STATE"
splice invoke missing.plugin --input '{}' --state-dir "$SPLICE_RFC_STATE"
```

预算不足可通过独立虚构样本复现，以下仅将临时副本的十月餐饮预算限额设为 970 元，使剩余预算从 40 元变为 10 元：

```bash
SPLICE_RFC_FIXTURES=$(mktemp -d)
cp -R examples/food examples/finance "$SPLICE_RFC_FIXTURES/"
python - "$SPLICE_RFC_FIXTURES" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1]) / 'finance/monthly_budgets.json'
budgets = json.loads(path.read_text())
for budget in budgets:
    if budget['month'] == '2026-10' and budget['category'] == '餐饮':
        budget['limit_cny'] = 970
path.write_text(json.dumps(budgets, ensure_ascii=False))
PY
splice demo combined --data-dir "$SPLICE_RFC_FIXTURES" --state-dir "$SPLICE_RFC_STATE" --json
```

最后一条应在 `budget.within_limits` 条件处拒绝，保留预算评估结果，并回滚草稿事务。自动化检查使用 `python -m unittest discover -s tests -v`，覆盖下表中的契约、权限、组合和拒绝行为，具体实现见 [tests](../tests)。

| 实验 | 操作与观察 | 预期 | 何种结果推翻当前主张 |
| --- | --- | --- | --- |
| 能力发现 | 发布新的或更高版本菜单 Manifest，再按描述和事实契约发现 | Registry 返回已发布版本，现有意图可绑定适合的输入 | 必须先给 CLI 添加插件 ID 分支才可调用 |
| 契约约束 | 提交负金额、错误类型或不存在的插件 | 输入或查找阶段拒绝，草稿不增加 | Schema 错误仍进入写入，或未知插件被模型臆造 |
| 授权边界 | 撤去草稿写权限、金融读取权限，或让外卖声明读取金融资源 | 相应访问被拒绝，无未授权草稿与金融明细 | 插件自报权限就能绕过用户授权或资源边界 |
| 跨域组合 | 运行一次预算允许的跨域请求，再切换菜单声明重复运行 | 使用含配送费总价评估预算和现金，通过相同公共路径生成草稿 | 跨域逻辑藏在专用 Handler，或修改声明后必须改公共代码 |
| 权限与预算差异 | 运行仅允许外卖读取的请求及预算不足的请求 | 不声称未知预算充足；预算不足不写入 | 未读取金融数据仍断言预算充足，或拒绝之后已留下草稿 |
| 现金语义 | 保留高额持仓估值，降低虚构现金至不足消费 | 投资估值不能使可支付现金检查通过 | 把总资产作为现金使用 |

新增能力实验有一个重要限制：已知 `x-fact` 体系内的 Manifest 替换，只证明数据驱动扩展和契约绑定；它不证明模型能理解任意未知领域。若新增领域仍需新事实词汇、资源适配或原语，应如实报告所需改动。

## 7. 不选择的替代方案

**仅给现有服务加工具接口。** 这能改善调用体验，但不足以检验能力生产和服务层简化。MCP 可作为后续互操作层，不能单独承担本 RFC 的完整命题。

**让模型直接执行数据库语句或任意代码。** 这会把数据契约、权限与状态边界交给不确定的输出，无法支持本次实验对行为的检查。首版选择受限解释器。

**每项请求都让云端大 Agent 完成全部流程。** 这种方式可能有效，但没有检验“服务侧生产能力、端侧消费能力”的成本与责任分工。

**首版先训练领域模型或搭建分布式基础设施。** 这会增加实验前置成本，且无法自动解决发现、绑定与执行边界。LoRA 路线保留到有真实基线后的研究阶段。

## 8. 未决设计与后续研究

本 RFC 不假装解决以下问题：

- 未见插件的发现和填参；能力重叠、版本冲突与依赖选择；多插件目标的分解与重组。
- Manifest、共用原语与确定性服务的界限；网关退化为业务后端的识别；高频计划的缓存、失效与自动编译。
- 多租户权限、插件来源、签名、撤销和审计；模型提案与确定性执行的边界。
- 不同手机硬件、离线索引、隐私与运行成本；真实小模型能否承担目标语义任务。
- 领域 LoRA / Adapter 热插拔：在专业解析、Query Rewrite、能力检索与重排、专业知识检索中的价值；加载前的领域路由、多领域协调、基座兼容、内存与延迟成本。它是领域级增强，不是每插件一个执行模型，且不替代 Registry 与 Gateway。
- 跨域最小数据共享；现金、资产估值、冻结金额与预算的严格语义；多笔拟消费的预占和并发冲突。

这些问题的具体研究条件见 [open-questions](../docs/open-questions.md) 与 [roadmap](../docs/roadmap.md)。未来扩展以失败证据和实际需求驱动，不以框架层数或工具数量衡量进展。

## 9. 接受标准与证据范围

接受首版意味着三条演示与关键拒绝行为可以运行，新增或替换声明能复用公共路径，Mock 与有限范围被清楚标注，读者能从执行轨迹和代码检查声明与执行的关系。

这不等于接受产业终局预测。下一阶段应真正测量：增加一种能力需要修改多少专用代码，模型面对未见契约时多大概率成功，验证与运行成本是否低于合理的传统方案，以及哪些反例使服务继续成为更好的形态。
