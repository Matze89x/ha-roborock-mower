"""Binary sensor platform for the Roborock mower.

Yes/no values of the mower's full status (``GET_ROBOT_STATUS``) and mowing
preferences. All diagnostic; only "last mow aborted" is enabled by default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
from .robot_status import RobotInfo, as_flag, dig


@dataclass(frozen=True, kw_only=True)
class RobotStatusBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[RobotInfo], bool | None]


def _flag(*path: str | int) -> Callable[[RobotInfo], bool | None]:
    return lambda info: as_flag(dig(info.status, *path))


BINARY_SENSORS: list[RobotStatusBinarySensorDescription] = [
    RobotStatusBinarySensorDescription(
        key="last_mow_aborted",
        translation_key="last_mow_aborted",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_flag("last_mow_abstract", "abnormal_end"),
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
        key="keep_edge",
        translation_key="keep_edge",
        icon="mdi:border-outside",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda info: as_flag(info.preference.get("keep_edge")),
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
