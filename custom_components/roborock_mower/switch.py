"""Switch platform for the Roborock mower.

Mowing preferences and settings that are on/off: edge cut, rain protection,
do not disturb, anti-theft and the navigation options.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockPreferenceEntity, RoborockUserModeEntity
from .robot_status import as_flag


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mower's switches."""
    entities: list[SwitchEntity] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.append(RoborockEdgeCutSwitch(coordinator))
        entities.extend(
            RoborockUserModeSwitch(coordinator, description)
            for description in USER_MODE_SWITCHES
        )
    async_add_entities(entities)


@dataclass(frozen=True, kw_only=True)
class UserModeSwitchDescription(SwitchEntityDescription):
    """An on/off field of the mower's settings (``GET_USER_MODE_CONFIG``)."""

    part: str
    field: str


USER_MODE_SWITCHES = (
    UserModeSwitchDescription(
        key="rain_protection",
        translation_key="rain_protection",
        icon="mdi:weather-rainy",
        entity_category=EntityCategory.CONFIG,
        part="rainfall_config",
        field="enable",
    ),
    UserModeSwitchDescription(
        key="do_not_disturb",
        translation_key="do_not_disturb",
        icon="mdi:minus-circle-outline",
        entity_category=EntityCategory.CONFIG,
        part="not_disturb_config",
        field="enable",
    ),
    UserModeSwitchDescription(
        key="anti_theft",
        translation_key="anti_theft",
        icon="mdi:shield-lock-outline",
        entity_category=EntityCategory.CONFIG,
        part="anti_theft_config",
        field="enable",
    ),
    UserModeSwitchDescription(
        key="path_obstacle_detection",
        translation_key="path_obstacle_detection",
        icon="mdi:road-variant",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="path_detect_avoid",
    ),
    UserModeSwitchDescription(
        key="edge_camera_avoidance",
        translation_key="edge_camera_avoidance",
        icon="mdi:camera-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="edge_vision_avoid",
    ),
    UserModeSwitchDescription(
        key="passage_camera_avoidance",
        translation_key="passage_camera_avoidance",
        icon="mdi:camera-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="channel_vision_avoid",
    ),
    UserModeSwitchDescription(
        key="obstacle_photo_privacy",
        translation_key="obstacle_photo_privacy",
        icon="mdi:image-off-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="obstacle_image_privacy",
    ),
)


class RoborockEdgeCutSwitch(RoborockPreferenceEntity, SwitchEntity):
    """Cut the edges as part of every mow (preference ``keep_edge``).

    Called "Kantenschnitt" in the app. Written with ``SET_MOW_PREFERENCE``.
    """

    _attr_translation_key = "edge_cut_while_mowing"
    _attr_icon = "mdi:border-outside"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_edge_cut_while_mowing"

    @property
    def is_on(self) -> bool | None:
        return as_flag(self.preference.get("keep_edge"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_preference("Set edge cut", keep_edge=1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_preference("Set edge cut", keep_edge=0)


class RoborockUserModeSwitch(RoborockUserModeEntity, SwitchEntity):
    """A setting of the mower that is on or off (rain protection, ...).

    Written with the app's command for its part (``SET_RAINFALL`` ...), the
    part sent whole; the mower is asked again to confirm it took the change.
    """

    entity_description: UserModeSwitchDescription

    def __init__(
        self, coordinator: RoborockMowerCoordinator, description: UserModeSwitchDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._device.duid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        desc = self.entity_description
        part = self.user_mode(desc.part)
        if not part:
            return None
        # The mower leaves out fields that are off.
        return bool(as_flag(part.get(desc.field)))

    async def _async_turn(self, on: bool) -> None:
        desc = self.entity_description
        await self._async_set_user_mode(
            f"Set {desc.key}", desc.part, **{desc.field: on}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_turn(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_turn(False)
