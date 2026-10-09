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
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfArea,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import (
    CHARGE_STATE_LABELS,
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
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.battery,
    ),
    RoborockMowerSensorDescription(
        key="mow_progress",
        translation_key="mow_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-check",
        value_fn=lambda s: s.mow_progress,
    ),
    RoborockMowerSensorDescription(
        key="mow_state",
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
        translation_key="mow_type",
        device_class=SensorDeviceClass.ENUM,
        options=_options(MOW_TYPE_LABELS),
        icon="mdi:vector-square",
        value_fn=_enum("mow_type", lambda s: s.mow_type, MOW_TYPE_LABELS, 0),
    ),
    RoborockMowerSensorDescription(
        key="charge_state",
        translation_key="charge_state",
        device_class=SensorDeviceClass.ENUM,
        options=_options(CHARGE_STATE_LABELS),
        icon="mdi:battery-charging",
        value_fn=_enum("charge_state", lambda s: s.charge_state, CHARGE_STATE_LABELS),
    ),
    RoborockMowerSensorDescription(
        key="charge_type",
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


def _preference_number(key: str) -> Callable[[RobotInfo], float | int | None]:
    return lambda info: as_number(info.preference.get(key))


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
        key="next_mow",
        translation_key="next_mow",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
        value_fn=_next_mow,
        attrs_fn=_next_mow_attrs,
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
        key="mow_passes",
        translation_key="mow_passes",
        icon="mdi:repeat",
        value_fn=_preference_number("mow_times"),
    ),
    RobotStatusSensorDescription(
        key="mow_direction",
        translation_key="mow_direction",
        native_unit_of_measurement=DEGREE,
        icon="mdi:compass-outline",
        value_fn=_preference_number("direction"),
    ),
    RobotStatusSensorDescription(
        key="direction_mode",
        translation_key="direction_mode",
        icon="mdi:arrow-decision-outline",
        value_fn=_preference_state("direction_type"),
    ),
    RobotStatusSensorDescription(
        key="rotation_angle",
        translation_key="rotation_angle",
        native_unit_of_measurement=DEGREE,
        icon="mdi:rotate-right",
        value_fn=_preference_number("rotation_angle"),
    ),
    RobotStatusSensorDescription(
        key="boundary_perception",
        translation_key="boundary_perception",
        icon="mdi:border-outside",
        value_fn=_preference_state("boundary_perception"),
    ),
]

# Everything after rtk_position is detail: diagnostic and disabled by default.
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
    entities: list[SensorEntity] = [
        RoborockMowerSensorEntity(coord, desc)
        for coord in coordinators
        for desc in SENSOR_DESCRIPTIONS
    ]
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
