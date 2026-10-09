"""Button platform for the Roborock mower.

Exposes an Edge Cut button, a Stop button, a Cancel Dock button and one button
per Roborock "routine" (scene) created in the app. Routines are triggered by id
through the cloud and are a convenient way to run app-authored tasks (e.g. a
saved zone mow) from Home Assistant.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .vendor.roborock.data.containers import HomeDataScene
from .vendor.roborock.exceptions import RoborockException

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mower control buttons and one button per app routine."""
    coordinators = entry.runtime_data.coordinators
    entities: list[ButtonEntity] = []
    for coordinator in coordinators:
        entities.append(RoborockEdgeCutButton(coordinator))
        entities.append(RoborockStopButton(coordinator))
        entities.append(RoborockCancelDockButton(coordinator))
    async_add_entities(entities)

    async def _add_routine_buttons() -> None:
        # Cloud call: done in the background so a slow or failing cloud never
        # delays (or breaks) startup.
        for coordinator in coordinators:
            try:
                routines = await coordinator.mower_api.get_routines()
            except RoborockException as err:
                _LOGGER.warning(
                    "Could not fetch routines for %s: %s", coordinator.device.duid, err
                )
                continue
            async_add_entities(
                RoborockRoutineButton(coordinator, routine) for routine in routines
            )

    entry.async_create_background_task(
        hass, _add_routine_buttons(), f"{entry.domain}_routines"
    )


class RoborockEdgeCutButton(RoborockMowerEntity, ButtonEntity):
    """Starts an edge cut (perimeter mow) via the remote_pb command."""

    _attr_translation_key = "edge_cut"
    _attr_icon = "mdi:vector-square"

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_edge_cut"

    async def async_press(self) -> None:
        await self._async_send("Edge cut", self.coordinator.mower_api.edge_cut)


class RoborockStopButton(RoborockMowerEntity, ButtonEntity):
    """Stops / ends the current mow task (AppButton MOW_END).

    Distinct from pause: this ends the task rather than suspending it.
    """

    _attr_translation_key = "stop"
    _attr_icon = "mdi:stop"

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_stop"

    async def async_press(self) -> None:
        await self._async_send("Stop", self.coordinator.mower_api.stop)


class RoborockCancelDockButton(RoborockMowerEntity, ButtonEntity):
    """Cancels an in-progress return-to-dock (AppButton DOCK_END)."""

    _attr_translation_key = "cancel_dock"
    _attr_icon = "mdi:home-export-outline"

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_cancel_dock"

    async def async_press(self) -> None:
        await self._async_send("Cancel dock", self.coordinator.mower_api.cancel_dock)


class RoborockRoutineButton(RoborockMowerEntity, ButtonEntity):
    """Triggers a Roborock routine/scene (e.g. mow, edge cut)."""

    def __init__(
        self, coordinator: RoborockMowerCoordinator, routine: HomeDataScene
    ) -> None:
        super().__init__(coordinator)
        self._routine_id = routine.id
        self._attr_name = routine.name or f"Routine {routine.id}"
        self._attr_unique_id = f"{self._device.duid}_routine_{routine.id}"

    @property
    def available(self) -> bool:
        # Routines run via the cloud, independent of device poll state.
        return True

    async def async_press(self) -> None:
        await self._async_send(
            "Routine",
            lambda: self.coordinator.mower_api.execute_routine(self._routine_id),
        )
