"""The Roborock Mower integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_AREA_IDS,
    ATTR_AREA_NAMES,
    ATTR_DEVICE_ID,
    ATTR_MAP_NAME,
    ATTR_PAYLOAD,
    ATTR_QUERY_TYPE,
    ATTR_QUERY_TYPES,
    CONF_BASE_URL,
    CONF_USER_DATA,
    DOMAIN,
    PLATFORMS,
    SERVICE_LIST_AREAS,
    SERVICE_MOW_AREAS,
    SERVICE_QUERY,
    SERVICE_SCAN_QUERIES,
)
from .coordinator import MowerConfigEntry, MowerRuntimeData, RoborockMowerCoordinator
from .home_data import SOURCE_CLOUD, HomeDataProvider
from .mower_api import MowerApi, is_mower, parse_dps_push, redact_dps
from .robot_status import redact_private
from .storage import MowerCacheStore
from .vendor.roborock.data import HomeData, UserData
from .vendor.roborock.devices.cache import DeviceCache
from .vendor.roborock.devices.rpc.v1_channel import create_v1_channel
from .vendor.roborock.exceptions import RoborockException, RoborockInvalidCredentials
from .vendor.roborock.mqtt.roborock_session import create_lazy_mqtt_session
from .vendor.roborock.mqtt.session import MqttSessionUnauthorized
from .vendor.roborock.protocol import create_mqtt_params
from .vendor.roborock.roborock_message import RoborockMessageProtocol
from .vendor.roborock.web_api import RoborockApiClient, UserWebApiClient

_LOGGER = logging.getLogger(__name__)

# The bundled python-roborock logs every raw message at DEBUG -- local pings,
# map/path frames while mowing (hundreds of MB per session) and the mower's
# Wi-Fi details. Keep it at INFO unless the user configures it explicitly, so
# enabling debug logging for this integration stays readable.
_VENDOR_LOGGER = logging.getLogger(f"{__name__}.vendor")
if _VENDOR_LOGGER.level == logging.NOTSET:
    _VENDOR_LOGGER.setLevel(logging.INFO)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_MOW_AREAS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_AREA_IDS): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional(ATTR_AREA_NAMES): vol.All(cv.ensure_list, [cv.string]),
    }
)

_QUERY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_QUERY_TYPE): cv.string,
        vol.Optional(ATTR_PAYLOAD, default={}): dict,
    }
)

_SCAN_QUERIES_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_QUERY_TYPES): vol.All(cv.ensure_list, [cv.string]),
    }
)

# Read-only query names tried by the scan_queries action when none are given:
# guesses for data the app shows but GET_ROBOT_STATUS lacks (blade and other
# consumables, mowing statistics, schedules, rain and wildlife protection).
SCAN_QUERY_CANDIDATES = (
    "GET_CONSUMABLES",
    "GET_CONSUMABLE",
    "GET_CONSUMABLE_INFO",
    "GET_STATISTICS",
    "GET_MOW_STATISTICS",
    "GET_TOTAL_STATISTICS",
    "GET_MOW_SUMMARY",
    "GET_MOW_RECORDS",
    "GET_MOW_HISTORY",
    "GET_PLANS",
    "GET_MOW_PLANS",
    "GET_PLAN_LIST",
    "GET_SCHEDULE",
    "GET_RAIN_CONFIG",
    "GET_RAIN_DETECTION",
    "GET_WILDLIFE_PROTECTION",
    "GET_DND",
    "GET_ROBOT_CONFIG",
    "GET_ROBOT_INFO",
    "GET_DEVICE_INFO",
    "GET_VERSION",
    "GET_NETWORK_INFO",
    "GET_ANTI_THEFT",
    "GET_CUTTER_INFO",
)
SCAN_QUERY_TIMEOUT = 6

_LIST_AREAS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_MAP_NAME, default=""): cv.string,
    }
)


# Local keep-alive pings (every 10 s) -- counted, never logged.
_KEEPALIVE_PROTOCOLS = frozenset(
    {RoborockMessageProtocol.PING_REQUEST, RoborockMessageProtocol.PING_RESPONSE}
)


def _make_push_handler(
    coordinator: RoborockMowerCoordinator, mower_api: MowerApi, duid: str
) -> Callable[[Any], None]:
    """Build an MQTT callback that merges live DPS pushes into the coordinator."""

    def _handle(message: Any) -> None:
        protocol = getattr(message, "protocol", None)
        if mower_api.note_message(protocol) and protocol not in _KEEPALIVE_PROTOCOLS:
            payload = getattr(message, "payload", None) or b""
            _LOGGER.debug(
                "[%s] First %s message from the mower (%d bytes)",
                duid,
                getattr(protocol, "name", protocol),
                len(payload),
            )
        dps = parse_dps_push(message)
        if not dps:
            return

        def _update() -> None:
            _LOGGER.debug("[%s] DPS push: %s", duid, redact_dps(dps))
            coordinator.async_set_updated_data(mower_api.apply_push(dps))
            coordinator.note_push(mower_api.last_changes)

        # Apply on the event loop so DPS state isn't mutated from two threads.
        coordinator.hass.loop.call_soon_threadsafe(_update)

    return _handle


def _mower_devices(home_data: HomeData) -> list[tuple[Any, Any]]:
    return [
        (device, product)
        for device, product in home_data.device_products.values()
        if is_mower(product)
    ]


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
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        for coordinator in entry.runtime_data.coordinators
        if coordinator.device.duid in duids
    ]
    if not matches:
        raise HomeAssistantError(f"Mower for device {device_id} is not loaded")
    return matches


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the zone-mowing services once for the integration."""

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
    async def _query(call: ServiceCall) -> ServiceResponse:
        result: dict[str, Any] = {}
        for device_id in call.data[ATTR_DEVICE_ID]:
            for coordinator in _coordinators_for_device(hass, device_id):
                try:
                    answer = await coordinator.mower_api.query(
                        call.data[ATTR_QUERY_TYPE], **call.data[ATTR_PAYLOAD]
                    )
                except ValueError as err:
                    raise ServiceValidationError(str(err)) from err
                except RoborockException as err:
                    raise HomeAssistantError(f"Query failed: {err}") from err
                # Never hand out the mower's position or network identifiers.
                result[coordinator.device.duid] = {"answer": redact_private(answer)}
        return result

    async def _scan_queries(call: ServiceCall) -> ServiceResponse:
        names = [
            name.strip().upper()
            for name in call.data.get(ATTR_QUERY_TYPES) or SCAN_QUERY_CANDIDATES
        ]
        if bad := [name for name in names if not name.startswith("GET_")]:
            raise ServiceValidationError(
                f"Only read-only GET_* queries are allowed: {', '.join(bad)}"
            )
        result: dict[str, Any] = {}
        for device_id in call.data[ATTR_DEVICE_ID]:
            for coordinator in _coordinators_for_device(hass, device_id):
                answered: dict[str, Any] = {}
                failed: dict[str, str] = {}
                # One at a time: the mower is a small device.
                for name in names:
                    try:
                        async with asyncio.timeout(SCAN_QUERY_TIMEOUT):
                            answer = await coordinator.mower_api.query(name)
                    except TimeoutError:
                        failed[name] = "no answer"
                    except RoborockException as err:
                        failed[name] = str(err)[:200]
                    else:
                        answered[name] = redact_private(answer)
                result[coordinator.device.duid] = {
                    "answered": answered,
                    "failed": failed,
                }
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY,
        _query,
        schema=_QUERY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_QUERIES,
        _scan_queries,
        schema=_SCAN_QUERIES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_AREAS,
        _list_areas,
        schema=_LIST_AREAS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


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
    cache = MowerCacheStore(hass, entry.entry_id)
    home_data_provider = HomeDataProvider(client, user_data, cache)
    previous_home_data = await home_data_provider.async_get_cached()

    try:
        home_data = await home_data_provider.async_get_for_setup()
    except RoborockInvalidCredentials as err:
        raise ConfigEntryAuthFailed(
            "Roborock login expired; please re-authenticate"
        ) from err
    except RoborockException as err:
        raise ConfigEntryNotReady(f"Failed to fetch home data: {err}") from err

    mower_devices = _mower_devices(home_data)
    fresh = home_data_provider.source == SOURCE_CLOUD
    if not mower_devices and previous_home_data is not None:
        # The cloud answered but listed no mower. Prefer the previous snapshot if
        # it had one (transient empty answers happen), otherwise stop below:
        # retrying would only burn the account's home_data budget.
        mower_devices = _mower_devices(previous_home_data)
        fresh = False
        if mower_devices:
            _LOGGER.warning(
                "Roborock cloud listed no mower this time; using the "
                "previously stored device list"
            )
    if not mower_devices:
        _LOGGER.error(
            "No mower found on Roborock account %s (base_url=%s). Devices on "
            "this account: %s. If this list is empty the login resolved to the "
            "wrong region; remove the integration and add it again with the "
            "correct region",
            username,
            base_url,
            [
                (device.name, getattr(product, "model", None))
                for device, product in home_data.device_products.values()
            ],
        )
        raise ConfigEntryError("No mower devices found on this Roborock account")

    mqtt_params = create_mqtt_params(user_data.rriot)
    mqtt_session = await create_lazy_mqtt_session(mqtt_params)
    runtime = MowerRuntimeData(
        coordinators=[],
        home_data=home_data_provider,
        cache=cache,
        mqtt_session=mqtt_session,
        web_api=web_api,
    )

    closed = False

    async def _shutdown(_: Event | None = None) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        for unsubscribe in runtime.unsubscribes:
            unsubscribe()
        runtime.unsubscribes.clear()
        await mqtt_session.close()
        # Persist the mowers' network info learned during this run.
        await cache.async_flush()

    try:
        for device, product in mower_devices:
            channel = create_v1_channel(
                user_data,
                mqtt_params,
                mqtt_session,
                device,
                DeviceCache(device.duid, cache),
            )
            mower_api = MowerApi(
                product, channel, web_api, device.duid, device.device_status
            )
            mower_api.online = getattr(device, "online", None)
            coordinator = RoborockMowerCoordinator(
                hass,
                entry,
                device,
                product,
                mower_api,
                home_data_provider,
                stale=not fresh,
            )
            runtime.unsubscribes.append(
                await channel.subscribe(
                    _make_push_handler(coordinator, mower_api, device.duid)
                )
            )
            # Seed state from the home_data snapshot instead of polling again;
            # live updates then arrive via MQTT push.
            coordinator.async_set_updated_data(mower_api.status)
            runtime.coordinators.append(coordinator)
    except MqttSessionUnauthorized as err:
        await _shutdown()
        raise ConfigEntryAuthFailed(
            "Roborock MQTT broker rejected the login; please re-authenticate"
        ) from err
    except RoborockException as err:
        # Typically the network is not up yet right after a reboot, or the
        # broker is throttling reconnects. Home Assistant retries the setup and
        # the cached home_data spares the rate-limited cloud endpoint.
        await _shutdown()
        raise ConfigEntryNotReady(
            f"Could not connect to the Roborock MQTT broker: {err}"
        ) from err
    except BaseException:
        await _shutdown()
        raise

    entry.runtime_data = runtime
    _remove_retired_entities(hass, runtime)

    stop_fired = False

    async def _on_stop(event: Event) -> None:
        nonlocal stop_fired
        stop_fired = True
        await _shutdown(event)

    remove_stop_listener = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, _on_stop
    )

    def _remove_stop_listener() -> None:
        # A once-listener that already fired is gone; removing it again makes
        # Home Assistant log "Unable to remove unknown job listener" (seen when
        # Home Assistant restarts while this entry is still setting up).
        if not stop_fired:
            remove_stop_listener()

    entry.async_on_unload(_remove_stop_listener)
    entry.async_on_unload(_shutdown)

    _LOGGER.debug(
        "Set up %d mower(s); home_data source=%s",
        len(runtime.coordinators),
        home_data_provider.source,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for coordinator in runtime.coordinators:
        entry.async_create_background_task(
            hass,
            coordinator.async_poll_robot_status(),
            f"{DOMAIN}_robot_status_{coordinator.device.duid}",
        )
    return True


def _remove_retired_entities(hass: HomeAssistant, runtime: MowerRuntimeData) -> None:
    """Drop entities earlier versions created that no longer exist."""
    registry = er.async_get(hass)
    for coordinator in runtime.coordinators:
        # 0.1.1: the "Mow Area" select became one "mow zone" button per area.
        if entity_id := registry.async_get_entity_id(
            "select", DOMAIN, f"{coordinator.device.duid}_mow_area"
        ):
            registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: MowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: MowerConfigEntry) -> None:
    """Delete the persistent cache when the config entry is removed."""
    await MowerCacheStore(hass, entry.entry_id).async_remove()
