"""Mock demos, real-model configuration, and verified local capability expansion."""

import argparse
import getpass
import json
import os
from pathlib import Path

from .cloud import DeepSeekClient
from .config import ConfigStore
from .errors import ContractError, SpliceError
from .expansion import ExpansionManager
from .gateway import Gateway
from .harness import Harness, MockCapabilityProducer
from .intent import DEMO_REQUESTS, MockOnDeviceIntentAdapter
from .learning import LearningStore
from .local_intent import LocalIntentError, LocalOnDeviceIntentAdapter
from .model import ModelManager
from .paths import project_data
from .registry import Registry
from .runtime import Runtime


# Explicit demo consent profiles, independent of untrusted manifest permissions.
# Combined consumption can inspect a budget, but not investment positions.
PROFILES = {
    "search": {"food:read"},
    "food": {"food:read", "food_order_draft:write"},
    "assets": {"finance:read", "finance_budget:read"},
    "combined": {"food:read", "food_order_draft:write", "finance_budget:read"},
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _common(parser, nested=False):
    default = argparse.SUPPRESS if nested else Path(".splice")
    parser.add_argument("--state-dir", type=Path, default=default, help="Registry、教学资料与本地草稿目录")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if nested else False,
                        help="输出完整 JSON，包括绑定与授权轨迹")
    parser.add_argument("--config-dir", type=Path, default=argparse.SUPPRESS if nested else None,
                        help="私有配置目录；默认遵从 SPLICE_CONFIG_DIR")


def _parser():
    parser = argparse.ArgumentParser(description="Splice 组合式服务访问原型（虚构业务数据）")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="运行 A 外卖、B 资产或 C 跨域场景")
    demo.add_argument("scenario", choices=DEMO_REQUESTS)
    run = sub.add_parser("run", help="解析自然语言并执行声明式计划")
    run.add_argument("request")
    run.add_argument("--adapter", choices=("mock", "local"), default="mock")
    run.add_argument("--offline", action="store_true", help="禁止云端生成；本地推理始终只使用缓存")
    run.add_argument("--auto-expand", action="store_true", help="仅为真实本地模型报告的能力缺口生成候选")
    run.add_argument("--experiment", type=Path, help="独立验收规范；指定后执行前必须保留其全部 gates")
    run.add_argument("--approve-expansion", action="store_true", help="预先授权通过验证的候选发布并安装教学包")
    search = sub.add_parser("search", help="按描述搜索本地能力（词法索引）")
    search.add_argument("query")
    publish = sub.add_parser("publish", help="校验并发布一份本地 Manifest")
    publish.add_argument("manifest", type=Path)
    produce = sub.add_parser("produce", help="由 Mock Harness 从模板生产并发布能力")
    produce.add_argument("requirement")
    invoke = sub.add_parser("invoke", help="低层契约实验；不包含自然语言意图的跨域门槛")
    invoke.add_argument("plugin_id")
    invoke.add_argument("--input", required=True, help="JSON object")
    invoke.add_argument("--version")
    sub.add_parser("init", help="由 Mock Harness 发布四个默认能力")

    config = sub.add_parser("config", help="管理私有模型配置与凭据")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("init")
    config_sub.add_parser("show")
    deepseek = config_sub.add_parser("deepseek", help="隐藏输入密钥；也可导入 DEEPSEEK_API_KEY 环境变量")
    deepseek.add_argument("--from-env", action="store_true", help="将 DEEPSEEK_API_KEY 保存到私有凭据文件")
    deepseek.add_argument("--model")
    deepseek.add_argument("--base-url")
    deepseek.add_argument("--allow-custom-base-url", action="store_true", default=None)
    deepseek.add_argument("--timeout", type=float)
    deepseek.add_argument("--max-tokens", type=int)
    deepseek.add_argument("--max-calls", type=int)
    clear = config_sub.add_parser("clear", help="清除本地凭据；不会清除 shell 环境变量")
    clear.add_argument("provider", choices=("deepseek",), nargs="?", default="deepseek")

    model = sub.add_parser("model", help="准备和检测真实本地模型")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    setup = model_sub.add_parser("setup")
    setup.add_argument("--backend", choices=("auto", "mlx-vlm", "mlx-lm", "transformers"), default="auto")
    setup.add_argument("--model-id")
    setup.add_argument("--yes", action="store_true", help="确认下载模型权重")
    model_sub.add_parser("status")
    model_sub.add_parser("test")

    cloud = sub.add_parser("cloud", help="真实云端连通性检测")
    cloud_sub = cloud.add_subparsers(dest="cloud_command", required=True)
    cloud_sub.add_parser("test")

    learn = sub.add_parser("learn", help="安装或列出与本地 Registry 版本绑定的教学包")
    learn_sub = learn.add_subparsers(dest="learn_command", required=True)
    install = learn_sub.add_parser("install")
    install.add_argument("pack", type=Path)
    learn_sub.add_parser("list")

    expand = sub.add_parser("expand", help="生成、验证并显式批准本地扩展")
    expand_sub = expand.add_subparsers(dest="expand_command", required=True)
    propose = expand_sub.add_parser("propose")
    propose.add_argument("--requirement", required=True)
    propose.add_argument("--experiment", type=Path, required=True)
    propose.add_argument("--approve", action="store_true", help="预先授权发布通过独立验证的候选")
    verify = expand_sub.add_parser("verify", help="离线重新验收已有生成包；默认不批准，不调用云端")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--experiment", type=Path, required=True)
    verify.add_argument("--approve", action="store_true", help="授权发布重新验收通过的候选")
    approve = expand_sub.add_parser("approve")
    approve.add_argument("candidate_dir", type=Path)

    for command in sub.choices.values():
        _common(command)
    for children in (config_sub, model_sub, cloud_sub, learn_sub, expand_sub):
        for command in children.choices.values():
            _common(command, nested=True)
    for command in (demo, run, invoke):
        command.add_argument("--profile", choices=PROFILES, help="显式演示权限集合；run/invoke 默认 food")
        command.add_argument("--grants", help="覆盖 profile 的逗号分隔权限；空字符串表示无权限")
        command.add_argument("--confirm", action="store_true", help="授权本次本地草稿效果；真实 local 模型必须显式确认")
        command.add_argument("--data-dir", type=Path, help="自定义虚构 fixture 根目录，含 food/ 与 finance/")
    return parser


