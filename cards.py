"""Self-contained report cards. No remote fonts, images or JavaScript."""

from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from typing import Any, Callable

if __package__:
    from .network_probe import parse_timestamp
else:
    from network_probe import parse_timestamp


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def first(*values: Any) -> Any:
    return next((value for value in values if value is not None and value != ""), None)


def section(node: dict, key: str) -> dict:
    value = node.get(key)
    return value if isinstance(value, dict) else {}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def size(value: Any, *, speed: bool = False) -> str:
    value = number(value)
    if value is None or value < 0:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}{'/s' if speed else ''}"


def duration(value: Any) -> str:
    value = number(value)
    if value is None or value < 0:
        return "—"
    days, seconds = divmod(int(value), 86400)
    hours, seconds = divmod(seconds, 3600)
    return f"{days}天 {hours}小时" if days else f"{hours}小时 {seconds // 60}分"


def observed_time(value: Any) -> str:
    try:
        numeric = number(value)
        if numeric is not None:
            stamp = datetime.fromtimestamp(numeric / 1000 if numeric > 1e12 else numeric, tz=timezone.utc)
        else:
            stamp = parse_timestamp(str(value))
            if stamp is None:
                return "未提供"
        return stamp.astimezone().strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return "未提供"


def capacity(node: dict, key: str) -> tuple[float | None, float | None]:
    aliases = {"memory": ("ram", "memory", "mem"), "disk": ("disk", "storage"), "swap": ("swap",)}[key]
    nested = next((section(node, alias) for alias in aliases if section(node, alias)), {})
    used = first(nested.get("used"), *(node.get(alias) for alias in aliases if not isinstance(node.get(alias), dict)),
                 node.get(f"{aliases[0]}_used"), node.get(f"{key}_used"), node.get("mem_used") if key == "memory" else None)
    total = first(nested.get("total"), *(node.get(f"{alias}_total") for alias in aliases))
    return number(used), number(total)


def traffic(node: dict) -> dict[str, Any]:
    net = section(node, "network")
    up = first(net.get("up"), node.get("net_out"))
    down = first(net.get("down"), node.get("net_in"))
    total_up = number(first(net.get("totalUp"), net.get("total_up"), node.get("net_total_up")))
    total_down = number(first(net.get("totalDown"), net.get("total_down"), node.get("net_total_down")))
    # Billing-period counters, when available, differ from lifetime totals.
    quota_up = number(first(node.get("traffic_up"), total_up))
    quota_down = number(first(node.get("traffic_down"), total_down))
    mode = str(node.get("traffic_limit_type") or "max")
    used = None
    if mode == "up":
        used = quota_up
    elif mode == "down":
        used = quota_down
    elif quota_up is not None and quota_down is not None:
        used = {"sum": quota_up + quota_down, "max": max(quota_up, quota_down), "min": min(quota_up, quota_down)}.get(mode)
    return {"up": up, "down": down, "total_up": total_up, "total_down": total_down,
            "used": used, "limit": number(node.get("traffic_limit")), "mode": mode}


def expiry(node: dict) -> str:
    value = node.get("expired_at")
    if not value:
        return "未设到期日"
    try:
        stamp = parse_timestamp(str(value))
        if stamp is None:
            return "未提供"
        if stamp.year <= 1:
            return "未设到期日"
        days = math.ceil((stamp - datetime.now(timezone.utc)).total_seconds() / 86400)
        return f"{stamp:%Y-%m-%d} · {'剩余 ' + str(days) + ' 天' if days >= 0 else '已到期'}"
    except (ValueError, OverflowError):
        return "未提供"


