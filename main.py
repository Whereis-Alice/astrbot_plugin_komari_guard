"""Komari Guard - scoped Komari monitoring and proactive notifications."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools

PLUGIN_ID = "astrbot_plugin_komari_guard"
PLUGIN_VERSION = "2.0.0"
REPOSITORY_URL = "https://github.com/Whereis-Alice/astrbot_plugin_komari_guard"

_MSG_TYPES = aiohttp.WSMsgType

_ALERT_HISTORY_LIMIT = 50
_HISTORY_CONCURRENCY = 8


class NotificationRoute(BaseModel):
    """One notification destination and its node/report scope."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = ""
    target_umo: str = ""
    node: str = "*"
    alerts: bool = True
    report_time: str = ""
    enabled: bool = True
    source: str = "config"


@dataclass(frozen=True)
class AlertMessage:
    text: str
    node: Optional[dict[str, Any]] = None


class KomariGuardConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    komari_url: str = Field("", description="Komari 服务器地址")
    komari_token: str = Field("", description="API Token 或 Session Token")
    image_output: bool = Field(False, description="以图片卡片发送状态报告")
    image_width: int = Field(900, ge=500, le=1600, description="状态图片宽度")
    poll_interval: int = Field(60, ge=15, le=3600)
    offline_grace_cycles: int = Field(2, ge=1, le=10)
    cpu_threshold: float = Field(90, ge=1, le=100)
    memory_threshold: float = Field(90, ge=1, le=100)
    disk_threshold: float = Field(90, ge=1, le=100)
    high_load_cycles: int = Field(2, ge=1, le=10)
    alert_cooldown: int = Field(1800, ge=0, le=86400)
    notify_recovery: bool = True
    request_timeout: int = Field(10, ge=3, le=60)
    filter_mode: str = Field("none", pattern="^(none|allow|deny)$", description="节点过滤：none/allow(仅监控)/deny(排除)")
    filter_nodes: str = Field("", description="要过滤的节点名，多个用英文逗号分隔")
    status_report_interval: int = Field(0, ge=0, le=720, description="定时状态推送间隔（小时），0 表示关闭")
    status_report_time: str = Field("", description="每天定时推送状态卡片的本地时刻（HH:MM，如 09:00），留空不启用")
    prune_missing_cycles: int = Field(5, ge=1, le=100, description="节点消失多少周期后清理其监控状态")
    panel_fail_cycles: int = Field(3, ge=0, le=10, description="面板连续失败多少个周期后推送不可达告警，0 表示关闭")
    notify_restart: bool = True
    long_offline_remind_hours: int = Field(0, ge=0, le=720, description="节点离线超过多少小时后每日提醒一次，0 表示关闭")
    notification_routes: list[NotificationRoute] = Field(
        default_factory=list,
        description="按会话、节点与时刻分发告警和日报",
    )


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
    else:
        if not isinstance(value, str) or not value:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
        except ValueError:
            try:
                ts = float(value)
            except ValueError:
                return None
    if ts > 1e12:
        ts /= 1000
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _metric(node: dict[str, Any], name: str) -> Optional[float]:
    aliases = {
        # Komari percentage fields are already expressed as 0..100. Values
        # between 0 and 1 are valid low utilization, not fractional ratios.
        "cpu": ("cpu_usage", "cpu_percent", "cpuUsage", "cpu_used_percent", "usage", "cpu"),
        "memory": ("memory_usage", "memory_percent", "memory_usage_percent", "ram_usage", "ram_percent", "mem_usage", "mem_percent"),
        "disk": ("disk_usage", "disk_percent", "disk_usage_percent", "storage_percent"),
    }
    for key in aliases[name]:
        value = _num(node.get(key))
        if value is not None:
            return value
    containers = {
        "cpu": (node.get("cpu"),),
        "memory": (node.get("ram"), node.get("memory"), node.get("mem")),
        "disk": (node.get("disk"), node.get("storage")),
    }
    for nested in containers[name]:
        if not isinstance(nested, dict):
            continue
        value = _num(nested.get("usage", nested.get("percent", nested.get("used_percent", nested.get("percentage")))))
        if value is not None:
            return value
        used, total = _num(nested.get("used")), _num(nested.get("total"))
        if used is not None and total and total > 0:
            return used / total * 100
    pairs = {
        "memory": (("ram", "ram_total"), ("mem_used", "mem_total"), ("memory_used", "memory_total"), ("ram_used", "ram_total")),
        "disk": (("disk", "disk_total"), ("disk_used", "disk_total"), ("storage_used", "storage_total")),
    }
    for used_key, total_key in pairs.get(name, ()):
        used, total = _num(node.get(used_key)), _num(node.get(total_key))
        if used is not None and total and total > 0:
            return used / total * 100
    return None


