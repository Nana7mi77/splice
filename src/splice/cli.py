"""Offline demonstrations through the same intent → facts → plugin path."""

import argparse
import json
from pathlib import Path

from .errors import SpliceError
from .gateway import Gateway
from .harness import Harness, MockCapabilityProducer
from .intent import DEMO_REQUESTS, MockOnDeviceIntentAdapter
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


def _parser():
    parser = argparse.ArgumentParser(description="Splice 离线架构原型（Mock 模型、虚构数据）")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="运行 A 外卖、B 资产或 C 跨域场景")
    demo.add_argument("scenario", choices=DEMO_REQUESTS)
    run = sub.add_parser("run", help="解析受限自然语言样本并执行声明式计划")
    run.add_argument("request")
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
    for command in sub.choices.values():
        command.add_argument("--state-dir", type=Path, default=Path(".splice"), help="Registry 与本地草稿目录")
        command.add_argument("--json", action="store_true", help="输出完整 JSON，包括绑定与授权轨迹")
    for command in (demo, run, invoke):
        command.add_argument("--profile", choices=PROFILES, help="显式演示权限集合；run/invoke 默认 food")
        command.add_argument("--grants", help="覆盖 profile 的逗号分隔权限；空字符串表示无权限")
        command.add_argument("--confirm", action="store_true", help="授权本次本地草稿效果")
        command.add_argument("--data-dir", type=Path, help="自定义虚构 fixture 根目录，含 food/ 与 finance/")
    return parser


def _print_report(report):
    print("Splice · Mock 端侧 Adapter / Mock 生产 Harness · 虚构人民币数据")
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
    print(report["notice"])


def main(argv=None):
    args = _parser().parse_args(argv)
    gateway = None
    try:
        registry = Registry(args.state_dir / "registry")
        harness = Harness(registry, MockCapabilityProducer(project_data() / "examples" / "plugins"))
        published = harness.publish_examples()
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
        profile = args.profile or (args.scenario if args.command == "demo" else "food")
        grants = PROFILES[profile] if args.grants is None else {value.strip() for value in args.grants.split(",") if value.strip()}
        gateway = Gateway(grants, args.state_dir / "drafts.sqlite3", args.data_dir)
        runtime = Runtime(registry, gateway)
        if args.command == "invoke":
            output = runtime.invoke(args.plugin_id, json.loads(args.input), args.version, args.confirm)
            print(_json({"output": output, "trace": runtime.trace, "audit": gateway.audit}))
            return 0
        utterance = DEMO_REQUESTS[args.scenario] if args.command == "demo" else args.request
        adapter = MockOnDeviceIntentAdapter()
        intent = adapter.parse(utterance)
        report = runtime.run(intent, confirmed=args.confirm)
        report["permission_profile"] = profile
        report["grants"] = sorted(grants)
        report["discovery"] = [{"id": match["manifest"]["id"], "version": match["manifest"]["version"],
                                "score": match["score"], "method": match["method"]} for match in registry.search(utterance)]
        if args.json:
            print(_json(report))
        else:
            _print_report(report)
        return 0 if report["status"] == "completed" else 2
    except (SpliceError, OSError, ValueError) as exc:
        print(_json({"status": "blocked", "error": {"type": type(exc).__name__, "message": str(exc)}}))
        return 2
    finally:
        if gateway:
            gateway.close()


if __name__ == "__main__":
    raise SystemExit(main())
