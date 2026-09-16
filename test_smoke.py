"""Offline regression suite for Komari Guard.

Run with: python test_smoke.py
Only runtime dependencies from requirements.txt are needed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _mock_astrbot() -> type:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    comp_mod = types.ModuleType("astrbot.api.message_components")
    star_mod = types.ModuleType("astrbot.api.star")

    class AstrBotConfig(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.save_calls = 0

        async def save_config_async(self):
            self.save_calls += 1
            return True

    class Plain:
        def __init__(self, text: str):
            self.text = text

    class Image:
        def __init__(self, url: str):
            self.url = url

        @staticmethod
        def fromURL(url: str):
            return Image(url)

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def message(self, text: str):
            self.chain.append(Plain(text))
            return self

        def get_plain_text(self):
            return " ".join(item.text for item in self.chain if isinstance(item, Plain))

    class AstrMessageEvent:
        pass

    class _PermissionType:
        ADMIN = "admin"

    class _Group:
        def command(self, *_args, **_kwargs):
            return lambda func: func

    class _Filter:
        PermissionType = _PermissionType

        @staticmethod
        def command_group(*_args, **_kwargs):
            return lambda _func: _Group()

        @staticmethod
        def permission_type(*_args, **_kwargs):
            return lambda func: func

    class Star:
        def __init__(self, context=None):
            self.context = context

        async def html_render(self, *_args, **_kwargs):
            return "https://renderer.invalid/card.png"

    class Context:
        pass

    class StarTools:
        data_dir = ROOT / ".test-data"

        @classmethod
        def get_data_dir(cls, _plugin_name=None):
            return Path(cls.data_dir)

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("komari-guard-test")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter()
    comp_mod.Image = Image
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    astrbot.api = api
    api.event = event_mod
    api.message_components = comp_mod
    api.star = star_mod
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.message_components": comp_mod,
        "astrbot.api.star": star_mod,
    })
    return StarTools


StarTools = _mock_astrbot()
sys.path.insert(0, str(ROOT))
import main as m  # noqa: E402


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool) -> None:
    (PASS if condition else FAIL).append(name)


class FakeMeta:
    support_proactive_message = True


class FakePlatform:
    @staticmethod
    def meta():
        return FakeMeta()


class FakeContext:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.fail_targets: set[str] = set()

    @staticmethod
    def get_platform_inst(_platform_id: str):
        return FakePlatform()

    async def send_message(self, target: str, chain) -> bool:
        if target in self.fail_targets:
            return False
        self.sent.append((target, chain.get_plain_text()))
        return True


class FakeEvent:
    unified_msg_origin = "test:GroupMessage:room"

    @staticmethod
    def get_platform_id() -> str:
        return "test"

    @staticmethod
    def plain_result(text: str):
        return ("plain", text)

    @staticmethod
    def chain_result(chain):
        assert isinstance(chain, list), "AstrBot chain_result requires a component list"
        return ("chain", chain)


async def collect(generator) -> list:
    return [item async for item in generator]


async def run() -> None:
    target_node = "test:GroupMessage:node-room"
    target_all = "test:GroupMessage:ops-room"
    context = FakeContext()
    cfg = m.KomariGuardConfig(
        komari_url="https://status.example.com",
        image_output=False,
        high_load_cycles=2,
        offline_grace_cycles=2,
        notification_routes=[
            {"target_umo": target_node, "node": "node1", "alerts": True},
            {"target_umo": target_node, "node": "~node1", "alerts": True},
            {"target_umo": target_all, "node": "*", "alerts": True},
        ],
    )
    plugin = m.KomariGuardPlugin(context, cfg)

    # Configuration, parser and metric regressions.
    check("image output defaults private", m.KomariGuardConfig().image_output is False)
    check("percent below one stays percent", m._metric({"cpu_usage": 0.5}, "cpu") == 0.5)
    ws_node = {"cpu": 5.0, "ram": 2, "ram_total": 4, "disk": 3, "disk_total": 4}
    check("ws scalar cpu", m._metric(ws_node, "cpu") == 5.0)
    check("ws scalar ram", m._metric(ws_node, "memory") == 50.0)
    check("ws scalar disk", m._metric(ws_node, "disk") == 75.0)
    nested = {"cpu": {"usage": 0.8}, "ram": {"used": 1, "total": 4}}
    check("nested percent stays percent", m._metric(nested, "cpu") == 0.8)
    check("nested used total", m._metric(nested, "memory") == 25.0)
    check("timestamp seconds and millis", m._parse_time(1757000000) is not None and m._parse_time(1757000000000) is not None)
    check("empty ws snapshot recognized", m.KomariGuardPlugin._is_ws_snapshot({"data": {"online": [], "data": {}}}))

    # Exact node routes do not leak node1 alerts to node10; ~ explicitly means substring.
    node1 = {"uuid": "u1", "name": "node1"}
    node10 = {"uuid": "u10", "name": "node10"}
    exact = m.NotificationRoute(target_umo=target_node, node="node1")
    fuzzy = m.NotificationRoute(target_umo=target_node, node="~node1")
    check("route exact match", plugin._route_matches(exact, node1) and not plugin._route_matches(exact, node10))
    check("route fuzzy match", plugin._route_matches(fuzzy, node10))

    # WS success with no clients means known-offline; total telemetry failure is an error.
    static_nodes = [{"uuid": "u1", "name": "node1"}, {"uuid": "u2", "name": "node2"}]

    async def static_ok():
        return [dict(item) for item in static_nodes], None

    async def ws_empty():
        return [], True

    plugin._nodes = static_ok
    plugin._realtime = ws_empty
    snapshot, error = await plugin._snapshot()
    check("empty ws marks known offline", error is None and all(item["is_online"] is False for item in snapshot))

    # The realtime command must retain its user-supplied selector while it marks
    # merged nodes online/offline.
    realtime_plugin = m.KomariGuardPlugin(FakeContext(), cfg)
    realtime_plugin._start_monitor = lambda: None

    async def realtime_nodes():
        return [
            {"uuid": "u1", "name": "alpha"},
            {"uuid": "u10", "name": "beta"},
        ], None

    async def realtime_live():
        return [
            {"uuid": "u1", "cpu_usage": 10},
            {"uuid": "u10", "cpu_usage": 20},
        ], True

    realtime_plugin._nodes = realtime_nodes
    realtime_plugin._realtime = realtime_live
    realtime_result = await collect(realtime_plugin.cmd_realtime(FakeEvent(), "alpha"))
    realtime_text = realtime_result[0][1]
    check("realtime keeps node selector", "alpha" in realtime_text and "beta" not in realtime_text)

    async def ws_failed():
        return [], False

    async def history_failed(_nodes):
        return [], set()

    plugin._realtime = ws_failed
    plugin._history_realtime = history_failed
    snapshot, error = await plugin._snapshot()
    check("telemetry failure is not offline", snapshot == [] and error is not None and "不判定节点离线" in error)

    # Overlapping routes are de-duplicated per target.
    context.sent.clear()
    await plugin._dispatch_alerts([m.AlertMessage("node1 alert", node1)])
    node_target_messages = [text for target, text in context.sent if target == target_node]
    check("overlapping route dedup", node_target_messages == ["node1 alert"])
    check("wildcard route delivery", any(target == target_all for target, _ in context.sent))

    # Failed and muted sends remain in the persistent outbox and retry.
    context.sent.clear()
    context.fail_targets.add(target_node)
    await plugin._dispatch_alerts([m.AlertMessage("retry me", node1)])
    check("failed send queued", "retry me" in plugin.state.get("pending_alerts", {}).get(target_node, []))
    context.fail_targets.clear()
    await plugin._dispatch_alerts([])
    check("failed send retried", any(target == target_node and "retry me" in text for target, text in context.sent))
    plugin.state.setdefault("muted", {})[target_node] = 9999999999
    await plugin._dispatch_alerts([m.AlertMessage("paused", node1)])
    check("muted send queued", "paused" in plugin.state.get("pending_alerts", {}).get(target_node, []))
    plugin.state["muted"].clear()
    await plugin._dispatch_alerts([])
    check("muted send delivered later", any(target == target_node and "paused" in text for target, text in context.sent))

    # Alert engine: node-scoped target gets only node1, wildcard target gets both.
    live_nodes = [
        {"uuid": "u1", "name": "node1", "is_online": True, "cpu_usage": 95, "memory_usage": 40, "disk_usage": 30, "uptime": 1000},
        {"uuid": "u2", "name": "node2", "is_online": False},
    ]

    async def live_snapshot():
        return live_nodes, None

    plugin._snapshot = live_snapshot
    context.sent.clear()
    await plugin._check_once()
    await plugin._check_once()
    scoped_text = "\n".join(text for target, text in context.sent if target == target_node)
    wildcard_text = "\n".join(text for target, text in context.sent if target == target_all)
    check("single node route isolation", "node1" in scoped_text and "node2" not in scoped_text)
    check("wildcard receives offline and high", "node1" in wildcard_text and "node2" in wildcard_text)

    # Missing metrics do not manufacture a load-recovery event.
    context.sent.clear()
    live_nodes[0].pop("cpu_usage")
    await plugin._check_once()
    check("missing metric no recovery", all("负载恢复" not in text for _, text in context.sent))
    live_nodes[0]["cpu_usage"] = 10
    await plugin._check_once()
    check("observed low metric recovers", any("负载恢复" in text for _, text in context.sent))

    # Each route owns its own daily progress marker.
    schedule_cfg = m.KomariGuardConfig(
        status_report_time="09:00",
        notification_routes=[
            {"target_umo": target_node, "node": "node1"},
            {"target_umo": target_all, "node": "*"},
        ],
    )
    schedule_plugin = m.KomariGuardPlugin(FakeContext(), schedule_cfg)
    routes = schedule_plugin._routes()
    noon = datetime(2026, 9, 17, 12, 0).timestamp()
    check("both routes initially due", all(schedule_plugin._report_due(route, noon) for route in routes))
    schedule_plugin.state.setdefault("report_sent", {})[schedule_plugin._route_key(routes[0])] = noon
    check("daily progress isolated", not schedule_plugin._report_due(routes[0], noon) and schedule_plugin._report_due(routes[1], noon))

    # Binding uses the current full UMO and fixed optional parameters.
    results = await collect(plugin.cmd_bind(FakeEvent(), "node1", "09:00", "daily"))
    command_routes = [route for route in plugin.state["routes"] if route.get("target_umo") == FakeEvent.unified_msg_origin]
    check("bind creates daily-only route", bool(results) and command_routes[-1]["alerts"] is False and command_routes[-1]["report_time"] == "09:00")

    # Real AstrBotConfig-backed bindings are persisted into the Dashboard-visible
    # template_list, and unbind removes only command-managed entries.
    dashboard_config = m.AstrBotConfig({
        "notification_routes": [
            {
                "__template_key": "route",
                "name": "手动路由",
                "target_umo": FakeEvent.unified_msg_origin,
                "node": "manual-node",
                "alerts": True,
                "report_time": "",
                "enabled": True,
                "source": "config",
            }
        ]
    })
    dashboard_plugin = m.KomariGuardPlugin(FakeContext(), dashboard_config)
    dashboard_plugin._start_monitor = lambda: None
    bind_result = await collect(dashboard_plugin.cmd_bind(FakeEvent(), "node1", "09:00", "daily"))
    saved_routes = dashboard_config["notification_routes"]
    command_saved = [route for route in saved_routes if route.get("source") == "command"]
    check(
        "bind persists dashboard route",
        bool(bind_result)
        and dashboard_config.save_calls == 1
        and len(command_saved) == 1
        and command_saved[0].get("__template_key") == "route"
        and command_saved[0].get("report_time") == "09:00",
    )
    await collect(dashboard_plugin.cmd_unbind(FakeEvent(), "node1"))
    remaining_routes = dashboard_config["notification_routes"]
    check(
        "unbind preserves manual route",
        dashboard_config.save_calls == 2
        and len(remaining_routes) == 1
        and remaining_routes[0].get("name") == "手动路由",
    )

    # Existing v2.0.0 state routes migrate once and are removed from state only
    # after the AstrBot config save succeeds.
    migration_config = m.AstrBotConfig({"notification_routes": []})
    migration_plugin = m.KomariGuardPlugin(FakeContext(), migration_config)
    migration_plugin.state["routes"] = [{
        "name": "旧命令绑定",
        "target_umo": target_node,
        "node": "node1",
        "alerts": True,
        "report_time": "",
        "enabled": True,
    }]
    await migration_plugin._migrate_state_routes_to_config()
    check(
        "legacy route migrates to dashboard",
        migration_config.save_calls == 1
        and migration_plugin.state["routes"] == []
        and migration_config["notification_routes"][0].get("source") == "command",
    )

    class FailingAstrBotConfig(m.AstrBotConfig):
        async def save_config_async(self):
            raise OSError("simulated write failure")

    class SilentLogger:
        @staticmethod
        def exception(*_args, **_kwargs):
            pass

        @staticmethod
        def warning(*_args, **_kwargs):
            pass

    failure_config = FailingAstrBotConfig({"notification_routes": []})
    failure_plugin = m.KomariGuardPlugin(FakeContext(), failure_config)
    failure_plugin.logger = SilentLogger()
    failure_plugin.state["routes"] = [{
        "target_umo": target_node,
        "node": "node1",
        "alerts": True,
        "report_time": "",
    }]
    await failure_plugin._migrate_state_routes_to_config()
    check(
        "failed migration retains legacy route",
        len(failure_plugin.state["routes"]) == 1
        and failure_config["notification_routes"] == [],
    )
    for name, method in inspect.getmembers(m.KomariGuardPlugin, inspect.isfunction):
        if name.startswith("cmd_"):
            check(f"fixed command signature {name}", all(param.kind is not inspect.Parameter.VAR_POSITIONAL for param in inspect.signature(method).parameters.values()))

    # Passive image results pass component lists, matching AstrBot's real API.
    image_plugin = m.KomariGuardPlugin(FakeContext(), m.KomariGuardConfig(image_output=True))
    result = await image_plugin._report_result(FakeEvent(), [{"name": "n", "is_online": True}])
    check("chain_result component list", result[0] == "chain" and isinstance(result[1], list))

    # Lifecycle starts and fully stops the background task.
    lifecycle = m.KomariGuardPlugin(FakeContext(), m.KomariGuardConfig())
    await lifecycle.initialize()
    task = lifecycle._monitor_task
    await asyncio.sleep(0)
    await lifecycle.terminate()
    check("lifecycle task cleaned", task is not None and task.done())

    # Public artifacts stay in sync with the runtime contract.
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    check("schema routes and privacy default", "notification_routes" in schema and schema["image_output"]["default"] is False)
    check("metadata renamed", "astrbot_plugin_komari_guard" in metadata and "astrbot_plugin_komari_watch" not in metadata)


with tempfile.TemporaryDirectory(prefix="komari-guard-test-") as temp_dir:
    StarTools.data_dir = Path(temp_dir)
    asyncio.run(run())

print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("FAILED:")
    for item in FAIL:
        print(" -", item)
    raise SystemExit(1)