CSS = """
*{box-sizing:border-box}html{margin:0;padding:0;background:transparent}
body{margin:0;padding:0;width:var(--width);background:transparent;color:#182f39;font-family:Inter,"Noto Sans CJK SC","Microsoft YaHei","PingFang SC",sans-serif;font-size:14px;line-height:1.5;font-variant-numeric:tabular-nums}
#capture{width:var(--width);background:#f0f5f4;border:1px solid #dce6e3;overflow:hidden}
.masthead{display:flex;align-items:center;justify-content:space-between;padding:20px 26px;background:#143d3c;color:#fff;gap:18px}
.brand{display:flex;align-items:center;gap:12px}.brand svg{width:32px;height:36px;flex:none}.brand b{font-size:21px;letter-spacing:.2px}.eyebrow{font-size:10px;letter-spacing:2.2px;color:#92bbb3;font-weight:600}.timestamp{text-align:right;font-size:13px;color:#d8e8e4;white-space:nowrap}
.content{padding:22px}.heading{display:flex;align-items:end;justify-content:space-between;gap:15px;margin-bottom:18px}h1{font-size:23px;letter-spacing:-.5px;line-height:1.3;margin:0;color:#163a3b}.sub{color:#6c827f;font-size:12px;margin-top:5px}.stats{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.pill{font-size:12px;padding:4px 9px;border-radius:6px;background:#e1ebe8;color:#486862;font-weight:600}.pill.ok{background:#d9efe5;color:#157052}.pill.bad{background:#fbe7e4;color:#b94b40}
.grid{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}.card{flex:1 1 calc(50% - 7px);min-width:0;background:#fff;border:1px solid #dce7e3;border-radius:14px;padding:20px;overflow:hidden}.single .card{flex-basis:100%}.node-head{display:flex;align-items:start;justify-content:space-between;gap:10px;margin-bottom:6px}.node-head strong{font-size:20px;line-height:1.4;overflow-wrap:anywhere}.node-head small{font-size:12px;color:#627c76}.status{border-radius:6px;font-size:12px;padding:4px 9px;white-space:nowrap;font-weight:600}.online{color:#0e8059;background:#e0f3ea}.offline{color:#b6413f;background:#fce9e7}.unknown{color:#996418;background:#fff0d8}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;background:currentColor}.node-meta{display:flex;gap:6px;flex-wrap:wrap;color:#6f817c;font-size:11px;margin:4px 0 16px}.chip{background:#f0f5f3;border-radius:4px;padding:3px 7px;overflow-wrap:anywhere}
.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 18px;margin-bottom:18px}.single .metrics{grid-template-columns:repeat(4,minmax(0,1fr));gap:18px}.metric-label{font-size:12px;color:#68807a}.metric-value{font-size:26px;font-weight:700;line-height:1.35;letter-spacing:-.7px;white-space:nowrap}.metric-value small{font-size:13px;font-weight:500;letter-spacing:0;color:#748a83;margin-left:3px}.bar{height:5px;border-radius:8px;background:#edf2f0;margin:8px 0;overflow:hidden}.bar i{height:100%;display:block;border-radius:8px;background:#20a780}.metric-detail{font-size:11px;color:#788a84;overflow-wrap:anywhere}
.speeds{display:grid;grid-template-columns:1fr 1fr;background:#f2f7f5;border:1px solid #e5eee9;border-radius:9px;margin-bottom:16px}.speed{padding:11px 14px}.speed+ .speed{border-left:1px solid #dfe9e4}.speed span{display:block;font-size:11px;color:#6b837a}.speed b{font-size:19px;white-space:nowrap}.speed.up b{color:#16865d}.speed.down b{color:#287a99}
.details{display:grid;grid-template-columns:1fr;gap:7px}.single .details{grid-template-columns:1fr 1fr;column-gap:26px}.detail{display:flex;align-items:baseline;justify-content:space-between;gap:12px;font-size:12px;border-bottom:1px dashed #e5ece9;padding-bottom:6px;min-width:0}.detail span{color:#71847d;flex-shrink:0}.detail b{font-weight:500;text-align:right;overflow-wrap:anywhere;min-width:0}.detail.wide{grid-column:1/-1}
.network-title{display:flex;justify-content:space-between;align-items:baseline;margin:18px 0 9px;gap:12px}.network-title b{font-size:13px}.network-title span{font-size:10px;color:#7d8f88}.carriers{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.carrier{background:#f6f9f8;border:1px solid #e4ece8;border-radius:8px;padding:10px;min-width:0}.carrier-head{font-size:11px;color:#6c8078;display:flex;justify-content:space-between;gap:5px}.carrier-head i{font-style:normal;font-size:9px;color:#80968c}.latency{font-size:23px;font-weight:700;line-height:1.4;color:#16825d;letter-spacing:-.5px}.latency small{font-size:11px;margin-left:3px;font-weight:500}.latency.warn{color:#bc8221}.latency.bad{color:#ca4c4b}.latency.missing{font-size:13px;letter-spacing:0;line-height:2.5;color:#91a198}.loss{font-size:11px;color:#6e8579}.loss b{font-weight:600;color:#405e4f}.probe-detail{font-size:9px;color:#8b9a92;margin-top:4px;overflow-wrap:anywhere}.probe-note{font-size:10px;color:#8a9a91;margin-top:7px}.updated{border-top:1px solid #ebf0ed;margin-top:15px;padding-top:10px;display:flex;justify-content:space-between;gap:10px;font-size:10px;color:#8a9b93}.footer{display:flex;justify-content:space-between;margin-top:15px;color:#84978d;font-size:10px;letter-spacing:.3px}.empty{width:100%;padding:25px;text-align:center;color:#7e958a}
.chart{width:100%;height:100px;display:block;background:#f7faf8;border-radius:6px;margin-top:7px}.chartinfo{font-size:12px;color:#587167;margin-top:16px}.facts{font-size:12px;color:#587167}
.card.full .metrics{grid-template-columns:repeat(4,minmax(0,1fr))}.card.full .details{grid-template-columns:1fr 1fr;column-gap:26px}.narrow .metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 18px}.narrow .details{grid-template-columns:1fr}
"""


