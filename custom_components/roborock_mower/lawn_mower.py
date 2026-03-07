"""Lawn mower platform for Roborock Mower integration."""

from __future__ import annotations

import logging
from typing import Any

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

# Best-effort mapping; unknown values are logged for future refinement
MOW_STATE_MOWING = 1
MOW_STATE_PAUSED = 2
MOW_STATE_ERROR = 3
MOW_STATE_RETURNING = 4
MOW_STATE_CHARGING = 5
MOW_STATE_IDLE = 0


def _derive_activity(
    mow_state: int | None,
    charge_state: int | None,
    error_code: int | None,
) -> LawnMowerActivity:
    if error_code is not None and error_code != 0:
        return LawnMowerActivity.ERROR
    if mow_state == MOW_STATE_MOWING:
        return LawnMowerActivity.MOWING
    if mow_state == MOW_STATE_PAUSED:
        return LawnMowerActivity.PAUSED
    if mow_state == MOW_STATE_RETURNING:
        return LawnMowerActivity.MOWING
    if charge_state is not None and charge_state > 0:
        return LawnMowerActivity.DOCKED
    if mow_state == MOW_STATE_IDLE:
        return LawnMowerActivity.DOCKED
    if mow_state is not None:
        _LOGGER.warning("Unknown mow_state %s, defaulting to DOCKED", mow_state)
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
            self.status.charge_state,
            self.status.error_code,
        )

    async def async_start_mowing(self) -> None:
        await self.coordinator.mower_api.start()
        await self.coordinator.async_request_refresh()

    async def async_pause(self) -> None:
        await self.coordinator.mower_api.pause()
        await self.coordinator.async_request_refresh()

    async def async_dock(self) -> None:
        await self.coordinator.mower_api.dock()
        await self.coordinator.async_request_refresh()
