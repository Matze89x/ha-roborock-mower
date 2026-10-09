"""Number platform for Roborock Mower integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import DEGREE, EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity, RoborockPreferenceEntity, remove_entity
from .mower_api import DPS_MOW_HEIGHT
from .robot_status import as_number


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower number entities."""
    entities: list[NumberEntity] = []
    for coord in entry.runtime_data.coordinators:
        entities.append(RoborockMowDirectionNumber(coord))
        entities.append(RoborockMowPassesNumber(coord))
        if coord.supports_dp(DPS_MOW_HEIGHT):
            entities.append(RoborockMowHeightNumber(coord))
        else:
            remove_entity(hass, "number", f"{coord.device.duid}_mow_height")
    async_add_entities(entities)


class RoborockMowHeightNumber(RoborockMowerEntity, NumberEntity):
    """Number entity to control mow height."""

    _attr_translation_key = "mow_height"
    _attr_icon = "mdi:arrow-expand-vertical"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 20
    _attr_native_max_value = 70
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_mow_height"

    @property
    def native_value(self) -> float | None:
        return self.status.mow_height

    async def async_set_native_value(self, value: float) -> None:
        # State reflects back via the MQTT push, not a rate-limited REST poll.
        await self._async_send(
            "Set mow height",
            lambda: self.coordinator.mower_api.set_mow_height(int(value)),
        )


class RoborockMowDirectionNumber(RoborockPreferenceEntity, NumberEntity):
    """Mowing direction in degrees (preference ``direction``), 5-degree steps.

    The angle the direction mode "Custom" mows at (the app's slider).
    """

    _attr_translation_key = "mow_direction"
    _attr_icon = "mdi:compass-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 180
    _attr_native_step = 5
    _attr_native_unit_of_measurement = DEGREE

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_mow_direction_angle"

    @property
    def native_value(self) -> float | None:
        return as_number(self.preference.get("direction"))

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_preference("Set mowing direction", direction=int(value))


class RoborockMowPassesNumber(RoborockPreferenceEntity, NumberEntity):
    """How often each spot is mown per run (preference ``mow_times``).

    "Mähdurchgänge" in the app. Written with ``SET_MOW_PREFERENCE``.
    """

    _attr_translation_key = "mow_passes"
    _attr_icon = "mdi:repeat"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 3
    _attr_native_step = 1

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_mow_passes"

    @property
    def native_value(self) -> float | None:
        return as_number(self.preference.get("mow_times"))

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_preference("Set mowing passes", mow_times=int(value))
