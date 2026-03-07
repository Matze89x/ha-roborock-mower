"""Select platform for Roborock Mower integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .entity import RoborockMowerEntity

EFF_MODE_MAP: dict[int, str] = {
    0: "Standard",
    1: "Efficient",
    2: "Quiet",
}
EFF_MODE_REVERSE: dict[str, int] = {v: k for k, v in EFF_MODE_MAP.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower select entities."""
    coordinators: list[RoborockMowerCoordinator] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        RoborockEfficiencyModeSelect(coord) for coord in coordinators
    )


class RoborockEfficiencyModeSelect(RoborockMowerEntity, SelectEntity):
    """Select entity for mowing efficiency mode."""

    _attr_translation_key = "mow_eff_mode"
    _attr_icon = "mdi:speedometer"
    _attr_options = list(EFF_MODE_MAP.values())

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_mow_eff_mode"

    @property
    def current_option(self) -> str | None:
        mode = self.status.mow_eff_mode
        if mode is None:
            return None
        return EFF_MODE_MAP.get(mode, f"Unknown ({mode})")

    async def async_select_option(self, option: str) -> None:
        code = EFF_MODE_REVERSE.get(option)
        if code is None:
            return
        await self.coordinator.mower_api.set_mow_eff_mode(code)
        await self.coordinator.async_request_refresh()