class KomariGuardPlugin(Star):
    """Komari queries plus stateful offline/high-load notifications."""

    def __init__(self, context: Context, config: AstrBotConfig | KomariGuardConfig | None = None):
        super().__init__(context)
        if isinstance(config, KomariGuardConfig):
            self.config = config
        else:
            try:
                raw_config = dict(config or {})
            except (TypeError, ValueError):
                raw_config = {}
            self.config = KomariGuardConfig.model_validate(raw_config)
        self.logger = logger
        try:
            self.state_dir = StarTools.get_data_dir(PLUGIN_ID)
        except (RuntimeError, ValueError):
            # Keeps isolated unit tests usable before AstrBot initializes StarTools.
            self.state_dir = Path("data") / "plugin_data" / PLUGIN_ID
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.state = self._load_state()
        self._migrate_state()
        self._stop = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        self._failure_count = 0
        self._filter_warned = False
        self._route_warnings: set[str] = set()

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _migrate_state(self) -> None:
        """Normalize state from older builds without sharing the old data directory."""
        changed = False
        routes = self.state.get("routes")
        if not isinstance(routes, list):
            routes = []
            self.state["routes"] = routes
            changed = True
        legacy_targets = self.state.pop("targets", [])
        if isinstance(legacy_targets, list):
            for target in legacy_targets:
                if target and not any(isinstance(route, dict) and route.get("target_umo") == str(target) for route in routes):
                    routes.append({"target_umo": str(target), "node": "*", "alerts": True, "report_time": ""})
                    changed = True
        legacy_mute = _num(self.state.pop("muted_until", None))
        if legacy_mute is not None and legacy_mute > time.time():
            muted = self.state.setdefault("muted", {})
            for route in routes:
                if isinstance(route, dict) and route.get("target_umo"):
                    muted[str(route["target_umo"])] = legacy_mute
            changed = True
        if self.state.get("schema_version") != 2:
            self.state["schema_version"] = 2
            changed = True
        if changed:
            self._save_state()

    def _save_state(self) -> None:
        temp_file = self.state_file.with_suffix(".json.tmp")
        try:
            temp_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_file.replace(self.state_file)
        except OSError as exc:
            self.logger.warning("保存监控状态失败: %s", exc)

    @staticmethod
    def _valid_umo(value: str) -> bool:
        parts = value.split(":", 2)
        return len(parts) == 3 and all(part.strip() for part in parts)

    @staticmethod
    def _normalize_selector(value: Any) -> str:
        selector = str(value or "*").strip()
        return "*" if selector.lower() in ("", "*", "all", "全部") else selector

    @staticmethod
    def _parse_daily_time(value: str) -> Optional[tuple[int, int]]:
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return None
        hour, minute = int(parts[0]), int(parts[1])
        return (hour, minute) if hour <= 23 and minute <= 59 else None

    def _warn_route(self, key: str, message: str, *args: Any) -> None:
        if key in self._route_warnings:
            return
        self._route_warnings.add(key)
        self.logger.warning(message, *args)

    def _routes(self) -> list[NotificationRoute]:
        routes: list[NotificationRoute] = []
        for configured in self.config.notification_routes:
            route = configured.model_copy(update={"source": "config"})
            if route.enabled:
                routes.append(route)
        for index, raw in enumerate(self.state.get("routes", [])):
            if not isinstance(raw, dict):
                continue
            try:
                route = NotificationRoute.model_validate({**raw, "source": "command"})
            except (TypeError, ValueError) as exc:
                self._warn_route(f"state:{index}", "忽略损坏的推送路由 #%s: %s", index + 1, exc)
                continue
            if route.enabled:
                routes.append(route)

        output: list[NotificationRoute] = []
        seen: set[tuple[str, str, bool, str]] = set()
        for route in routes:
            route = route.model_copy(update={"node": self._normalize_selector(route.node)})
            if not self._valid_umo(route.target_umo):
                self._warn_route(
                    f"umo:{route.target_umo}",
                    "忽略无效的推送目标 %r：应为 platform:message_type:session_id 格式。",
                    route.target_umo,
                )
                continue
            identity = (route.target_umo, route.node.casefold(), route.alerts, route.report_time)
            if identity not in seen:
                seen.add(identity)
                output.append(route)
        return output

    def _targets(self) -> list[str]:
        return list(dict.fromkeys(route.target_umo for route in self._routes()))

    @staticmethod
    def _route_key(route: NotificationRoute) -> str:
        raw = f"{route.target_umo}\0{route.node.casefold()}\0{route.report_time}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def _route_matches(self, route: NotificationRoute, node: dict[str, Any]) -> bool:
        selector = self._normalize_selector(route.node)
        if selector == "*":
            return True
        idents = self._node_idents(node)
        tokens = [token.strip().casefold() for token in selector.split(",") if token.strip()]
        return any(
            any((token[1:] in ident) if token.startswith("~") else (token == ident) for ident in idents)
            for token in tokens
            if token != "~"
        )

    def _supports_proactive_message(self, event: AstrMessageEvent) -> bool:
        """Return whether the current adapter advertises proactive sends."""
        try:
            platform = self.context.get_platform_inst(event.get_platform_id())
            metadata = platform.meta() if platform is not None else None
            return bool(metadata and metadata.support_proactive_message)
        except (AttributeError, KeyError, TypeError):
            # Older compatible adapters may not expose metadata consistently.
            return True

    def _headers(self) -> dict[str, str]:
        if not self.config.komari_token:
            return {}
        return {"Authorization": f"Bearer {self.config.komari_token}", "Cookie": f"session_token={self.config.komari_token}"}

    async def _get_session(self) -> aiohttp.ClientSession:
        # 方法名不能叫 _session：实例属性 self._session 会遮蔽同名方法，
        # 调用时抛 TypeError 并被当作连接失败吞掉（1.1.0 引入、本版修复的严重问题）。
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
                self._session = aiohttp.ClientSession(timeout=timeout, headers=self._headers())
            return self._session

    async def _get_json(self, endpoint: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if not self.config.komari_url:
            return None, "请先在插件配置中填写 Komari 服务器地址。"
        try:
            session = await self._get_session()
            async with session.get(self.config.komari_url.rstrip("/") + endpoint) as response:
                if response.status != 200:
                    return None, f"Komari API 返回 HTTP {response.status}"
                raw = await response.read()
                try:
                    payload = json.loads(raw.decode("utf-8-sig", "replace"))
                except (UnicodeError, ValueError, TypeError) as exc:
                    return None, f"Komari 返回了无法解析的 JSON：{exc}"
                if not isinstance(payload, dict):
                    return None, "Komari API 返回了非对象 JSON。"
                return payload, None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            return None, f"连接 Komari 失败：{exc}"

    async def _nodes(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        payload, error = await self._get_json("/api/nodes")
        if error:
            return [], error
        raw: Any = payload.get("data", []) if payload else []
        if isinstance(raw, dict):
            raw = raw.get("nodes", raw.get("servers", list(raw.values())))
        return ([item for item in raw if isinstance(item, dict)], None) if isinstance(raw, list) else ([], None)

    def _ws_url(self) -> str:
        base = self.config.komari_url.rstrip("/")
        if base.lower().startswith("https://"):
            return "wss://" + base.split("://", 1)[1] + "/api/clients"
        if base.lower().startswith("http://"):
            return "ws://" + base.split("://", 1)[1] + "/api/clients"
        return base + "/api/clients"

    @staticmethod
    def _ws_bytes(data: Any) -> bytes:
        if isinstance(data, bytes):
            return data
        if isinstance(data, bytearray):
            return bytes(data)
        return str(data).encode("utf-8", "replace")

    async def _read_ws_payload(self, ws: aiohttp.ClientWebSocketResponse, timeout: float = 6.0) -> Optional[str]:
        """Read a single complete WS payload, tolerating binary frames, pings and
        (defensively) fragmented frames instead of stopping after a fixed count."""
        parts: list[bytes] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if message.type == _MSG_TYPES.CONTINUATION:
                parts.append(self._ws_bytes(message.data))
                continue
            if message.type in (_MSG_TYPES.TEXT, _MSG_TYPES.BINARY):
                if isinstance(message.data, str):
                    text: Optional[str] = message.data
                else:
                    text = self._ws_bytes(message.data).decode("utf-8", "replace")
                if not text:
                    continue
                if parts:
                    text = "".join(p.decode("utf-8", "replace") for p in parts) + text
                return text
            if message.type in (_MSG_TYPES.CLOSED, _MSG_TYPES.CLOSE, _MSG_TYPES.ERROR):
                break
            # ignore PING / PONG
        return None

    @staticmethod
    def _parse_ws_clients(payload: Any) -> list[dict[str, Any]]:
        raw = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            details = raw["data"]
            online = raw.get("online", details.keys())
            result = []
            for key in online:
                value = details.get(key)
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = None
                if isinstance(value, dict):
                    result.append({**value, "uuid": key})
            return result
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        # Some older Komari builds return {uuid: metrics} directly.
        if isinstance(raw, dict):
            mapped = []
            for key, value in raw.items():
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = None
                if isinstance(value, dict) and any(field in value for field in ("cpu", "ram", "memory", "disk")):
                    mapped.append({**value, "uuid": key})
            return mapped
        return []

    @staticmethod
    def _is_ws_snapshot(payload: Any) -> bool:
        raw = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(raw, list):
            return True
        if not isinstance(raw, dict):
            return False
        if "online" in raw and isinstance(raw.get("online"), list):
            return True
        if isinstance(raw.get("data"), dict):
            return True
        return any(
            isinstance(value, dict) and any(field in value for field in ("cpu", "ram", "memory", "disk"))
            for value in raw.values()
        )

    async def _realtime(self) -> tuple[list[dict[str, Any]], bool]:
        if not self.config.komari_url:
            return [], False
        try:
            session = await self._get_session()
            ws_timeout = float(min(self.config.request_timeout, 15))
            origin = self.config.komari_url.rstrip("/")
            async with session.ws_connect(
                self._ws_url(),
                heartbeat=10,
                timeout=ws_timeout,
                origin=origin,
            ) as ws:
                await ws.send_str("get")
                # 首条消息可能是 ack/pong 等非数据帧，最多再读两条直到解析出客户端数据。
                for _ in range(3):
                    text = await self._read_ws_payload(ws)
                    if not text:
                        break
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        continue
                    clients = self._parse_ws_clients(payload)
                    if clients or self._is_ws_snapshot(payload):
                        return clients, True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as exc:
            self.logger.debug("Komari WebSocket 读取失败: %s", exc)
            return [], False
        return [], False

    async def _history_series(self, node: dict[str, Any], hours: int) -> list[dict[str, Any]]:
        uuid = node.get("uuid") or node.get("id")
        if not uuid:
            return []
        try:
            payload, _ = await self._get_json(f"/api/records/load?uuid={quote(str(uuid))}&hours={hours}&load_type=all")
        except Exception as exc:
            self.logger.debug("读取 %s 历史记录失败: %s", uuid, exc)
            return []
        data = payload.get("data", {}) if payload else {}
        records = data.get("records", []) if isinstance(data, dict) else []
        output: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            cpu = _num(item.get("cpu_percent", item.get("cpu")))
            ram = _num(item.get("ram_percent"))
            if ram is None:
                used, total = _num(item.get("ram")), _num(item.get("ram_total"))
                if used is not None and total and total > 0:
                    ram = used / total * 100
            disk = _num(item.get("disk_percent"))
            if disk is None:
                used, total = _num(item.get("disk")), _num(item.get("disk_total"))
                if used is not None and total and total > 0:
                    disk = used / total * 100
            output.append({"cpu": cpu, "ram": ram, "disk": disk,
                           "net_in": _num(item.get("net_in")), "net_out": _num(item.get("net_out"))})
        return output

    async def _history_by_node(self, node: dict[str, Any], hours: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        series = await self._history_series(node, hours)
        return (node, series)

    @staticmethod
    def _record_time(item: dict[str, Any]) -> float:
        """记录时间排序键：兼容 unix 秒/毫秒时间戳与 ISO 字符串，无法解析返回 0。"""
        num = _num(item.get("time"))
        if num is not None:
            return num / 1000 if num > 1e12 else num
        parsed = _parse_time(item.get("time"))
        return parsed.timestamp() if parsed else 0.0

    async def _history_one(self, node: dict[str, Any]) -> tuple[Optional[dict[str, Any]], bool]:
        uuid = node.get("uuid") or node.get("id")
        if not uuid:
            return None, False
        try:
            payload, error = await self._get_json(f"/api/records/load?uuid={quote(str(uuid))}&hours=1&load_type=all")
        except Exception as exc:
            self.logger.debug("读取 %s 历史记录失败: %s", uuid, exc)
            return None, False
        if error:
            self.logger.debug("读取 %s 历史记录失败: %s", uuid, error)
            return None, False
        data = payload.get("data", {}) if payload else {}
        records = data.get("records", []) if isinstance(data, dict) else []
        if not isinstance(records, list):
            return None, False
        if not records:
            return None, True
        latest = max((item for item in records if isinstance(item, dict)), key=self._record_time, default=None)
        if not latest:
            return None, True
        item: dict[str, Any] = {"uuid": str(uuid), "updated_at": latest.get("time")}
        if latest.get("cpu") is not None:
            item["cpu_usage"] = latest["cpu"]
        ram_total = latest.get("ram_total") or node.get("mem_total") or node.get("memory_total")
        if latest.get("ram") is not None:
            item["ram"] = {"used": latest["ram"], "total": ram_total or 0}
        if latest.get("ram_percent") is not None:
            item["ram_usage"] = latest["ram_percent"]
        if latest.get("disk") is not None:
            item["disk"] = {"used": latest["disk"], "total": latest.get("disk_total") or node.get("disk_total") or 0}
        if latest.get("disk_percent") is not None:
            item["disk_usage"] = latest["disk_percent"]
        if latest.get("net_in") is not None or latest.get("net_out") is not None:
            item["network"] = {"down": latest.get("net_in", 0), "up": latest.get("net_out", 0)}
        item["load"] = {"load1": latest.get("load", "-")}
        return item, True

    async def _history_realtime(self, nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
        """Fallback for panels where the client WebSocket is disabled by a proxy."""
        candidates = [node for node in nodes if node.get("uuid") or node.get("id")]
        semaphore = asyncio.Semaphore(_HISTORY_CONCURRENCY)

        async def fetch(node: dict[str, Any]):
            async with semaphore:
                return await self._history_one(node)

        tasks = [fetch(node) for node in candidates]
        if not tasks:
            return [], set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        output: list[dict[str, Any]] = []
        successful: set[str] = set()
        for node, result in zip(candidates, results):
            if isinstance(result, BaseException) or not isinstance(result, tuple):
                continue
            item, ok = result
            if ok:
                successful.add(self._node_key(node))
            if isinstance(item, dict):
                output.append(item)
        return output, successful

    @staticmethod
    def _merge_nodes(static: list[dict[str, Any]], live: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key = {str(item.get(key)): item for item in static for key in ("id", "uuid") if item.get(key) is not None}
        merged, live_keys = [], set()
        for item in live:
            key = str(item.get("uuid") or item.get("id") or "")
            if key:
                live_keys.add(key)
            merged.append({**by_key.get(key, {}), **item})
        for item in static:
            key = str(item.get("uuid") or item.get("id") or "")
            if key not in live_keys:
                merged.append(dict(item))
        return merged

    def _is_online(self, node: dict[str, Any], live_keys: set[str], ws_live: bool) -> bool:
        """WS 通道可用时其在线列表是权威信号；仅当 WS 整体不可用、纯靠
        历史记录兜底时才校验记录时间新鲜度——否则稀疏的历史时间戳会
        否决 WS 刚上报的在线状态，制造假离线告警。"""
        if ws_live:
            return self._node_key(node) in live_keys
        updated = _parse_time(node.get("updated_at") or node.get("last_seen"))
        freshness_window = max(self.config.poll_interval * 3, 180)
        return bool(updated and (datetime.now(timezone.utc) - updated).total_seconds() < freshness_window)

    def _format_report(self, nodes: list[dict[str, Any]]) -> str:
        lines = ["📡 Komari 服务器状态"]
        for node in nodes:
            name = node.get("name") or node.get("hostname") or node.get("id") or "未知节点"
            status = node.get("is_online")
            online = "在线" if status is True else ("离线" if status is False else "未知")
            icon = "🟢" if status is True else ("🔴" if status is False else "🟡")
            cpu, memory, disk = _metric(node, "cpu"), _metric(node, "memory"), _metric(node, "disk")
            metrics = " / ".join(f"{label} {value:.1f}%" for value, label in ((cpu, "CPU"), (memory, "内存"), (disk, "磁盘")) if value is not None)
            lines.append(f"\n{icon} {name} · {online}{(' · ' + metrics) if metrics else ''}")
        return "\n".join(lines) if len(lines) > 1 else "Komari 没有返回节点。"

    @staticmethod
    def _reltime(value: Any) -> str:
        updated = _parse_time(value)
        if updated is None:
            return "等待心跳"
        delta = (datetime.now(timezone.utc) - updated).total_seconds()
        delta = max(0.0, delta)
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{int(delta // 60)} 分钟前"
        if delta < 86400:
            return f"{int(delta // 3600)} 小时前"
        return f"{int(delta // 86400)} 天前"

    @staticmethod
    def _fmt_bytes(value: Any) -> str:
        amount = _num(value)
        if amount is None:
            return "-"
        units = ("B", "KB", "MB", "GB", "TB")
        index = 0
        while abs(amount) >= 1024 and index < len(units) - 1:
            amount /= 1024
            index += 1
        return f"{amount:.1f} {units[index]}"

    @staticmethod
    def _fmt_speed(value: Any) -> str:
        return f"{KomariGuardPlugin._fmt_bytes(value)}/s"

    @staticmethod
    def _fmt_uptime(value: Any) -> str:
        seconds = _num(value)
        if seconds is None:
            return "-"
        return KomariGuardPlugin._fmt_duration(seconds)

    @staticmethod
    def _fmt_duration(value: Any) -> str:
        seconds = _num(value)
        if seconds is None:
            return "-"
        seconds = max(0, int(seconds))
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        parts = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}时")
        parts.append(f"{minutes}分")
        return "".join(parts)

    def _page_html(self, title: str, subtitle: str, body: str, stats: str = "") -> str:
        return f'''<!doctype html><html><head><meta charset="utf-8"><style>
        *{{box-sizing:border-box}} body{{width:{self.config.image_width}px;margin:0;padding:14px;background:#e8efed;font-family:"Microsoft YaHei",sans-serif;color:#173b3f}}
        .wrap{{background:#f8fbfa;border:1px solid #d7e3e0;border-radius:20px;padding:18px;box-shadow:0 10px 24px #173b3f22}} .top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}}
        .tag{{background:#0f766e;border-radius:8px;padding:11px 18px;color:#fff;font-size:23px;font-weight:700}} .stamp{{background:#173b3f;color:#fff;border-radius:8px;padding:11px 16px;font-size:16px;font-weight:700}}
        h1{{font-size:29px;margin:0 0 4px}} .sub{{color:#647876;font-size:15px}} .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}
        .card{{background:#fff;border:1px solid #dce7e5;border-radius:8px;padding:16px;box-shadow:0 2px 8px #173b3f12}} .node-head,.metric>div,.facts{{display:flex;justify-content:space-between;align-items:center}} .node-head{{margin-bottom:14px;font-size:18px}} .node-head small{{font-size:13px;color:#647876}} .dot{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:9px}} .online{{background:#19a974}} .offline{{background:#e45757}} .unknown{{background:#f5a623}}
        .metric{{margin:10px 0}} .metric>div{{font-size:14px;color:#647876}} .metric b{{color:#173b3f}} .metric i{{display:block;height:8px;background:#e8efed;border-radius:8px;margin-top:6px;overflow:hidden}} .metric em{{display:block;height:100%;border-radius:8px}} .facts{{flex-wrap:wrap;gap:8px;margin-top:17px;color:#647876;font-size:12px}} .updated{{border-top:1px solid #e1e9e7;margin-top:14px;padding-top:11px;color:#839491;font-size:11px}} .empty{{padding:40px;text-align:center;color:#647876}}
        .chart{{width:100%;height:72px;display:block;background:#f8fbfa;border-radius:6px;margin-top:6px}} .chartinfo{{font-size:13px;color:#647876;margin-top:10px}}
        .stats{{display:flex;gap:10px;margin:15px 0 2px}} .pill{{background:#fff;border:1px solid #dce7e5;border-radius:999px;padding:7px 15px;font-size:14px;font-weight:700;color:#647876}} .pill.ok{{color:#12815b}} .pill.bad{{color:#d44747}}
        </style></head><body><main class="wrap"><div class="top"><span class="tag">Komari Guard</span><span class="stamp">{datetime.now().strftime('%Y-%m-%d %H:%M')}</span></div><h1>{title}</h1><div class="sub">{subtitle}</div>{f'<div class="stats">{stats}</div>' if stats else ''}<div class="grid">{body}</div></main></body></html>'''

    def _report_html(self, nodes: list[dict[str, Any]]) -> str:
        """Build a self-contained card; no external assets or copied template."""
        cards: list[str] = []
        for node in nodes:
            name = html.escape(str(node.get("name") or node.get("hostname") or node.get("id") or "未知节点"))
            status = node.get("is_online")
            online = status is True
            status_label = "在线" if status is True else ("离线" if status is False else "未知")
            status_class = "online" if status is True else ("offline" if status is False else "unknown")
            cpu, memory, disk = _metric(node, "cpu"), _metric(node, "memory"), _metric(node, "disk")
            network = node.get("network") if isinstance(node.get("network"), dict) else {}
            load = node.get("load") if isinstance(node.get("load"), dict) else {}
            def progress(label: str, value: Optional[float], color: str) -> str:
                shown = "-" if value is None else f"{value:.1f}%"
                width = 0 if value is None else min(max(value, 0), 100)
                return f'<div class="metric"><div><span>{label}</span><b>{shown}</b></div><i><em style="width:{width}%;background:{color}"></em></i></div>'
            rel = self._reltime(node.get("updated_at") or node.get("last_seen"))
            if status is None:
                updated = "遥测状态：暂时不可用"
            elif online:
                updated = f"更新时间：{html.escape(rel)}"
            elif rel != "等待心跳":
                updated = f"最后在线：{html.escape(rel)}"
            else:
                updated = "状态：离线"
            cards.append(f'''<section class="card"><div class="node-head"><div><span class="dot {status_class}"></span><strong>{name}</strong></div><small>{status_label}</small></div>
                {progress('CPU', cpu, '#f25f5c')}{progress('内存', memory, '#f5a623')}{progress('磁盘', disk, '#0ea5a6')}
                <div class="facts"><span>上行 {html.escape(self._fmt_speed(network.get('up')))}</span><span>下行 {html.escape(self._fmt_speed(network.get('down')))}</span><span>负载 {html.escape(str(load.get('load1', '-')))}</span><span>运行 {html.escape(self._fmt_uptime(node.get('uptime')))}</span></div>
                <div class="updated">{updated}</div></section>''')
        body = "".join(cards) or '<div class="empty">Komari 没有返回节点数据</div>'
        total = len(nodes)
        online_count = sum(1 for node in nodes if node.get("is_online") is True)
        offline_count = sum(1 for node in nodes if node.get("is_online") is False)
        unknown_count = total - online_count - offline_count
        stats = (f'<span class="pill">共 {total} 节点</span><span class="pill ok">在线 {online_count}</span>'
                 f'<span class="pill bad">离线 {offline_count}</span>'
                 f'<span class="pill">未知 {unknown_count}</span>') if nodes and unknown_count else (
                     f'<span class="pill">共 {total} 节点</span><span class="pill ok">在线 {online_count}</span>'
                     f'<span class="pill bad">离线 {offline_count}</span>' if nodes else ""
                 )
        return self._page_html("服务器运行状态", "实时资源概览 · 自动刷新由 AstrBot 监控任务负责", body, stats)

    @staticmethod
    def _mini_chart(label: str, values: list[Any], color: str, hours: int) -> str:
        width, height = 300, 72
        grid = "".join(f'<line x1="0" y1="{height - g / 100 * height:.1f}" x2="{width}" y2="{height - g / 100 * height:.1f}" stroke="#f1e5ea" stroke-width="1"/>' for g in (0, 50, 100))
        count = len(values)
        if count < 2:
            inner = f'<text x="{width / 2}" y="40" text-anchor="middle" font-size="12" fill="#927f8c">暂无数据</text>'
        else:
            prev = 0.0
            points = []
            for i, value in enumerate(values):
                val = _num(value)
                val = prev if val is None else min(max(val, 0), 100)
                prev = val
                points.append(f"{i / (count - 1) * width:.1f},{height - val / 100 * height:.1f}")
            inner = f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
        valid = [v for v in (_num(x) for x in values) if v is not None]
        suffix = f" · 当前 {valid[-1]:.1f}% · 峰值 {max(valid):.1f}%" if valid else ""
        return f'<div class="chartinfo">{label}（最近 {hours} 小时）{suffix}</div><svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart">{grid}{inner}</svg>'

    @classmethod
    def _traffic_chart(cls, series: list[dict[str, Any]], hours: int) -> str:
        width, height = 300, 72
        up = [v if (v := _num(p.get("net_out"))) is not None and v >= 0 else 0.0 for p in series]
        down = [v if (v := _num(p.get("net_in"))) is not None and v >= 0 else 0.0 for p in series]
        peak = max(up + down, default=0.0)
        scale = peak if peak > 0 else 1.0
        grid = "".join(f'<line x1="0" y1="{height - g / 100 * height:.1f}" x2="{width}" y2="{height - g / 100 * height:.1f}" stroke="#f1e5ea" stroke-width="1"/>' for g in (0, 50, 100))

        def poly(vals: list[float], color: str) -> str:
            points = " ".join(f"{i / (len(vals) - 1) * width:.1f},{height - 3 - min(v / scale, 1.0) * (height - 6):.1f}" for i, v in enumerate(vals))
            return f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'

        if len(up) < 2:
            inner = f'<text x="{width / 2}" y="40" text-anchor="middle" font-size="12" fill="#927f8c">暂无数据</text>'
        else:
            inner = poly(up, "#22b8cf") + poly(down, "#ff6b9d")
        info = ""
        if up:
            info = (f" · 当前 ↑{cls._fmt_speed(up[-1])} ↓{cls._fmt_speed(down[-1] if down else None)}"
                    f" · 峰值 ↑{cls._fmt_speed(max(up, default=0.0))} ↓{cls._fmt_speed(max(down, default=0.0))}")
        return f'<div class="chartinfo">流量（最近 {hours} 小时）{info}</div><svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart">{grid}{inner}</svg>'

    def _history_html(self, series_by_node: dict[str, Any], hours: int) -> str:
        cards: list[str] = []
        for entry in series_by_node.values():
            node = entry["node"]
            series = entry["series"]
            name = html.escape(str(node.get("name") or node.get("hostname") or node.get("id") or "未知节点"))
            status = node.get("is_online")
            status_label = "在线" if status is True else ("离线" if status is False else "未知")
            status_class = "online" if status is True else ("offline" if status is False else "unknown")
            charts = (
                self._mini_chart("CPU", [p.get("cpu") for p in series], "#f25f5c", hours)
                + self._mini_chart("内存", [p.get("ram") for p in series], "#f5a623", hours)
                + self._mini_chart("磁盘", [p.get("disk") for p in series], "#0ea5a6", hours)
                + self._traffic_chart(series, hours)
            )
            cards.append(f'<section class="card"><div class="node-head"><div><span class="dot {status_class}"></span><strong>{name}</strong></div><small>{status_label} · 最近 {hours} 小时</small></div>{charts}</section>')
        body = "".join(cards) or '<div class="empty">没有可用的历史数据</div>'
        return self._page_html("历史资源趋势", f"CPU / 内存 / 磁盘 · 最近 {hours} 小时", body)

    def _history_text(self, series_by_node: dict[str, Any], hours: int) -> str:
        lines = [f"📊 Komari 历史（最近 {hours} 小时）"]
        for entry in series_by_node.values():
            node = entry["node"]
            name = node.get("name") or node.get("hostname") or node.get("id") or "未知节点"
            series = entry["series"]
            last = series[-1] if series else {}
            def fmt(v: Any) -> str:
                return "-" if v is None else f"{v:.1f}%"
            traffic = ""
            if last.get("net_in") is not None or last.get("net_out") is not None:
                traffic = f" / 流量 ↑{self._fmt_speed(last.get('net_out'))} ↓{self._fmt_speed(last.get('net_in'))}"
            lines.append(f"{name}：CPU {fmt(last.get('cpu'))} / 内存 {fmt(last.get('ram'))} / 磁盘 {fmt(last.get('disk'))}{traffic}")
        return "\n".join(lines) or "没有可用的历史数据。"

    async def _chain_from_html(self, html_text: str, text_fallback: str) -> MessageChain:
        """Build an image Chain from HTML, falling back to text on render failure or image_output off."""
        if not self.config.image_output:
            return MessageChain().message(text_fallback)
        try:
            image_url = await self.html_render(html_text, {"content": html_text}, options={"type": "jpeg", "quality": 92, "full_page": True})
            if image_url:
                return MessageChain([Image.fromURL(image_url)])
        except Exception as exc:
            self.logger.warning("状态卡片渲染失败，回退文本：%s", exc)
        return MessageChain().message(text_fallback)

    async def _report_chain(self, nodes: list[dict[str, Any]]) -> MessageChain:
        return await self._chain_from_html(self._report_html(nodes), self._format_report(nodes))

    async def _report_result(self, event: AstrMessageEvent, nodes: list[dict[str, Any]]):
        if not nodes:
            return event.plain_result("Komari 没有返回节点。")
        if not self.config.image_output:
            return event.plain_result(self._format_report(nodes))
        chain = await self._report_chain(nodes)
        return event.chain_result(list(chain.chain))

    # ---- 节点过滤 / 选择 ----

    @staticmethod
    def _node_key(node: dict[str, Any]) -> str:
        return str(node.get("uuid") or node.get("id") or "")

    @staticmethod
    def _node_idents(node: dict[str, Any]) -> list[str]:
        return [str(node.get(key) or "").lower() for key in ("name", "hostname", "id", "uuid") if node.get(key)]

    def _filter_tokens(self) -> list[str]:
        return [t.strip().lower() for t in (self.config.filter_nodes or "").split(",") if t.strip()]

    def _monitored(self, node: dict[str, Any]) -> bool:
        mode = self.config.filter_mode
        if mode == "none":
            return True
        idents = self._node_idents(node)
        hit = any(any(token in ident for ident in idents) for token in self._filter_tokens())
        return hit if mode == "allow" else (not hit)

    def _visible(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [node for node in nodes if self._monitored(node)]

    def _select(self, nodes: list[dict[str, Any]], keyword: Any) -> list[dict[str, Any]]:
        if not isinstance(keyword, str) or not keyword.strip():
            return nodes
        keyword = keyword.strip().casefold()
        return [node for node in nodes if any(keyword in value for value in self._node_idents(node))]

    def _warn_filter_misconfig(self) -> None:
        if self._filter_warned or self.config.filter_mode != "allow":
            return
        if not self._filter_tokens():
            self.logger.warning("filter_mode 为 allow 但 filter_nodes 为空：当前不会监控或推送任何节点。"
                                "请填写 filter_nodes，或将 filter_mode 改为 none / deny。")
            self._filter_warned = True

    def _can_alert(self, record: dict[str, Any], kind: str, now: float) -> bool:
        """Prevent repeated alerts when a node flaps around a threshold."""
        last = _num(record.get("sent", {}).get(kind))
        return last is None or self.config.alert_cooldown == 0 or now - last >= self.config.alert_cooldown

    # ---- 静默 ----

    def _muted(self, target: str) -> bool:
        muted = self.state.get("muted")
        if not isinstance(muted, dict):
            return False
        until = _num(muted.get(target))
        return until is not None and time.time() < until

    # ---- 告警历史 ----

    def _append_alert(self, text: str) -> None:
        history = self.state.setdefault("alert_history", [])
        history.append({"time": time.time(), "text": text})
        if len(history) > _ALERT_HISTORY_LIMIT:
            del history[: len(history) - _ALERT_HISTORY_LIMIT]

    async def _send_target(self, target: str, chain: MessageChain) -> bool:
        try:
            result = await self.context.send_message(target, chain)
            if result is False:
                self.logger.warning("向 %s 推送失败：AstrBot 未找到对应平台实例", target)
                return False
            return True
        except Exception as exc:
            self.logger.warning("向 %s 推送失败: %s", target, exc)
            return False

    async def _dispatch_alerts(self, alerts: list[AlertMessage]) -> None:
        """Fan out alerts with a persistent per-target outbox."""
        deliveries: dict[str, list[str]] = {}
        delivered_text: dict[str, set[str]] = {}
        for route in self._routes():
            if not route.alerts:
                continue
            for alert in alerts:
                if alert.node is not None and not self._route_matches(route, alert.node):
                    continue
                seen = delivered_text.setdefault(route.target_umo, set())
                if alert.text in seen:
                    continue
                seen.add(alert.text)
                deliveries.setdefault(route.target_umo, []).append(alert.text)

        pending = self.state.get("pending_alerts")
        if not isinstance(pending, dict):
            pending = {}
            self.state["pending_alerts"] = pending
        active_targets = set(self._targets())
        changed = False
        for target in list(pending):
            if target not in active_targets:
                del pending[target]
                changed = True
        for target in active_targets:
            queued = pending.get(target, [])
            messages = [str(item) for item in queued if isinstance(item, str) and item]
            messages.extend(deliveries.get(target, []))
            messages = list(dict.fromkeys(messages))[-_ALERT_HISTORY_LIMIT:]
            if not messages:
                continue
            if self._muted(target):
                if pending.get(target) != messages:
                    pending[target] = messages
                    changed = True
                continue
            if await self._send_target(target, MessageChain().message("\n\n".join(messages))):
                if target in pending:
                    del pending[target]
                    changed = True
            elif pending.get(target) != messages:
                pending[target] = messages
                changed = True
        if changed or deliveries:
            self._save_state()

    async def _send(self, text: str) -> None:
        """Compatibility helper for panel-wide notifications."""
        await self._dispatch_alerts([AlertMessage(text)])

    def _report_due(self, route: NotificationRoute, now: float) -> bool:
        sent = self.state.get("report_sent")
        if not isinstance(sent, dict):
            sent = {}
            self.state["report_sent"] = sent
        last = _num(sent.get(self._route_key(route)))
        interval_due = (
            self.config.status_report_interval > 0
            and (last is None or now - last >= self.config.status_report_interval * 3600)
        )

        spec = route.report_time.strip() or (self.config.status_report_time or "").strip()
        if not spec:
            return interval_due
        parsed = self._parse_daily_time(spec)
        if parsed is None:
            self._warn_route(
                f"time:{spec}",
                "日报时刻 %r 无效，应为 HH:MM 格式，如 09:00。",
                spec,
            )
            return interval_due
        hour, minute = parsed
        now_dt = datetime.fromtimestamp(now)
        trigger = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        fixed_due = now_dt >= trigger and (last is None or last < trigger.timestamp())
        return fixed_due or interval_due

    async def _maybe_status_push(self, nodes: list[dict[str, Any]], now: float) -> None:
        visible = self._visible(nodes)
        if not visible:
            return
        changed = False
        sent = self.state.setdefault("report_sent", {})
        for route in self._routes():
            has_schedule = bool(route.report_time.strip() or (self.config.status_report_time or "").strip())
            if self.config.status_report_interval <= 0 and not has_schedule:
                continue
            if not self._report_due(route, now):
                continue
            selected = [node for node in visible if self._route_matches(route, node)]
            if not selected:
                continue
            if self._muted(route.target_umo):
                continue
            chain = await self._report_chain(selected)
            if await self._send_target(route.target_umo, chain):
                sent[self._route_key(route)] = now
                changed = True
        if changed:
            self._save_state()

    def _prune_missing(self, known_keys: set[str]) -> None:
        nodes = self.state.get("nodes")
        if not isinstance(nodes, dict):
            return
        for key in list(nodes.keys()):
            record = nodes[key]
            if key in known_keys:
                record["missing"] = 0
                continue
            missing = record.get("missing", 0) + 1
            record["missing"] = missing
            if missing >= self.config.prune_missing_cycles:
                del nodes[key]

    def _prune_muted(self) -> None:
        muted = self.state.get("muted")
        if not isinstance(muted, dict):
            return
        now_ts = time.time()
        for key in [key for key, value in muted.items() if not isinstance(value, (int, float)) or value <= now_ts]:
            del muted[key]

    async def _check_once(self, track_failure: bool = True) -> bool:
        """Run one monitoring cycle. Returns True if the check failed."""
        async with self._check_lock:
            nodes, error = await self._snapshot()
            if error:
                if track_failure:
                    self._failure_count += 1
                    if (self.config.panel_fail_cycles > 0
                            and self._failure_count >= self.config.panel_fail_cycles
                            and not self.state.get("panel_alert")):
                        self.state["panel_alert"] = True
                        message = f"⚠️ Komari 面板不可达\n连续 {self._failure_count} 次检查失败，离线与高负载告警暂停。\n{error}"
                        self._append_alert(message)
                        self._save_state()
                        await self._dispatch_alerts([AlertMessage(message)])
                self.logger.warning(error)
                return True
            self._failure_count = 0
            alerts: list[AlertMessage] = []
            if self.state.get("panel_alert"):
                self.state["panel_alert"] = False
                message = "🟢 Komari 面板已恢复\n检查恢复正常，告警继续生效。"
                alerts.append(AlertMessage(message))
            now = datetime.now(timezone.utc).timestamp()
            known_keys: set[str] = set()
            for node in nodes:
                key = str(node.get("uuid") or node.get("id") or node.get("name") or "unknown")
                known_keys.add(key)
                if not self._monitored(node):
                    continue
                status = node.get("is_online")
                if status is None:
                    # A telemetry failure is not evidence that the node is offline.
                    continue
                record = self.state.setdefault("nodes", {}).setdefault(key, {"offline": 0, "high": 0, "sent": {}, "active": {}})
                record.setdefault("sent", {})
                record.setdefault("active", {})
                is_online = status is True
                record["offline"] = record.get("offline", 0) + 1 if not is_online else 0
                cpu, mem, disk = _metric(node, "cpu"), _metric(node, "memory"), _metric(node, "disk")
                metric_values = {"cpu": cpu, "memory": mem, "disk": disk}
                metric_limits = {
                    "cpu": self.config.cpu_threshold,
                    "memory": self.config.memory_threshold,
                    "disk": self.config.disk_threshold,
                }
                exceeded_metrics = [
                    key for key, value in metric_values.items()
                    if value is not None and value >= metric_limits[key]
                ]
                # 离线节点的指标可能是陈旧历史值，跳过其高负载告警，避免死节点误报。
                metrics_observed = any(value is not None for value in (cpu, mem, disk))
                high: Optional[bool] = None
                if is_online and metrics_observed:
                    high = bool(exceeded_metrics)
                if high is True:
                    record["high"] = record.get("high", 0) + 1
                else:
                    record["high"] = 0
                name = node.get("name") or key
                uptime = _num(node.get("uptime"))
                prev_uptime = _num(record.get("uptime"))
                if (self.config.notify_restart and is_online and uptime is not None
                        and prev_uptime is not None and prev_uptime - uptime > 30
                        and self._can_alert(record, "restart", now)):
                    record["sent"]["restart"] = now
                    alerts.append(AlertMessage(
                        f"🔄 Komari 节点重启\n节点：{name}\n此前已运行 {self._fmt_duration(prev_uptime)}，当前已运行 {self._fmt_duration(uptime)}",
                        node,
                    ))
                if uptime is not None:
                    record["uptime"] = uptime
                # 使用 >= 而非 ==：若触发告警时仍在冷却期内（_can_alert 为 False），
                # 计数器会继续累加，== 判断将永不再成立，导致本次宕机静默。
                if (not is_online
                        and record["offline"] >= self.config.offline_grace_cycles
                        and not record["active"].get("offline")
                        and self._can_alert(record, "offline", now)):
                    record["sent"]["offline"] = now
                    record["active"]["offline"] = True
                    record["offline_started"] = now
                    alerts.append(AlertMessage(
                        f"🔴 Komari 离线告警\n节点：{name}\n连续 {self.config.offline_grace_cycles} 个周期未收到心跳。",
                        node,
                    ))
                elif is_online and record["active"].get("offline"):
                    duration = self._fmt_duration(now - record.get("offline_started", now))
                    record["active"]["offline"] = False
                    if self.config.notify_recovery:
                        alerts.append(AlertMessage(
                            f"🟢 Komari 节点恢复\n节点：{name}\n离线时长：{duration}",
                            node,
                        ))
                if (not is_online and record["active"].get("offline")
                        and self.config.long_offline_remind_hours > 0):
                    offline_secs = now - record.get("offline_started", now)
                    last_remind = _num(record.get("offline_last_remind"))
                    if (offline_secs >= self.config.long_offline_remind_hours * 3600
                            and (last_remind is None or now - last_remind >= 86400)):
                        record["offline_last_remind"] = now
                        alerts.append(AlertMessage(
                            f"🔴 Komari 节点仍离线\n节点：{name}\n已离线 {self._fmt_duration(offline_secs)}",
                            node,
                        ))
                if (high is True
                        and record["high"] >= self.config.high_load_cycles
                        and not record["active"].get("high")
                        and self._can_alert(record, "high", now)):
                    details = ", ".join(f"{label} {value:.1f}%" for value, label, limit in (
                        (cpu, "CPU", self.config.cpu_threshold),
                        (mem, "内存", self.config.memory_threshold),
                        (disk, "磁盘", self.config.disk_threshold)) if value is not None and value >= limit)
                    record["sent"]["high"] = now
                    record["active"]["high"] = True
                    record["high_started"] = now
                    record["high_metrics"] = exceeded_metrics
                    alerts.append(AlertMessage(f"⚠️ Komari 高负载告警\n节点：{name}\n{details}", node))
                elif high is True and record["active"].get("high"):
                    active_metrics = record.setdefault("high_metrics", [])
                    record["high_metrics"] = list(dict.fromkeys([*active_metrics, *exceeded_metrics]))
                elif high is False and record["active"].get("high"):
                    # 只有节点仍在在线时才报恢复，避免把"离线"误报成"负载恢复"。
                    active_metrics = record.get("high_metrics")
                    if not isinstance(active_metrics, list) or not active_metrics:
                        active_metrics = ["cpu", "memory", "disk"]
                    if not all(metric_values.get(key) is not None for key in active_metrics):
                        continue
                    duration = self._fmt_duration(now - record.get("high_started", now))
                    record["active"]["high"] = False
                    record.pop("high_metrics", None)
                    if self.config.notify_recovery:
                        alerts.append(AlertMessage(
                            f"✅ Komari 负载恢复\n节点：{name}\n持续时长：{duration}",
                            node,
                        ))
            self._prune_missing(known_keys)
            self._prune_muted()
            for alert in alerts:
                self._append_alert(alert.text)
            self._save_state()
            await self._dispatch_alerts(alerts)
            await self._maybe_status_push(nodes, now)
            return False

    def _poll_delay(self, failed: bool) -> float:
        if not failed:
            return float(self.config.poll_interval)
        factor = 2 ** min(max(self._failure_count - 1, 0), 10)
        return float(min(self.config.poll_interval * factor, 1800))

    async def _monitor_loop(self) -> None:
        try:
            while not self._stop.is_set():
                failed = False
                try:
                    if self._targets() and self.config.komari_url:
                        failed = await self._check_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._failure_count += 1
                    failed = True
                    self.logger.exception("Komari Guard 监控循环异常，将在退避后重试")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_delay(failed))
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def initialize(self) -> None:
        """Start background monitoring after AstrBot finishes plugin setup."""
        self._start_monitor()

    def _start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name=f"{PLUGIN_ID}:monitor",
            )

    async def _snapshot(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        static, error = await self._nodes()
        if error:
            return [], error
        live, ws_live = await self._realtime()
        history_success: set[str] = set()
        if ws_live:
            stamp = datetime.now(timezone.utc).isoformat()
            for item in live:
                item.setdefault("updated_at", stamp)
            # 只为缺失内存/磁盘指标的在线节点拉历史记录补全展示，
            # 不再因个别节点缺指标而对全部节点发起 N 次请求。
            lacking = [item for item in live if _metric(item, "memory") is None or _metric(item, "disk") is None]
            if lacking:
                history_items, _ = await self._history_realtime(lacking)
                history_by_key = {self._node_key(item): item for item in history_items}
                for item in live:
                    fallback = history_by_key.get(self._node_key(item))
                    if not fallback:
                        continue
                    for field in ("cpu_usage", "ram_usage", "disk_usage"):
                        if item.get(field) is None and fallback.get(field) is not None:
                            item[field] = fallback[field]
                    for section in ("cpu", "ram", "memory", "disk", "storage", "network", "load"):
                        if isinstance(fallback.get(section), dict):
                            current = item.get(section)
                            item[section] = {**fallback[section], **current} if isinstance(current, dict) else fallback[section]
        else:
            live, history_success = await self._history_realtime(static)
            if static and not history_success:
                return [], "Komari 节点列表可读，但 WebSocket 与历史遥测均不可用，本轮不判定节点离线。"
        merged = self._merge_nodes(static, live)
        live_keys = {self._node_key(item) for item in live}
        for node in merged:
            key = self._node_key(node)
            known = ws_live or key in history_success
            node["status_known"] = known
            node["is_online"] = self._is_online(node, live_keys, ws_live) if known else None
        return merged, None

    @filter.command_group("kg", alias={"kguard"})
    def kg(self):
        """Komari Guard command group."""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("s", alias={"status", "状态"})
    async def cmd_status(self, event: AstrMessageEvent, node: str = ""):
        """查询所有 Komari 节点的状态与资源使用率；可加节点名（支持子串）只看指定节点。"""
        self._start_monitor()
        self._warn_filter_misconfig()
        async with self._check_lock:
            nodes, error = await self._snapshot()
        if error:
            yield event.plain_result(error)
            return
        selected = self._visible(self._select(nodes, node))
        if isinstance(node, str) and node.strip() and nodes and not selected:
            yield event.plain_result(f"没有匹配「{node}」的节点，可发送 /kg ls 查看节点列表。")
            return
        yield await self._report_result(event, selected)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("rt", alias={"realtime", "实时"})
    async def cmd_realtime(self, event: AstrMessageEvent, node: str = ""):
        """查询 Komari WebSocket 实时数据（不经历史兜底）；WebSocket 不可用时提示改用状态命令。"""
        self._start_monitor()
        async with self._check_lock:
            live, ws_available = await self._realtime()
            static: list[dict[str, Any]] = []
            static_error: Optional[str] = None
            if ws_available:
                static, static_error = await self._nodes()
        if not ws_available:
            yield event.plain_result("WebSocket 实时通道暂时不可用（可能被反代禁用），请改用 /kg s 查看状态报告。")
            return
        if static_error:
            yield event.plain_result(static_error)
            return
        merged = self._merge_nodes(static, live)
        # 只有出现在 WebSocket 返回里的节点才是在线，掉线节点如实显示离线。
        live_keys = {self._node_key(item) for item in live}
        for node_item in merged:
            node_item["is_online"] = self._node_key(node_item) in live_keys
        selected = self._visible(self._select(merged, node))
        if isinstance(node, str) and node.strip() and merged and not selected:
            yield event.plain_result(f"没有匹配「{node}」的节点，可发送 /kg ls 查看节点列表。")
            return
        yield await self._report_result(event, selected)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("his", alias={"history", "历史"})
    async def cmd_history(self, event: AstrMessageEvent, hours: str = "", node: str = ""):
        """查询历史资源趋势；如 /kg his 6 nodeA。"""
        self._start_monitor()
        hours_text = hours.strip() if isinstance(hours, str) else ""
        keyword = node.strip() if isinstance(node, str) else ""
        if hours_text and not hours_text.isdigit():
            keyword = hours_text
            hours_text = ""
        hour_count = max(1, min(int(hours_text or "1"), 24))
        async with self._check_lock:
            static, error = await self._snapshot()
        if error:
            yield event.plain_result(error)
            return
        static = self._visible(static)
        if not static:
            yield event.plain_result("Komari 没有返回节点。")
            return
        if keyword:
            matched = self._select(static, keyword)
            if not matched:
                yield event.plain_result(f"没有匹配「{keyword}」的节点。")
                return
            static = matched
        semaphore = asyncio.Semaphore(_HISTORY_CONCURRENCY)

        async def fetch_history(node_item: dict[str, Any]):
            async with semaphore:
                return await self._history_by_node(node_item, hour_count)

        tasks = [fetch_history(node_item) for node_item in static]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        series_by_node: dict[str, Any] = {}
        for result in results:
            if isinstance(result, BaseException) or not isinstance(result, tuple):
                continue
            node, series = result
            if series:
                series_by_node[str(node.get("uuid") or node.get("id") or node.get("name"))] = {"node": node, "series": series}
        if not series_by_node:
            yield event.plain_result("没有可用的历史数据。")
            return
        chain = await self._chain_from_html(
            self._history_html(series_by_node, hour_count),
            self._history_text(series_by_node, hour_count),
        )
        yield event.chain_result(list(chain.chain))

    @kg.command("h", alias={"help", "帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 Komari 插件全部命令。"""
        lines = [
            "📖 Komari Guard 命令",
            "/kg s [节点] - 状态报告",
            "/kg rt [节点] - WebSocket 实时状态",
            "/kg his [小时] [节点] - 历史趋势",
            "/kg ls - 节点列表",
            "/kg top [cpu|mem|disk] [数量] - 占用排行",
            "/kg b [节点|*] [HH:MM] [alert|daily|both] - 绑定当前会话",
            "/kg ub [节点|*] - 解除当前会话的命令路由",
            "/kg r - 查看推送路由",
            "/kg m [分钟] [all] / /kg um [all] - 静默/恢复",
            "/kg a - 最近告警；/kg ck - 立即检查",
            "/kg i / /kg v - 站点信息/服务端版本",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("ls", alias={"nodes", "节点"})
    async def cmd_nodes(self, event: AstrMessageEvent):
        """列出全部节点名称，便于填写 filter_nodes 或查询命令参数。"""
        static, error = await self._nodes()
        if error:
            yield event.plain_result(error)
            return
        if not static:
            yield event.plain_result("Komari 没有返回节点。")
            return
        lines = ["📋 Komari 节点列表"]
        excluded = 0
        for node in static:
            name = node.get("name") or node.get("hostname") or node.get("id") or "未知节点"
            if self._monitored(node):
                lines.append(f"· {name}")
            else:
                lines.append(f"· {name}（已被 filter 配置排除）")
                excluded += 1
        summary = f"共 {len(static)} 个节点"
        if excluded:
            summary += f"，其中 {excluded} 个被过滤配置排除"
        lines.append(summary)
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("top", alias={"排行"})
    async def cmd_top(self, event: AstrMessageEvent, metric_name: str = "", limit: str = ""):
        """查看资源占用 Top 榜；如 /kg top mem 10。"""
        self._start_monitor()
        metric, count = "cpu", 5
        for arg in (metric_name, limit):
            text = str(arg).strip().lower()
            if text in ("cpu", "c"):
                metric = "cpu"
            elif text in ("mem", "memory", "m", "内存"):
                metric = "memory"
            elif text in ("disk", "d", "磁盘"):
                metric = "disk"
            elif text.isdigit():
                count = int(text)
        count = max(1, min(count, 20))
        async with self._check_lock:
            nodes, error = await self._snapshot()
        if error:
            yield event.plain_result(error)
            return
        scored = [(_metric(node, metric), node) for node in self._visible(nodes) if node.get("is_online") is True]
        scored = [(value, node) for value, node in scored if value is not None]
        if not scored:
            yield event.plain_result("没有可排序的在线节点。")
            return
        scored.sort(key=lambda pair: pair[0], reverse=True)
        label = {"cpu": "CPU", "memory": "内存", "disk": "磁盘"}[metric]
        top = scored[:count]
        lines = [f"🏆 Komari {label} Top {len(top)}（在线节点）"]
        for rank, (value, node) in enumerate(top, 1):
            name = node.get("name") or node.get("hostname") or node.get("id") or "未知节点"
            lines.append(f"{rank}. {name} · {value:.1f}%")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("info", alias={"i", "站点"})
    async def cmd_info(self, event: AstrMessageEvent):
        """查询 Komari 公开站点信息。"""
        payload, error = await self._get_json("/api/public")
        if error:
            yield event.plain_result(error)
            return
        data = payload.get("data", payload) if payload else {}
        if not isinstance(data, dict):
            yield event.plain_result("Komari 未返回公开站点信息。")
            return
        name = data.get("sitename") or data.get("name") or "未命名站点"
        description = data.get("description") or "无"
        yield event.plain_result(f"🌐 Komari 站点\n名称：{name}\n描述：{description}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("ver", alias={"v", "version", "版本"})
    async def cmd_version(self, event: AstrMessageEvent):
        """查询 Komari 服务端版本。"""
        payload, error = await self._get_json("/api/version")
        if error:
            yield event.plain_result(error)
            return
        data = payload.get("data", payload) if payload else {}
        if not isinstance(data, dict):
            yield event.plain_result("Komari 未返回版本信息。")
            return
        version = data.get("version", "未知")
        commit = data.get("hash") or data.get("commit") or ""
        yield event.plain_result(f"Komari 版本：{version}{f' ({commit})' if commit else ''}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("b", alias={"bind", "绑定"})
    async def cmd_bind(
        self,
        event: AstrMessageEvent,
        node: str = "*",
        report_time: str = "",
        mode: str = "",
    ):
        """将当前会话绑定为全部或指定节点的告警/日报目标。"""
        target = str(event.unified_msg_origin)
        if not self._valid_umo(target):
            yield event.plain_result("当前会话没有可用的 UMO，无法绑定主动推送。")
            return
        if not self._supports_proactive_message(event):
            yield event.plain_result("当前平台不支持主动消息，无法用于告警或日报。")
            return

        selector = self._normalize_selector(node)
        time_spec = report_time.strip() if isinstance(report_time, str) else ""
        if time_spec in ("-", "none", "off", "关闭"):
            time_spec = ""
        if time_spec and self._parse_daily_time(time_spec) is None:
            yield event.plain_result("日报时刻应为 HH:MM，例如 09:00。")
            return
        if time_spec:
            hour, minute = self._parse_daily_time(time_spec) or (0, 0)
            time_spec = f"{hour:02d}:{minute:02d}"

        mode_key = mode.strip().casefold() if isinstance(mode, str) else ""
        if not mode_key:
            mode_key = "both" if time_spec else "alert"
        mode_aliases = {
            "a": "alert", "alert": "alert", "告警": "alert",
            "d": "daily", "daily": "daily", "日报": "daily",
            "b": "both", "both": "both", "全部": "both",
        }
        route_mode = mode_aliases.get(mode_key)
        if route_mode is None:
            yield event.plain_result("模式只支持 alert、daily 或 both。")
            return
        if route_mode in ("daily", "both") and not time_spec:
            inherited = (self.config.status_report_time or "").strip()
            if self._parse_daily_time(inherited) is None and self.config.status_report_interval <= 0:
                yield event.plain_result("日报路由需要 HH:MM，或先在配置中设置全局日报时刻/间隔。")
                return
        if route_mode == "alert":
            time_spec = ""

        raw_routes = self.state.setdefault("routes", [])
        raw_routes[:] = [
            route for route in raw_routes
            if not (isinstance(route, dict)
                    and route.get("target_umo") == target
                    and self._normalize_selector(route.get("node")).casefold() == selector.casefold())
        ]
        raw_routes.append({
            "name": "命令绑定",
            "target_umo": target,
            "node": selector,
            "alerts": route_mode in ("alert", "both"),
            "report_time": time_spec,
            "enabled": True,
        })
        self._save_state()
        self._start_monitor()
        schedule = time_spec or ((self.config.status_report_time or "").strip() if route_mode != "alert" else "")
        summary = ["✅ 已绑定当前会话", f"节点：{selector}", f"模式：{route_mode}"]
        if schedule:
            summary.append(f"日报：{schedule}（AstrBot 宿主本地时间）")
        yield event.plain_result("\n".join(summary))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("ub", alias={"unbind", "解绑"})
    async def cmd_unbind(self, event: AstrMessageEvent, node: str = ""):
        """删除当前会话由命令创建的路由。"""
        target = str(event.unified_msg_origin)
        selector = self._normalize_selector(node) if isinstance(node, str) and node.strip() else ""
        raw_routes = self.state.setdefault("routes", [])
        before = len(raw_routes)
        raw_routes[:] = [
            route for route in raw_routes
            if not (isinstance(route, dict)
                    and route.get("target_umo") == target
                    and (not selector or self._normalize_selector(route.get("node")).casefold() == selector.casefold()))
        ]
        removed = before - len(raw_routes)
        if target not in self._targets():
            self.state.setdefault("pending_alerts", {}).pop(target, None)
            self.state.setdefault("muted", {}).pop(target, None)
        self._save_state()
        if removed:
            yield event.plain_result(f"✅ 已删除 {removed} 条当前会话的命令路由。")
        else:
            yield event.plain_result("未找到匹配的命令路由；配置页创建的路由需在配置页删除。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("r", alias={"routes", "路由"})
    async def cmd_routes(self, event: AstrMessageEvent):
        """列出当前配置路由和命令路由。"""
        routes = self._routes()
        if not routes:
            yield event.plain_result("暂无推送路由。可使用 /kg b 绑定当前会话。")
            return
        lines = ["🛡️ Komari Guard 推送路由"]
        for index, route in enumerate(routes, 1):
            inherited_time = route.report_time or (self.config.status_report_time or "").strip()
            modes = []
            if route.alerts:
                modes.append("告警")
            if inherited_time or self.config.status_report_interval > 0:
                modes.append(f"日报 {inherited_time or f'{self.config.status_report_interval}h'}")
            lines.append(
                f"{index}. [{route.source}] {route.target_umo}\n"
                f"   节点 {route.node} · {' + '.join(modes) if modes else '无推送'}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("m", alias={"mute", "静默"})
    async def cmd_mute(self, event: AstrMessageEvent, minutes_arg: str = "30", scope: str = ""):
        """临时静默当前会话的告警推送，默认 30 分钟；加 all 静默全部绑定会话。"""
        minutes, scope_all = 30, False
        for arg in (minutes_arg, scope):
            text = str(arg).strip().lower()
            if text.isdigit():
                minutes = int(text)
            elif text in ("all", "全部", "全局"):
                scope_all = True
        minutes = max(1, min(minutes, 1440))
        until = time.time() + minutes * 60
        muted = self.state.setdefault("muted", {})
        if scope_all:
            targets = self._targets()
            if not targets:
                yield event.plain_result("当前没有绑定任何会话，无静默对象；可先发送 /kg b 绑定。")
                return
            for target in targets:
                muted[target] = until
            scope_text = "全部绑定会话"
        else:
            if event.unified_msg_origin not in self._targets():
                yield event.plain_result("当前会话没有推送路由。")
                return
            muted[event.unified_msg_origin] = until
            scope_text = "当前会话"
        self._save_state()
        yield event.plain_result(f"🔇 已暂停{scope_text} {minutes} 分钟；告警会进入待发队列，恢复后补发。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("um", alias={"unmute", "恢复"})
    async def cmd_unmute(self, event: AstrMessageEvent, scope: str = ""):
        """解除静默；加 all 解除全部会话的静默。"""
        muted = self.state.get("muted")
        if not isinstance(muted, dict):
            muted = {}
        if str(scope).strip().lower() in ("all", "全部", "全局"):
            muted.clear()
            scope_text = "全部会话"
        else:
            muted.pop(event.unified_msg_origin, None)
            scope_text = "当前会话"
        self.state["muted"] = muted
        self._save_state()
        await self._dispatch_alerts([])
        yield event.plain_result(f"🔊 已解除{scope_text}的静默，恢复正常推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("alert", alias={"a", "alerts", "告警"})
    async def cmd_alerts(self, event: AstrMessageEvent):
        """查看最近的一批告警记录。"""
        history = [item for item in self.state.get("alert_history", []) if isinstance(item, dict)]
        if not history:
            yield event.plain_result("暂无告警记录。")
            return
        lines = ["📜 最近告警"]
        for item in history[-10:]:
            ts = _num(item.get("time"))
            stamp = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "--"
            lines.append(f"▶ {stamp}\n{item.get('text', '')}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @kg.command("ck", alias={"check", "检查"})
    async def cmd_check(self, event: AstrMessageEvent):
        """立即执行一次检查；告警会发往已绑定会话。"""
        failed = await self._check_once(track_failure=False)
        if failed:
            yield event.plain_result("❌ Komari 检查失败，请查看 AstrBot 日志中的具体原因。")
        else:
            yield event.plain_result("✅ 已完成一次 Komari 检查。")

    async def terminate(self):
        self._stop.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


__all__ = ["KomariGuardPlugin", "KomariGuardConfig", "NotificationRoute"]
