"""Lawn mower platform for Roborock Mower integration."""

from __future__ import annotations

import logging

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import (
    ACTIVITY_DOCKED,
    ACTIVITY_ERROR,
    ACTIVITY_IDLE,
    ACTIVITY_MOWING,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
    MOW_STATES_PAUSED,
    derive_activity,
)

_LOGGER = logging.getLogger(__name__)

_ACTIVITIES: dict[str, LawnMowerActivity] = {
    ACTIVITY_MOWING: LawnMowerActivity.MOWING,
    ACTIVITY_PAUSED: LawnMowerActivity.PAUSED,
    ACTIVITY_RETURNING: LawnMowerActivity.RETURNING,
    ACTIVITY_DOCKED: LawnMowerActivity.DOCKED,
    ACTIVITY_ERROR: LawnMowerActivity.ERROR,
    # "Stopped, but neither docked nor paused" (HA 2025.x+); older HA: docked.
    ACTIVITY_IDLE: getattr(LawnMowerActivity, "IDLE", LawnMowerActivity.DOCKED),
}

# Unmapped mow_state codes already reported (warn once per code, not per write).
_REPORTED_UNMAPPED: set[int] = set()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower lawn_mower entities."""
    async_add_entities(
        RoborockLawnMowerEntity(coord) for coord in entry.runtime_data.coordinators
    )


class RoborockLawnMowerEntity(RoborockMowerEntity, LawnMowerEntity):
    """Represents a Roborock mower as a lawn mower entity."""

    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
        # "Stop" (end the task where the mower is): Home Assistant 2026.10+.
        | getattr(LawnMowerEntityFeature, "STOP", LawnMowerEntityFeature(0))
    )

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_lawn_mower"

    @property
    def activity(self) -> LawnMowerActivity:
        status = self.status
        api = self.coordinator.mower_api
        activity = derive_activity(status, api.return_pending, api.task_pending)
        if activity is None:
            if status.mow_state not in _REPORTED_UNMAPPED:
                _REPORTED_UNMAPPED.add(status.mow_state)
                _LOGGER.warning(
                    "Unmapped mower mow_state %s; treating as mowing. Please report "
                    "it together with the integration diagnostics",
                    status.mow_state,
                )
            return LawnMowerActivity.MOWING
        return _ACTIVITIES[activity]

    # NOTE: commands do not trigger a REST refresh -- the resulting state change
    # arrives over the MQTT DPS push (get_home_data is rate-limited; see the
    # coordinator). The push updates the coordinator and hence these entities.

    async def async_start_mowing(self) -> None:
        api = self.coordinator.mower_api
        if self.status.mow_state in MOW_STATES_PAUSED:
            await self._async_send("Resume", api.resume)
        else:
            await self._async_send("Start mowing", api.start)

    async def async_pause(self) -> None:
        await self._async_send("Pause", self.coordinator.mower_api.pause)

    async def async_dock(self) -> None:
        await self._async_send("Return to dock", self.coordinator.mower_api.dock)

    async def async_stop(self) -> None:
        await self._async_send("Stop", self.coordinator.mower_api.stop)
