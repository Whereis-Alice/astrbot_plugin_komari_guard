"""Komari Watch - an AstrBot plugin for status and proactive alerts."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import aiohttp
from pydantic import BaseModel, Field

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register

PLUGIN_ID = "astrbot_plugin_komari_watch"

_MSG_TYPES = aiohttp.WSMsgType

_ALERT_HISTORY_LIMIT = 50


class KomariWatchConfig(BaseModel):
    komari_url: str = Field("", description="Komari 服务器地址")
    komari_token: str = Field("", description="API Token 或 Session Token")
    image_output: bool = Field(True, description="以图片卡片发送状态报告")
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
    prune_missing_cycles: int = Field(5, ge=1, le=100, description="节点消失多少周期后清理其监控状态")


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def _metric(node: dict[str, Any], name: str) -> Optional[float]:
    aliases = {
        "cpu": ("cpu_usage", "cpu_percent", "cpuUsage", "cpu_used_percent", "usage"),
        "memory": ("memory_usage", "memory_percent", "memory_usage_percent", "ram_usage", "ram_percent", "mem_usage", "mem_percent"),
        "disk": ("disk_usage", "disk_percent", "disk_usage_percent", "storage_percent"),
    }
    for key in aliases[name]:
        value = _num(node.get(key))
        if value is not None:
            return value * 100 if 0 <= value <= 1 else value
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
            return value * 100 if 0 <= value <= 1 else value
        used, total = _num(nested.get("used")), _num(nested.get("total"))
        if used is not None and total and total > 0:
            return used / total * 100
    pairs = {
        "memory": (("mem_used", "mem_total"), ("memory_used", "memory_total"), ("ram_used", "ram_total")),
        "disk": (("disk_used", "disk_total"), ("storage_used", "storage_total")),
    }
    for used_key, total_key in pairs.get(name, ()):
        used, total = _num(node.get(used_key)), _num(node.get(total_key))
        if used is not None and total and total > 0:
            return used / total * 100
    return None


@register(PLUGIN_ID, "xiaowan", "Komari 监控推送插件", "1.2.0", "https://github.com/xiaowan138/astrbot_plugin_komari_watch")
class KomariWatchPlugin(Star):
    """Komari queries plus stateful offline/high-load notifications."""

    def __init__(self, context: Context, config: KomariWatchConfig | None = None):
        super().__init__(context)
        self.config = config or KomariWatchConfig()
        self.logger = logging.getLogger(PLUGIN_ID)
        self.state_dir = Path("data") / "plugin_data" / PLUGIN_ID
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.state = self._load_state()
        self._stop = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._failure_count = 0
        self._filter_warned = False
        try:
            self._monitor_task = asyncio.get_running_loop().create_task(self._monitor_loop())
        except RuntimeError:
            self.logger.debug("No running event loop; monitor starts on first command")

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.logger.warning("保存监控状态失败: %s", exc)

    def _targets(self) -> list[str]:
        return [str(item) for item in self.state.get("targets", []) if item]

    def _headers(self) -> dict[str, str]:
        if not self.config.komari_token:
            return {}
        return {"Authorization": f"Bearer {self.config.komari_token}", "Cookie": f"session_token={self.config.komari_token}"}

    async def _session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
            self._session = aiohttp.ClientSession(timeout=timeout, headers=self._headers())
        return self._session

    async def _get_json(self, endpoint: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if not self.config.komari_url:
            return None, "请先在插件配置中填写 Komari 服务器地址。"
        try:
            session = await self._session()
            async with session.get(self.config.komari_url.rstrip("/") + endpoint) as response:
                if response.status != 200:
                    return None, f"Komari API 返回 HTTP {response.status}"
                payload = await response.json(content_type=None)
                return payload if isinstance(payload, dict) else None, None
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

    async def _realtime(self) -> list[dict[str, Any]]:
        if not self.config.komari_url:
            return []
        try:
            session = await self._session()
            ws_timeout = aiohttp.ClientTimeout(total=min(self.config.request_timeout, 15))
            async with session.ws_connect(self._ws_url(), heartbeat=10, timeout=ws_timeout) as ws:
                await ws.send_str("get")
                text = await self._read_ws_payload(ws)
                if not text:
                    return []
                payload = json.loads(text)
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
                    if mapped:
                        return mapped
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
            return []
        return []

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
            output.append({"cpu": cpu, "ram": ram, "disk": disk})
        return output

    async def _history_by_node(self, node: dict[str, Any], hours: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        series = await self._history_series(node, hours)
        return (node, series)

    async def _history_one(self, node: dict[str, Any]) -> Optional[dict[str, Any]]:
        uuid = node.get("uuid") or node.get("id")
        if not uuid:
            return None
        try:
            payload, _ = await self._get_json(f"/api/records/load?uuid={quote(str(uuid))}&hours=1&load_type=all")
        except Exception as exc:
            self.logger.debug("读取 %s 历史记录失败: %s", uuid, exc)
            return None
        data = payload.get("data", {}) if payload else {}
        records = data.get("records", []) if isinstance(data, dict) else []
        if not isinstance(records, list) or not records:
            return None
        latest = max((item for item in records if isinstance(item, dict)), key=lambda item: str(item.get("time", "")), default=None)
        if not latest:
            return None
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
        return item

    async def _history_realtime(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fallback for panels where the client WebSocket is disabled by a proxy."""
        tasks = [self._history_one(node) for node in nodes if node.get("uuid") or node.get("id")]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [item for item in results if isinstance(item, dict)]

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

    def _is_online(self, node: dict[str, Any], live_keys: set[str], history_keys: set[str]) -> bool:
        key = str(node.get("uuid") or node.get("id") or "")
        updated = _parse_time(node.get("updated_at") or node.get("last_seen"))
        fresh = bool(updated and (datetime.now(timezone.utc) - updated).total_seconds() < self.config.poll_interval * 3)
        if key and key in live_keys:
            # WebSocket 在线列表是权威心跳；历史记录兜底时须校验时间新鲜度，
            # 否则节点死亡后残留的旧记录会把它永远标记为在线，离线告警永不触发。
            return True if key not in history_keys else fresh
        return fresh

    def _format_report(self, nodes: list[dict[str, Any]]) -> str:
        lines = ["📡 Komari 服务器状态"]
        for node in nodes:
            name = node.get("name") or node.get("hostname") or node.get("id") or "未知节点"
            online = "在线" if node.get("is_online") else "离线"
            cpu, memory, disk = _metric(node, "cpu"), _metric(node, "memory"), _metric(node, "disk")
            metrics = " / ".join(f"{label} {value:.1f}%" for value, label in ((cpu, "CPU"), (memory, "内存"), (disk, "磁盘")) if value is not None)
            lines.append(f"\n{'🟢' if online == '在线' else '🔴'} {name} · {online}{(' · ' + metrics) if metrics else ''}")
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
        return f"{KomariWatchPlugin._fmt_bytes(value)}/s"

    @staticmethod
    def _fmt_uptime(value: Any) -> str:
        seconds = _num(value)
        if seconds is None:
            return "-"
        return KomariWatchPlugin._fmt_duration(seconds)

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

    def _page_html(self, title: str, subtitle: str, body: str) -> str:
        return f'''<!doctype html><html><head><meta charset="utf-8"><style>
        *{{box-sizing:border-box}} body{{width:{self.config.image_width}px;margin:0;padding:28px;background:#d7aabd;font-family:"Microsoft YaHei",sans-serif;color:#392d3b}}
        .wrap{{background:#f7e7ed;border-radius:24px;padding:26px;box-shadow:0 12px 28px #8f627455}} .top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}}
        .tag{{background:#fff;border-radius:10px;padding:12px 22px;color:#ee6394;font-size:24px;font-weight:700}} .stamp{{background:#25b9e8;color:#fff;border-radius:12px;padding:12px 18px;font-size:18px;font-weight:700}}
        h1{{font-size:30px;margin:0 0 4px}} .sub{{color:#927f8c;font-size:15px}} .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}
        .card{{background:#fffafc;border-radius:18px;padding:20px;box-shadow:0 3px 10px #9f708522}} .node-head,.metric>div,.facts{{display:flex;justify-content:space-between;align-items:center}} .node-head{{margin-bottom:15px;font-size:18px}} .node-head small{{font-size:13px;color:#8e7c88}} .dot{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:9px}} .online{{background:#42c88a}} .offline{{background:#f05d74}}
        .metric{{margin:10px 0}} .metric>div{{font-size:14px;color:#7d6d77}} .metric b{{color:#392d3b}} .metric i{{display:block;height:8px;background:#f1e5ea;border-radius:8px;margin-top:6px;overflow:hidden}} .metric em{{display:block;height:100%;border-radius:8px}} .facts{{flex-wrap:wrap;gap:8px;margin-top:18px;color:#877681;font-size:12px}} .updated{{border-top:1px solid #f0e2e8;margin-top:15px;padding-top:12px;color:#ad9ba4;font-size:11px}} .empty{{padding:40px;text-align:center;color:#927f8c}}
        .chart{{width:100%;height:72px;display:block;background:#fffafc;border-radius:8px;margin-top:6px}} .chartinfo{{font-size:13px;color:#7d6d77;margin-top:10px}}
        </style></head><body><main class="wrap"><div class="top"><span class="tag">Komari 监控</span><span class="stamp">{datetime.now().strftime('%Y-%m-%d %H:%M')}</span></div><h1>{title}</h1><div class="sub">{subtitle}</div><div class="grid">{body}</div></main></body></html>'''

    def _report_html(self, nodes: list[dict[str, Any]]) -> str:
        """Build a self-contained card; no external assets or copied template."""
        cards: list[str] = []
        for node in nodes:
            name = html.escape(str(node.get("name") or node.get("hostname") or node.get("id") or "未知节点"))
            online = bool(node.get("is_online"))
            cpu, memory, disk = _metric(node, "cpu"), _metric(node, "memory"), _metric(node, "disk")
            network = node.get("network") if isinstance(node.get("network"), dict) else {}
            load = node.get("load") if isinstance(node.get("load"), dict) else {}
            def progress(label: str, value: Optional[float], color: str) -> str:
                shown = "-" if value is None else f"{value:.1f}%"
                width = 0 if value is None else min(max(value, 0), 100)
                return f'<div class="metric"><div><span>{label}</span><b>{shown}</b></div><i><em style="width:{width}%;background:{color}"></em></i></div>'
            updated = html.escape(self._reltime(node.get("updated_at") or node.get("last_seen")))
            cards.append(f'''<section class="card"><div class="node-head"><div><span class="dot {'online' if online else 'offline'}"></span><strong>{name}</strong></div><small>{'在线' if online else '离线'}</small></div>
                {progress('CPU', cpu, '#ff6b9d')}{progress('内存', memory, '#8b7bff')}{progress('磁盘', disk, '#22b8cf')}
                <div class="facts"><span>上行 {html.escape(self._fmt_speed(network.get('up')))}</span><span>下行 {html.escape(self._fmt_speed(network.get('down')))}</span><span>负载 {html.escape(str(load.get('load1', '-')))}</span><span>运行 {html.escape(self._fmt_uptime(node.get('uptime')))}</span></div>
                <div class="updated">更新时间：{updated}</div></section>''')
        body = "".join(cards) or '<div class="empty">Komari 没有返回节点数据</div>'
        return self._page_html("服务器运行状态", "实时资源概览 · 自动刷新由 AstrBot 监控任务负责", body)

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
        return f'<div class="chartinfo">{label}（最近 {hours} 小时）</div><svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="chart">{grid}{inner}</svg>'

    def _history_html(self, series_by_node: dict[str, Any], hours: int) -> str:
        cards: list[str] = []
        for entry in series_by_node.values():
            node = entry["node"]
            series = entry["series"]
            name = html.escape(str(node.get("name") or node.get("hostname") or node.get("id") or "未知节点"))
            charts = (
                self._mini_chart("CPU", [p.get("cpu") for p in series], "#ff6b9d", hours)
                + self._mini_chart("内存", [p.get("ram") for p in series], "#8b7bff", hours)
                + self._mini_chart("磁盘", [p.get("disk") for p in series], "#22b8cf", hours)
            )
            cards.append(f'<section class="card"><div class="node-head"><div><span class="dot online"></span><strong>{name}</strong></div><small>最近 {hours} 小时</small></div>{charts}</section>')
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
            lines.append(f"{name}：CPU {fmt(last.get('cpu'))} / 内存 {fmt(last.get('ram'))} / 磁盘 {fmt(last.get('disk'))}")
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
        return event.chain_result(await self._report_chain(nodes))

    # ---- 节点过滤 / 选择 ----

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

    def _select(self, nodes: list[dict[str, Any]], args: tuple[Any, ...]) -> list[dict[str, Any]]:
        if not args or not str(args[0]):
            return nodes
        keyword = str(args[0]).lower()
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

    def _muted(self) -> bool:
        until = _num(self.state.get("muted_until"))
        return until is not None and time.time() < until

    # ---- 告警历史 ----

    def _append_alert(self, text: str) -> None:
        history = self.state.setdefault("alert_history", [])
        history.append({"time": time.time(), "text": text})
        if len(history) > _ALERT_HISTORY_LIMIT:
            del history[: len(history) - _ALERT_HISTORY_LIMIT]

    async def _send(self, text: str) -> None:
        if self._muted():
            return
        for target in self._targets():
            try:
                await self.context.send_message(target, MessageChain().message(text))
            except Exception as exc:
                self.logger.warning("向 %s 推送失败: %s", target, exc)

    async def _send_chain(self, chain: MessageChain) -> None:
        if self._muted():
            return
        for target in self._targets():
            try:
                await self.context.send_message(target, chain)
            except Exception as exc:
                self.logger.warning("向 %s 推送失败: %s", target, exc)

    async def _maybe_status_push(self, nodes: list[dict[str, Any]], now: float) -> None:
        if self.config.status_report_interval <= 0:
            return
        last = _num(self.state.get("last_status_report"))
        if last is not None and now - last < self.config.status_report_interval * 3600:
            return
        visible = self._visible(nodes)
        if not visible:
            return
        chain = await self._report_chain(visible)
        await self._send_chain(chain)
        self.state["last_status_report"] = now
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

    async def _check_once(self, track_failure: bool = True) -> bool:
        """Run one monitoring cycle. Returns True if the check failed."""
        async with self._check_lock:
            nodes, error = await self._snapshot()
            if error:
                if track_failure:
                    self._failure_count += 1
                self.logger.warning(error)
                return True
            self._failure_count = 0
            now = datetime.now(timezone.utc).timestamp()
            known_keys: set[str] = set()
            offline_alerted: list[str] = []
            offline_recovered: list[tuple[str, str]] = []
            high_alerted: list[str] = []
            high_recovered: list[tuple[str, str]] = []
            for node in nodes:
                key = str(node.get("uuid") or node.get("id") or node.get("name") or "unknown")
                known_keys.add(key)
                if not self._monitored(node):
                    continue
                record = self.state.setdefault("nodes", {}).setdefault(key, {"offline": 0, "high": 0, "sent": {}, "active": {}})
                record.setdefault("sent", {})
                record.setdefault("active", {})
                is_online = bool(node["is_online"])
                record["offline"] = record.get("offline", 0) + 1 if not is_online else 0
                cpu, mem, disk = _metric(node, "cpu"), _metric(node, "memory"), _metric(node, "disk")
                # 离线节点的指标可能是陈旧历史值，跳过其高负载告警，避免死节点误报。
                high = is_online and ((cpu is not None and cpu >= self.config.cpu_threshold) or (mem is not None and mem >= self.config.memory_threshold) or (disk is not None and disk >= self.config.disk_threshold))
                record["high"] = record.get("high", 0) + 1 if high else 0
                name = node.get("name") or key
                # 使用 >= 而非 ==：若触发告警时仍在冷却期内（_can_alert 为 False），
                # 计数器会继续累加，== 判断将永不再成立，导致本次宕机静默。
                if (not is_online
                        and record["offline"] >= self.config.offline_grace_cycles
                        and not record["active"].get("offline")
                        and self._can_alert(record, "offline", now)):
                    record["sent"]["offline"] = now
                    record["active"]["offline"] = True
                    record["offline_started"] = now
                    offline_alerted.append(name)
                elif is_online and record["active"].get("offline"):
                    duration = self._fmt_duration(now - record.get("offline_started", now))
                    record["active"]["offline"] = False
                    offline_recovered.append((name, duration))
                if (record["high"] >= self.config.high_load_cycles
                        and not record["active"].get("high")
                        and self._can_alert(record, "high", now)):
                    details = ", ".join(f"{label} {value:.1f}%" for value, label in ((cpu, "CPU"), (mem, "内存"), (disk, "磁盘")) if value is not None)
                    record["sent"]["high"] = now
                    record["active"]["high"] = True
                    record["high_started"] = now
                    high_alerted.append(f"⚠️ Komari 高负载告警\n节点：{name}\n{details}")
                elif is_online and not high and record["active"].get("high"):
                    # 只有节点仍在在线时才报恢复，避免把"离线"误报成"负载恢复"。
                    duration = self._fmt_duration(now - record.get("high_started", now))
                    record["active"]["high"] = False
                    high_recovered.append((name, duration))
            alerts: list[str] = []
            if offline_alerted:
                if len(offline_alerted) == 1:
                    alerts.append(f"🔴 Komari 离线告警\n节点：{offline_alerted[0]}\n连续 {self.config.offline_grace_cycles} 个周期未收到心跳。")
                else:
                    alerts.append(f"🔴 Komari 离线告警\n{len(offline_alerted)} 个节点连续 {self.config.offline_grace_cycles} 个周期未收到心跳：\n" + "\n".join(f"· {n}" for n in offline_alerted))
            if self.config.notify_recovery:
                if len(offline_recovered) == 1:
                    name, duration = offline_recovered[0]
                    alerts.append(f"🟢 Komari 节点恢复\n节点：{name}\n离线时长：{duration}")
                elif len(offline_recovered) > 1:
                    alerts.append(f"🟢 Komari 节点恢复\n{len(offline_recovered)} 个节点已恢复：\n" + "\n".join(f"· {n}（离线时长 {d}）" for n, d in offline_recovered))
                if len(high_recovered) == 1:
                    name, duration = high_recovered[0]
                    alerts.append(f"✅ Komari 负载恢复\n节点：{name}\n持续时长：{duration}")
                elif len(high_recovered) > 1:
                    alerts.append(f"✅ Komari 负载恢复\n{len(high_recovered)} 个节点已恢复：\n" + "\n".join(f"· {n}（持续时长 {d}）" for n, d in high_recovered))
            alerts.extend(high_alerted)
            self._prune_missing(known_keys)
            for alert in alerts:
                self._append_alert(alert)
            self._save_state()
            if alerts:
                await self._send("\n\n".join(alerts))
            if self.config.status_report_interval > 0:
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
                if self._targets() and self.config.komari_url:
                    failed = await self._check_once()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll_delay(failed))
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def _start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _snapshot(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        static, error = await self._nodes()
        if error:
            return [], error
        live = await self._realtime()
        needs_history = not live or any(_metric(item, "memory") is None or _metric(item, "disk") is None for item in live)
        history = await self._history_realtime(static) if needs_history else []
        history_keys = {str(item.get("uuid") or item.get("id")) for item in history}
        if not live:
            live = history
        elif history:
            history_by_key = {str(item.get("uuid") or item.get("id")): item for item in history}
            enriched = []
            for item in live:
                key = str(item.get("uuid") or item.get("id") or "")
                fallback = history_by_key.get(key, {})
                merged = {**fallback, **item}
                for section in ("cpu", "ram", "memory", "disk", "storage", "network", "load"):
                    if isinstance(fallback.get(section), dict) and isinstance(item.get(section), dict):
                        merged[section] = {**fallback[section], **item[section]}
                enriched.append(merged)
            live = enriched
        merged = self._merge_nodes(static, live)
        live_keys = {str(item.get("uuid") or item.get("id")) for item in live}
        for node in merged:
            node["is_online"] = self._is_online(node, live_keys, history_keys)
        return merged, None

    @filter.command("komari_status", alias=["kstatus", "komari"])
    async def komari_status(self, event: AstrMessageEvent, *args):
        """查询所有 Komari 节点的状态与资源使用率；可加节点名（支持子串）只看指定节点。"""
        self._start_monitor()
        self._warn_filter_misconfig()
        async with self._check_lock:
            nodes, error = await self._snapshot()
        if error:
            yield event.plain_result(error)
            return
        yield await self._report_result(event, self._visible(self._select(nodes, args)))

    @filter.command("komari_realtime", alias=["krealtime", "实时状态"])
    async def komari_realtime(self, event: AstrMessageEvent, *args):
        """查询 Komari WebSocket 实时数据（不经历史兜底）；WebSocket 不可用时提示改用状态命令。"""
        self._start_monitor()
        async with self._check_lock:
            live = await self._realtime()
            if not live:
                yield event.plain_result("WebSocket 实时通道暂时不可用（可能被反代禁用），请改用 /komari_status 查看状态报告。")
                return
            static, error = await self._nodes()
        if error:
            yield event.plain_result(error)
            return
        merged = self._merge_nodes(static, live)
        for node in merged:
            node["is_online"] = True
        yield await self._report_result(event, self._visible(self._select(merged, args)))

    @filter.command("komari_history", alias=["khistory", "历史"])
    async def komari_history(self, event: AstrMessageEvent, *args):
        """查询历史资源趋势；可带小时数参数，如 /komari_history 6（1-24 小时）。"""
        self._start_monitor()
        try:
            hours = int(str(args[0])) if args and str(args[0]).isdigit() else 1
        except (ValueError, IndexError):
            hours = 1
        hours = max(1, min(hours, 24))
        async with self._check_lock:
            static, error = await self._nodes()
        if error:
            yield event.plain_result(error)
            return
        static = self._visible(static)
        if not static:
            yield event.plain_result("Komari 没有返回节点。")
            return
        tasks = [self._history_by_node(node, hours) for node in static]
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
        chain = await self._chain_from_html(self._history_html(series_by_node, hours), self._history_text(series_by_node, hours))
        yield event.chain_result(chain)

    @filter.command("komari_public", alias=["kpublic", "站点信息"])
    async def komari_public(self, event: AstrMessageEvent):
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

    @filter.command("komari_version", alias=["kversion", "版本"])
    async def komari_version(self, event: AstrMessageEvent):
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

    @filter.command("komari_bind")
    async def komari_bind(self, event: AstrMessageEvent):
        """绑定当前 OneBot 会话为告警接收目标。"""
        target = event.unified_msg_origin
        targets = self._targets()
        if target not in targets:
            targets.append(target)
            self.state["targets"] = targets
            self._save_state()
        self._start_monitor()
        yield event.plain_result("✅ 当前会话已绑定 Komari 告警推送；发送 /komari_unbind 可解除绑定。")

    @filter.command("komari_unbind")
    async def komari_unbind(self, event: AstrMessageEvent):
        """解除当前会话的告警推送。"""
        self.state["targets"] = [item for item in self._targets() if item != event.unified_msg_origin]
        self._save_state()
        yield event.plain_result("✅ 当前会话已解除绑定。")

    @filter.command("komari_mute")
    async def komari_mute(self, event: AstrMessageEvent, *args):
        """临时静默告警推送，默认 30 分钟；可指定分钟数，如 /komari_mute 60。"""
        try:
            minutes = int(str(args[0])) if args and str(args[0]).isdigit() else 30
        except (ValueError, IndexError):
            minutes = 30
        minutes = max(1, min(minutes, 1440))
        self.state["muted_until"] = time.time() + minutes * 60
        self._save_state()
        yield event.plain_result(f"🔇 已静默 {minutes} 分钟，期间不推送告警。发送 /komari_unmute 可提前恢复。")

    @filter.command("komari_unmute")
    async def komari_unmute(self, event: AstrMessageEvent):
        """解除静默，恢复告警推送。"""
        self.state["muted_until"] = 0
        self._save_state()
        yield event.plain_result("🔊 已完成静默解除，恢复正常推送。")

    @filter.command("komari_alerts", alias=["kalerts", "告警历史"])
    async def komari_alerts(self, event: AstrMessageEvent):
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

    @filter.command("komari_check")
    async def komari_check(self, event: AstrMessageEvent):
        """立即执行一次检查；告警会发往已绑定会话。"""
        await self._check_once(track_failure=False)
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


__all__ = ["KomariWatchPlugin", "KomariWatchConfig"]