def page_html(title: str, subtitle: str, body: str, stats: str = "", *, width: int = 900, single: bool = False, scale: int = 2) -> str:
    # CSS zoom raises actual screenshot pixels even on older T2I deployments.
    # The capture boundary is an opaque rectangle against a transparent canvas.
    return f'''<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head>
<body style="--width:{int(width)}px;zoom:{int(scale)}"><main id="capture" class="{'single' if single else ''} {'narrow' if width < 800 else ''}">
<header class="masthead"><div class="brand"><svg viewBox="0 0 32 36" fill="none"><path d="M16 2 29 7v11c0 8-8 13-13 16C11 31 3 26 3 18V7Z" stroke="#8bd1b7" stroke-width="1.5"/><path d="m8 19 5-6 5 11 6-9" stroke="#c7f0dc" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><div><div class="eyebrow">INFRASTRUCTURE MONITOR</div><b>Komari Guard</b></div></div><div class="timestamp">{datetime.now():%Y-%m-%d}<br>{datetime.now():%H:%M:%S}</div></header>
<div class="content"><div class="heading"><div><h1>{esc(title)}</h1><div class="sub">{esc(subtitle)}</div></div><div class="stats">{stats}</div></div><div class="grid">{body}</div><div class="footer"><span>KOMARI GUARD · 节点状态快照</span><span>— 表示未提供数据</span></div></div></main></body></html>'''


def _metric_tile(label: str, value: float | None, detail: str) -> str:
    value = number(value)
    shown = f"{value:.1f}<small>%</small>" if value is not None else "—"
    color = "#d35b54" if value is not None and value >= 90 else ("#d29a39" if value is not None and value >= 75 else "#20a780")
    fill = min(100, max(0, value or 0))
    return f'<div><div class="metric-label">{esc(label)}</div><div class="metric-value">{shown}</div><div class="bar"><i style="width:{fill}%;background:{color}"></i></div><div class="metric-detail">{esc(detail)}</div></div>'


def _details(label: str, value: Any, wide: bool = False) -> str:
    return f'<div class="detail{" wide" if wide else ""}"><span>{esc(label)}</span><b>{esc(value)}</b></div>'


def probe_html(node: dict) -> str:
    data = node.get("_network_probe", {})
    hours = data.get("hours", 1)
    rows = []
    for carrier, label in (("telecom", "电信"), ("unicom", "联通"), ("mobile", "移动")):
        entry = data.get("carriers", {}).get(carrier, {})
        status = entry.get("status", data.get("status", "unavailable"))
        missing = {"not_configured": "未配置", "ambiguous_tasks": "需选任务", "no_data": "暂无样本", "disabled": "未启用", "unavailable": "读取失败"}
        latency = number(entry.get("latest_ms"))
        stale = bool(entry.get("stale"))
        if status in missing:
            value, tone = missing[status], "missing"
        elif stale:
            value, tone = "数据过期", "missing"
        elif entry.get("latest_lost") or status == "all_lost":
            value, tone = "超时", "bad"
        elif latency is not None:
            value = f'{latency:g}<small>ms</small>'
            tone = "bad" if latency >= 200 else ("warn" if latency >= 100 else "")
        else:
            value, tone = "暂无样本", "missing"
        loss = number(entry.get("loss_percent"))
        loss_text = f"{loss:.1f}%" if loss is not None else "—"
        avg = number(entry.get("avg_ms"))
        range_text = f"均值 {avg:.0f} ms · {entry.get('sample_count', 0)} 样本" if avg is not None else f"{entry.get('sample_count', 0)} 样本"
        minimum, maximum = number(entry.get("min_ms")), number(entry.get("max_ms"))
        if minimum is not None and maximum is not None:
            range_text += f" · 范围 {minimum:g}–{maximum:g} ms"
        task = " / ".join(entry.get("task_names", []))
        task_html = f'<div class="probe-detail">{esc(task)}</div>' if task else ""
        rows.append(f'<div class="carrier"><div class="carrier-head">{label}<i>{carrier.upper()[:2]}</i></div><div class="latency {tone}">{value}</div><div class="loss">丢包 <b>{loss_text}</b></div><div class="probe-detail">{esc(range_text)}</div>{task_html}</div>')
    note = "探测方向：本节点 → 运营商目标；丢包按实际样本统计"
    if data.get("unclassified"):
        note = "部分任务未识别运营商，可在插件配置中指定任务 ID"
    return f'<div class="network-title"><b>三网连接质量</b><span>最近 {int(hours)} 小时</span></div><div class="carriers">{"".join(rows)}</div><div class="probe-note">{note}</div>'


