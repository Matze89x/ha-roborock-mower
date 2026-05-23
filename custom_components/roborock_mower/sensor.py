"""Sensor platform for Roborock Mower integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import MowerStatus


@dataclass(frozen=True, kw_only=True)
class RoborockMowerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MowerStatus], Any]


# mow_type (DPS 122): 1=full mow, 2=edge cut (confirmed live), 0=idle/no task.
_MOW_TYPE_LABELS = {0: "idle", 1: "full_mow", 2: "edge_cut"}


def _mow_type_label(status: MowerStatus) -> str | None:
    if status.mow_type is None:
        return None
    return _MOW_TYPE_LABELS.get(status.mow_type, str(status.mow_type))


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
        key="charge_state",
        translation_key="charge_state",
        icon="mdi:battery-charging",
        value_fn=lambda s: s.charge_state,
    ),
    RoborockMowerSensorDescription(
        key="error_code",
        translation_key="error_code",
        icon="mdi:alert-circle",
        value_fn=lambda s: s.error_code,
    ),
    RoborockMowerSensorDescription(
        key="mow_state",
        translation_key="mow_state",
        icon="mdi:robot-mower",
        value_fn=lambda s: s.mow_state,
    ),
    RoborockMowerSensorDescription(
        key="mow_type",
        translation_key="mow_type",
        icon="mdi:vector-square",
        value_fn=_mow_type_label,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower sensor entities."""
    coordinators: list[RoborockMowerCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[RoborockMowerSensorEntity] = []
    for coord in coordinators:
        for desc in SENSOR_DESCRIPTIONS:
            entities.append(RoborockMowerSensorEntity(coord, desc))
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
