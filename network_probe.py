"""Pure helpers for Komari ping-task and ping-record responses.

Komari 1.4 and 1.5 expose the same public compatibility endpoints:

* ``GET /api/task/ping`` lists task metadata.
* ``GET /api/records/ping`` returns task records and per-task summaries.

The API has no carrier field. Carrier classification in this module is therefore
deliberately conservative and uses only an unambiguous task-name match. Callers
can provide explicit task IDs when a panel has custom or duplicate task names.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlencode

PING_TASKS_PATH = "/api/task/ping"
PING_RECORDS_PATH = "/api/records/ping"


class ProbePayloadError(ValueError):
    """Raised when a Komari response is an API error or has an invalid shape."""


class Carrier(str, Enum):
    TELECOM = "telecom"
    UNICOM = "unicom"
    MOBILE = "mobile"

    @property
    def display_name(self) -> str:
        return {
            Carrier.TELECOM: "China Telecom",
            Carrier.UNICOM: "China Unicom",
            Carrier.MOBILE: "China Mobile",
        }[self]


class ProbeStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    AMBIGUOUS_TASKS = "ambiguous_tasks"
    NO_DATA = "no_data"
    ALL_LOST = "all_lost"
    OK = "ok"


@dataclass(frozen=True)
class PingTask:
    task_id: int
    name: str
    clients: tuple[str, ...] | None = None
    default_on: bool | None = None
    ping_type: str | None = None
    interval_seconds: int | None = None
    weight: int | None = None

    def applies_to(self, node_uuid: str | None) -> bool:
        """Check the explicit client assignment when the response includes it.

        ``default_on`` only controls enrollment of future nodes in Komari. It is
        not an all-current-nodes flag, so it is intentionally not consulted.
        A missing ``clients`` field means the records endpoint already filtered
        the task, while an explicit empty list means no current assignment.
        """

        if not node_uuid or self.clients is None:
            return True
        return node_uuid in self.clients


@dataclass(frozen=True)
class PingSample:
    task_id: int
    value_ms: int
    observed_at: datetime | None = None
    client: str | None = None

    @property
    def lost(self) -> bool:
        return self.value_ms < 0


@dataclass(frozen=True)
class CarrierProbeSummary:
    carrier: Carrier
    status: ProbeStatus
    task_ids: tuple[int, ...] = ()
    task_names: tuple[str, ...] = ()
    sample_count: int = 0
    received_count: int = 0
    loss_count: int = 0
    loss_percent: float | None = None
    latest_ms: int | None = None
    latest_at: datetime | None = None
    latest_lost: bool | None = None
    min_ms: int | None = None
    max_ms: int | None = None
    avg_ms: float | None = None

    @property
    def configured(self) -> bool:
        return self.status is not ProbeStatus.NOT_CONFIGURED

    @property
    def has_samples(self) -> bool:
        return self.sample_count > 0


_CHINESE_ALIASES: dict[Carrier, tuple[str, ...]] = {
    Carrier.TELECOM: ("中国电信", "电信"),
    Carrier.UNICOM: ("中国联通", "联通"),
    Carrier.MOBILE: ("中国移动", "移动"),
}

_ASCII_ALIASES: dict[Carrier, tuple[str, ...]] = {
    Carrier.TELECOM: ("telecom", "chinatelecom", "chinanet", "cn2"),
    Carrier.UNICOM: ("unicom", "chinaunicom", "china169"),
    Carrier.MOBILE: ("chinamobile", "cmcc", "cmi"),
}

_ASCII_PHRASES: dict[Carrier, tuple[tuple[str, ...], ...]] = {
    Carrier.TELECOM: (),
    Carrier.UNICOM: (),
    Carrier.MOBILE: (("china", "mobile"),),
}


def build_ping_records_path(
    *,
    node_uuid: str | None = None,
    task_id: int | None = None,
    hours: int = 4,
) -> str:
    """Build the stable Komari 1.4/1.5 public ping-records path."""

    node_uuid = str(node_uuid or "").strip()
    if not node_uuid and task_id is None:
        raise ValueError("node_uuid or task_id is required")
    if task_id is not None and (
        isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0
    ):
        raise ValueError("task_id must be a positive integer")
    if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
        raise ValueError("hours must be a positive integer")

    query: list[tuple[str, str]] = []
    if node_uuid:
        query.append(("uuid", node_uuid))
    if task_id is not None:
        query.append(("task_id", str(task_id)))
    query.append(("hours", str(hours)))
    return f"{PING_RECORDS_PATH}?{urlencode(query)}"


def carrier_candidates(task_name: str) -> tuple[Carrier, ...]:
    """Return every carrier explicitly named by a task.

    Short generic abbreviations such as ``ct`` and ``cu`` are intentionally not
    recognized. A name containing aliases for multiple carriers is ambiguous.
    """

    normalized = unicodedata.normalize("NFKC", str(task_name or "")).casefold()
    tokens = tuple(re.findall(r"[a-z0-9]+", normalized))
    token_set = set(tokens)
    matched: list[Carrier] = []
    for carrier in Carrier:
        chinese_match = any(alias in normalized for alias in _CHINESE_ALIASES[carrier])
        ascii_match = any(alias in token_set for alias in _ASCII_ALIASES[carrier])
        phrase_match = any(
            tokens[start : start + len(phrase)] == phrase
            for phrase in _ASCII_PHRASES[carrier]
            for start in range(len(tokens) - len(phrase) + 1)
        )
        if chinese_match or ascii_match or phrase_match:
            matched.append(carrier)
    return tuple(matched)


def classify_carrier(task_name: str) -> Carrier | None:
    """Classify only task names that identify exactly one carrier."""

    candidates = carrier_candidates(task_name)
    return candidates[0] if len(candidates) == 1 else None


def parse_ping_tasks(payload: Any) -> list[PingTask]:
    """Parse either ``/api/task/ping`` or ``/api/records/ping`` task data."""

    raw_tasks = _extract_ping_collection(payload, "tasks", allow_omitted_tasks=True)

    tasks: list[PingTask] = []
    seen_ids: set[int] = set()
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, Mapping):
            raise ProbePayloadError(f"Komari ping task {index} must be an object")
        task_id = _as_int(item.get("id"))
        if task_id is None or task_id <= 0:
            raise ProbePayloadError(f"Komari ping task {index} has an invalid id")
        if task_id in seen_ids:
            raise ProbePayloadError(f"Komari ping task id {task_id} is duplicated")
        seen_ids.add(task_id)
        clients: tuple[str, ...] | None
        if "clients" not in item:
            clients = None
        elif item.get("clients") is None:
            clients = ()
        elif isinstance(item.get("clients"), list):
            raw_clients = item["clients"]
            if any(not isinstance(value, str) or not value.strip() for value in raw_clients):
                raise ProbePayloadError(f"Komari ping task {task_id} has invalid clients")
            clients = tuple(value.strip() for value in raw_clients)
        else:
            raise ProbePayloadError(f"Komari ping task {task_id} has invalid clients")

        interval = _as_int(item.get("interval"))
        weight = _as_int(item.get("weight"))
        task_type = item.get("type")
        tasks.append(
            PingTask(
                task_id=task_id,
                name=str(item.get("name") or "").strip(),
                clients=clients,
                default_on=item.get("default_on") if isinstance(item.get("default_on"), bool) else None,
                ping_type=str(task_type).strip() if isinstance(task_type, str) and task_type.strip() else None,
                interval_seconds=interval if interval is not None and interval > 0 else None,
                weight=weight,
            )
        )
    return tasks


def parse_ping_records(payload: Any) -> list[PingSample]:
    """Parse Komari ping records, preserving negative loss sentinels."""

    raw_records = _extract_ping_collection(payload, "records")

    samples: list[PingSample] = []
    for index, item in enumerate(raw_records):
        if not isinstance(item, Mapping):
            raise ProbePayloadError(f"Komari ping record {index} must be an object")
        task_id = _as_int(item.get("task_id"))
        value = _as_int(item.get("value"))
        if task_id is None or task_id <= 0 or value is None:
            raise ProbePayloadError(f"Komari ping record {index} has invalid metrics")
        client_value = item.get("client")
        if client_value is not None and not isinstance(client_value, str):
            raise ProbePayloadError(f"Komari ping record {index} has an invalid client")
        client = client_value.strip() if client_value is not None else ""
        observed_at = parse_timestamp(item.get("time"))
        if observed_at is None:
            raise ProbePayloadError(f"Komari ping record {index} has an invalid time")
        samples.append(
            PingSample(
                task_id=task_id,
                value_ms=value,
                observed_at=observed_at,
                client=client or None,
            )
        )
    return samples


def unclassified_ping_tasks(
    tasks: Iterable[PingTask],
    *,
    node_uuid: str | None = None,
) -> list[PingTask]:
    """Return applicable tasks whose names are absent or carrier-ambiguous."""

    return [
        task
        for task in tasks
        if task.applies_to(node_uuid) and classify_carrier(task.name) is None
    ]


def summarize_three_network(
    tasks: Sequence[PingTask],
    samples: Sequence[PingSample],
    *,
    node_uuid: str | None = None,
    task_overrides: Mapping[Carrier | str, int] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[Carrier, CarrierProbeSummary]:
    """Summarize one explicit task per carrier for a node and inclusive window.

    Duplicate matching tasks are reported as ``ambiguous_tasks`` unless the
    caller supplies ``task_overrides``. Loss is based only on returned samples;
    missing expected intervals are not invented as packet loss.
    """

    start = _to_utc(window_start)
    end = _to_utc(window_end)
    if start is not None and end is not None and end < start:
        raise ValueError("window_end must not be before window_start")

    overrides = _normalize_overrides(task_overrides)
    applicable = [task for task in tasks if task.applies_to(node_uuid)]
    reserved_task_ids = set(overrides.values())
    by_carrier: dict[Carrier, list[PingTask]] = {carrier: [] for carrier in Carrier}
    for task in applicable:
        if task.task_id in reserved_task_ids:
            continue
        carrier = classify_carrier(task.name)
        if carrier is not None:
            by_carrier[carrier].append(task)
    for carrier_tasks in by_carrier.values():
        carrier_tasks.sort(key=lambda task: (task.weight is None, task.weight or 0, task.task_id))

    output: dict[Carrier, CarrierProbeSummary] = {}
    for carrier in Carrier:
        matches = by_carrier[carrier]
        override = overrides.get(carrier)
        if override is not None:
            matches = [task for task in applicable if task.task_id == override]

        if not matches:
            output[carrier] = CarrierProbeSummary(carrier=carrier, status=ProbeStatus.NOT_CONFIGURED)
            continue
        if len(matches) > 1:
            output[carrier] = CarrierProbeSummary(
                carrier=carrier,
                status=ProbeStatus.AMBIGUOUS_TASKS,
                task_ids=tuple(task.task_id for task in matches),
                task_names=tuple(task.name for task in matches),
            )
            continue

        task = matches[0]
        selected = [
            sample
            for sample in samples
            if sample.task_id == task.task_id
            and (not node_uuid or not sample.client or sample.client == node_uuid)
            and _inside_window(sample.observed_at, start, end)
        ]
        if not selected:
            output[carrier] = CarrierProbeSummary(
                carrier=carrier,
                status=ProbeStatus.NO_DATA,
                task_ids=(task.task_id,),
                task_names=(task.name,),
            )
            continue

        latest = _latest_sample(selected)
        received = [sample.value_ms for sample in selected if not sample.lost]
        loss_count = len(selected) - len(received)
        status = ProbeStatus.OK if received else ProbeStatus.ALL_LOST
        output[carrier] = CarrierProbeSummary(
            carrier=carrier,
            status=status,
            task_ids=(task.task_id,),
            task_names=(task.name,),
            sample_count=len(selected),
            received_count=len(received),
            loss_count=loss_count,
            loss_percent=loss_count / len(selected) * 100,
            latest_ms=None if latest.lost else latest.value_ms,
            latest_at=latest.observed_at,
            latest_lost=latest.lost,
            min_ms=min(received) if received else None,
            max_ms=max(received) if received else None,
            avg_ms=sum(received) / len(received) if received else None,
        )
    return output


def summarize_ping_payloads(
    tasks_payload: Any,
    records_payload: Any,
    **kwargs: Any,
) -> dict[Carrier, CarrierProbeSummary]:
    """Parse official response payloads and summarize them in one call."""

    return summarize_three_network(
        parse_ping_tasks(tasks_payload),
        parse_ping_records(records_payload),
        **kwargs,
    )


def _unwrap_api_data(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        return payload
    if "status" in payload:
        status = payload.get("status")
        if not isinstance(status, str):
            raise ProbePayloadError("Komari API response has an invalid status")
        normalized_status = status.casefold()
        if normalized_status == "error":
            raise ProbePayloadError(str(payload.get("message") or "Komari API error"))
        if normalized_status != "success":
            raise ProbePayloadError(f"Komari API response has unknown status: {status}")
        if "data" not in payload:
            raise ProbePayloadError("Komari success response is missing data")
        return payload["data"]
    if "data" in payload:
        # Also accept a pre-unwrapped/local compatibility envelope. Actual API
        # failures still carry a non-success status and are rejected above.
        return payload["data"]
    return payload


def _extract_ping_collection(
    payload: Any,
    field: str,
    *,
    allow_omitted_tasks: bool = False,
) -> list[Any]:
    data = _unwrap_api_data(payload)
    if isinstance(data, list):
        return data
    if not isinstance(data, Mapping):
        raise ProbePayloadError(f"Komari ping {field} must be a list")
    if field in data:
        collection = data[field]
    elif allow_omitted_tasks and "records" in data:
        if not isinstance(data["records"], list):
            raise ProbePayloadError("Komari ping records must be a list")
        # The records response uses `omitempty`; zero matching tasks omit this key.
        collection = []
    else:
        raise ProbePayloadError(f"Komari ping response is missing {field}")
    if not isinstance(collection, list):
        raise ProbePayloadError(f"Komari ping {field} must be a list")
    return collection


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    return None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO/RFC3339 string, including Go's 1-9 digit fractions.

    Numeric epoch conversion intentionally stays with callers because Komari
    fields use different second/millisecond conventions.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    nano_match = re.fullmatch(
        r"(?P<prefix>.+T\d{2}:\d{2}:\d{2})[.,](?P<fraction>\d+)"
        r"(?P<zone>[Zz]|[+-]\d{2}:\d{2})?",
        text,
    )
    if nano_match:
        # Python 3.10 accepts only 3 or 6 fractional digits, while Go's
        # RFC3339Nano encoder emits any precision from 1 through 9 digits.
        fraction = nano_match.group("fraction")[:6].ljust(6, "0")
        text = f"{nano_match.group('prefix')}.{fraction}{nano_match.group('zone') or ''}"
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _to_utc(parsed)


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _inside_window(
    observed_at: datetime | None,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    if start is None and end is None:
        return True
    if observed_at is None:
        return False
    return (start is None or observed_at >= start) and (end is None or observed_at <= end)


def _latest_sample(samples: Sequence[PingSample]) -> PingSample:
    dated = [sample for sample in samples if sample.observed_at is not None]
    if not dated:
        return samples[0]
    return max(dated, key=lambda sample: sample.observed_at or datetime.min.replace(tzinfo=timezone.utc))


def _normalize_overrides(
    values: Mapping[Carrier | str, int] | None,
) -> dict[Carrier, int]:
    output: dict[Carrier, int] = {}
    for raw_carrier, raw_task_id in (values or {}).items():
        try:
            carrier = (
                raw_carrier
                if isinstance(raw_carrier, Carrier)
                else Carrier(str(raw_carrier).strip().casefold())
            )
        except ValueError as exc:
            raise ValueError(f"unsupported carrier override: {raw_carrier}") from exc
        task_id = _as_int(raw_task_id)
        if task_id == 0:
            # The plugin's public configuration uses zero to request detection
            # from the task name instead of selecting an explicit task ID.
            continue
        if task_id is None or task_id < 0:
            raise ValueError(
                f"task override for {carrier.value} must be zero or a positive integer"
            )
        if task_id in output.values():
            raise ValueError(f"ping task {task_id} cannot override multiple carriers")
        output[carrier] = task_id
    return output


__all__ = [
    "PING_RECORDS_PATH",
    "PING_TASKS_PATH",
    "Carrier",
    "CarrierProbeSummary",
    "PingSample",
    "PingTask",
    "ProbePayloadError",
    "ProbeStatus",
    "build_ping_records_path",
    "carrier_candidates",
    "classify_carrier",
    "parse_ping_records",
    "parse_ping_tasks",
    "parse_timestamp",
    "summarize_ping_payloads",
    "summarize_three_network",
    "unclassified_ping_tasks",
]