def report_html(nodes: list[dict], *, metric: Callable, width: int = 900, scale: int = 2) -> str:
    cards = []
    for index, node in enumerate(nodes):
        status = node.get("is_online")
        status_name, tone = ("在线", "online") if status is True else (("离线", "offline") if status is False else ("遥测未知", "unknown"))
        cpu = section(node, "cpu")
        used_ram, total_ram = capacity(node, "memory")
        used_disk, total_disk = capacity(node, "disk")
        used_swap, total_swap = capacity(node, "swap")
        load = section(node, "load")
        loads = [first(load.get(key), node.get(key), node.get("load") if key == "load1" and not isinstance(node.get("load"), dict) else None) for key in ("load1", "load5", "load15")]
        load_text = " / ".join(f"{number(value):.2f}" if number(value) is not None else "—" for value in loads)
        net = traffic(node)
        quota_pct = net["used"] / net["limit"] * 100 if net["used"] is not None and net["limit"] and net["limit"] > 0 else None
        quota_limit = "不限额" if net["limit"] == 0 else size(net["limit"])
        metrics = _metric_tile("CPU", metric(node, "cpu"), f"{first(cpu.get('cores'), node.get('cpu_cores'), '—')} 核 · {first(cpu.get('arch'), node.get('arch'), '架构未提供')}")
        metrics += _metric_tile("内存", metric(node, "memory"), f"{size(used_ram)} / {size(total_ram)}")
        metrics += _metric_tile("磁盘", metric(node, "disk"), f"{size(used_disk)} / {size(total_disk)}")
        metrics += _metric_tile("流量配额", quota_pct, f"{size(net['used'])} / {quota_limit}")
        os_name = node.get("os") or "系统未提供"
        region = node.get("region")
        chips = [os_name, region, node.get("virtualization"), node.get("group")]
        tags = node.get("tags")
        if isinstance(tags, str):
            chips.extend(tags.split(";"))
        elif isinstance(tags, list):
            chips.extend(str(tag) for tag in tags)
        chips_html = "".join(f'<span class="chip">{esc(chip)}</span>' for chip in dict.fromkeys(c for c in chips if c))
        connection = section(node, "connections")
        tcp = first(connection.get("tcp"), node.get("connections") if not isinstance(node.get("connections"), dict) else None)
        udp = first(connection.get("udp"), node.get("connections_udp"))
        details = _details("运行时长", duration(node.get("uptime")))
        details += _details("负载 1 / 5 / 15 分", load_text)
        details += _details("累计上行 / 下行", f"{size(net['total_up'])} / {size(net['total_down'])}")
        mode_name = {"max": "上下行取大", "min": "上下行取小", "sum": "双向合计", "up": "仅上行", "down": "仅下行"}.get(net["mode"], "未知")
        details += _details("配额统计方式", mode_name)
        details += _details("交换空间", "未启用" if total_swap == 0 else f"{size(used_swap)} / {size(total_swap)}")
        details += _details("进程 · TCP / UDP", f"{first(node.get('process'), '—')} · {first(tcp, '—')} / {first(udp, '—')}")
        if node.get("expired_at"):
            details += _details("到期", expiry(node), True)
        model = first(cpu.get("name"), node.get("cpu_name"))
        if model:
            details += _details("处理器", model, True)
        if node.get("kernel_version"):
            details += _details("内核", node["kernel_version"], True)
        gpu = section(node, "gpu")
        if gpu or node.get("gpu_name"):
            devices = gpu.get("detailed_info")
            devices = devices if isinstance(devices, list) else []
            gpu_name = node.get("gpu_name") or " / ".join(str(device.get("name", "GPU")) for device in devices if isinstance(device, dict))
            usage = number(gpu.get("average_usage"))
            details += _details("GPU", f"{gpu_name or 'GPU'}" + (f" · {usage:.1f}%" if usage is not None else ""), True)
        if node.get("public_remark"):
            details += _details("公开备注", node["public_remark"], True)
        timestamp = first(node.get("_telemetry_time"), node.get("updated_at"), node.get("last_seen"))
        updated = observed_time(timestamp)
        source = node.get("_telemetry_source") or "实时快照"
        if status is False:
            source = "离线 · 指标可能为最后记录"
        elif status is None:
            source = "遥测不可用"
        name = first(node.get("name"), node.get("hostname"), node.get("id"), "未知节点")
        layout = " full" if width >= 800 and len(nodes) % 2 and index == len(nodes) - 1 else ""
        cards.append(f'<section class="card{layout}"><div class="node-head"><strong>{esc(name)}</strong><span class="status {tone}"><i class="dot"></i>{status_name}</span></div><div class="node-meta">{chips_html}</div><div class="metrics">{metrics}</div><div class="speeds"><div class="speed up"><span>↑ 上行速率</span><b>{esc(size(net["up"], speed=True))}</b></div><div class="speed down"><span>↓ 下行速率</span><b>{esc(size(net["down"], speed=True))}</b></div></div><div class="details">{details}</div>{probe_html(node)}<div class="updated"><span>{esc(source)}</span><span>数据时间 {esc(updated)}</span></div></section>')
    online = sum(node.get("is_online") is True for node in nodes)
    offline = sum(node.get("is_online") is False for node in nodes)
    unknown = len(nodes) - online - offline
    stats = f'<span class="pill">{len(nodes)} 节点</span><span class="pill ok">{online} 在线</span>'
    if offline:
        stats += f'<span class="pill bad">{offline} 离线</span>'
    if unknown:
        stats += f'<span class="pill">{unknown} 未知</span>'
    return page_html("服务器运行状态", "资源 · 网络 · 三网连接质量", "".join(cards) or '<div class="empty">没有节点数据</div>', stats,
                     width=width, single=len(nodes) <= 1 or width < 800, scale=scale)


