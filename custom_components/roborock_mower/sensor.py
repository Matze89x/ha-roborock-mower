"""Sensor platform for Roborock Mower integration.

Two kinds of sensors: the live data points the mower pushes (state, battery,
...) and the values of its full status (``GET_ROBOT_STATUS``) and mowing
preferences, which the coordinator asks the mower for. Like the official
Roborock integration, the details sit in the diagnostic category; the less
useful ones are disabled until a user enables them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfArea,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity, remove_entity
from .mower_api import (
    CHARGE_STATE_LABELS,
    DPS_BATTERY,
    DPS_BLADE_LIFESPAN,
    DPS_CHARGE_STATE,
    DPS_CHARGE_TYPE,
    DPS_ERROR_CODE,
    DPS_MOW_PROGRESS,
    DPS_MOW_STATE,
    DPS_MOW_TYPE,
    DPS_PEND_TYPE,
    CHARGE_TYPE_LABELS,
    MOW_TYPE_LABELS,
    PEND_TYPE_LABELS,
    ROBOT_DETAIL_STATE_LABELS,
    MowerStatus,
)
from .robot_status import (
    RobotInfo,
    as_datetime,
    as_number,
    as_utc_file_time,
    dig,
    enum_key,
    last_item,
    next_plan_end,
    next_plan_start,
    plan_days,
)

_LOGGER = logging.getLogger(__name__)

# (sensor key, code) pairs already reported as unknown -- warn once each.
_REPORTED_UNKNOWN: set[tuple[str, int]] = set()


@dataclass(frozen=True, kw_only=True)
class RoborockMowerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MowerStatus], Any]
    dps: int  # the data point it shows; created only if the model has it


def _enum(
    key: str,
    get: Callable[[MowerStatus], int | None],
    labels: dict[int, str],
    default: int | None = None,
) -> Callable[[MowerStatus], str | None]:
    """Value function for a translated enum sensor.

    Data points that were never reported are absent from the snapshot; for
    those with a natural "nothing" value (no error, not paused, ...) ``default``
    is shown instead of "unknown" once the mower reported anything at all.
    Unknown codes read as unknown and are logged once, so they can be mapped.
    """

    def _value(status: MowerStatus) -> str | None:
        code = get(status)
        if code is None and status.raw_dps:
            code = default
        if code is None:
            return None
        if code not in labels:
            if (key, code) not in _REPORTED_UNKNOWN:
                _REPORTED_UNKNOWN.add((key, code))
                _LOGGER.warning(
                    "Unknown %s value %s from the mower; please report it", key, code
                )
            return None
        return labels[code]

    return _value


def _options(labels: dict[int, str]) -> list[str]:
    return sorted(set(labels.values()))


SENSOR_DESCRIPTIONS: list[RoborockMowerSensorDescription] = [
    RoborockMowerSensorDescription(
        key="battery",
        dps=DPS_BATTERY,
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.battery,
    ),
    RoborockMowerSensorDescription(
        key="mow_progress",
        dps=DPS_MOW_PROGRESS,
        translation_key="mow_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-check",
        value_fn=lambda s: s.mow_progress,
    ),
    RoborockMowerSensorDescription(
        key="mow_state",
        dps=DPS_MOW_STATE,
        translation_key="mow_state",
        device_class=SensorDeviceClass.ENUM,
        options=_options(ROBOT_DETAIL_STATE_LABELS),
        icon="mdi:robot-mower",
        value_fn=_enum(
            "mow_state", lambda s: s.mow_state, ROBOT_DETAIL_STATE_LABELS
        ),
    ),
    RoborockMowerSensorDescription(
        key="mow_type",
        dps=DPS_MOW_TYPE,
        translation_key="mow_type",
        device_class=SensorDeviceClass.ENUM,
        options=_options(MOW_TYPE_LABELS),
        icon="mdi:vector-square",
        value_fn=_enum("mow_type", lambda s: s.mow_type, MOW_TYPE_LABELS, 0),
    ),
    RoborockMowerSensorDescription(
        key="charge_state",
        dps=DPS_CHARGE_STATE,
        translation_key="charge_state",
        device_class=SensorDeviceClass.ENUM,
        options=_options(CHARGE_STATE_LABELS),
        icon="mdi:battery-charging",
        value_fn=_enum("charge_state", lambda s: s.charge_state, CHARGE_STATE_LABELS),
    ),
    RoborockMowerSensorDescription(
        key="charge_type",
        dps=DPS_CHARGE_TYPE,
        translation_key="charge_type",
        device_class=SensorDeviceClass.ENUM,
        options=_options(CHARGE_TYPE_LABELS),
        icon="mdi:home-import-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_enum("charge_type", lambda s: s.charge_type, CHARGE_TYPE_LABELS, 0),
    ),
    RoborockMowerSensorDescription(
        key="pend_type",
        dps=DPS_PEND_TYPE,
        translation_key="pend_type",
        device_class=SensorDeviceClass.ENUM,
        options=_options(PEND_TYPE_LABELS),
        icon="mdi:pause-octagon",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_enum("pend_type", lambda s: s.pend_type, PEND_TYPE_LABELS, 0),
    ),
    RoborockMowerSensorDescription(
        key="error_code",
        dps=DPS_ERROR_CODE,
        translation_key="error_code",
        icon="mdi:alert-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Only reported once an error occurred; absent means "no error" (0).
        value_fn=lambda s: s.error_code
        if s.error_code is not None or not s.raw_dps
        else 0,
    ),
    RoborockMowerSensorDescription(
        key="blade_lifespan",
        dps=DPS_BLADE_LIFESPAN,
        translation_key="blade_lifespan",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:saw-blade",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.blade_lifespan,
    ),
]


@dataclass(frozen=True, kw_only=True)
class RobotStatusSensorDescription(SensorEntityDescription):
    """A value from the mower's full status or its mowing preferences."""

    value_fn: Callable[[RobotInfo], Any]
    attrs_fn: Callable[[RobotInfo], dict[str, Any] | None] | None = None