def _bootstrap(state_dir):
    registry = Registry(state_dir / "registry")
    harness = Harness(registry, MockCapabilityProducer(project_data() / "examples" / "plugins"))
    published = harness.publish_examples()
    return registry, harness, published


def _print_report(report):
    adapter = report.get("intent_adapter", report.get("intent", {}).get("adapter", "mock"))
    print(f"Splice · {adapter} 端侧 Adapter · 虚构人民币数据")
    print(f"意图：{report['intent']['utterance']}")
    print(f"目标事实：{', '.join(report['intent']['requested_outputs'])}")
    print("发现与绑定（JSON Schema 验证）：")
    for step in report["plan"]:
        bindings = ", ".join(f"{field} ← {fact}" for field, fact in step["bindings"].items()) or "无输入"
        print(f"  {step['id']}@{step['version']} [{bindings}]")
    print("逐步输入与产出：")
    for event in report["trace"]:
        if event["event"] == "bind":
            print(f"  输入 {event['plugin']}: {json.dumps(event['inputs'], ensure_ascii=False)}")
        elif event["event"] == "output":
            print(f"  产出 {event['plugin']}: {json.dumps(event['output'], ensure_ascii=False)}")
        elif event["event"] == "gate":
            print(f"  条件 {event['fact']}: {'通过' if event['accepted'] else '拒绝'}")
    print("Gateway 授权：")
    for event in report["audit"]:
        print("  " + " · ".join(str(event[key]) for key in ("plugin", "action", "resource", "decision", "missing") if key in event))
    print(f"结果：{report['status']}")
    print(_json(report["outputs"]))
    if "error" in report:
        print(f"拒绝原因：{report['error']['type']} — {report['error']['message']}")
    if report.get("local_model_metadata"):
        metadata = report["local_model_metadata"]
        print(f"本地模型：{metadata.get('local_model')} / {metadata.get('backend')}；教学方式：dynamic_context")
    print(report["notice"])


