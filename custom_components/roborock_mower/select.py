"""Select platform for Roborock Mower integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockPreferenceEntity
from .mower_api import (
    DIRECTION_MODE_WIRE,
    EFF_MODE_LABELS,
    EFF_MODE_REVERSE,
    ROTATION_ANGLES,
)
from .robot_status import as_number

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Roborock mower select entities."""
    async_add_entities(
        entity
        for coord in entry.runtime_data.coordinators
        for entity in (
            RoborockEfficiencyModeSelect(coord),
            RoborockDirectionModeSelect(coord),
            RoborockRotationAngleSelect(coord),
        )
    )


class RoborockEfficiencyModeSelect(RoborockPreferenceEntity, SelectEntity):
    """Select entity for the mowing efficiency mode (MowPreference.effective).

    Read from DP 133 (MowEffModeDpValue), written via the SET_MOW_PREFERENCE
    remote_pb command. Values: 1=Daily, 2=Efficient, 3=Manicure (0=Unknown).
    """

    _attr_translation_key = "mow_eff_mode"
    _attr_icon = "mdi:speedometer"
    _attr_entity_category = EntityCategory.CONFIG
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
        self.coordinator.request_settings_refresh()


class RoborockDirectionModeSelect(RoborockPreferenceEntity, SelectEntity):
    """The app's mowing direction: Auto, Optimal or Custom (``direction_type``)."""

    _attr_translation_key = "direction_mode"
    _attr_icon = "mdi:arrow-decision-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(DIRECTION_MODE_WIRE)

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_direction_mode"

    @property
    def current_option(self) -> str | None:
        wire = self.preference.get("direction_type")
        return next(
            (option for option, name in DIRECTION_MODE_WIRE.items() if name == wire), None
        )

    async def async_select_option(self, option: str) -> None:
        await self._async_set_preference(
            "Set direction mode", direction_type=DIRECTION_MODE_WIRE[option]
        )


class RoborockRotationAngleSelect(RoborockPreferenceEntity, SelectEntity):
    """How far "Auto" turns the mowing direction every mow (``rotation_angle``)."""

    _attr_translation_key = "rotation_angle"
    _attr_icon = "mdi:rotate-right"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [str(angle) for angle in ROTATION_ANGLES]

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_rotation_angle_select"

    @property
    def current_option(self) -> str | None:
        angle = as_number(self.preference.get("rotation_angle"))
        return str(int(angle)) if angle in ROTATION_ANGLES else None

    async def async_select_option(self, option: str) -> None:
        await self._async_set_preference(
            "Set direction change per mow", rotation_angle=int(option)
        )
