"""An intentionally narrow, offline stand-in for an on-device intent model.

The parser accepts three demonstration families and a read-only food-search
variant. It supports a few wording substitutions, positive prices, and numeric
months; unfamiliar clauses fail closed instead of silently losing constraints.
No plugin identities, execution sequence, or function routing live here.
"""

from decimal import Decimal, InvalidOperation
import re
from typing import Protocol, TYPE_CHECKING

from .contracts import validate_intent
from .errors import PlanningError

if TYPE_CHECKING:
    from .registry import Registry


DEMO_REQUESTS = {
    "food": "帮我找今晚30元以内的盖饭，算上配送费，口味清淡，先生成订单草稿，不要真正下单。",
    "assets": "我目前现金和投资资产各有多少？本月餐饮花了多少，还剩多少预算？只要汇总，不要操作我的资产。",
    "combined": "今晚想吃一份30元以内的盖饭，但不能超过这个月的餐饮预算。先检查我还剩多少钱，合适才生成订单草稿，顺便告诉我买完以后餐饮预算和可用现金还剩多少。",
}

DEFAULT_MONTH = "2026-10"  # Fixed fictional data snapshot, not the system clock.


class OnDeviceIntentAdapter(Protocol):
    def parse(self, utterance: str) -> dict:
        ...


def _chinese_integer(value: str) -> int:
    """Small explicit vocabulary: 一 to 九十九, including 十 and 两."""
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if value in digits:
        return digits[value]
    match = re.fullmatch(r"([一二两三四五六七八九]?)十([一二三四五六七八九]?)", value)
    if not match:
        raise PlanningError("Mock 金额仅支持阿拉伯数字或一至九十九的中文整数")
    tens, ones = match.groups()
    return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)


def _price(text: str) -> tuple[str, float]:
    pattern = r"(?:人民币)?(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)(?:元|块钱|块)"
    matches = list(re.finditer(pattern, text))
    if len(matches) > 1:
        raise PlanningError("Mock 每次只支持一个餐品含配送费价格上限")
    if not matches:
        return text, 30.0
    value = matches[0].group(1)
    try:
        amount = Decimal(value) if value[0].isdigit() else Decimal(_chinese_integer(value))
    except InvalidOperation as exc:
        raise PlanningError("价格必须是有效的正数") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1e12"):
        raise PlanningError("Mock 价格必须是有限正数且不超过一万亿元")
    if amount.as_tuple().exponent < -2:
        raise PlanningError("人民币金额最多支持两位小数")
    return text[:matches[0].start()] + "金额" + text[matches[0].end():], float(amount)


def _month(text: str) -> tuple[str, str]:
    pattern = r"(?:(20\d{2})年)?(1[0-2]|0?[1-9])月|(20\d{2})-(0[1-9]|1[0-2])(?!\d)"
    matches = list(re.finditer(pattern, text))
    if len(matches) > 1:
        raise PlanningError("Mock 每次只支持一个预算月份")
    if not matches:
        return text, DEFAULT_MONTH
    match = matches[0]
    year, month, iso_year, iso_month = match.groups()
    selected = f"{year or iso_year or '2026'}-{int(month or iso_month):02d}"
    return text[:match.start()] + "本月" + text[match.end():], selected


_FOOD_START = (
    r"(?:(?:帮我|给我|请)?(?:找|选|推荐)(?:今晚)?|今晚(?:想吃|(?:给我|帮我)?找))"
    r"(?:一份)?(?:金额以内)?的?盖饭(?:含配送费)?(?:清淡)?"
)
_DRAFT = r"(?:先)?(?:生成|创建)?(?:订单)?草稿"
_NO_ACTION = r"(?:(?:不要真正下单|不要下单|不要付款|不要支付|不要转账))*"
_FOOD = re.compile(_FOOD_START + rf"(?P<action>{_DRAFT}|拒绝草稿)?" + _NO_ACTION)
_COMBINED = re.compile(
    _FOOD_START
    + r"(?:但)?(?:不能超过本月的?餐饮预算|不能让本月的?餐饮预算超支|不能超出本月的?餐饮预算)"
    + r"(?:先检查我还剩多少钱)?"
    + rf"(?P<action>合适(?:才|的话)?{_DRAFT}|拒绝草稿)?"
    + r"(?:顺便告诉我买完以后餐饮预算和可用现金还剩多少|再告诉我剩下多少预算)?"
    + _NO_ACTION
)
_ASSETS = re.compile(
    r"(?:我目前)?现金和投资资产各有多少本月餐饮花了多少还剩多少预算"
    r"(?:只要汇总)?(?:不要操作我的资产)?"
)