def _number(*path: str | int) -> Callable[[RobotInfo], float | int | None]:
    return lambda info: as_number(dig(info.status, *path))


def _state(*path: str | int) -> Callable[[RobotInfo], str | None]:
    """A protobuf enum name as a translation key.

    These sensors are deliberately not ENUM sensors: the full set of values
    is not known, and an unknown one shows up as is instead of breaking the
    sensor. Known values are translated (strings.json).
    """
    return lambda info: enum_key(dig(info.status, *path))


def _first_state(*paths: tuple[str | int, ...]) -> Callable[[RobotInfo], str | None]:
    def _value(info: RobotInfo) -> str | None:
        for path in paths:
            if (value := enum_key(dig(info.status, *path))) is not None:
                return value
        return None

    return _value


def _time(*path: str | int) -> Callable[[RobotInfo], Any]:
    return lambda info: as_datetime(dig(info.status, *path), info.now.tzinfo)


def _preference_state(key: str) -> Callable[[RobotInfo], str | None]:
    return lambda info: enum_key(info.preference.get(key))


def _next_mow(info: RobotInfo) -> Any:
    return next_plan_start(info.status.get("next_plan"), info.now)


def _next_mow_attrs(info: RobotInfo) -> dict[str, Any] | None:
    plan = info.status.get("next_plan")
    if not isinstance(plan, dict):
        return None
    end = next_plan_end(plan, _next_mow(info))
    return {
        "end": end.isoformat() if end else None,
        "days": plan_days(plan),
        "mode": enum_key(plan.get("mode")),
    }


def _last_mow_attrs(info: RobotInfo) -> dict[str, Any] | None:
    last = info.status.get("last_mow_abstract")
    if not isinstance(last, dict):
        return None
    return {"mode": enum_key(last.get("fsm_state"))}


def _hardware_errors(info: RobotInfo) -> list[str] | None:
    """Errors the mower's controller reports (``hardware.mcu_error.errors``).

    The answer leaves an empty list out, so a known status without the field
    means "no error".
    """
    if not info.status:
        return None
    errors = dig(info.status, "hardware", "mcu_error", "errors") or []
    if not isinstance(errors, list):
        errors = [errors]
    return [key for error in errors if (key := enum_key(error))]


def _hardware_error(info: RobotInfo) -> str | None:
    errors = _hardware_errors(info)
    if errors is None:
        return None
    return errors[0] if errors else "none"


def _hardware_error_attrs(info: RobotInfo) -> dict[str, Any] | None:
    errors = _hardware_errors(info)
    return None if errors is None else {"errors": errors}


def _extra_number(kind: str, *path: str | int) -> Callable[[RobotInfo], Any]:
    """A number of a further answer (``USER_MODE_CONFIG`` ...)."""
    return lambda info: as_number(dig(info.extra.get(kind), *path))


def _extra_state(kind: str, *path: str | int) -> Callable[[RobotInfo], str | None]:
    return lambda info: enum_key(dig(info.extra.get(kind), *path))


