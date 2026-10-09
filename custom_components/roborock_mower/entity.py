"""Base entity for Roborock Mower integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .mower_api import MowerStatus
from .vendor.roborock.exceptions import RoborockException


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
