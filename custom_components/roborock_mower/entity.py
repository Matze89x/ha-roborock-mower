"""Base entity for Roborock Mower integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .mower_api import MowerStatus
from .robot_status import RobotInfo
from .vendor.roborock.exceptions import RoborockException


def remove_entity(hass: HomeAssistant, platform: str, unique_id: str) -> None:
    """Drop an entity an earlier version created that this mower can't fill."""
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
        registry.async_remove(entity_id)


class RoborockMowerEntity(CoordinatorEntity[RoborockMowerCoordinator]):
    """Base entity for a Roborock mower device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._device = coordinator.device
        self._product = coordinator.product

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.duid)},
            name=self._device.name,
            manufacturer="Roborock",
            model=self._product.name or self._product.model,
            model_id=self._product.model,
            serial_number=self._device.sn,
            sw_version=self._device.fv,
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def status(self) -> MowerStatus:
        return self.coordinator.data

    @property
    def robot_info(self) -> RobotInfo:
        """The mower's full status and settings (empty until it answered)."""
        coordinator = self.coordinator
        return RobotInfo(
            status=coordinator.robot_status or {},
            preference=coordinator.mow_preference or {},
            now=dt_util.now(),
            updated=coordinator.robot_status_time,
            local_connected=getattr(
                coordinator.mower_api.channel, "is_local_connected", None
            ),
        )

    async def _async_send(
        self, action: str, command: Callable[[], Awaitable[Any]]
    ) -> None:
        """Run a mower command, surfacing failures as a readable HA error.

        The resulting state change arrives over the MQTT DPS push, so no
        (rate-limited) refresh is requested here.
        """
        try:
            await command()
        except RoborockException as err:
            raise HomeAssistantError(f"{action} failed: {err}") from err
