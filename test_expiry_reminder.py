"""Expiry scheduling boundary and privacy regressions."""

import unittest
from datetime import datetime, timedelta, timezone

from expiry_reminder import expiry_notice, normalize_reminder_days, reminder_days


class ExpiryReminderTests(unittest.TestCase):
    now = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    days = (7, 3, 1)

    def notice(self, remaining):
        return expiry_notice({"uuid": "node-a", "expired_at": (self.now + remaining).isoformat()}, self.days, self.now)

    def test_days_are_validated_normalized_and_deduplicated(self):
        self.assertEqual(normalize_reminder_days(" 1,7，3,7 "), "7,3,1")
        self.assertEqual(reminder_days("30,1"), (30, 1))
        for invalid in ("", "0", "366", "-1", "2.5", "7,,3", "x", "3,"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_reminder_days(invalid)

    def test_threshold_edges_and_skip_old_stages(self):
        self.assertIsNone(self.notice(timedelta(days=7, seconds=1)))
        for remaining, expected in ((7, 7), (6, 7), (3, 3), (2, 3), (1, 1), (.5, 1)):
            with self.subTest(remaining=remaining):
                self.assertEqual(self.notice(timedelta(days=remaining)).threshold, expected)
        self.assertIsNone(self.notice(timedelta(0)))
        self.assertIsNone(self.notice(timedelta(seconds=-1)))

    def test_invalid_dates_and_missing_node_id_are_skipped(self):
        for value in (None, "", "invalid", 0, "0001-01-01T00:00:00Z"):
            with self.subTest(value=value):
                self.assertIsNone(expiry_notice({"uuid": "a", "expired_at": value}, self.days, self.now))
        self.assertIsNone(expiry_notice({"name": "no-id", "expired_at": "2026-10-05T06:00:00Z"}, self.days, self.now))

    def test_timezones_and_nanoseconds_share_canonical_identity(self):
        first = expiry_notice({"id": "a", "expired_at": "2026-10-05T06:00:00.123456789Z"}, self.days, self.now)
        second = expiry_notice({"id": "a", "expired_at": "2026-10-05T14:00:00.123456+08:00"}, self.days, self.now)
        self.assertEqual(first.expires_at, second.expires_at)
        self.assertEqual(first.threshold, 3)  # One day plus a fractional second, not yet the 1-day stage.
        self.assertIn("UTC", first.text)

    def test_message_exposes_no_billing_or_private_node_fields(self):
        node = {"uuid": "a", "name": "example", "expired_at": "2026-10-05T06:00:00Z",
                "price": 9823.77, "currency": "USD", "ipv4": "192.0.2.98", "remark": "secret-private"}
        text = expiry_notice(node, self.days, self.now).text
        self.assertIn("续费提醒", text)
        self.assertIn("example", text)
        for secret in ("9823.77", "USD", "192.0.2.98", "secret-private"):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