def _faults(info: RobotInfo) -> list[dict[str, Any]] | None:
    """The fault history (``GET_FAULT_RECORDS``), latest fault first.

    An empty history is left out of the answer, so a known answer without
    ``cards`` means "no faults".
    """
    answer = info.extra.get("FAULT_RECORDS")
    if not isinstance(answer, dict):
        return None
    cards = dig(answer, "fault_records", "cards") or []
    if not isinstance(cards, list):
        return None
    faults = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        items = [item for item in card.get("items") or [] if isinstance(item, dict)]
        dates = sorted(str(item.get("fault_time")) for item in items if item.get("fault_time"))
        latest = next(
            (item for item in items if str(item.get("fault_time")) == (dates[-1] if dates else None)),
            {},
        )
        faults.append(
            {
                "code": card.get("e_code"),
                "count": card.get("occur_count", len(items)),
                "last": dates[-1] if dates else None,
                "task": enum_key(latest.get("task_type")),
            }
        )
    faults.sort(key=lambda fault: fault["last"] or "", reverse=True)
    return faults


def _last_fault(info: RobotInfo) -> Any:
    faults = _faults(info)
    return faults[0]["code"] if faults else None


def _last_fault_attrs(info: RobotInfo) -> dict[str, Any] | None:
    faults = _faults(info)
    if not faults:
        return None
    return {"date": faults[0]["last"], "task": faults[0]["task"], "faults": faults}


def _last_fault_date(info: RobotInfo) -> date | None:
    faults = _faults(info)
    if not faults or not faults[0]["last"]:
        return None
    try:
        return date.fromisoformat(faults[0]["last"])
    except ValueError:
        return None


def _fault_count(info: RobotInfo) -> int | None:
    faults = _faults(info)
    if faults is None:
        return None
    return sum(int(fault["count"] or 0) for fault in faults)


def _zone_plans(info: RobotInfo) -> list[dict[str, Any]] | None:
    answer = info.extra.get("ZONES_PLAN_INFO")
    if not isinstance(answer, dict):
        return None
    zones = dig(answer, "zones_plan_info", "zones_plan_info") or []
    if not isinstance(zones, list):
        return None
    return [zone for zone in zones if isinstance(zone, dict)]


def _plan_count(info: RobotInfo) -> int | None:
    zones = _zone_plans(info)
    if zones is None:
        return None
    return len({plan for zone in zones for plan in zone.get("plan_id") or []})


def _plan_attrs(info: RobotInfo) -> dict[str, Any] | None:
    zones = _zone_plans(info)
    if zones is None:
        return None
    return {
        "zones": {
            str(zone.get("name") or zone.get("id")): len(zone.get("plan_id") or [])
            for zone in zones
        }
    }


def _while_working(*path: str | int) -> Callable[[RobotInfo], float | int | None]:
    """A live value that the mower leaves out when it is 0 (docked, idle)."""

    def _value(info: RobotInfo) -> float | int | None:
        if not info.status:
            return None
        return abs(as_number(dig(info.status, *path)) or 0)

    return _value


def _remaining_mow_time(info: RobotInfo) -> float | int | None:
    """Estimate for the running task: its expected time times what is left."""
    if not info.status:
        return None
    progress = dig(info.status, "navigation", "nav_task_progress")
    if not isinstance(progress, dict):
        return 0
    expected = as_number(progress.get("expected_time"))
    percent = as_number(progress.get("percentage", progress.get("percent")))
    if expected is None or percent is None:
        return None
    return max(0, round(expected * (1 - min(percent, 100) / 100)))


def _map_name(info: RobotInfo) -> str | None:
    name = dig(info.status, "map_abstracts", 0, "name") or dig(
        info.status, "map_names", 0
    )
    if not isinstance(name, str) or not name:
        return None
    return name.removesuffix(".bin")


_DIAGNOSTIC = EntityCategory.DIAGNOSTIC

