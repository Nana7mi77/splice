"""Fact-driven planning and a bounded declarative execution engine."""

import copy

from .contracts import validate_data, validate_intent, validate_manifest
from .errors import ConfirmationRequired, ExecutionError, GateRejected, SpliceError, PlanningError
from .expressions import evaluate


class Planner:
    """Backward-chain semantic facts, with no plugin IDs or domain workflows."""

    def __init__(self, registry):
        self.registry = registry

    def plan(self, intent):
        validate_intent(intent)
        if any(gate["fact"] in intent["facts"] for gate in intent["gates"]):
            raise PlanningError("Gate facts must be produced by a registered capability, not supplied as initial facts")
        available = set(intent["facts"])
        visiting = set()
        scheduled = set()
        plan = []

        def resolve(fact):
            if fact in available:
                return
            if fact in visiting:
                raise PlanningError(f"Cyclic fact dependency: {fact}")
            providers = self.registry.providers(fact)
            if len(providers) != 1:
                names = [p["id"] for p in providers]
                raise PlanningError(f"Fact {fact} needs exactly one provider, found {names}")
            plugin = providers[0]
            validate_manifest(plugin)
            visiting.add(fact)
            for field, prop in plugin["input_schema"].get("properties", {}).items():
                if field in plugin["input_schema"].get("required", []):
                    resolve(prop["x-fact"])
            visiting.remove(fact)
            key = (plugin["id"], plugin["version"])
            if key not in scheduled:
                scheduled.add(key)
                plan.append(plugin)
            available.update(prop["x-fact"] for prop in plugin["output_schema"]["properties"].values())

        for gate in intent["gates"]:
            resolve(gate["fact"])
        for fact in intent["requested_outputs"]:
            resolve(fact)
        # Only support plans whose read-only dependencies can precede all effects.
        effect_facts = {p["x-fact"] for m in plan if m["effects"] for p in m["output_schema"]["properties"].values()}
        for manifest in plan:
            if not manifest["effects"] and any(
                p["x-fact"] in effect_facts for p in manifest["input_schema"].get("properties", {}).values()
            ):
                raise PlanningError("Read-only steps cannot depend on a pending write in this MVP")
        if any(g["fact"] in effect_facts for g in intent["gates"]):
            raise PlanningError("A gate must be resolved before writes")
        return [m for m in plan if not m["effects"]] + [m for m in plan if m["effects"]]


