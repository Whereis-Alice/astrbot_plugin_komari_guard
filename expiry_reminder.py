"""Pure expiry scheduling and text generation, independent of resource alerts."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

if __package__:
    from .network_probe import parse_timestamp
else:
    from network_probe import parse_timestamp


def normalize_reminder_days(value: str) -> str:
    """Validate configured whole days, preserving only descending unique values."""
    parts = value.replace("，", ",").split(",")
    if not parts or any(not re.fullmatch(r"[0-9]+", part.strip()) for part in parts):
        raise ValueError("续费提前天数需为逗号分隔的整数，例如 7,3,1")
    days = {int(part.strip()) for part in parts}
    if any(day < 1 or day > 365 for day in days):
        raise ValueError("续费提前天数必须在 1–365 之间")
    return ",".join(str(day) for day in sorted(days, reverse=True))


def reminder_days(value: str) -> tuple[int, ...]:
    return tuple(int(day) for day in normalize_reminder_days(value).split(","))


@dataclass(frozen=True)
class ExpiryNotice:
    node_key: str
    expires_at: str
    threshold: int
    text: str


def expiry_notice(node: dict[str, Any], days: tuple[int, ...], now: datetime) -> ExpiryNotice | None:
    """Choose only the most urgent currently crossed stage; never send old stages."""
    key = str(node.get("uuid") or node.get("id") or "")
    expires = parse_timestamp(node.get("expired_at"))
    if not key or expires is None or expires.year <= 1:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seconds = (expires - now).total_seconds()
    if seconds <= 0:
        return None
    crossed = [day for day in days if seconds <= day * 86400]
    if not crossed:
        return None
    threshold = min(crossed)
    minutes = max(1, math.ceil(seconds / 60))
    days_left, minutes = divmod(minutes, 1440)
    hours_left, minutes_left = divmod(minutes, 60)
    if days_left:
        remaining = f"约 {days_left} 天 {hours_left} 小时"
    elif hours_left:
        remaining = f"约 {hours_left} 小时 {minutes_left} 分钟"
    else:
        remaining = f"约 {minutes_left} 分钟"
    name = node.get("name") or node.get("hostname") or key
    local_expiry = expires.astimezone()
    text = (
        "⏰ Komari 服务器续费提醒\n"
        f"节点：{name}\n"
        f"到期：{local_expiry:%Y-%m-%d %H:%M:%S}（UTC{local_expiry:%z}）\n"
        f"剩余：{remaining} · 已进入提前 {threshold} 天提醒范围\n"
        "请核实是否需要续费；续费后请在 Komari 更新到期日期。"
    )
    return ExpiryNotice(key, expires.astimezone(timezone.utc).isoformat(), threshold, text)
