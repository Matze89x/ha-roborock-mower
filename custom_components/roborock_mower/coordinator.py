"""Data update coordinator for Roborock Mower."""

from __future__ import annotations

import logging

from roborock.data import HomeDataDevice, HomeDataProduct
from roborock.exceptions import RoborockException

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, UPDATE_INTERVAL
from .mower_api import MowerApi, MowerStatus

_LOGGER = logging.getLogger(__name__)


class RoborockMowerCoordinator(DataUpdateCoordinator[MowerStatus]):
    """Coordinator that polls a single Roborock mower device."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: HomeDataDevice,
        product: HomeDataProduct,
        mower_api: MowerApi,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{device.duid}",
            update_interval=UPDATE_INTERVAL,
        )
        self.device = device
        self.product = product
        self.mower_api = mower_api

    async def _async_update_data(self) -> MowerStatus:
        try:
            status = await self.mower_api.poll_status()
        except RoborockException as err:
            raise UpdateFailed(f"Error communicating with mower: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

        if status.raw_dps:
            _LOGGER.debug("[%s] Mower DPS: %s", self.device.duid, status.raw_dps)

        return status