class Runtime:
    def __init__(self, registry, gateway):
        self.registry = registry
        self.gateway = gateway
        self.trace = []

    def _execute(self, manifest, inputs, confirmed=False):
        try:
            return self._interpret(manifest, inputs, confirmed)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ExecutionError(f"Malformed declarative plan: {exc}") from exc

    def _interpret(self, manifest, inputs, confirmed=False):
        validate_manifest(manifest)
        validate_data(inputs, manifest["input_schema"], f"{manifest['id']} input")
        self.gateway.authorize_manifest(manifest)
        context = {"input": copy.deepcopy(inputs)}
        for condition in manifest["preconditions"]:
            if evaluate(condition, context) is not True:
                raise ExecutionError(f"{manifest['id']}: precondition failed")
        for step in manifest["execution"]["steps"]:
            op = step["op"]
            if op == "read":
                result = self.gateway.read(manifest, step["resource"])
            elif op == "filter":
                result = [row for row in context[step["from"]] if evaluate(step["where"], {**context, "row": row}) is True]
            elif op == "project":
                if "from" in step:
                    result = [evaluate(step["values"], {**context, "row": row}) for row in context[step["from"]]]
                else:
                    result = evaluate(step["values"], context)
            elif op == "aggregate":
                result = evaluate(step["values"], {**context, "rows": context[step["from"]]})
            elif op == "compare":
                result = evaluate(step["value"], context)
            elif op == "sort":
                result = sorted(context[step["from"]], key=lambda row: row[step["by"]], reverse=step.get("direction", "asc") == "desc")
            elif op == "assert":
                if evaluate(step["condition"], context) is not True:
                    raise ExecutionError(step["message"])
                result = True
            elif op == "insert":
                result = self.gateway.insert(manifest, step["resource"], evaluate(step["values"], context), inputs, confirmed)
            else:
                raise ExecutionError(f"Unsupported step: {op}")
            if "as" in step:
                context[step["as"]] = result
            event = {"event": "step", "plugin": manifest["id"], "op": op}
            if "resource" in step:
                event["resource"] = step["resource"]
            if isinstance(result, list):
                event["row_count"] = len(result)
            self.trace.append(event)
        output = evaluate(manifest["execution"]["output"], context)
        validate_data(output, manifest["output_schema"], f"{manifest['id']} output")
        self.trace.append({"event": "output", "plugin": manifest["id"], "output": output})
        return output

    def invoke(self, plugin_id, inputs, version=None, confirmed=False):
        """Low-level contract experiment; does not imply an intent's budget gates."""
        manifest = self.registry.get(plugin_id, version)
        with self.gateway.transaction():
            return self._execute(manifest, inputs, confirmed)

    def run(self, intent, confirmed=False):
        self.trace = []
        self.gateway.audit = []
        facts = {}
        report = {"status": "completed", "intent": intent, "plan": [], "outputs": {}, "mock": True,
                  "notice": "完全虚构数据；余额仅为计划消费模拟；草稿不会下单、支付或扣减账户。"}
        try:
            validate_intent(intent)
            facts = copy.deepcopy(intent["facts"])
            plan = Planner(self.registry).plan(intent)
            report["plan"] = [{"id": m["id"], "version": m["version"], "capability": m["capability"],
                                "effects": m["effects"], "bindings": {key: prop["x-fact"] for key, prop in m["input_schema"].get("properties", {}).items()}}
                               for m in plan]
            with self.gateway.transaction():
                gates_checked = False
                for manifest in plan:
                    if manifest["effects"] and not gates_checked:
                        self._check_gates(intent, facts)
                        gates_checked = True
                    if manifest["effects"] and not (confirmed or set(manifest["effects"]).issubset(intent["authorized_effects"])):
                        raise ConfirmationRequired("请明确授权生成本地草稿；可使用 --confirm 或在请求中明确说明生成草稿")
                    inputs = {}
                    bindings = {}
                    for field, prop in manifest["input_schema"].get("properties", {}).items():
                        fact = prop["x-fact"]
                        if fact in facts:
                            inputs[field] = copy.deepcopy(facts[fact])
                            bindings[field] = {"fact": fact, "schema_type": prop.get("type")}
                        elif "default" in prop:
                            inputs[field] = prop["default"]
                    self.trace.append({"event": "bind", "plugin": manifest["id"], "bindings": bindings, "inputs": inputs})
                    output = self._execute(manifest, inputs, confirmed=bool(manifest["effects"]))
                    for field, value in output.items():
                        fact = manifest["output_schema"]["properties"][field]["x-fact"]
                        if fact in facts and facts[fact] != value:
                            raise PlanningError(f"Conflicting fact value: {fact}")
                        facts[fact] = value
                if not gates_checked:
                    self._check_gates(intent, facts)
        except SpliceError as exc:
            report.update(status="blocked", error={"type": type(exc).__name__, "message": str(exc)})
            # A staged write is never reported as a committed result after rollback.
            for manifest in locals().get("plan", []):
                if manifest["effects"]:
                    for prop in manifest["output_schema"]["properties"].values():
                        facts.pop(prop["x-fact"], None)
        requested = intent.get("requested_outputs", []) if isinstance(intent, dict) else []
        report["outputs"] = {fact: facts[fact] for fact in requested if isinstance(fact, str) and fact in facts} if isinstance(requested, list) else {}
        report["trace"] = self.trace
        report["audit"] = self.gateway.audit
        return report

    def _check_gates(self, intent, facts):
        for gate in intent["gates"]:
            if gate["fact"] not in facts:
                raise PlanningError(f"Missing gate fact: {gate['fact']}")
            accepted = type(facts[gate["fact"]]) is type(gate["equals"]) and facts[gate["fact"]] == gate["equals"]
            self.trace.append({"event": "gate", "fact": gate["fact"], "accepted": accepted})
            if not accepted:
                raise GateRejected(f"约束未满足：{gate['fact']}；不生成草稿，预算与现金评估见 outputs")
