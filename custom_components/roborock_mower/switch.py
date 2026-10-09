"""Switch platform for the Roborock mower: mowing preferences that are on/off."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .robot_status import as_flag


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mower's switches."""
    async_add_entities(
        RoborockEdgeCutSwitch(coordinator)
        for coordinator in entry.runtime_data.coordinators
    )


class RoborockEdgeCutSwitch(RoborockMowerEntity, SwitchEntity):
    """Cut the edges as part of every mow (preference ``keep_edge``).

    Called "Kantenschnitt" in the app. Written with ``SET_MOW_PREFERENCE``.
    """

    _attr_translation_key = "edge_cut_while_mowing"
    _attr_icon = "mdi:border-outside"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_edge_cut_while_mowing"

    @property
    def is_on(self) -> bool | None:
        return as_flag((self.coordinator.mow_preference or {}).get("keep_edge"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(0)

    async def _async_set(self, value: int) -> None:
        await self._async_send(
            "Set edge cut",
            lambda: self.coordinator.mower_api.set_mow_preference(keep_edge=value),
        )
        self.async_write_ha_state()
        self.coordinator.request_settings_refresh()