def _config(args, store):
    if args.config_command == "init":
        return store.init()
    if args.config_command == "show":
        return store.show()
    if args.config_command == "clear":
        store.clear_api_key()
        return {"status": "cleared", "local_credentials": "removed",
                "environment_credential_present": bool(os.environ.get("DEEPSEEK_API_KEY")), "config": store.show()}
    values = {name: getattr(args, name) for name in (
        "model", "base_url", "allow_custom_base_url", "timeout", "max_tokens", "max_calls"
    ) if getattr(args, name) is not None}
    if values:
        store.update("deepseek", values)
    # Changing model/budget settings should not prompt for an already configured
    # credential. A bare command always opens getpass, allowing key rotation.
    if args.from_env:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise ContractError("DEEPSEEK_API_KEY is not configured in the environment")
        store.set_api_key(key)
    elif not values or not store.get_api_key():
        store.set_api_key(getpass.getpass("DeepSeek API Key（输入隐藏）："))
    return store.show()


def _can_expand(report):
    reason = report.get("reason")
    if reason == "missing_capability":
        # Ambiguous providers and cycles are catalog errors, not missing services.
        return "found []" in report.get("planning_error", "")
    if reason != "unresolved_constraint" or not report.get("unresolved_constraints"):
        return False
    # An unsupported real-world effect or a fabricated computed fact cannot be
    # repaired by generating a capability. Permission/gate errors never reach here.
    return not any("unsupported_effect" in item.get("reason", "") or
                   "Computed fact" in item.get("reason", "")
                   for item in report["unresolved_constraints"])


def _require_experiment_gates(intent, experiment):
    required = experiment["target_contract"]["required_gates"]
    missing = [gate for gate in required if gate not in intent["gates"]]
    if missing:
        raise ContractError("Local intent omitted required experiment gates; no execution: " + _json(missing))


