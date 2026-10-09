"""Button platform for the Roborock mower.

Exposes an Edge Cut button, a Stop button, a Cancel Dock button, one "mow
zone" button per saved area and one button per Roborock "routine" (scene)
created in the app. Routines are triggered by id through the cloud and are a
convenient way to run app-authored tasks from Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import AREA_DISCOVERY_RETRY_DELAYS
from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import areas_from_preference_config
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
    for coordinator in coordinators:
        entry.async_create_background_task(
            hass,
            _async_add_area_buttons(coordinator, async_add_entities),
            f"{entry.domain}_areas_{coordinator.device.duid}",
        )


async def _async_add_area_buttons(
    coordinator: RoborockMowerCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Add one "mow zone" button per saved area once the mower reports them.

    The query goes to the mower itself, which is often asleep or out of Wi-Fi
    range right after a restart, so this runs in the background with retries
    instead of blocking (or failing) the platform setup.
    """
    api = coordinator.mower_api
    for delay in AREA_DISCOVERY_RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        cfg = await api.get_mow_preference_config()
        if cfg is None:
            continue  # mower did not answer; try again later
        api.areas = areas_from_preference_config(cfg)
        async_add_entities(
            RoborockAreaMowButton(coordinator, area) for area in api.areas
        )
        return
    _LOGGER.info(
        "[%s] Mower did not report its saved areas; the zone buttons will "
        "appear after the next restart/reload while the mower is online",
        coordinator.device.duid,
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
    # The lawn mower entity can stop too.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_stop"

    async def async_press(self) -> None:
        await self._async_send("Stop", self.coordinator.mower_api.stop)


class RoborockCancelDockButton(RoborockMowerEntity, ButtonEntity):
    """Cancels an in-progress return-to-dock (AppButton DOCK_END)."""

    _attr_translation_key = "cancel_dock"
    _attr_icon = "mdi:home-export-outline"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_cancel_dock"

    async def async_press(self) -> None:
        await self._async_send("Cancel dock", self.coordinator.mower_api.cancel_dock)


class RoborockAreaMowButton(RoborockMowerEntity, ButtonEntity):
    """Starts a select-area (zone) mow of one saved area (AppButton MOW_SELECT)."""

    _attr_translation_key = "mow_area"
    _attr_icon = "mdi:select-marker"

    def __init__(
        self, coordinator: RoborockMowerCoordinator, area: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._area = {"id": area["id"], "name": area.get("name", "")}
        self._attr_translation_placeholders = {
            "area": str(area.get("name") or area["id"])
        }
        self._attr_unique_id = f"{self._device.duid}_mow_area_{area['id']}"

    async def async_press(self) -> None:
        await self._async_send(
            "Zone mow",
            lambda: self.coordinator.mower_api.start_area_mow([self._area]),
        )


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
