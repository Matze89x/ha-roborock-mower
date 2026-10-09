"""Data update coordinator for Roborock Mower."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    HOME_DATA_REUSE_AGE,
    STALE_SNAPSHOT_REFRESH_INTERVAL,
    UPDATE_INTERVAL,
)
from .home_data import HomeDataProvider
from .mower_api import MowerApi, MowerStatus, redact_dps
from .storage import MowerCacheStore
from .vendor.roborock.data import HomeDataDevice, HomeDataProduct
from .vendor.roborock.exceptions import RoborockException, RoborockInvalidCredentials
from .vendor.roborock.mqtt.session import MqttSession
from .vendor.roborock.web_api import UserWebApiClient

_LOGGER = logging.getLogger(__name__)


@dataclass
class MowerRuntimeData:
    """Everything a loaded config entry owns."""

    coordinators: list[RoborockMowerCoordinator]
    home_data: HomeDataProvider
    cache: MowerCacheStore
    mqtt_session: MqttSession
    web_api: UserWebApiClient
    unsubscribes: list[Callable[[], None]] = field(default_factory=list)


type MowerConfigEntry = ConfigEntry[MowerRuntimeData]


class RoborockMowerCoordinator(DataUpdateCoordinator[MowerStatus]):
    """Coordinator for a single Roborock mower.

    Status is push-first: live DPS arrive over MQTT and are fed in via
    ``async_set_updated_data``. The periodic poll of the cloud ``home_data``
    snapshot is only a safety net for missed pushes. Its rate limit is shared
    with the official Roborock integration, so it runs rarely, and a failed poll
    keeps the last known (push-fed) state instead of marking the mower
    unavailable.

    When setup had to use a stored snapshot (``stale``), pushes missed while
    Home Assistant was down are not reflected yet, so one real cloud refresh is
    done soon after setup before falling back to the normal interval.
    """

    config_entry: MowerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MowerConfigEntry,
        device: HomeDataDevice,
        product: HomeDataProduct,
        mower_api: MowerApi,
        home_data: HomeDataProvider,
        stale: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{device.duid}",
            update_interval=(
                STALE_SNAPSHOT_REFRESH_INTERVAL if stale else UPDATE_INTERVAL
            ),
            always_update=False,
        )
        self.device = device
        self.product = product
        self.mower_api = mower_api
        self._home_data = home_data
        self._needs_cloud_refresh = stale
        self._poll_failing = False

    async def _async_update_data(self) -> MowerStatus:
        api = self.mower_api
        try:
            home_data = await self._home_data.async_refresh(
                None if self._needs_cloud_refresh else HOME_DATA_REUSE_AGE
            )
        except RoborockInvalidCredentials as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except RoborockException as err:
            if not self._poll_failing:
                _LOGGER.warning(
                    "[%s] Cloud status poll failed (%s); keeping the live MQTT state",
                    self.device.duid,
                    err,
                )
            self._poll_failing = True
            return api.status

        if self._poll_failing:
            _LOGGER.info("[%s] Cloud status poll recovered", self.device.duid)
        self._poll_failing = False
        if self._needs_cloud_refresh:
            self._needs_cloud_refresh = False
            self.update_interval = UPDATE_INTERVAL
        status = api.apply_home_data(home_data)
        if status.raw_dps:
            _LOGGER.debug(
                "[%s] Mower DPS: %s", self.device.duid, redact_dps(status.raw_dps)
            )
        return status
