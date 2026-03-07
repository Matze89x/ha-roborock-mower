"""Base entity for Roborock Mower integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RoborockMowerCoordinator
from .mower_api import MowerStatus


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
            model=self._product.model,
            sw_version=self._device.fv,
        )

    @property
    def status(self) -> MowerStatus:
        return self.coordinator.data