def main(argv=None):
    args = _parser().parse_args(argv)
    gateway = None
    client = None
    adapter = None
    expansion = None
    try:
        store = ConfigStore(args.config_dir)
        # Configuration, download/status and connectivity do not create or seed a
        # Registry. Only model test needs the same catalog as normal intent parsing.
        if args.command == "config":
            print(_json(_config(args, store)))
            return 0
        if args.command == "model" and args.model_command != "test":
            manager = ModelManager(store)
            result = manager.setup(args.backend, args.model_id, args.yes) if args.model_command == "setup" else manager.status()
            print(_json(result))
            return 0
        if args.command == "cloud":
            client = DeepSeekClient(store)
            result = client.complete_json([
                {"role": "system", "content": "Return JSON only. This is a bounded connectivity test."},
                {"role": "user", "content": 'Return exactly {"ready":true} as JSON.'},
            ], max_tokens=64)
            if result != {"ready": True}:
                raise ContractError("DeepSeek connectivity response did not match the probe contract")
            print(_json({"status": "completed", "result": result, "cloud_calls": client.metadata, "usage": client.usage}))
            return 0

        registry, harness, published = _bootstrap(args.state_dir)
        learning = LearningStore(args.state_dir / "learning")
        if args.command == "model":
            print(_json(ModelManager(store).test(registry, learning)))
            return 0
        if args.command == "init":
            print(_json({"producer": "MockCapabilityProducer", "published": [f"{m['id']}@{m['version']}" for m in published]}))
            return 0
        if args.command == "publish":
            manifest = registry.publish(json.loads(args.manifest.read_text(encoding="utf-8")))
            print(_json({"published": f"{manifest['id']}@{manifest['version']}", "state_dir": str(args.state_dir)}))
            return 0
        if args.command == "produce":
            manifest = harness.publish(args.requirement)
            print(_json({"producer": "MockCapabilityProducer", "published": f"{manifest['id']}@{manifest['version']}"}))
            return 0
        if args.command == "search":
            matches = registry.search(args.query)
            print(_json([{"id": match["manifest"]["id"], "version": match["manifest"]["version"],
                          "description": match["manifest"]["description"], "score": match["score"],
                          "matched_terms": match["matched_terms"], "method": match["method"]} for match in matches]))
            return 0
        if args.command == "learn":
            if args.learn_command == "install":
                result = learning.install(json.loads(args.pack.read_text(encoding="utf-8")), registry)
            else:
                result = {"learning_packs": learning.all(registry), "adaptation_mode": "dynamic_context", "weights_updated": False}
            print(_json(result))
            return 0
        if args.command == "expand":
            if args.expand_command == "propose":
                client = DeepSeekClient(store)
                manager = ExpansionManager(registry, learning, args.state_dir, client)
                result = manager.propose(args.requirement,
                                         {"type": "manual_request", "message": "Explicit capability expansion request"},
                                         args.experiment, approve=args.approve)
            elif args.expand_command == "verify":
                manager = ExpansionManager(registry, learning, args.state_dir)
                result = manager.stage(json.loads(args.bundle.read_text(encoding="utf-8")), args.experiment)
                if args.approve:
                    result = manager.approve(result["candidate_dir"])
                result.update(producer="imported_bundle", cloud_calls=[])
            else:
                result = ExpansionManager(registry, learning, args.state_dir).approve(args.candidate_dir)
            print(_json(result))
            return 0 if result["status"] in {"verified", "published"} else 2

        profile = args.profile or (args.scenario if args.command == "demo" else "food")
        grants = PROFILES[profile] if args.grants is None else {value.strip() for value in args.grants.split(",") if value.strip()}
        if args.command == "invoke":
            gateway = Gateway(grants, args.state_dir / "drafts.sqlite3", args.data_dir)
            runtime = Runtime(registry, gateway)
            output = runtime.invoke(args.plugin_id, json.loads(args.input), args.version, args.confirm)
            print(_json({"output": output, "trace": runtime.trace, "audit": gateway.audit}))
            return 0

        utterance = DEMO_REQUESTS[args.scenario] if args.command == "demo" else args.request
        local = args.command == "run" and args.adapter == "local"
        experiment = ExpansionManager._experiment(args.experiment) if args.command == "run" and args.experiment else None
        if args.command == "run" and args.auto_expand and not local:
            raise ContractError("Automatic expansion requires --adapter local")
        if args.command == "run" and args.approve_expansion and not args.auto_expand:
            raise ContractError("--approve-expansion requires --auto-expand")
        adapter = LocalOnDeviceIntentAdapter(registry, learning, ModelManager(store)) if local else MockOnDeviceIntentAdapter()
        try:
            intent = adapter.parse(utterance)
        except LocalIntentError as exc:
            if not (args.auto_expand and not args.offline and _can_expand(exc.report)):
                raise
            if experiment is None:
                raise LocalIntentError("Automatic expansion requires --experiment with independent acceptance cases", exc.report) from exc
            client = DeepSeekClient(store)
            manager = ExpansionManager(registry, learning, args.state_dir, client)
            expansion = manager.propose(utterance, exc.report, experiment, approve=args.approve_expansion)
            expansion["initial_local_failure"] = exc.report
            if expansion["status"] != "published":
                print(_json(expansion))
                return 0 if expansion["status"] == "verified" else 2
            # The same adapter reads the newly installed Registry/teaching state.
            # A retry failure stays visible; it never causes a second expansion.
            intent = adapter.parse(utterance)
        if local:
            # Effect consent belongs to this invocation, never to model output
            # or a teaching example, even if an adapter implementation changes.
            intent["authorized_effects"] = []
        if experiment is not None:
            _require_experiment_gates(intent, experiment)
        gateway = Gateway(grants, args.state_dir / "drafts.sqlite3", args.data_dir)
        report = Runtime(registry, gateway).run(intent, confirmed=args.confirm)
        report.update(permission_profile=profile, grants=sorted(grants),
                      fictional_data=True, mock=not local, intent_adapter="local" if local else "mock",
                      cloud_calls=client.metadata if client else [],
                      adaptation_mode="dynamic_context" if local else "none", weights_updated=False)
        if local:
            report["local_model_metadata"] = adapter.last_report
        if expansion is not None:
            report["expansion"] = expansion
        report["discovery"] = [{"id": match["manifest"]["id"], "version": match["manifest"]["version"],
                                "score": match["score"], "method": match["method"]} for match in registry.search(utterance)]
        if args.json:
            print(_json(report))
        else:
            _print_report(report)
        return 0 if report["status"] == "completed" else 2
    except (SpliceError, OSError, ValueError, EOFError) as exc:
        report = {"status": "blocked", "error": {"type": type(exc).__name__, "message": str(exc)},
                  "cloud_calls": client.metadata if client else [], "fictional_data": True}
        if isinstance(exc, LocalIntentError):
            report["local_model_metadata"] = exc.report
            report["adaptation_mode"] = "dynamic_context"
            report["weights_updated"] = False
        elif isinstance(adapter, LocalOnDeviceIntentAdapter):
            report["local_model_metadata"] = adapter.last_report
            report["adaptation_mode"] = "dynamic_context"
            report["weights_updated"] = False
        if expansion is not None:
            report["expansion"] = expansion
        print(_json(report))
        return 2
    finally:
        if gateway:
            gateway.close()


if __name__ == "__main__":
    raise SystemExit(main())
