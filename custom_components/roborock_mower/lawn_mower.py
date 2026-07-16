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
from .mower_api import (
    MOW_STATES_DOCKED,
    MOW_STATES_ERROR,
    MOW_STATES_MOWING,
    MOW_STATES_PAUSED,
    MOW_STATES_RETURNING,
)

_LOGGER = logging.getLogger(__name__)

MOW_STATE_IDLE = 0

# LawnMowerActivity.RETURNING was added after the original lawn_mower enum;
# fall back to MOWING on HA versions that predate it so the entity still loads.
_RETURNING = getattr(LawnMowerActivity, "RETURNING", LawnMowerActivity.MOWING)

# off_dock_no_task_status (DP 143): 3 = DOCKING (returning to dock).
_OFF_DOCK_DOCKING = 3
# dock_state (DP 128) DockStateDpValue: 1 MOVING_TO_TARGET, 2 DOCKING.
_DOCK_STATE_RETURNING = frozenset({1, 2})


def _derive_activity(
    mow_state: int | None,
    error_code: int | None,
    off_dock_no_task_status: int | None,
    dock_state: int | None,
) -> LawnMowerActivity:
    """Map the mower's RobotDetailState (DP 123) to a lawn-mower activity."""
    if error_code or (mow_state is not None and mow_state in MOW_STATES_ERROR):
        return LawnMowerActivity.ERROR
    if mow_state in MOW_STATES_PAUSED:
        return LawnMowerActivity.PAUSED
    if mow_state in MOW_STATES_RETURNING or dock_state in _DOCK_STATE_RETURNING:
        return _RETURNING
    if mow_state in MOW_STATES_MOWING:
        return LawnMowerActivity.MOWING
    if mow_state in MOW_STATES_DOCKED:
        # Idle but off the dock with no task means it is heading back.
        if mow_state == MOW_STATE_IDLE and off_dock_no_task_status == _OFF_DOCK_DOCKING:
            return _RETURNING
        return LawnMowerActivity.DOCKED
    if mow_state in (None, MOW_STATE_IDLE):
        if off_dock_no_task_status == _OFF_DOCK_DOCKING:
            return _RETURNING
        return LawnMowerActivity.DOCKED
    # Unknown non-idle code: treat as active and log so it can be mapped later.
    _LOGGER.warning("Unmapped mower mow_state %s; treating as mowing", mow_state)
    return LawnMowerActivity.MOWING


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower lawn_mower entities."""
    coordinators: list[RoborockMowerCoordinator] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(RoborockLawnMowerEntity(coord) for coord in coordinators)


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
        status = self.status
        return _derive_activity(
            status.mow_state,
            status.error_code,
            status.off_dock_no_task_status,
            status.dock_state,
        )

    # NOTE: commands do not trigger a REST refresh -- the resulting state change
    # arrives over the MQTT DPS push (get_home_data is rate-limited; see the
    # coordinator). The push updates the coordinator and hence these entities.

    async def async_start_mowing(self) -> None:
        if self.status.mow_state in MOW_STATES_PAUSED:
            await self.coordinator.mower_api.resume()
        else:
            await self.coordinator.mower_api.start()

    async def async_pause(self) -> None:
        await self.coordinator.mower_api.pause()

    async def async_dock(self) -> None:
        await self.coordinator.mower_api.dock()
