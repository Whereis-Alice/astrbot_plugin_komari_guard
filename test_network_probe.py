"""Regression tests using the public Komari 1.4/1.5 ping API fields."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from network_probe import (
    Carrier,
    ProbePayloadError,
    ProbeStatus,
    build_ping_records_path,
    carrier_candidates,
    classify_carrier,
    parse_ping_records,
    parse_ping_tasks,
    parse_timestamp,
    summarize_ping_payloads,
    summarize_three_network,
    unclassified_ping_tasks,
)

TASKS_RESPONSE = {
    "status": "success",
    "message": "",
    "data": [
        {
            "id": 11,
            "weight": 1,
            "name": "中国电信 - Shanghai",
            "clients": ["node-a"],
            "default_on": False,
            "type": "icmp",
            "interval": 60,
        },
        {
            "id": 12,
            "weight": 2,
            "name": "China Unicom Beijing",
            "clients": ["node-a"],
            "default_on": False,
            "type": "tcp",
            "interval": 60,
        },
        {
            "id": 13,
            "weight": 3,
            "name": "CMCC Guangzhou",
            "clients": ["node-a"],
            "default_on": False,
            "type": "http",
            "interval": 60,
        },
        {
            "id": 99,
            "weight": 4,
            "name": "Internal gateway",
            "clients": ["node-a"],
            "default_on": False,
            "type": "icmp",
            "interval": 60,
        },
    ],
}


RECORDS_RESPONSE = {
    "status": "success",
    "message": "",
    "data": {
        "count": 6,
        "basic_info": [
            {"client": "node-a", "loss": 50, "min": 29, "max": 31},
        ],
        "records": [
            {"task_id": 11, "time": "2026-09-17T03:03:00Z", "value": 31, "client": "node-a"},
            {"task_id": 11, "time": "2026-09-17T03:02:00Z", "value": -1, "client": "node-a"},
            {"task_id": 11, "time": "2026-09-17T03:01:00Z", "value": 29, "client": "node-a"},
            {"task_id": 12, "time": "2026-09-17T03:03:00.123456789Z", "value": -1, "client": "node-a"},
            {"task_id": 12, "time": "2026-09-17T03:02:00Z", "value": -1, "client": "node-a"},
            {"task_id": 11, "time": "2026-09-17T03:04:00Z", "value": 7, "client": "node-b"},
        ],
        "tasks": [
            {
                "id": 11,
                "name": "中国电信 - Shanghai",
                "type": "icmp",
                "interval": 60,
                "default_on": False,
                "loss": 33.33333333333333,
                "min": 29,
                "max": 31,
                "avg": 30,
                "total": 3,
            }
        ],
    },
}


class NetworkProbeTests(unittest.TestCase):
    def test_paths_match_stable_public_endpoints(self) -> None:
        self.assertEqual(
            build_ping_records_path(node_uuid="node a", task_id=11, hours=6),
            "/api/records/ping?uuid=node+a&task_id=11&hours=6",
        )
        with self.assertRaises(ValueError):
            build_ping_records_path(hours=4)
        with self.assertRaises(ValueError):
            build_ping_records_path(node_uuid="node-a", hours=0)
        with self.assertRaises(ValueError):
            build_ping_records_path(task_id=0)
        with self.assertRaises(ValueError):
            build_ping_records_path(task_id="11")  # type: ignore[arg-type]

    def test_parse_official_task_fields_and_assignments(self) -> None:
        tasks = parse_ping_tasks(TASKS_RESPONSE)
        self.assertEqual(len(tasks), 4)
        self.assertEqual(tasks[0].task_id, 11)
        self.assertEqual(tasks[0].clients, ("node-a",))
        self.assertEqual(tasks[0].ping_type, "icmp")
        self.assertEqual(tasks[0].interval_seconds, 60)
        self.assertTrue(tasks[0].applies_to("node-a"))
        self.assertFalse(tasks[0].applies_to("node-b"))

        with self.assertRaisesRegex(ProbePayloadError, "invalid id"):
            parse_ping_tasks({
                "status": "success",
                "data": [{"id": 0, "name": "CMCC", "clients": ["node-a"]}],
            })

    def test_empty_configuration_is_distinct_from_invalid_payload(self) -> None:
        empty_tasks = {"status": "success", "data": []}
        empty_records = {
            "status": "success",
            "data": {"count": 0, "records": []},
        }
        local_empty_records = {"data": {"records": []}}
        self.assertEqual(parse_ping_tasks(empty_tasks), [])
        self.assertEqual(parse_ping_tasks(empty_records), [])
        self.assertEqual(parse_ping_records(empty_records), [])
        self.assertEqual(parse_ping_records(local_empty_records), [])

        summary = summarize_ping_payloads(empty_tasks, empty_records, node_uuid="node-a")
        self.assertTrue(
            all(item.status is ProbeStatus.NOT_CONFIGURED for item in summary.values())
        )

        invalid_payloads = (
            {},
            {"status": "success"},
            {"status": "success", "data": {}},
            {"status": "unexpected", "data": []},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ProbePayloadError):
                parse_ping_tasks(payload)

        with self.assertRaises(ProbePayloadError):
            parse_ping_records({})

    def test_default_on_does_not_mean_assigned_to_existing_node(self) -> None:
        tasks = parse_ping_tasks(
            {"status": "success", "data": [{
                "id": 1,
                "name": "CMCC",
                "clients": [],
                "default_on": True,
                "type": "icmp",
                "interval": 60,
            }]}
        )
        self.assertFalse(tasks[0].applies_to("existing-node"))

    def test_carrier_matching_is_explicit_and_ambiguity_is_preserved(self) -> None:
        self.assertIs(classify_carrier("电信 CN2"), Carrier.TELECOM)
        self.assertIs(classify_carrier("China Unicom"), Carrier.UNICOM)
        self.assertIs(classify_carrier("CMI Hong Kong"), Carrier.MOBILE)
        self.assertIs(classify_carrier("China-Mobile Guangzhou"), Carrier.MOBILE)
        self.assertIsNone(classify_carrier("mobile dashboard"))
        self.assertIsNone(classify_carrier("telecommunications lab"))
        self.assertIsNone(classify_carrier("backupchinamobilegateway"))
        self.assertEqual(
            carrier_candidates("电信 / 联通"),
            (Carrier.TELECOM, Carrier.UNICOM),
        )

    def test_parse_records_preserves_negative_loss_and_rfc3339_nanoseconds(self) -> None:
        samples = parse_ping_records(RECORDS_RESPONSE)
        self.assertEqual(len(samples), 6)
        self.assertTrue(samples[1].lost)
        self.assertEqual(samples[0].value_ms, 31)
        self.assertEqual(samples[3].observed_at.tzinfo, timezone.utc)
        self.assertEqual(samples[3].observed_at.microsecond, 123456)

    def test_rfc3339nano_fraction_is_python_310_compatible(self) -> None:
        samples = parse_ping_records({
            "status": "success",
            "data": {"records": [
                {
                    "task_id": 1,
                    "time": "2026-09-17T03:02:00.1Z",
                    "value": 20,
                    "client": "node-a",
                },
                {
                    "task_id": 1,
                    "time": "2026-09-17T11:02:00.1234+08:00",
                    "value": 21,
                    "client": "node-a",
                },
            ]},
        })
        self.assertEqual(samples[0].observed_at.microsecond, 100000)
        self.assertEqual(samples[1].observed_at.microsecond, 123400)
        self.assertEqual(samples[1].observed_at.hour, 3)

        direct = parse_timestamp("2026-09-17T03:02:00.123456789Z")
        self.assertIsNotNone(direct)
        self.assertEqual(direct.microsecond, 123456)
        self.assertIsNone(parse_timestamp(1_700_000_000))

    def test_summary_distinguishes_ok_all_lost_and_no_data(self) -> None:
        summary = summarize_ping_payloads(
            TASKS_RESPONSE,
            RECORDS_RESPONSE,
            node_uuid="node-a",
        )
        telecom = summary[Carrier.TELECOM]
        self.assertIs(telecom.status, ProbeStatus.OK)
        self.assertEqual(telecom.sample_count, 3)
        self.assertEqual(telecom.received_count, 2)
        self.assertAlmostEqual(telecom.loss_percent or 0, 100 / 3)
        self.assertEqual(telecom.latest_ms, 31)
        self.assertEqual(telecom.min_ms, 29)
        self.assertEqual(telecom.max_ms, 31)
        self.assertEqual(telecom.avg_ms, 30)

        unicom = summary[Carrier.UNICOM]
        self.assertIs(unicom.status, ProbeStatus.ALL_LOST)
        self.assertEqual(unicom.loss_percent, 100)
        self.assertIsNone(unicom.latest_ms)
        self.assertTrue(unicom.latest_lost)

        mobile = summary[Carrier.MOBILE]
        self.assertIs(mobile.status, ProbeStatus.NO_DATA)
        self.assertTrue(mobile.configured)
        self.assertFalse(mobile.has_samples)

    def test_unassigned_node_is_not_configured(self) -> None:
        summary = summarize_ping_payloads(
            TASKS_RESPONSE,
            RECORDS_RESPONSE,
            node_uuid="node-b",
        )
        self.assertTrue(all(item.status is ProbeStatus.NOT_CONFIGURED for item in summary.values()))

    def test_duplicate_tasks_require_override(self) -> None:
        tasks = parse_ping_tasks(TASKS_RESPONSE)
        duplicate = tasks[0].__class__(task_id=21, name="China Telecom backup", clients=("node-a",))
        samples = parse_ping_records(RECORDS_RESPONSE)
        ambiguous = summarize_three_network([*tasks, duplicate], samples, node_uuid="node-a")
        self.assertIs(ambiguous[Carrier.TELECOM].status, ProbeStatus.AMBIGUOUS_TASKS)
        self.assertEqual(ambiguous[Carrier.TELECOM].task_ids, (11, 21))

        selected = summarize_three_network(
            [*tasks, duplicate],
            samples,
            node_uuid="node-a",
            task_overrides={"telecom": 11},
        )
        self.assertIs(selected[Carrier.TELECOM].status, ProbeStatus.OK)
        self.assertEqual(selected[Carrier.TELECOM].task_ids, (11,))

    def test_explicit_ids_are_validated_and_take_exclusive_precedence(self) -> None:
        tasks = parse_ping_tasks(TASKS_RESPONSE)
        samples = parse_ping_records(RECORDS_RESPONSE)
        reassigned = summarize_three_network(
            tasks,
            samples,
            node_uuid="node-a",
            task_overrides={"mobile": 11},
        )
        self.assertIs(reassigned[Carrier.MOBILE].status, ProbeStatus.OK)
        self.assertEqual(reassigned[Carrier.MOBILE].task_ids, (11,))
        self.assertIs(reassigned[Carrier.TELECOM].status, ProbeStatus.NOT_CONFIGURED)

        automatic = summarize_three_network(
            tasks,
            samples,
            node_uuid="node-a",
            task_overrides={"telecom": 0, "unicom": 0, "mobile": 0},
        )
        self.assertIs(automatic[Carrier.TELECOM].status, ProbeStatus.OK)
        self.assertIs(automatic[Carrier.MOBILE].status, ProbeStatus.NO_DATA)

        invalid_overrides = (
            {"unknown": 11},
            {"telecom": -1},
            {"telecom": 11, "mobile": 11},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                summarize_three_network(tasks, samples, task_overrides=overrides)

    def test_time_window_is_inclusive_and_does_not_invent_missing_samples(self) -> None:
        tasks = parse_ping_tasks(TASKS_RESPONSE)
        samples = parse_ping_records(RECORDS_RESPONSE)
        start = datetime(2026, 9, 17, 3, 2, tzinfo=timezone.utc)
        end = datetime(2026, 9, 17, 3, 3, tzinfo=timezone.utc)
        summary = summarize_three_network(
            tasks,
            samples,
            node_uuid="node-a",
            window_start=start,
            window_end=end,
        )
        telecom = summary[Carrier.TELECOM]
        self.assertEqual(telecom.sample_count, 2)
        self.assertEqual(telecom.loss_count, 1)
        self.assertEqual(telecom.loss_percent, 50)

    def test_records_response_tasks_can_supply_configuration(self) -> None:
        tasks = parse_ping_tasks(RECORDS_RESPONSE)
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0].clients)
        summary = summarize_three_network(tasks, parse_ping_records(RECORDS_RESPONSE), node_uuid="node-a")
        self.assertIs(summary[Carrier.TELECOM].status, ProbeStatus.OK)

    def test_unclassified_tasks_are_exposed_without_guessing(self) -> None:
        tasks = parse_ping_tasks(TASKS_RESPONSE)
        self.assertEqual([task.task_id for task in unclassified_ping_tasks(tasks, node_uuid="node-a")], [99])

    def test_error_envelope_is_not_misreported_as_no_data(self) -> None:
        with self.assertRaisesRegex(ProbePayloadError, "UUID or task_id"):
            parse_ping_records({"status": "error", "message": "UUID or task_id is required"})

        with self.assertRaisesRegex(ProbePayloadError, "database unavailable"):
            parse_ping_tasks({"status": "error", "message": "database unavailable"})

    def test_malformed_records_do_not_bias_loss_metrics(self) -> None:
        invalid_records = (
            {"task_id": True, "value": 10, "time": "2026-09-17T03:00:00Z"},
            {"task_id": 0, "value": 10, "time": "2026-09-17T03:00:00Z"},
            {"task_id": 1, "value": 2.5, "time": "2026-09-17T03:00:00Z"},
            {"task_id": 1, "value": -1, "client": 7},
            {"task_id": 1, "value": -1, "time": "invalid"},
            {"task_id": 1, "value": -1},
            "not an object",
        )
        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(ProbePayloadError):
                parse_ping_records({
                    "status": "success",
                    "data": {"records": [record]},
                })


if __name__ == "__main__":
    unittest.main()
