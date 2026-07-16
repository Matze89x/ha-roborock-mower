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
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import (
    CHARGE_STATE_LABELS,
    CHARGE_TYPE_LABELS,
    MOW_TYPE_LABELS,
    PEND_TYPE_LABELS,
    MowerStatus,
)


@dataclass(frozen=True, kw_only=True)
class RoborockMowerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MowerStatus], Any]


def _labelled(value: int | None, labels: dict[int, str]) -> str | None:
    """Decode an enum value to its label, falling back to the raw number."""
    if value is None:
        return None
    return labels.get(value, str(value))


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
        icon="mdi:robot-mower",
        value_fn=lambda s: s.mow_state_label,
    ),
    RoborockMowerSensorDescription(
        key="mow_type",
        translation_key="mow_type",
        icon="mdi:vector-square",
        value_fn=lambda s: _labelled(s.mow_type, MOW_TYPE_LABELS),
    ),
    RoborockMowerSensorDescription(
        key="charge_state",
        translation_key="charge_state",
        icon="mdi:battery-charging",
        value_fn=lambda s: _labelled(s.charge_state, CHARGE_STATE_LABELS),
    ),
    RoborockMowerSensorDescription(
        key="charge_type",
        translation_key="charge_type",
        icon="mdi:home-import-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: _labelled(s.charge_type, CHARGE_TYPE_LABELS),
    ),
    RoborockMowerSensorDescription(
        key="pend_type",
        translation_key="pend_type",
        icon="mdi:pause-octagon",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: _labelled(s.pend_type, PEND_TYPE_LABELS),
    ),
    RoborockMowerSensorDescription(
        key="error_code",
        translation_key="error_code",
        icon="mdi:alert-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.error_code,
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