def report_text(nodes: list[dict], *, metric: Callable) -> str:
    """Retain useful resource and probe details when rendering is disabled/fails."""
    if not nodes:
        return "Komari 没有返回节点。"
    lines = ["📡 Komari 服务器状态"]
    for node in nodes:
        name = first(node.get("name"), node.get("hostname"), node.get("id"), "未知节点")
        status = node.get("is_online")
        online = "在线" if status is True else ("离线" if status is False else "未知")
        lines.append(f"\n{name} · {online}")
        for key, label in (("cpu", "CPU"), ("memory", "内存"), ("disk", "磁盘")):
            value = number(metric(node, key))
            shown = f"{value:.1f}%" if value is not None else "—"
            if key != "cpu":
                used, total = capacity(node, key)
                shown += f" ({size(used)} / {size(total)})"
            lines.append(f"{label} {shown}")
        net = traffic(node)
        lines.append(f"上行 {size(net['up'], speed=True)} / 下行 {size(net['down'], speed=True)}")
        lines.append(f"累计上行 {size(net['total_up'])} / 下行 {size(net['total_down'])}")
        limit = "不限额" if net["limit"] == 0 else size(net["limit"])
        lines.append(f"流量配额 {size(net['used'])} / {limit} · 运行 {duration(node.get('uptime'))}")
        probe = node.get("_network_probe", {})
        lines.append(f"三网（最近 {probe.get('hours', 1)} 小时）：")
        for carrier, label in (("telecom", "电信"), ("unicom", "联通"), ("mobile", "移动")):
            entry = probe.get("carriers", {}).get(carrier, {})
            state = entry.get("status", probe.get("status", "unavailable"))
            summary = {"not_configured": "未配置", "ambiguous_tasks": "需选任务", "no_data": "暂无样本",
                       "disabled": "未启用", "unavailable": "读取失败"}.get(state)
            if summary is None:
                latest, loss = number(entry.get("latest_ms")), number(entry.get("loss_percent"))
                latency = "超时" if entry.get("latest_lost") or state == "all_lost" else (f"{latest:g} ms" if latest is not None else "—")
                if entry.get("stale"):
                    latency = "数据过期"
                loss_text = f"{loss:.1f}%" if loss is not None else "—"
                summary = f"{latency} · 丢包 {loss_text} · {entry.get('sample_count', 0)} 样本"
            lines.append(f"  {label}：{summary}")
        source = node.get("_telemetry_source") or "状态快照"
        if status is False:
            source = "离线 · 指标可能为最后记录"
        lines.append(f"{source} · 数据时间 {observed_time(first(node.get('_telemetry_time'), node.get('updated_at')))}")
    return "\n".join(lines)
