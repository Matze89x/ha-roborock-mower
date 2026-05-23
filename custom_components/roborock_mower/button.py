"""Button platform exposing Roborock routines (scenes) for the mower.

Roborock "routines" are created in the Roborock app (e.g. a full mow or an edge
cut) and triggered by id through the cloud. This is the supported way to start a
mow, since the raw start data point requires the app's task payload.
"""

from __future__ import annotations

import logging

from roborock.data.containers import HomeDataScene
from roborock.exceptions import RoborockException

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .entity import RoborockMowerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a button for each app-defined routine on each mower."""
    coordinators: list[RoborockMowerCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[RoborockRoutineButton] = []
    for coordinator in coordinators:
        try:
            routines = await coordinator.mower_api.get_routines()
        except RoborockException as err:
            _LOGGER.warning(
                "Could not fetch routines for %s: %s", coordinator.device.duid, err
            )
            continue
        entities.extend(
            RoborockRoutineButton(coordinator, routine) for routine in routines
        )
    async_add_entities(entities)


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
        await self.coordinator.mower_api.execute_routine(self._routine_id)
