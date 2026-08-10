"""Komari Watch - an AstrBot plugin for status and proactive alerts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
from pydantic import BaseModel, Field

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_ID = "astrbot_plugin_komari_watch"


class KomariWatchConfig(BaseModel):
    komari_url: str = Field("", description="Komari 服务器地址")
    komari_token: str = Field("", description="API Token 或 Session Token")
    poll_interval: int = Field(60, ge=15, le=3600)
    offline_grace_cycles: int = Field(2, ge=1, le=10)
    cpu_threshold: float = Field(90, ge=1, le=100)
    memory_threshold: float = Field(90, ge=1, le=100)
    disk_threshold: float = Field(90, ge=1, le=100)
    high_load_cycles: int = Field(2, ge=1, le=10)
    alert_cooldown: int = Field(1800, ge=0, le=86400)
    notify_recovery: bool = True
    request_timeout: int = Field(10, ge=3, le=60)


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
        "cpu": ("cpu_usage", "cpu_percent", "cpuUsage", "usage"),
        "memory": ("memory_usage", "memory_percent", "ram_usage", "mem_usage"),
        "disk": ("disk_usage", "disk_percent"),
    }
    for key in aliases[name]:
        value = _num(node.get(key))
        if value is not None:
            return value * 100 if 0 <= value <= 1 else value
    nested = node.get("cpu") if name == "cpu" else node.get("ram") if name == "memory" else node.get("disk")
    if isinstance(nested, dict):
        value = _num(nested.get("usage", nested.get("percent")))
        if value is not None:
            return value * 100 if 0 <= value <= 1 else value
        used, total = _num(nested.get("used")), _num(nested.get("total"))
        if used is not None and total and total > 0:
            return used / total * 100
    return None


@register(PLUGIN_ID, "xiaowan", "Komari 监控推送插件", "1.0.0", "https://github.com/xiaowan/astrbot_plugin_komari_watch")
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
        self._monitor_task: Optional[asyncio.Task] = None
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

    async def _get_json(self, endpoint: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        if not self.config.komari_url:
            return None, "请先在插件配置中填写 Komari 服务器地址。"
        try:
            timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
            async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
                async with session.get(self.config.komari_url.rstrip("/") + endpoint) as response:
                    if response.status != 200:
                        return None, f"Komari API 返回 HTTP {response.status}"
                    payload = await response.json(content_type=None)
                    return payload if isinstance(payload, dict) else None, None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            return None, f"连接 Komari 失败：{exc}"

    async def _nodes(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        payload, error = await self._get_json("/api/nodes")
        if error:
            return [], error
        raw: Any = payload.get("data", []) if payload else []
        if isinstance(raw, dict):
            raw = raw.get("nodes", raw.get("servers", list(raw.values())))
        return ([item for item in raw if isinstance(item, dict)], None) if isinstance(raw, list) else ([], None)

    async def _realtime(self) -> list[dict[str, Any]]:
        if not self.config.komari_url:
            return []
        ws_url = re.sub(r"^http", "ws", self.config.komari_url.rstrip("/")) + "/api/clients"
        try:
            timeout = aiohttp.ClientTimeout(total=min(self.config.request_timeout, 10))
            async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
                async with session.ws_connect(ws_url, heartbeat=10) as ws:
                    await ws.send_str("get")
                    for _ in range(3):
                        message = await ws.receive(timeout=3)
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        payload = json.loads(message.data)
                        raw = payload.get("data", payload) if isinstance(payload, dict) else payload
                        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
                            details = raw["data"]
                            online = raw.get("online", details.keys())
                            return [{**details[key], "uuid": key} for key in online if key in details and isinstance(details[key], dict)]
                        if isinstance(raw, list):
                            return [item for item in raw if isinstance(item, dict)]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError):
            return []
        return []

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

    def _is_online(self, node: dict[str, Any], live_keys: set[str]) -> bool:
        key = str(node.get("uuid") or node.get("id") or "")
        if key and key in live_keys:
            return True
        updated = _parse_time(node.get("updated_at") or node.get("last_seen"))
        return bool(updated and (datetime.now(timezone.utc) - updated).total_seconds() < self.config.poll_interval * 3)

    def _format_report(self, nodes: list[dict[str, Any]]) -> str:
        lines = ["📡 Komari 服务器状态"]
        for node in nodes:
            name = node.get("name") or node.get("hostname") or node.get("id") or "未知节点"
            online = "在线" if node.get("is_online") else "离线"
            values = ((_metric(node, "cpu"), "CPU"), (_metric(node, "memory"), "内存"), (_metric(node, "disk"), "磁盘"))
            metrics = " / ".join(f"{label} {value:.1f}%" for value, label in values if value is not None)
            lines.append(f"\n{'🟢' if online == '在线' else '🔴'} {name} · {online}{(' · ' + metrics) if metrics else ''}")
        return "\n".join(lines) if len(lines) > 1 else "Komari 没有返回节点。"

    def _can_alert(self, record: dict[str, Any], kind: str, now: float) -> bool:
        """Prevent repeated alerts when a node flaps around a threshold."""
        last = _num(record.get("sent", {}).get(kind))
        return last is None or self.config.alert_cooldown == 0 or now - last >= self.config.alert_cooldown

    async def _send(self, text: str) -> None:
        for target in self._targets():
            try:
                await self.context.send_message(target, MessageChain().message(text))
            except Exception as exc:
                self.logger.warning("向 %s 推送失败: %s", target, exc)

    async def _check_once(self) -> None:
        static, error = await self._nodes()
        if error:
            self.logger.warning(error)
            return
        live = await self._realtime()
        live_keys = {str(item.get("uuid") or item.get("id")) for item in live}
        nodes = self._merge_nodes(static, live)
        now = datetime.now(timezone.utc).timestamp()
        alerts: list[str] = []
        for node in nodes:
            key = str(node.get("uuid") or node.get("id") or node.get("name") or "unknown")
            record = self.state.setdefault("nodes", {}).setdefault(key, {"offline": 0, "high": 0, "sent": {}, "active": {}})
            record.setdefault("sent", {})
            record.setdefault("active", {})
            node["is_online"] = self._is_online(node, live_keys)
            record["offline"] = record.get("offline", 0) + 1 if not node["is_online"] else 0
            cpu, mem, disk = _metric(node, "cpu"), _metric(node, "memory"), _metric(node, "disk")
            high = ((cpu is not None and cpu >= self.config.cpu_threshold) or (mem is not None and mem >= self.config.memory_threshold) or (disk is not None and disk >= self.config.disk_threshold))
            record["high"] = record.get("high", 0) + 1 if high else 0
            name = node.get("name") or key
            if (record["offline"] == self.config.offline_grace_cycles
                    and not record["active"].get("offline")
                    and self._can_alert(record, "offline", now)):
                alerts.append(f"🔴 Komari 离线告警\n节点：{name}\n连续 {record['offline']} 个周期未收到心跳。")
                record["sent"]["offline"] = now
                record["active"]["offline"] = True
            elif node["is_online"] and record["active"].get("offline"):
                if self.config.notify_recovery:
                    alerts.append(f"🟢 Komari 节点恢复\n节点：{name}")
                record["active"]["offline"] = False
            if (record["high"] == self.config.high_load_cycles
                    and not record["active"].get("high")
                    and self._can_alert(record, "high", now)):
                details = ", ".join(f"{label} {value:.1f}%" for value, label in ((cpu, "CPU"), (mem, "内存"), (disk, "磁盘")) if value is not None)
                alerts.append(f"⚠️ Komari 高负载告警\n节点：{name}\n{details}")
                record["sent"]["high"] = now
                record["active"]["high"] = True
            elif not high and record["active"].get("high"):
                if self.config.notify_recovery:
                    alerts.append(f"✅ Komari 负载恢复\n节点：{name}")
                record["active"]["high"] = False
        self._save_state()
        if alerts:
            await self._send("\n\n".join(alerts))

    async def _monitor_loop(self) -> None:
        try:
            while not self._stop.is_set():
                if self._targets() and self.config.komari_url:
                    await self._check_once()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.poll_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def _start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    @filter.command("komari_status", alias=["kstatus", "komari"])
    async def komari_status(self, event: AstrMessageEvent):
        """查询所有 Komari 节点的状态与资源使用率。"""
        self._start_monitor()
        nodes, error = await self._nodes()
        if error:
            yield event.plain_result(error)
            return
        live = await self._realtime()
        merged = self._merge_nodes(nodes, live)
        live_keys = {str(item.get("uuid") or item.get("id")) for item in live}
        for node in merged:
            node["is_online"] = self._is_online(node, live_keys)
        yield event.plain_result(self._format_report(merged))

    @filter.command("komari_realtime", alias=["krealtime", "实时状态"])
    async def komari_realtime(self, event: AstrMessageEvent):
        """查询 Komari WebSocket 实时数据（没有 WebSocket 时回退节点 API）。"""
        nodes, error = await self._nodes()
        if error:
            yield event.plain_result(error)
            return
        live = await self._realtime()
        merged = self._merge_nodes(nodes, live)
        live_keys = {str(item.get("uuid") or item.get("id")) for item in live}
        for node in merged:
            node["is_online"] = self._is_online(node, live_keys)
        yield event.plain_result(self._format_report(merged))

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

    @filter.command("komari_check")
    async def komari_check(self, event: AstrMessageEvent):
        """立即执行一次检查；告警会发往已绑定会话。"""
        await self._check_once()
        yield event.plain_result("✅ 已完成一次 Komari 检查。")

    async def terminate(self):
        self._stop.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            await self._monitor_task


__all__ = ["KomariWatchPlugin", "KomariWatchConfig"]
