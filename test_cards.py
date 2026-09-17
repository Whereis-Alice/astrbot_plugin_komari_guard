"""Field and presentation semantics that must survive renderer changes."""

import unittest

import cards


class CardDataTests(unittest.TestCase):
    def test_flat_and_nested_capacity(self):
        self.assertEqual(cards.capacity({"ram": 0, "ram_total": 1024}, "memory"), (0, 1024))
        self.assertEqual(cards.capacity({"ram": {"used": 512}, "mem_total": 1024}, "memory"), (512, 1024))
        self.assertEqual(cards.capacity({}, "memory"), (None, None))
        self.assertEqual(cards.capacity({"swap": {"used": 0, "total": 0}}, "swap"), (0, 0))

    def test_traffic_uses_billing_counters_without_confusing_lifetime(self):
        node = {"network": {"totalUp": 1000, "totalDown": 2000}, "traffic_up": 0, "traffic_down": 30}
        for mode, used in (("max", 30), ("min", 0), ("sum", 30), ("up", 0), ("down", 30)):
            with self.subTest(mode=mode):
                result = cards.traffic({**node, "traffic_limit_type": mode})
                self.assertEqual(result["used"], used)
                self.assertEqual(result["total_up"], 1000)
        self.assertIsNone(cards.traffic({})["used"])

    def test_missing_and_nonfinite_values_are_not_zero(self):
        for value in (None, "", "nan", float("inf"), True, -1):
            self.assertEqual(cards.size(value), "—")
        self.assertEqual(cards.size(0), "0.0 B")
        self.assertNotEqual(cards.observed_time(1700000000000), "未提供")
        self.assertEqual(cards.observed_time("bad"), "未提供")
        self.assertNotEqual(cards.observed_time("2026-09-17T03:02:00.123456789Z"), "未提供")
        self.assertIn("2027-03-31", cards.expiry({"expired_at": "2027-03-31T00:00:00.1Z"}))

    def test_escape_public_fields_and_omit_private_fields(self):
        node = {"name": '<img src="x" onerror="x">', "public_remark": "a<b",
                "ipv4": "192.0.2.7", "remark": "SECRET-PRIVATE", "token": "SECRET-TOKEN",
                "gpu": {"detailed_info": None}}
        rendered = cards.report_html([node], metric=lambda *_: None)
        self.assertIn("&lt;img", rendered)
        self.assertIn("a&lt;b", rendered)
        for private in ("192.0.2.7", "SECRET-PRIVATE", "SECRET-TOKEN", '<img src="x"'):
            self.assertNotIn(private, rendered)

    def test_probe_states_are_distinct_in_html_and_text(self):
        states = {"not_configured": "未配置", "ambiguous_tasks": "需选任务", "no_data": "暂无样本",
                  "all_lost": "超时", "disabled": "未启用", "unavailable": "读取失败"}
        for state, expected in states.items():
            node = {"_network_probe": {"status": state}}
            self.assertIn(expected, cards.probe_html(node))
            self.assertIn(expected, cards.report_text([node], metric=lambda *_: None))
        node = {"_network_probe": {"carriers": {"telecom": {"status": "ok", "stale": True, "latest_ms": 10}}}}
        self.assertIn("数据过期", cards.probe_html(node))


if __name__ == "__main__":
    unittest.main()
