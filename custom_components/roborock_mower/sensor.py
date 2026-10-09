"""Sensor platform for Roborock Mower integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower sensor entities."""
    async_add_entities(
        RoborockMowerSensorEntity(coord, desc)
        for coord in entry.runtime_data.coordinators
        for desc in SENSOR_DESCRIPTIONS
    )


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
