"""Select platform for Roborock Mower integration."""

from __future__ import annotations

import logging
from typing import Any

from roborock.exceptions import RoborockException

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import EFF_MODE_LABELS, EFF_MODE_REVERSE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower select entities."""
    coordinators: list[RoborockMowerCoordinator] = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    for coord in coordinators:
        entities.append(RoborockEfficiencyModeSelect(coord))
        # Add a zone picker if the device exposes saved areas.
        try:
            areas = await coord.mower_api.get_areas()
        except RoborockException as err:
            _LOGGER.debug("Could not fetch areas for %s: %s", coord.device.duid, err)
            areas = []
        if areas:
            entities.append(RoborockMowAreaSelect(coord, areas))
    async_add_entities(entities)


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
        await self.coordinator.mower_api.set_mow_eff_mode(code)


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
        await self.coordinator.mower_api.start_area_mow(
            [{"id": area["id"], "name": area.get("name", "")}]
        )
