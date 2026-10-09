"""Select platform for Roborock Mower integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .mower_api import EFF_MODE_LABELS, EFF_MODE_REVERSE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower select entities."""
    async_add_entities(
        RoborockEfficiencyModeSelect(coord) for coord in entry.runtime_data.coordinators
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
