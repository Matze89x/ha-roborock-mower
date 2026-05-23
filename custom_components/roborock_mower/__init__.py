"""The Roborock Mower integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from roborock.data import RoborockCategory, UserData
from roborock.devices.cache import DeviceCache, NoCache
from roborock.devices.rpc.v1_channel import create_v1_channel
from roborock.exceptions import RoborockException
from roborock.mqtt.roborock_session import create_lazy_mqtt_session
from roborock.mqtt.session import MqttSession
from roborock.protocol import create_mqtt_params
from roborock.web_api import RoborockApiClient, UserWebApiClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BASE_URL, CONF_USER_DATA, DOMAIN, PLATFORMS
from .coordinator import RoborockMowerCoordinator
from .mower_api import MowerApi, parse_dps_push

_LOGGER = logging.getLogger(__name__)

type MowerConfigEntry = ConfigEntry


def _make_push_handler(
    coordinator: RoborockMowerCoordinator, mower_api: MowerApi, duid: str
) -> Callable[[Any], None]:
    """Build an MQTT callback that merges live DPS pushes into the coordinator."""

    def _handle(message: Any) -> None:
        dps = parse_dps_push(message)
        if not dps:
            return

        def _update() -> None:
            _LOGGER.debug("[%s] DPS push: %s", duid, dps)
            coordinator.async_set_updated_data(mower_api.apply_push(dps))

        # Apply on the event loop so DPS state isn't mutated from two threads.
        coordinator.hass.loop.call_soon_threadsafe(_update)

    return _handle


async def async_setup_entry(hass: HomeAssistant, entry: MowerConfigEntry) -> bool:
    """Set up Roborock Mower from a config entry."""
    user_data = UserData.from_dict(entry.data[CONF_USER_DATA])
    username = entry.data[CONF_USERNAME]
    base_url = entry.data.get(CONF_BASE_URL)

    client = RoborockApiClient(
        username,
        base_url=base_url,
        session=async_get_clientsession(hass),
    )
    web_api = UserWebApiClient(client, user_data)

    try:
        home_data = await web_api.get_home_data()
    except RoborockException as err:
        raise ConfigEntryNotReady(f"Failed to fetch home data: {err}") from err

    mower_devices = [
        (device, product)
        for _duid, (device, product) in home_data.device_products.items()
        if product.category == RoborockCategory.MOWER
    ]

    if not mower_devices:
        _LOGGER.warning("No mower devices found on account %s", username)
        raise ConfigEntryNotReady("No mower devices found on this account")

    cache = NoCache()
    mqtt_params = create_mqtt_params(user_data.rriot)
    mqtt_session: MqttSession = await create_lazy_mqtt_session(mqtt_params)

    coordinators: list[RoborockMowerCoordinator] = []
    unsubscribes: list[Callable[[], None]] = []

    for device, product in mower_devices:
        device_cache = DeviceCache(device.duid, cache)
        channel = create_v1_channel(
            user_data, mqtt_params, mqtt_session, device, device_cache
        )
        mower_api = MowerApi(
            product, channel, web_api, device.duid, device.device_status
        )
        coordinator = RoborockMowerCoordinator(hass, device, product, mower_api)

        unsubscribes.append(
            await channel.subscribe(
                _make_push_handler(coordinator, mower_api, device.duid)
            )
        )

        await coordinator.async_config_entry_first_refresh()
        coordinators.append(coordinator)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinators

    closed = False

    async def _shutdown(_: Event | None = None) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        for unsubscribe in unsubscribes:
            unsubscribe()
        await mqtt_session.close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    )
    entry.async_on_unload(_shutdown)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MowerConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
