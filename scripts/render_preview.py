"""Render synthetic examples and verify screenshot bounds with Chromium.

Developer-only dependency: pip install playwright && playwright install chromium
Run from the repository: python scripts/render_preview.py [--browser EXE]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cards  # noqa: E402
from card_render import crop_to_alpha_bounds  # noqa: E402


def demo_node() -> dict:
    gib = 1024**3
    stamp = datetime.now(timezone.utc).isoformat()
    probes = {}
    for carrier, name, latency, loss in (
        ("telecom", "上海电信", 31, 0),
        ("unicom", "上海联通", 42, 0.3),
        ("mobile", "广州移动", 56, 1.2),
    ):
        probes[carrier] = {"status": "ok", "latest_ms": latency, "loss_percent": loss,
                           "avg_ms": latency + 2, "min_ms": latency - 4, "max_ms": latency + 9,
                           "sample_count": 600, "task_names": [name], "latest_at": stamp}
    return {
        "uuid": "demo-node-01", "name": "香港 · 生产节点 01", "is_online": True,
        "os": "Debian 12", "region": "HK", "virtualization": "KVM", "group": "示例数据",
        "cpu": {"usage": 18.6, "name": "AMD EPYC 7B13 64-Core Processor", "cores": 4, "arch": "x86_64"},
        "ram": {"used": 3.8 * gib, "total": 8 * gib},
        "disk": {"used": 38.7 * gib, "total": 100 * gib},
        "swap": {"used": 128 * 1024**2, "total": 2 * gib},
        "network": {"up": 1.6 * 1024**2, "down": 6.4 * 1024**2,
                    "totalUp": 128.6 * gib, "totalDown": 286.2 * gib},
        "traffic_up": 28.6 * gib, "traffic_down": 156.2 * gib,
        "traffic_limit": 500 * gib, "traffic_limit_type": "sum",
        "load": {"load1": .42, "load5": .38, "load15": .35},
        "uptime": 18 * 86400 + 7 * 3600, "process": 148,
        "connections": {"tcp": 86, "udp": 12}, "price": 29.9, "currency": "¥", "billing_cycle": 30,
        "expired_at": "2027-03-31T00:00:00Z", "kernel_version": "6.1.0-23-amd64",
        "updated_at": stamp, "_telemetry_source": "实时快照 · 演示数据",
        "_network_probe": {"status": "ok", "hours": 1, "carriers": probes},
    }


def demo_metric(node: dict, key: str) -> float | None:
    if key == "cpu":
        return cards.number(cards.section(node, "cpu").get("usage"))
    used, total = cards.capacity(node, key)
    return used / total * 100 if used is not None and total else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Optional Chromium executable path")
    args = parser.parse_args()
    output = ROOT / "docs"
    output.mkdir(exist_ok=True)
    single = demo_node()
    second, third = copy.deepcopy(single), copy.deepcopy(single)
    second.update(name="东京 · 边缘节点 02", region="JP")
    second["cpu"]["usage"] = 82.4
    second["_network_probe"]["carriers"]["mobile"].update(latest_lost=True, loss_percent=8.5)
    third.update(name="新加坡 · 数据节点 03", region="SG", is_online=False)
    third["_network_probe"]["carriers"]["telecom"]["stale"] = True
    missing = {"name": "未上报遥测的节点 · 演示", "is_online": None,
               "_network_probe": {"status": "unavailable"}}
    long_name = copy.deepcopy(single)
    long_name["name"] = "超长节点名称 · " + "production-edge-server-" * 5
    long_name["public_remark"] = "公开备注：" + "测试长文本自动换行、完整展示。" * 6
    long_name["_telemetry_source"] = "历史记录兜底"
    cases = [("card-single", [single], 900, 2), ("card-multiple", [single, second, third], 900, 2),
             ("card-missing", [missing], 900, 1), ("card-narrow", [long_name], 500, 2),
             ("card-empty", [], 900, 1)]
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser)
        for name, nodes, width, scale in cases:
            # 720px simulates older AstrBot T2I services that ignore viewport_height.
            page = browser.new_page(viewport={"width": width * scale, "height": 720}, device_scale_factor=1)
            page.set_content(cards.report_html(nodes, metric=demo_metric, width=width, scale=scale))
            page.evaluate("document.fonts.ready")
            bounds = page.locator("#capture").bounding_box()
            overflow = page.evaluate("""() => [...document.querySelectorAll('.card,.metric-value,.speed b,.detail,.carrier')]
                .filter(e => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).display !== 'inline')
                .map(e => ({tag:e.className, text:e.innerText.slice(0,80)}))""")
            if overflow:
                raise AssertionError(f"{name}: overflowing content: {overflow}")
            raw = page.screenshot(type="png", full_page=True, omit_background=True)
            png = crop_to_alpha_bounds(raw)
            # Generated image artifacts are the output of this render script.
            (output / f"{name}.png").write_bytes(png)
            with Image.open(BytesIO(png)) as image:
                assert image.width == width * scale, (name, image.size, bounds)
                assert abs(image.height - bounds["height"]) <= 2, (name, image.size, bounds)
                assert image.getchannel("A").getextrema() == (255, 255), name
                results.append({"name": name, "pixels": list(image.size), "bytes": len(png), "overflow": overflow})
            page.close()
        browser.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