ROBOT_STATUS_SENSORS: list[RobotStatusSensorDescription] = [
    # -- the lawn and the mowing runs -----------------------------------------
    RobotStatusSensorDescription(
        key="lawn_area",
        translation_key="lawn_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        suggested_display_precision=1,
        icon="mdi:texture-box",
        value_fn=_number("mow_progress", "mow_all_area"),
    ),
    RobotStatusSensorDescription(
        key="estimated_mow_time",
        translation_key="estimated_mow_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        icon="mdi:timer-sand",
        value_fn=_number("mow_progress", "expected_time"),
    ),
    RobotStatusSensorDescription(
        key="remaining_mow_time",
        translation_key="remaining_mow_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        icon="mdi:timer-sand-complete",
        value_fn=_remaining_mow_time,
    ),
    RobotStatusSensorDescription(
        key="next_mow",
        translation_key="next_mow",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
        value_fn=_next_mow,
        attrs_fn=_next_mow_attrs,
    ),
    RobotStatusSensorDescription(
        key="plan_count",
        translation_key="plan_count",
        icon="mdi:calendar-multiple",
        value_fn=_plan_count,
        attrs_fn=_plan_attrs,
    ),
    RobotStatusSensorDescription(
        key="last_mow_start",
        translation_key="last_mow_start",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
        value_fn=_time("last_mow_abstract", "start", "time"),
        attrs_fn=_last_mow_attrs,
    ),
    RobotStatusSensorDescription(
        key="last_mow_end",
        translation_key="last_mow_end",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-end",
        value_fn=_time("last_mow_abstract", "end", "time"),
        attrs_fn=_last_mow_attrs,
    ),
    RobotStatusSensorDescription(
        key="last_mow_duration",
        translation_key="last_mow_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        suggested_display_precision=0,
        icon="mdi:timer-outline",
        value_fn=_number("last_mow_abstract", "seconds"),
    ),
    RobotStatusSensorDescription(
        key="last_mow_area",
        translation_key="last_mow_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        suggested_display_precision=1,
        icon="mdi:texture-box",
        value_fn=_number("last_mow_abstract", "area"),
    ),
    RobotStatusSensorDescription(
        key="last_mow_coverage",
        translation_key="last_mow_coverage",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        icon="mdi:progress-check",
        value_fn=_number("last_mow_abstract", "percentage"),
    ),
    # -- diagnostic, enabled ----------------------------------------------------
    RobotStatusSensorDescription(
        key="last_mow_end_reason",
        translation_key="last_mow_end_reason",
        entity_category=_DIAGNOSTIC,
        icon="mdi:flag-checkered",
        value_fn=_state("last_mow_abstract", "end", "type"),
    ),
    RobotStatusSensorDescription(
        key="last_event",
        translation_key="last_event",
        entity_category=_DIAGNOSTIC,
        icon="mdi:bell-outline",
        value_fn=lambda info: enum_key(last_item(info.status.get("robot_status_event"))),
    ),
    RobotStatusSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=_DIAGNOSTIC,
        value_fn=_number("network", "rssi"),
    ),
    RobotStatusSensorDescription(
        key="network_route",
        translation_key="network_route",
        entity_category=_DIAGNOSTIC,
        icon="mdi:router-network",
        value_fn=_state("wireless_devices", "route"),
    ),
    RobotStatusSensorDescription(
        key="mobile_network",
        translation_key="mobile_network",
        entity_category=_DIAGNOSTIC,
        icon="mdi:signal-4g",
        value_fn=_state("wireless_devices", "mobile_4g", "state"),
    ),
    RobotStatusSensorDescription(
        key="rtk_position",
        translation_key="rtk_position",
        entity_category=_DIAGNOSTIC,
        icon="mdi:crosshairs-gps",
        value_fn=_first_state(
            ("rtk", "position_type"), ("wireless_devices", "rtk_position")
        ),
    ),
    RobotStatusSensorDescription(
        key="hardware_error",
        translation_key="hardware_error",
        entity_category=_DIAGNOSTIC,
        icon="mdi:alert-circle-outline",
        value_fn=_hardware_error,
        attrs_fn=_hardware_error_attrs,
    ),
    RobotStatusSensorDescription(
        key="blade_speed",
        translation_key="blade_speed",
        native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=_DIAGNOSTIC,
        icon="mdi:saw-blade",
        value_fn=_while_working("hardware", "cutter_info", "main_cutter_speed"),
    ),
    RobotStatusSensorDescription(
        key="speed",
        translation_key="speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=_DIAGNOSTIC,
        value_fn=_while_working("hardware", "wheel", "linear_velocity"),
    ),
    RobotStatusSensorDescription(
        key="last_fault",
        translation_key="last_fault",
        entity_category=_DIAGNOSTIC,
        icon="mdi:alert-octagon-outline",
        value_fn=_last_fault,
        attrs_fn=_last_fault_attrs,
    ),
    RobotStatusSensorDescription(
        key="last_fault_date",
        translation_key="last_fault_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=_DIAGNOSTIC,
        icon="mdi:calendar-alert",
        value_fn=_last_fault_date,
    ),
    RobotStatusSensorDescription(
        key="rain_delay",
        translation_key="rain_delay",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=_DIAGNOSTIC,
        icon="mdi:weather-rainy",
        value_fn=_extra_number("USER_MODE_CONFIG", "user_mode_config", "rainfall_config", "delay_time"),
    ),
    # -- diagnostic, disabled by default ----------------------------------------
    RobotStatusSensorDescription(
        key="wifi_quality",
        translation_key="wifi_quality",
        icon="mdi:wifi",
        value_fn=_state("wireless_devices", "wifi", "level"),
    ),
    RobotStatusSensorDescription(
        key="wifi_state",
        translation_key="wifi_state",
        icon="mdi:wifi-check",
        value_fn=_state("wireless_devices", "wifi", "state"),
    ),
    RobotStatusSensorDescription(
        key="wifi_band",
        translation_key="wifi_band",
        icon="mdi:wifi-settings",
        value_fn=lambda info: dig(info.status, "network", "wifi_band"),
    ),
    RobotStatusSensorDescription(
        key="rtk_mode",
        translation_key="rtk_mode",
        icon="mdi:satellite-variant",
        value_fn=_state("rtk", "nrtk", "rtk_mode"),
    ),
    RobotStatusSensorDescription(
        key="rtk_state",
        translation_key="rtk_state",
        icon="mdi:satellite-uplink",
        value_fn=_state("fsm_rtk_state"),
    ),
    RobotStatusSensorDescription(
        key="network_rtk",
        translation_key="network_rtk",
        icon="mdi:cloud-outline",
        value_fn=_state("rtk", "nrtk", "dock_nrtk_status"),
    ),
    RobotStatusSensorDescription(
        key="lora_status",
        translation_key="lora_status",
        icon="mdi:radio-tower",
        value_fn=_state("lora_status"),
    ),
    RobotStatusSensorDescription(
        key="anti_theft",
        translation_key="anti_theft",
        icon="mdi:shield-lock-outline",
        value_fn=_state("fsm_anti_theft_state"),
    ),
    RobotStatusSensorDescription(
        key="energy_state",
        translation_key="energy_state",
        icon="mdi:sleep",
        value_fn=_state("fsm_energy_state"),
    ),
    RobotStatusSensorDescription(
        key="runtime_state",
        translation_key="runtime_state",
        icon="mdi:heart-pulse",
        value_fn=_state("runtime_state"),
    ),
    RobotStatusSensorDescription(
        key="ota_state",
        translation_key="ota_state",
        icon="mdi:update",
        value_fn=_state("fsm_ota_state"),
    ),
    RobotStatusSensorDescription(
        key="working_state",
        translation_key="working_state",
        icon="mdi:robot-mower-outline",
        value_fn=_first_state(("robot_task", "working_state"), ("mow_working_state",)),
    ),
    RobotStatusSensorDescription(
        key="docking_state",
        translation_key="docking_state",
        icon="mdi:home-import-outline",
        value_fn=_state("fsm_dock_state"),
    ),
    RobotStatusSensorDescription(
        key="mapping_state",
        translation_key="mapping_state",
        icon="mdi:map-outline",
        value_fn=_state("fsm_map_state"),
    ),
    RobotStatusSensorDescription(
        key="navigation_state",
        translation_key="navigation_state",
        icon="mdi:navigation-variant-outline",
        value_fn=_state("navigation", "type"),
    ),
    RobotStatusSensorDescription(
        key="slam_state",
        translation_key="slam_state",
        icon="mdi:map-marker-radius-outline",
        value_fn=_state("slam", "type"),
    ),
    RobotStatusSensorDescription(
        key="mcu_state",
        translation_key="mcu_state",
        icon="mdi:chip",
        value_fn=_first_state(("hardware", "mcu_state"), ("fsm_mcu_state",)),
    ),
    RobotStatusSensorDescription(
        key="map_name",
        translation_key="map_name",
        icon="mdi:map",
        value_fn=_map_name,
    ),
    RobotStatusSensorDescription(
        key="map_updated",
        translation_key="map_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:map-clock-outline",
        value_fn=lambda info: as_utc_file_time(
            dig(info.status, "map_abstracts", 0, "file_change_time")
        ),
    ),
    RobotStatusSensorDescription(
        key="status_updated",
        translation_key="status_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:refresh",
        value_fn=lambda info: info.updated,
    ),
    # -- mowing preferences (set in the app) --------------------------------------
    RobotStatusSensorDescription(
        key="boundary_perception",
        translation_key="boundary_perception",
        icon="mdi:border-outside",
        value_fn=_preference_state("boundary_perception"),
    ),
    # -- settings, fault history and product details ------------------------------
    RobotStatusSensorDescription(
        key="rain_state",
        translation_key="rain_state",
        icon="mdi:weather-pouring",
        value_fn=_extra_state("USER_MODE_CONFIG", "user_mode_config", "rainfall_config", "type"),
    ),
    RobotStatusSensorDescription(
        key="anti_theft_range",
        translation_key="anti_theft_range",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        icon="mdi:map-marker-radius",
        value_fn=_extra_number(
            "USER_MODE_CONFIG", "user_mode_config", "anti_theft_config", "e_fence_range"
        ),
    ),
    RobotStatusSensorDescription(
        key="navigation_mode",
        translation_key="navigation_mode",
        icon="mdi:map-marker-path",
        value_fn=_extra_state(
            "USER_MODE_CONFIG", "user_mode_config", "nav_common_config", "navigation_type"
        ),
    ),
    RobotStatusSensorDescription(
        key="fault_count",
        translation_key="fault_count",
        icon="mdi:counter",
        value_fn=_fault_count,
    ),
    RobotStatusSensorDescription(
        key="positioning",
        translation_key="positioning",
        icon="mdi:crosshairs",
        value_fn=_extra_state("FEATURE_INFO", "feature_info", "nav_feature", "solution"),
    ),
    RobotStatusSensorDescription(
        key="rated_area",
        translation_key="rated_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        icon="mdi:texture-box",
        value_fn=_extra_number("FEATURE_INFO", "feature_info", "sku_info", "declare_cut_area"),
    ),
    RobotStatusSensorDescription(
        key="max_area",
        translation_key="max_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        icon="mdi:texture-box",
        value_fn=_extra_number("FEATURE_INFO", "feature_info", "sku_info", "real_cut_area"),
    ),
    RobotStatusSensorDescription(
        key="blade_diameter",
        translation_key="blade_diameter",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        icon="mdi:saw-blade",
        value_fn=_extra_number(
            "FEATURE_INFO", "feature_info", "cutter_feature", "main_cutter_diameter_mm"
        ),
    ),
    RobotStatusSensorDescription(
        key="battery_capacity",
        translation_key="battery_capacity",
        native_unit_of_measurement="Ah",
        icon="mdi:battery-high",
        value_fn=_extra_number("FEATURE_INFO", "feature_info", "sku_info", "battery_capacity"),
    ),
]

