"""Independent routing, persistence, retries and command integration."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from test_smoke import FakeContext, FakeEvent, StarTools, collect, m


class ExpiryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="komari-expiry-test-")
        self.old_dir = StarTools.data_dir
        StarTools.data_dir = Path(self.temp.name)
        self.instances = []
        self.now = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
        self.target = "test:FriendMessage:billing"
        self.other = "test:GroupMessage:ops"
        self.nodes = [{"uuid": "node-a", "name": "demo", "expired_at": (self.now + timedelta(days=6)).isoformat()}]

    async def asyncTearDown(self):
        for plugin in self.instances:
            await plugin.terminate()
        StarTools.data_dir = self.old_dir
        self.temp.cleanup()

    def make_plugin(self, config=None, **changes):
        if config is None:
            config = m.KomariGuardConfig(
                **{"komari_url": "https://status.example.com", "network_probe_enabled": False,
                   "expiry_reminder_enabled": True, "expiry_notification_targets": [self.target], **changes}
            )
        plugin = m.KomariGuardPlugin(FakeContext(), config)
        plugin.logger = Mock()
        plugin._nodes = AsyncMock(side_effect=lambda: (copy.deepcopy(self.nodes), None))
        plugin._snapshot = AsyncMock(side_effect=AssertionError("expiry must not depend on telemetry"))
        self.instances.append(plugin)
        return plugin

    async def test_reminders_and_normal_alerts_never_cross_destinations(self):
        plugin = self.make_plugin(notification_routes=[{"target_umo": self.other}])
        await plugin._check_expiry_once(self.now)
        self.assertEqual([target for target, _ in plugin.context.sent], [self.target])
        plugin.context.sent.clear()
        await plugin._dispatch_alerts([m.AlertMessage("normal alert")])
        self.assertEqual(plugin.context.sent, [(self.other, "normal alert")])
        plugin._snapshot.assert_not_awaited()

    async def test_disabled_or_no_targets_never_inherit_normal_routes(self):
        for config in ({"expiry_reminder_enabled": False}, {"expiry_notification_targets": []},
                       {"expiry_notification_targets": ["12345"]}):
            plugin = self.make_plugin(notification_routes=[{"target_umo": self.other}], **config)
            self.assertFalse(await plugin._check_expiry_once(self.now))
            plugin._nodes.assert_not_awaited()
            self.assertEqual(plugin.context.sent, [])

    async def test_stages_and_restart_are_deduplicated(self):
        plugin = self.make_plugin()
        await plugin._check_expiry_once(self.now)
        await plugin._check_expiry_once(self.now)
        self.assertEqual(len(plugin.context.sent), 1)
        restarted = self.make_plugin()
        await restarted._check_expiry_once(self.now)
        self.assertEqual(restarted.context.sent, [])
        await restarted._check_expiry_once(self.now + timedelta(days=3))
        await restarted._check_expiry_once(self.now + timedelta(days=5))
        await restarted._check_expiry_once(self.now + timedelta(days=5, hours=1))
        self.assertEqual(len(restarted.context.sent), 2)
        self.assertIn("提前 3 天", restarted.context.sent[0][1])
        self.assertIn("提前 1 天", restarted.context.sent[1][1])

    async def test_failure_retries_per_target_without_repeating_success(self):
        plugin = self.make_plugin(expiry_notification_targets=[self.target, self.other, self.target])
        plugin.context.fail_targets.add(self.target)
        self.assertTrue(await plugin._check_expiry_once(self.now))
        self.assertNotIn("node-a", plugin.state["expiry_reminders"][self.target])
        plugin.context.fail_targets.clear()
        self.assertFalse(await plugin._check_expiry_once(self.now))
        self.assertEqual([target for target, _ in plugin.context.sent], [self.other, self.target])

    async def test_renewal_clears_old_due_message_and_restarts_schedule(self):
        plugin = self.make_plugin()
        await plugin._check_expiry_once(self.now)
        self.nodes[0]["expired_at"] = (self.now + timedelta(days=30)).isoformat()
        await plugin._check_expiry_once(self.now)
        self.assertEqual(len(plugin.context.sent), 1)
        await plugin._check_expiry_once(self.now + timedelta(days=28))
        self.assertEqual(len(plugin.context.sent), 2)
        self.assertIn("提前 3 天", plugin.context.sent[-1][1])

    async def test_failed_delivery_is_not_replayed_after_renewal_or_expiry(self):
        plugin = self.make_plugin()
        plugin.context.fail_targets.add(self.target)
        await plugin._check_expiry_once(self.now)
        plugin.context.fail_targets.clear()
        for new_date in ((self.now + timedelta(days=30)).isoformat(), (self.now - timedelta(days=1)).isoformat(), None):
            self.nodes[0]["expired_at"] = new_date
            await plugin._check_expiry_once(self.now)
        self.assertEqual(plugin.context.sent, [])

    async def test_node_api_failure_preserves_history_and_does_not_send_stale_data(self):
        plugin = self.make_plugin()
        await plugin._check_expiry_once(self.now)
        previous = copy.deepcopy(plugin.state["expiry_reminders"])
        plugin._nodes = AsyncMock(return_value=([], "request failed"))
        self.assertTrue(await plugin._check_expiry_once(self.now + timedelta(days=4)))
        self.assertEqual(plugin.state["expiry_reminders"], previous)
        self.assertEqual(len(plugin.context.sent), 1)

    async def test_concurrent_checks_and_duplicate_nodes_do_not_duplicate_messages(self):
        self.nodes.append(copy.deepcopy(self.nodes[0]))
        plugin = self.make_plugin()
        await asyncio.gather(*(plugin._check_expiry_once(self.now) for _ in range(3)))
        self.assertEqual(len(plugin.context.sent), 1)

    async def test_global_filter_applies_but_online_status_does_not(self):
        self.nodes += [{"uuid": "node-b", "name": "excluded", "expired_at": self.nodes[0]["expired_at"]}]
        self.nodes[0]["is_online"] = False
        plugin = self.make_plugin(filter_mode="deny", filter_nodes="excluded")
        await plugin._check_expiry_once(self.now)
        text = plugin.context.sent[0][1]
        self.assertIn("demo", text)
        self.assertNotIn("excluded", text)

    async def test_monitor_runs_with_only_expiry_targets(self):
        self.nodes[0]["expired_at"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        plugin = self.make_plugin()
        regular_check = plugin._check_once = AsyncMock(side_effect=AssertionError("no ordinary route"))
        original = plugin._check_expiry_once

        async def one_cycle():
            result = await original()
            plugin._stop.set()
            return result

        plugin._check_expiry_once = one_cycle
        await asyncio.wait_for(plugin._monitor_loop(), timeout=2)
        regular_check.assert_not_awaited()
        self.assertEqual(len(plugin.context.sent), 1)

    async def test_many_nodes_are_batched_and_persisted(self):
        self.nodes = [{**self.nodes[0], "uuid": f"node-{i}"} for i in range(21)]
        plugin = self.make_plugin()
        await plugin._check_expiry_once(self.now)
        self.assertEqual(len(plugin.context.sent), 3)
        self.assertEqual(len(plugin.state["expiry_reminders"][self.target]), 21)
        await plugin._check_expiry_once(self.now)
        self.assertEqual(len(plugin.context.sent), 3)

    async def test_bind_unbind_updates_dashboard_without_changing_regular_routes(self):
        config = m.AstrBotConfig({"notification_routes": [{"target_umo": FakeEvent.unified_msg_origin}],
                                 "expiry_reminder_days": "14,7,1"})
        previous_routes = copy.deepcopy(config["notification_routes"])
        plugin = self.make_plugin(config)
        plugin._start_monitor = lambda: None
        result = await collect(plugin.cmd_expiry_bind(FakeEvent()))
        self.assertIn("已绑定", result[0][1])
        self.assertEqual(config["expiry_notification_targets"], [FakeEvent.unified_msg_origin])
        self.assertTrue(config["expiry_reminder_enabled"])
        self.assertEqual(plugin.config.expiry_reminder_days, "14,7,1")
        await collect(plugin.cmd_expiry_bind(FakeEvent()))
        self.assertEqual(config["expiry_notification_targets"], [FakeEvent.unified_msg_origin])
        await collect(plugin.cmd_expiry_unbind(FakeEvent()))
        self.assertEqual(config["expiry_notification_targets"], [])
        self.assertEqual(config["notification_routes"], previous_routes)

    async def test_config_save_failure_rolls_back_runtime_and_raw_config(self):
        config = m.AstrBotConfig({"expiry_notification_targets": [self.target], "expiry_reminder_enabled": False})
        config.save_config_async = AsyncMock(side_effect=OSError("disk failure"))
        previous = copy.deepcopy(dict(config))
        plugin = self.make_plugin(config)
        result = await collect(plugin.cmd_expiry_bind(FakeEvent()))
        self.assertIn("失败", result[0][1])
        self.assertEqual(dict(config), previous)
        self.assertFalse(plugin.config.expiry_reminder_enabled)
        self.assertEqual(plugin.config.expiry_notification_targets, [self.target])

    async def test_mute_supports_expiry_only_target_and_rechecks_date_on_resume(self):
        target = FakeEvent.unified_msg_origin
        plugin = self.make_plugin(expiry_notification_targets=[target])
        await collect(plugin.cmd_mute(FakeEvent(), "60"))
        self.assertGreater(plugin.state["muted"][target], time.time())
        await plugin._check_expiry_once(self.now)
        self.assertEqual(plugin.context.sent, [])
        self.nodes[0]["expired_at"] = (self.now + timedelta(days=30)).isoformat()
        await collect(plugin.cmd_unmute(FakeEvent()))
        await plugin._check_expiry_once(self.now)
        self.assertEqual(plugin.context.sent, [])

    async def test_unbinding_regular_route_preserves_expiry_target_and_its_mute(self):
        target = FakeEvent.unified_msg_origin
        config = m.AstrBotConfig({
            "notification_routes": [{"target_umo": target, "source": "command"}],
            "expiry_reminder_enabled": True, "expiry_notification_targets": [target],
        })
        plugin = self.make_plugin(config)
        until = time.time() + 3600
        plugin.state["muted"] = {target: until}
        await collect(plugin.cmd_unbind(FakeEvent()))
        self.assertEqual(plugin._targets(), [])
        self.assertEqual(plugin._expiry_targets(), [target])
        self.assertEqual(plugin.state["muted"][target], until)


if __name__ == "__main__":
    unittest.main()
