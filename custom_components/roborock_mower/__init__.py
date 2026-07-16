"""The Roborock Mower integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from roborock.data import UserData
from roborock.devices.cache import DeviceCache, NoCache
from roborock.devices.rpc.v1_channel import create_v1_channel
from roborock.exceptions import RoborockException
from roborock.mqtt.roborock_session import create_lazy_mqtt_session
from roborock.mqtt.session import MqttSession
from roborock.protocol import create_mqtt_params
from roborock.web_api import RoborockApiClient, UserWebApiClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ATTR_AREA_IDS,
    ATTR_AREA_NAMES,
    ATTR_DEVICE_ID,
    ATTR_MAP_NAME,
    CONF_BASE_URL,
    CONF_USER_DATA,
    DOMAIN,
    PLATFORMS,
    SERVICE_LIST_AREAS,
    SERVICE_MOW_AREAS,
)
from .coordinator import RoborockMowerCoordinator
from .mower_api import MowerApi, is_mower, parse_dps_push, redact_dps

_LOGGER = logging.getLogger(__name__)

type MowerConfigEntry = ConfigEntry

_MOW_AREAS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_AREA_IDS): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional(ATTR_AREA_NAMES): vol.All(cv.ensure_list, [cv.string]),
    }
)

_LIST_AREAS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_MAP_NAME, default=""): cv.string,
    }
)


def _make_push_handler(
    coordinator: RoborockMowerCoordinator, mower_api: MowerApi, duid: str
) -> Callable[[Any], None]:
    """Build an MQTT callback that merges live DPS pushes into the coordinator."""

    def _handle(message: Any) -> None:
        dps = parse_dps_push(message)
        if not dps:
            return

        def _update() -> None:
            _LOGGER.debug("[%s] DPS push: %s", duid, redact_dps(dps))
            coordinator.async_set_updated_data(mower_api.apply_push(dps))

        # Apply on the event loop so DPS state isn't mutated from two threads.
        coordinator.hass.loop.call_soon_threadsafe(_update)

    return _handle


def _coordinators_for_device(
    hass: HomeAssistant, device_id: str
) -> list[RoborockMowerCoordinator]:
    """Resolve a Home Assistant device id to this integration's coordinator(s)."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise HomeAssistantError(f"Unknown device: {device_id}")
    duids = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    if not duids:
        raise HomeAssistantError(f"Device {device_id} is not a Roborock mower")
    matches = [
        coordinator
        for coordinators in hass.data.get(DOMAIN, {}).values()
        for coordinator in coordinators
        if coordinator.device.duid in duids
    ]
    if not matches:
        raise HomeAssistantError(f"Mower for device {device_id} is not loaded")
    return matches


def _register_services(hass: HomeAssistant) -> None:
    """Register the zone-mowing services once for the integration."""
    if hass.services.has_service(DOMAIN, SERVICE_MOW_AREAS):
        return

    async def _mow_areas(call: ServiceCall) -> None:
        ids: list[int] = call.data[ATTR_AREA_IDS]
        names: list[str] = call.data.get(ATTR_AREA_NAMES, [])
        areas = [
            {"id": area_id, "name": names[i] if i < len(names) else ""}
            for i, area_id in enumerate(ids)
        ]
        for device_id in call.data[ATTR_DEVICE_ID]:
            for coordinator in _coordinators_for_device(hass, device_id):
                try:
                    await coordinator.mower_api.start_area_mow(areas)
                except RoborockException as err:
                    raise HomeAssistantError(f"Area mow failed: {err}") from err
                # State reflects back over the MQTT push (no rate-limited poll).

    async def _list_areas(call: ServiceCall) -> ServiceResponse:
        map_name: str = call.data[ATTR_MAP_NAME]
        result: dict[str, Any] = {}
        for device_id in call.data[ATTR_DEVICE_ID]:
            for coordinator in _coordinators_for_device(hass, device_id):
                api = coordinator.mower_api
                result[coordinator.device.duid] = {
                    "areas": await api.get_areas(),
                    "map_names": await api.get_map_names(),
                    "map_boundaries": await api.get_full_map(map_name),
                }
        return result

    hass.services.async_register(
        DOMAIN, SERVICE_MOW_AREAS, _mow_areas, schema=_MOW_AREAS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_AREAS,
        _list_areas,
        schema=_LIST_AREAS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


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
        if is_mower(product)
    ]

    if not mower_devices:
        seen = [
            (
                device.name,
                getattr(product, "model", None),
                str(getattr(product, "category", None)),
            )
            for _duid, (device, product) in home_data.device_products.items()
        ]
        _LOGGER.warning(
            "No mower devices found on account %s. Devices seen: %s", username, seen
        )
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

    _register_services(hass)

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
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_MOW_AREAS)
            hass.services.async_remove(DOMAIN, SERVICE_LIST_AREAS)
    return unload_ok
