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
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage

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
        def __init__(self, url: str | None = None, data: bytes | None = None):
            self.url = url
            self.data = data

        @staticmethod
        def fromURL(url: str):
            return Image(url)

        @staticmethod
        def fromBytes(data: bytes):
            return Image(data=bytes(data))

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
            self.html_render_calls: list[dict] = []

        async def html_render(self, *args, **kwargs):
            self.html_render_calls.append({"args": args, "kwargs": kwargs})
            render_dir = Path(StarTools.data_dir) / "renders"
            render_dir.mkdir(parents=True, exist_ok=True)
            image_path = render_dir / f"render-{len(self.html_render_calls)}.png"
            image = PILImage.new("RGBA", (12, 10), (0, 0, 0, 0))
            image.paste((32, 96, 160, 255), (3, 2, 9, 8))
            image.save(image_path, format="PNG")
            return str(image_path)

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
import main as m

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
        network_probe_enabled=False,
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

    # History fills only missing metrics; valid realtime zeroes remain authoritative.
    supplement_plugin = m.KomariGuardPlugin(
        FakeContext(),
        m.KomariGuardConfig(network_probe_enabled=False),
    )

    async def supplement_nodes():
        return [{"uuid": "u-zero", "name": "zero-node"}], None

    async def supplement_realtime():
        return [{"uuid": "u-zero", "cpu_usage": 0, "disk_usage": 0}], True

    async def supplement_history(nodes):
        return ([{
            "uuid": "u-zero",
            "cpu_usage": 99,
            "ram_usage": 50,
            "disk_usage": 88,
        }], {"u-zero"})

    supplement_plugin._nodes = supplement_nodes
    supplement_plugin._realtime = supplement_realtime
    supplement_plugin._history_realtime = supplement_history
    supplemented, supplement_error = await supplement_plugin._snapshot()
    supplemented_node = supplemented[0]
    check(
        "history supplements missing metrics only",
        supplement_error is None
        and m._metric(supplemented_node, "cpu") == 0
        and m._metric(supplemented_node, "memory") == 50
        and m._metric(supplemented_node, "disk") == 0
        and supplemented_node.get("_telemetry_source") == "实时快照 · 缺失项由历史补全",
    )

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
        network_probe_enabled=False,
        notification_routes=[
            {"target_umo": target_node, "node": "node1"},
            {"target_umo": target_all, "node": "*"},
        ],
    )
    schedule_plugin = m.KomariGuardPlugin(FakeContext(), schedule_cfg)
    routes = schedule_plugin._routes()
    noon = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc).timestamp()
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

    # Passive image results use AstrBot's bytes component and renderer contract.
    image_plugin = m.KomariGuardPlugin(
        FakeContext(),
        m.KomariGuardConfig(image_output=True, network_probe_enabled=False),
    )
    result = await image_plugin._report_result(FakeEvent(), [{"name": "n", "is_online": True}])
    image_component = result[1][0] if result[0] == "chain" and result[1] else None
    cropped_size = None
    if image_component is not None and image_component.data:
        with PILImage.open(BytesIO(image_component.data)) as rendered:
            cropped_size = rendered.size
    check("chain_result component list", result[0] == "chain" and isinstance(result[1], list))
    check("image component uses png bytes", image_component is not None and image_component.data.startswith(b"\x89PNG"))
    check("transparent render padding cropped", cropped_size == (6, 6))
    render_call = image_plugin.html_render_calls[-1]
    expected_options = {
        "type": "png",
        "quality": None,
        "omit_background": True,
        "full_page": True,
        "viewport_width": 1800,
        "viewport_height": 1,
        "device_scale_factor_level": "normal",
        "scale": "device",
    }
    check(
        "html render options",
        render_call["kwargs"].get("return_url") is False
        and render_call["kwargs"].get("options") == expected_options,
    )

    # Network probes cache both task metadata and per-node records for 60 seconds.
    recent_time = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    cache_tasks = {
        "status": "success",
        "data": [{
            "id": 11,
            "name": "China Telecom Shanghai",
            "clients": ["node-a"],
            "type": "icmp",
            "interval": 60,
        }],
    }
    cache_records = {
        "status": "success",
        "data": {"records": [{
            "task_id": 11,
            "time": recent_time,
            "value": 32,
            "client": "node-a",
        }]},
    }
    cache_plugin = m.KomariGuardPlugin(
        FakeContext(),
        m.KomariGuardConfig(komari_url="https://status.example.com"),
    )
    cache_calls: list[str] = []

    async def cache_get_json(endpoint: str):
        cache_calls.append(endpoint)
        return (cache_tasks, None) if endpoint == m.PING_TASKS_PATH else (cache_records, None)

    cache_plugin._get_json = cache_get_json
    cache_source = [{"uuid": "node-a", "name": "cache-node", "is_online": True}]
    first_cached = await cache_plugin._with_network_probes(cache_source)
    second_cached = await cache_plugin._with_network_probes(cache_source)
    record_calls = [endpoint for endpoint in cache_calls if endpoint.startswith("/api/records/ping?")]
    check(
        "network probe cache",
        cache_calls.count(m.PING_TASKS_PATH) == 1
        and len(record_calls) == 1
        and first_cached[0]["_network_probe"] == second_cached[0]["_network_probe"]
        and "_network_probe" not in cache_source[0],
    )

    # Selecting one node must not fetch records for tasks assigned to other nodes.
    isolation_tasks = {
        "status": "success",
        "data": [
            {"id": 21, "name": "China Telecom A", "clients": ["node-a"], "interval": 60},
            {"id": 22, "name": "China Unicom B", "clients": ["node-b"], "interval": 60},
        ],
    }
    isolation_records = {
        "status": "success",
        "data": {"records": [{
            "task_id": 21,
            "time": recent_time,
            "value": 24,
            "client": "node-a",
        }]},
    }
    isolation_plugin = m.KomariGuardPlugin(
        FakeContext(),
        m.KomariGuardConfig(komari_url="https://status.example.com"),
    )
    isolation_calls: list[str] = []

    async def isolation_get_json(endpoint: str):
        isolation_calls.append(endpoint)
        return (isolation_tasks, None) if endpoint == m.PING_TASKS_PATH else (isolation_records, None)

    isolation_plugin._get_json = isolation_get_json
    isolated = await isolation_plugin._with_network_probes([{"uuid": "node-a", "name": "only-a"}])
    isolated_record_calls = [endpoint for endpoint in isolation_calls if endpoint.startswith("/api/records/ping?")]
    check(
        "network probe node isolation",
        len(isolated) == 1
        and isolated[0]["name"] == "only-a"
        and len(isolated_record_calls) == 1
        and "uuid=node-a" in isolated_record_calls[0]
        and "node-b" not in isolated_record_calls[0],
    )

    # A Komari deployment without the ping API still gets a complete resource card.
    unavailable_plugin = m.KomariGuardPlugin(
        FakeContext(),
        m.KomariGuardConfig(image_output=True, komari_url="https://status.example.com"),
    )
    unavailable_calls: list[str] = []

    async def unavailable_get_json(endpoint: str):
        unavailable_calls.append(endpoint)
        return None, "Komari API 返回 HTTP 404"

    unavailable_plugin._get_json = unavailable_get_json
    unavailable_result = await unavailable_plugin._report_result(
        FakeEvent(),
        [{
            "uuid": "resource-node-id",
            "name": "resource-node",
            "is_online": True,
            "cpu_usage": 42,
            "ram": 512,
            "ram_total": 1024,
        }],
    )
    unavailable_html = unavailable_plugin.html_render_calls[-1]["args"][1]["content"]
    unavailable_image = unavailable_result[1][0] if unavailable_result[0] == "chain" else None
    check(
        "probe api unavailable keeps resource card",
        unavailable_calls == [m.PING_TASKS_PATH]
        and unavailable_image is not None
        and unavailable_image.data.startswith(b"\x89PNG")
        and "resource-node" in unavailable_html
        and "CPU" in unavailable_html
        and "读取失败" in unavailable_html,
    )

    # Samples older than three task intervals are marked stale while remaining visible.
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    stale_tasks = {
        "status": "success",
        "data": [{
            "id": 31,
            "name": "China Mobile stale",
            "clients": ["node-stale"],
            "interval": 60,
        }],
    }
    stale_records = {
        "status": "success",
        "data": {"records": [{
            "task_id": 31,
            "time": stale_time,
            "value": 55,
            "client": "node-stale",
        }]},
    }
    stale_plugin = m.KomariGuardPlugin(
        FakeContext(),
        m.KomariGuardConfig(komari_url="https://status.example.com", network_probe_hours=1),
    )

    async def stale_get_json(endpoint: str):
        return (stale_tasks, None) if endpoint == m.PING_TASKS_PATH else (stale_records, None)

    stale_plugin._get_json = stale_get_json
    stale_nodes = await stale_plugin._with_network_probes([{"uuid": "node-stale", "name": "stale"}])
    stale_mobile = stale_nodes[0]["_network_probe"]["carriers"]["mobile"]
    check(
        "network probe stale state",
        stale_mobile["status"] == "ok" and stale_mobile["latest_ms"] == 55 and stale_mobile["stale"] is True,
    )

    # Lifecycle starts and fully stops the background task.
    lifecycle = m.KomariGuardPlugin(
        FakeContext(),
        m.KomariGuardConfig(network_probe_enabled=False),
    )
    await lifecycle.initialize()
    task = lifecycle._monitor_task
    await asyncio.sleep(0)
    await lifecycle.terminate()
    check("lifecycle task cleaned", task is not None and task.done())

    for instance in (
        plugin,
        supplement_plugin,
        realtime_plugin,
        schedule_plugin,
        dashboard_plugin,
        migration_plugin,
        failure_plugin,
        image_plugin,
        cache_plugin,
        isolation_plugin,
        unavailable_plugin,
        stale_plugin,
    ):
        await instance.terminate()

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