# Everything from wifi_quality on is detail: diagnostic and disabled by default.
_FIRST_HIDDEN = next(
    i for i, desc in enumerate(ROBOT_STATUS_SENSORS) if desc.key == "wifi_quality"
)
ROBOT_STATUS_SENSORS[_FIRST_HIDDEN:] = [
    replace(desc, entity_category=_DIAGNOSTIC, entity_registry_enabled_default=False)
    for desc in ROBOT_STATUS_SENSORS[_FIRST_HIDDEN:]
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower sensor entities."""
    coordinators = entry.runtime_data.coordinators
    entities: list[SensorEntity] = []
    for coord in coordinators:
        for desc in SENSOR_DESCRIPTIONS:
            if coord.supports_dp(desc.dps):
                entities.append(RoborockMowerSensorEntity(coord, desc))
            else:
                remove_entity(hass, "sensor", f"{coord.device.duid}_{desc.key}")
    entities.extend(
        RoborockRobotStatusSensor(coord, desc)
        for coord in coordinators
        for desc in ROBOT_STATUS_SENSORS
    )
    async_add_entities(entities)


class RoborockMowerSensorEntity(RoborockMowerEntity, SensorEntity):
    """Sensor entity for a Roborock mower."""

    entity_description: RoborockMowerSensorDescription

    def __init__(
        self,
        coordinator: RoborockMowerCoordinator,
        description: RoborockMowerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._device.duid}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.status)


class RoborockRobotStatusSensor(RoborockMowerEntity, SensorEntity):
    """A value from the mower's full status or its mowing preferences."""

    entity_description: RobotStatusSensorDescription

    def __init__(
        self,
        coordinator: RoborockMowerCoordinator,
        description: RobotStatusSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._device.duid}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.robot_info)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.robot_info)
