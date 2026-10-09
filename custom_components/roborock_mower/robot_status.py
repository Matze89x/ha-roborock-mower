"""Helpers for the mower's GET_ROBOT_STATUS answer (no Home Assistant imports).

The answer is a JSON object (``rock.common.remote`` ``RobotStatus``) with far
more than the data points: Wi-Fi / 4G / RTK / LoRa state, the lawn area, the
last mow summary, the next schedule, hardware and firmware state machines.
Paths below were taken from a live RockNeo Q105 (firmware 02.72.44).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from typing import Any

# Keys whose values identify the owner's place or network; removed from
# everything the integration shows or exports (diagnostics, query answers).
PRIVATE_KEYS = frozenset(
    {
        "robot_gps",
        "gps",
        "gps_coordinate",
        "latitude",
        "longitude",
        "lat",
        "lon",
        "mac",
        "ip",
        "ssid",
        "bssid",
        # mac + a name derived from it
        "bluetooth",
    }
)
REDACTED = "**REDACTED**"


@dataclass(frozen=True, slots=True)
class RobotInfo:
    """What the status entities read: one consistent snapshot."""

    status: dict[str, Any]  # GET_ROBOT_STATUS answer ({} until known)
    preference: dict[str, Any]  # global mowing preference ({} until known)
    now: datetime  # aware, in Home Assistant's time zone
    updated: datetime | None = None  # when the status was last read
    local_connected: bool | None = None

WEEKDAYS = (
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
)


def redact_private(value: Any) -> Any:
    """Copy of ``value`` with location and network identifiers redacted."""
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).lower() in PRIVATE_KEYS else redact_private(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_private(item) for item in value]
    return value


def shorten_long_strings(value: Any, limit: int = 2000) -> Any:
    """Copy of ``value`` with very long strings (raw map data) as their length."""
    if isinstance(value, dict):
        return {key: shorten_long_strings(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [shorten_long_strings(item, limit) for item in value]
    if isinstance(value, str) and len(value) > limit:
        return f"<{len(value)} characters>"
    return value


def dig(data: Any, *path: str | int) -> Any:
    """``data[path[0]][path[1]]...`` or None when any step is missing."""
    for step in path:
        if isinstance(step, int):
            if not isinstance(data, list) or not -len(data) <= step < len(data):
                return None
            data = data[step]
        elif isinstance(data, dict):
            data = data.get(step)
        else:
            return None
    return data


def as_number(value: Any) -> float | int | None:
    """A numeric field (the mower sends 64-bit values as strings).

    Whole numbers stay ints, so a count reads "1", not "1.0".
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_datetime(value: Any, tz: Any) -> datetime | None:
    """A unix-seconds field (string or number) as an aware datetime."""
    seconds = as_number(value)
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, tz)


def as_utc_file_time(value: Any) -> datetime | None:
    """A map file time (``"2026-10-09-08-15-21"``, UTC) as an aware datetime."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d-%H-%M-%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def enum_key(value: Any) -> str | None:
    """Protobuf enum name -> translation key (``"FIXED_SOLUTION"`` -> ``"fixed_solution"``)."""
    if not isinstance(value, str) or not value:
        return None
    return value.strip().lower()


def next_plan_start(plan: Any, now: datetime) -> datetime | None:
    """Next start of a mowing schedule (``next_plan``) after ``now``.

    The plan carries a template ``start``/``end`` (an old date; only the time of
    day counts) and the weekdays it repeats on. Times are interpreted in the
    time zone of ``now``.
    """
    if not isinstance(plan, dict):
        return None
    template = as_datetime(plan.get("start"), now.tzinfo)
    if template is None:
        return None
    weekdays = {
        WEEKDAYS.index(day["type"])
        for day in plan.get("days") or []
        if isinstance(day, dict) and day.get("type") in WEEKDAYS
    }
    if not weekdays:  # one-off plan
        return template if template > now else None
    start_time: dt_time = template.time()
    today: date = now.date()
    for offset in range(8):
        day = today + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        candidate = datetime.combine(day, start_time, tzinfo=now.tzinfo)
        if candidate > now:
            return candidate
    return None


def next_plan_end(plan: Any, start: datetime | None) -> datetime | None:
    """End of the planned run beginning at ``start`` (same length as the template)."""
    if start is None or not isinstance(plan, dict):
        return None
    begin = as_number(plan.get("start"))
    end = as_number(plan.get("end"))
    if begin is None or end is None or end <= begin:
        return None
    return start + timedelta(seconds=end - begin)


def plan_days(plan: Any) -> list[str]:
    """Weekday names of a plan in lowercase (``["friday"]``)."""
    if not isinstance(plan, dict):
        return []
    return [
        day["type"].lower()
        for day in plan.get("days") or []
        if isinstance(day, dict) and isinstance(day.get("type"), str)
    ]


def as_flag(value: Any) -> bool | None:
    """A boolean field (``true`` / ``1``) or None when absent."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
        return value.lower() in ("true", "1")
    return None


def last_item(value: Any) -> Any:
    """Last entry of a list (``robot_status_event``), or None."""
    if isinstance(value, list) and value:
        return value[-1]
    return None
