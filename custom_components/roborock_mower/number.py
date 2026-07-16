"""Number platform for Roborock Mower integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .entity import RoborockMowerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower number entities."""
    coordinators: list[RoborockMowerCoordinator] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        RoborockMowHeightNumber(coord) for coord in coordinators
    )


class RoborockMowHeightNumber(RoborockMowerEntity, NumberEntity):
    """Number entity to control mow height."""

    _attr_translation_key = "mow_height"
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 20
    _attr_native_max_value = 70
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "mm"

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_mow_height"

    @property
    def native_value(self) -> float | None:
        return self.status.mow_height

    async def async_set_native_value(self, value: float) -> None:
        # State reflects back via the MQTT push, not a rate-limited REST poll.
        await self.coordinator.mower_api.set_mow_height(int(value))
