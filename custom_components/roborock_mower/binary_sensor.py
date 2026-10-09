"""Binary sensor platform for the Roborock mower.

Yes/no values of the mower's full status (``GET_ROBOT_STATUS``), its mowing
preferences and its settings (``GET_USER_MODE_CONFIG``: rain protection,
do-not-disturb, anti-theft). All diagnostic; only "last mow aborted" is
enabled by default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .robot_status import RobotInfo, as_flag, clock, dig, in_daily_window


@dataclass(frozen=True, kw_only=True)
class RobotStatusBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[RobotInfo], bool | None]
    attrs_fn: Callable[[RobotInfo], dict[str, Any] | None] | None = None


def _flag(*path: str | int) -> Callable[[RobotInfo], bool | None]:
    return lambda info: as_flag(dig(info.status, *path))


def _setting(*path: str | int) -> Callable[[RobotInfo], bool | None]:
    """A switch of the user settings (``GET_USER_MODE_CONFIG``)."""
    return lambda info: as_flag(
        dig(info.extra.get("USER_MODE_CONFIG"), "user_mode_config", *path)
    )


def _dnd_window(info: RobotInfo) -> tuple[str, str] | None:
    """The do-not-disturb time as ``("20:30", "08:00")``."""
    window = dig(
        info.extra.get("USER_MODE_CONFIG"), "user_mode_config", "not_disturb_config", "time", 0
    )
    if not isinstance(window, dict):
        return None
    start, end = clock(window.get("start") or {}), clock(window.get("end") or {})
    return (start, end) if start and end else None


def _dnd_attrs(info: RobotInfo) -> dict[str, Any] | None:
    window = _dnd_window(info)
    return {"start": window[0], "end": window[1]} if window else None


def _dnd_now(info: RobotInfo) -> bool | None:
    enabled = _setting("not_disturb_config", "enable")(info)
    if enabled is None:
        return None
    window = _dnd_window(info)
    return bool(enabled and window and in_daily_window(*window, info.now))


BINARY_SENSORS: list[RobotStatusBinarySensorDescription] = [
    RobotStatusBinarySensorDescription(
        key="last_mow_aborted",
        translation_key="last_mow_aborted",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_flag("last_mow_abstract", "abnormal_end"),
    ),
    RobotStatusBinarySensorDescription(
        key="rain_protection",
        translation_key="rain_protection",
        icon="mdi:weather-rainy",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_setting("rainfall_config", "enable"),
    ),
    RobotStatusBinarySensorDescription(
        key="do_not_disturb",
        translation_key="do_not_disturb",
        icon="mdi:minus-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_setting("not_disturb_config", "enable"),
        attrs_fn=_dnd_attrs,
    ),
    RobotStatusBinarySensorDescription(
        key="do_not_disturb_active",
        translation_key="do_not_disturb_active",
        icon="mdi:sleep",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_dnd_now,
        attrs_fn=_dnd_attrs,
    ),
    RobotStatusBinarySensorDescription(
        key="anti_theft_enabled",
        translation_key="anti_theft_enabled",
        icon="mdi:shield-lock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_setting("anti_theft_config", "enable"),
    ),
    RobotStatusBinarySensorDescription(
        key="local_connection",
        translation_key="local_connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda info: info.local_connected,
    ),
    RobotStatusBinarySensorDescription(
        key="obstacle_avoidance",
        translation_key="obstacle_avoidance",
        icon="mdi:wall",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_flag("navigation", "ai_obs_cmd", "generic_obs_avoidance"),
    ),
    RobotStatusBinarySensorDescription(
        key="object_recognition",
        translation_key="object_recognition",
        icon="mdi:eye-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_flag("navigation", "ai_obs_cmd", "class_obs_avoidance"),
    ),
    RobotStatusBinarySensorDescription(
        key="edge_cutter",
        translation_key="edge_cutter",
        icon="mdi:content-cut",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_flag("hardware", "cutter_info", "has_edge_cutter"),
    ),
    RobotStatusBinarySensorDescription(
        key="safety_lock",
        translation_key="safety_lock",
        icon="mdi:lock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_flag("hardware", "safety_lock_status"),
    ),
    RobotStatusBinarySensorDescription(
        key="map_editing",
        translation_key="map_editing",
        icon="mdi:map-marker-path",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_flag("navigation", "map_editing"),
    ),
    RobotStatusBinarySensorDescription(
        key="path_obstacle_detection",
        translation_key="path_obstacle_detection",
        icon="mdi:road-variant",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_setting("nav_common_config", "path_detect_avoid"),
    ),
    RobotStatusBinarySensorDescription(
        key="edge_camera_avoidance",
        translation_key="edge_camera_avoidance",
        icon="mdi:camera-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_setting("nav_common_config", "edge_vision_avoid"),
    ),
    RobotStatusBinarySensorDescription(
        key="passage_camera_avoidance",
        translation_key="passage_camera_avoidance",
        icon="mdi:camera-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_setting("nav_common_config", "channel_vision_avoid"),
    ),
    RobotStatusBinarySensorDescription(
        key="obstacle_photo_privacy",
        translation_key="obstacle_photo_privacy",
        icon="mdi:image-off-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_setting("nav_common_config", "obstacle_image_privacy"),
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mower's binary sensors."""
    async_add_entities(
        RoborockMowerBinarySensor(coordinator, description)
        for coordinator in entry.runtime_data.coordinators
        for description in BINARY_SENSORS
    )


class RoborockMowerBinarySensor(RoborockMowerEntity, BinarySensorEntity):
    """A yes/no value of the mower's full status."""

    entity_description: RobotStatusBinarySensorDescription

    def __init__(
        self,
        coordinator: RoborockMowerCoordinator,
        description: RobotStatusBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._device.duid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.robot_info)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.robot_info)
