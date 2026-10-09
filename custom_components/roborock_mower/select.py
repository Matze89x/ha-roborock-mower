"""Select platform for Roborock Mower integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import AREA_DISCOVERY_RETRY_DELAYS
from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import EFF_MODE_LABELS, EFF_MODE_REVERSE, areas_from_preference_config

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower select entities."""
    coordinators = entry.runtime_data.coordinators
    async_add_entities(RoborockEfficiencyModeSelect(coord) for coord in coordinators)

    for coord in coordinators:
        entry.async_create_background_task(
            hass,
            _async_add_area_select(coord, async_add_entities),
            f"{entry.domain}_areas_{coord.device.duid}",
        )


async def _async_add_area_select(
    coordinator: RoborockMowerCoordinator, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the zone picker once the mower reports its saved areas.

    The query goes to the mower itself, which is often asleep or out of Wi-Fi
    range right after a restart, so this runs in the background with retries
    instead of blocking (or failing) the platform setup.
    """
    for delay in AREA_DISCOVERY_RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        cfg = await coordinator.mower_api.get_mow_preference_config()
        if cfg is None:
            continue  # mower did not answer; try again later
        if areas := areas_from_preference_config(cfg):
            async_add_entities([RoborockMowAreaSelect(coordinator, areas)])
        return
    _LOGGER.info(
        "[%s] Mower did not report its saved areas; the Mow Area selector will "
        "appear after the next restart/reload while the mower is online",
        coordinator.device.duid,
    )


class RoborockEfficiencyModeSelect(RoborockMowerEntity, SelectEntity):
    """Select entity for the mowing efficiency mode (MowPreference.effective).

    Read from DP 133 (MowEffModeDpValue), written via the SET_MOW_PREFERENCE
    remote_pb command. Values: 1=Daily, 2=Efficient, 3=Manicure (0=Unknown).
    """

    _attr_translation_key = "mow_eff_mode"
    _attr_icon = "mdi:speedometer"
    _attr_options = list(EFF_MODE_LABELS.values())

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_mow_eff_mode"

    @property
    def current_option(self) -> str | None:
        # 0 = UNKNOWN (not a selectable mode); show as unset.
        return EFF_MODE_LABELS.get(self.status.mow_eff_mode or 0)

    async def async_select_option(self, option: str) -> None:
        code = EFF_MODE_REVERSE.get(option)
        if code is None:
            return
        # New value reflects back over the MQTT push (DP 133).
        await self._async_send(
            "Set efficiency mode",
            lambda: self.coordinator.mower_api.set_mow_eff_mode(code),
        )


class RoborockMowAreaSelect(RoborockMowerEntity, SelectEntity):
    """Action-style selector: picking a saved area starts a select-area mow.

    Options are the device's saved boundaries/zones (from the mowing preference
    config). Selecting one sends the MOW_SELECT command for that area. This is a
    trigger, not a persistent state, so it always reads back as unset.
    """

    _attr_translation_key = "mow_area"
    _attr_icon = "mdi:select-marker"

    def __init__(
        self, coordinator: RoborockMowerCoordinator, areas: list[dict[str, Any]]
    ) -> None:
        super().__init__(coordinator)
        self._areas: dict[str, dict[str, Any]] = {
            str(area.get("name") or area.get("id")): area for area in areas
        }
        self._attr_options = list(self._areas)
        self._attr_unique_id = f"{self._device.duid}_mow_area"

    @property
    def current_option(self) -> str | None:
        return None  # trigger-only; no persistent selection

    async def async_select_option(self, option: str) -> None:
        area = self._areas.get(option)
        if area is None:
            return
        await self._async_send(
            "Area mow",
            lambda: self.coordinator.mower_api.start_area_mow(
                [{"id": area["id"], "name": area.get("name", "")}]
            ),
        )