class MockOnDeviceIntentAdapter:
    """Deterministic sample grammar; not open-domain Chinese NLU or a model.

    Supported families: food draft, asset+budget summary, food+budget composite,
    and food search without a draft. Currency is CNY; unspecified month/ceiling
    use the fictional snapshot's 2026-10 and 30 CNY. Unknown restrictions reject.
    """

    def parse(self, utterance: str) -> dict:
        if not isinstance(utterance, str) or not utterance.strip():
            raise PlanningError("意图文本不能为空")
        text = re.sub(r"[\s，,。！？!?；;：:、]", "", utterance).strip(".")
        # Explicit prohibitions do not request the prohibited action. In
        # particular the required demo's 不要真正下单 must remain acceptable.
        operation_check = re.sub(
            r"不要(?:真正下单|实际下单|下单|付款|支付|转账|操作我的资产)", "", text
        )
        if re.search(r"付款|支付|转账|买入|卖出|买卖|交易|划拨|提交订单|真正下单|实际下单|立即下单|直接下单", operation_check):
            raise PlanningError("此原型不支持付款、真实下单、转账或资产交易")
        text = re.sub(r"(?:不要|别|无需|不用)(?:先)?(?:生成|创建)?(?:外卖)?(?:订单)?草稿", "拒绝草稿", text)
        substitutions = {
            "这个月": "本月", "当月": "本月", "算上配送费": "含配送费",
            "包含配送费": "含配送费", "口味清淡": "清淡", "外卖订单草稿": "订单草稿",
            "不要实际下单": "不要真正下单",
        }
        for old, new in substitutions.items():
            text = text.replace(old, new)
        text, month = _month(text)
        text, amount = _price(text)
        text = text.replace("不超过金额", "金额以内").replace("金额以下", "金额以内").replace("金额内", "金额以内")
        composite = _COMBINED.fullmatch(text)
        food = _FOOD.fullmatch(text)
        assets = _ASSETS.fullmatch(text)
        if not (composite or food or assets):
            raise PlanningError(
                "Mock 只支持三个演示句式及不生成草稿的盖饭搜索；可替换金额、月份和清淡偏好。"
                "未识别或复杂约束不会被忽略，请使用 demo 示例或接入新的 IntentAdapter。"
            )
        if assets:
            goal = "assets_and_budget_summary"
            outputs = ["assets.summary", "budget.assessment"]
            facts = {"spending.amount_cny": 0, "budget.month": month}
            wants_draft = False
            gates = []
        else:
            match = composite or food
            action = match.group("action") or ""
            wants_draft = bool(action and action != "拒绝草稿")
            goal = "food_with_budget_check" if composite else ("food_order_draft" if wants_draft else "food_search")
            outputs = ["order.draft" if wants_draft else "meal.candidates"]
            if composite:
                outputs.append("budget.assessment")
            facts = {
                "meal.query": "盖饭",
                "meal.taste": "清淡" if "清淡" in text else "不限",
                "spending.max_cny": amount,
                "budget.month": month,
            }
            gates = [{"fact": "budget.within_limits", "equals": True}] if composite else []
        intent = {
            "goal": goal,
            "requested_outputs": outputs,
            "facts": facts,
            "gates": gates,
            "requires_confirmation": wants_draft,
            "authorized_effects": ["draft.create"] if wants_draft else [],
            "constraints": {"place_order": False, "make_payment": False},
            "language": "zh-CN",
            "utterance": utterance,
            "adapter": "mock",
        }
        validate_intent(intent)
        return intent


def search_candidates(utterance: str, registry: "Registry") -> list[dict]:
    """Discover newly published descriptions without imposing a plugin sequence."""
    return registry.search(utterance)
