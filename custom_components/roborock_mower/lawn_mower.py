"""Lawn mower platform for Roborock Mower integration."""

from __future__ import annotations

import logging

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .entity import RoborockMowerEntity

_LOGGER = logging.getLogger(__name__)

# mow_state (DPS 123), confirmed live: 0=idle/docked, 51=transient,
# 56/57=mowing, 58=paused. Active task codes vary, so treat any non-idle,
# non-paused value as mowing rather than enumerating each one.
MOW_STATE_IDLE = 0
MOW_STATE_PAUSED = 58


def _derive_activity(
    mow_state: int | None,
    error_code: int | None,
    off_dock_no_task_status: int | None,
) -> LawnMowerActivity:
    if error_code:
        return LawnMowerActivity.ERROR
    if mow_state == MOW_STATE_PAUSED:
        return LawnMowerActivity.PAUSED
    if mow_state not in (None, MOW_STATE_IDLE):
        return LawnMowerActivity.MOWING
    # Idle mow_state but off the dock with no task means returning to dock.
    if off_dock_no_task_status:
        return LawnMowerActivity.MOWING
    return LawnMowerActivity.DOCKED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower lawn_mower entities."""
    coordinators: list[RoborockMowerCoordinator] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        RoborockLawnMowerEntity(coord) for coord in coordinators
    )


class RoborockLawnMowerEntity(RoborockMowerEntity, LawnMowerEntity):
    """Represents a Roborock mower as a lawn mower entity."""

    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_lawn_mower"

    @property
    def activity(self) -> LawnMowerActivity:
        return _derive_activity(
            self.status.mow_state,
            self.status.error_code,
            self.status.off_dock_no_task_status,
        )

    async def async_start_mowing(self) -> None:
        if self.status.mow_state == MOW_STATE_PAUSED:
            await self.coordinator.mower_api.resume()
        else:
            await self.coordinator.mower_api.start()
        await self.coordinator.async_request_refresh()

    async def async_pause(self) -> None:
        await self.coordinator.mower_api.pause()
        await self.coordinator.async_request_refresh()

    async def async_dock(self) -> None:
        await self.coordinator.mower_api.dock()
        await self.coordinator.async_request_refresh()
