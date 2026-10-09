"""The Roborock Mower integration."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
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
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_AREA_IDS,
    ATTR_AREA_NAMES,
    ATTR_CONTAINS,
    ATTR_DEVICE_ID,
    ATTR_FROM_APP,
    ATTR_MAP_NAME,
    ATTR_PAYLOAD,
    ATTR_QUERY_TYPE,
    ATTR_QUERY_TYPES,
    ATTR_WAIT,
    CONF_BASE_URL,
    CONF_USER_DATA,
    DOMAIN,
    PLATFORMS,
    SERVICE_LIST_AREAS,
    SERVICE_MOW_AREAS,
    SERVICE_QUERY,
    SERVICE_SCAN_QUERIES,
    SERVICE_APP_STRINGS,
    SERVICE_SAVE_MAP_DATA,
)
from .app_plugin import async_find_query_names, async_find_strings
from .coordinator import MowerConfigEntry, MowerRuntimeData, RoborockMowerCoordinator
from .home_data import SOURCE_CLOUD, HomeDataProvider
from .map_data import PROTOCOL_STATUS_STREAM, pose_from_stream, readable_frame
from .mower_api import (
    MAP_RPC_METHODS,
    MowerApi,
    MowerCommandRejected,
    is_mower,
    parse_dps_push,
    redact_dps,
)
from .robot_status import (
    FRAME_SHOW_LIMIT,
    dig,
    redact_private,
    shorten_long_strings,
)
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
        vol.Optional(ATTR_FROM_APP, default=True): cv.boolean,
    }
)

# Read-only query names tried by the scan_queries action when neither given
# nor found in the app plugin: guesses for data the app shows but
# GET_ROBOT_STATUS lacks (consumables, statistics, schedules, rain, ...).
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
SCAN_QUERY_LIMIT = 300

_APP_STRINGS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Required(ATTR_CONTAINS): vol.All(
            cv.ensure_list, [vol.All(cv.string, vol.Length(min=3))]
        ),
    }
)

_SAVE_MAP_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_WAIT, default=20): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=300)
        ),
    }
)
# save_map_data: map data asked through the map channel (cloud, encrypted
# answer), plus the map list asked normally. All raw messages are kept too.
MAP_BLOB_QUERIES = ("GET_FULL_MAP", "GET_MAP_MOW_SNAPSHOT", "GET_MAP_DIFFS")
MAP_INFO_QUERIES = ("GET_MAP_ABSTRACTS",)
MAP_TIMEOUT = 30

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


def _protocol_code(protocol: Any) -> int | None:
    try:
        return int(protocol)
    except (TypeError, ValueError):
        return None


def _make_push_handler(
    coordinator: RoborockMowerCoordinator, mower_api: MowerApi, duid: str
) -> Callable[[Any], None]:
    """Build an MQTT callback that merges live DPS pushes into the coordinator."""

    def _handle(message: Any) -> None:
        protocol = getattr(message, "protocol", None)
        mower_api.note_frame(protocol, getattr(message, "payload", None))
        if _protocol_code(protocol) == PROTOCOL_STATUS_STREAM and (
            pose := pose_from_stream(getattr(message, "payload", None))
        ) is not None:
            # Live position while mowing (the frame's GPS is never read).
            coordinator.hass.loop.call_soon_threadsafe(coordinator.note_pose, pose)
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


async def _app_query_names(
    coordinator: RoborockMowerCoordinator,
) -> tuple[list[str], list[str]]:
    """Query names from the official app plugin (via the user's own account)."""
    runtime = coordinator.config_entry.runtime_data
    if runtime.api_client is None or runtime.user_data is None:
        return [], ["no account data"]
    return await async_find_query_names(
        coordinator.hass, runtime.api_client, runtime.user_data, coordinator.product
    )


async def _scan(coordinator: RoborockMowerCoordinator, names: list[str]) -> dict[str, Any]:
    """Ask the mower each query, one at a time (it is a small device)."""
    answered: dict[str, Any] = {}
    acknowledged: list[str] = []
    rejected: list[str] = []
    failed: dict[str, str] = {}
    for name in names:
        try:
            async with asyncio.timeout(SCAN_QUERY_TIMEOUT):
                answer = await coordinator.mower_api.query(name)
        except TimeoutError:
            failed[name] = "no answer"
        except MowerCommandRejected:
            rejected.append(name)
        except RoborockException as err:
            failed[name] = str(err)[:200]
        else:
            if answer in (["ok"], "ok"):
                acknowledged.append(name)  # accepted, but no data in the answer
            else:
                answered[name] = shorten_long_strings(redact_private(answer))
    return {
        "tried": len(names),
        "answered": answered,
        "acknowledged": acknowledged,
        "rejected": rejected,
        "failed": failed,
    }


async def _capture_map_data(
    hass: HomeAssistant, coordinator: RoborockMowerCoordinator, wait: int
) -> dict[str, Any]:
    """Keep every message for ``wait`` seconds after asking for the map.

    The messages come back in the answer -- JSON unpacked and redacted,
    protobuf decoded with every 64-bit value (the GPS position) hidden,
    anything else as base64 -- and are also saved unchanged in
    ``<config>/roborock_mower/map_<time>/`` (``NNN_p<protocol>.bin`` plus the
    query answers). The diagnostics only list what arrived. The saved files
    and the map show your garden: share them privately, not publicly.
    """
    api = coordinator.mower_api
    names = dig(coordinator.robot_status, "map_names") or []
    map_name = names[0] if names and isinstance(names[0], str) else ""
    api.start_capture()
    answers: dict[str, Any] = {}
    maps: dict[str, bytes] = {}
    try:
        for name in MAP_INFO_QUERIES:
            try:
                async with asyncio.timeout(SCAN_QUERY_TIMEOUT):
                    answers[name] = redact_private(await api.query(name))
            except (TimeoutError, RoborockException) as err:
                answers[name] = {"error": str(err)[:200] or type(err).__name__}
        for name in MAP_BLOB_QUERIES:
            try:
                async with asyncio.timeout(MAP_TIMEOUT):
                    data = await api.get_map_data(name, map_name)
            except (TimeoutError, RoborockException) as err:
                answers[name] = {"error": str(err)[:200] or "no answer"}
                continue
            if data:
                maps[name] = data
                answers[name] = {"bytes": len(data)} | readable_frame(data)
            else:
                answers[name] = {"error": "no map data in the answer"}
        # The app's map RPCs, through the map channel and the normal one.
        for method in MAP_RPC_METHODS:
            for map_channel in (True, False):
                key = f"{method}_{'map' if map_channel else 'rpc'}"
                try:
                    async with asyncio.timeout(MAP_TIMEOUT):
                        result = await api.get_map_rpc(method, map_channel=map_channel)
                except (TimeoutError, RoborockException) as err:
                    answers[key] = {"error": str(err)[:200] or "no answer"}
                    continue
                if isinstance(result, bytes):
                    maps[key] = result
                    answers[key] = {"bytes": len(result)} | readable_frame(result)
                else:
                    answers[key] = shorten_long_strings(
                        redact_private(result), FRAME_SHOW_LIMIT
                    )
        await asyncio.sleep(wait)
    finally:
        frames = api.stop_capture()
    folder = Path(hass.config.path(DOMAIN, f"map_{dt_util.now():%Y%m%d_%H%M%S}"))
    files = await hass.async_add_executor_job(
        _write_map_data, folder, frames, answers, maps
    )
    result = {
        "folder": str(folder),
        "map_name": map_name,
        "files": [entry["file"] for entry in files],
        "note": "Map data may show your garden; share it privately.",
        "answers": answers,
        "messages": [
            {"time": received, "protocol": protocol, "bytes": len(payload)}
            | readable_frame(payload)
            for received, protocol, payload in frames
        ],
    }
    api.last_capture = result
    return result


def _write_map_data(
    folder: Path,
    frames: list[tuple[str, int, bytes]],
    answers: dict[str, Any],
    maps: dict[str, bytes],
) -> list[dict[str, Any]]:
    folder.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for query, data in maps.items():
        name = f"{query}_map.bin"
        (folder / name).write_bytes(data)
        files.append({"file": name, "bytes": len(data)})
    for index, (received, protocol, payload) in enumerate(frames):
        name = f"{index:03d}_p{protocol}.bin"
        (folder / name).write_bytes(payload)
        files.append(
            {"file": name, "protocol": protocol, "bytes": len(payload), "time": received}
        )
    for query, answer in answers.items():
        if isinstance(answer, str):
            try:
                data = base64.b64decode(answer, validate=True)
                name = f"{query}.bin"
            except ValueError:
                data, name = answer.encode(), f"{query}.txt"
        else:
            data, name = json.dumps(answer, indent=2).encode(), f"{query}.json"
        (folder / name).write_bytes(data)
        files.append({"file": name, "bytes": len(data)})
    return files


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
        given = [name.strip().upper() for name in call.data.get(ATTR_QUERY_TYPES) or []]
        if bad := [name for name in given if not name.startswith("GET_")]:
            raise ServiceValidationError(
                f"Only read-only GET_* queries are allowed: {', '.join(bad)}"
            )
        result: dict[str, Any] = {}
        for device_id in call.data[ATTR_DEVICE_ID]:
            for coordinator in _coordinators_for_device(hass, device_id):
                report: dict[str, Any] = {}
                names = given
                if given:
                    report["source"] = "given"
                elif call.data[ATTR_FROM_APP]:
                    names, report["app_errors"] = await _app_query_names(coordinator)
                    report["source"] = "app" if names else "built-in list"
                if not names:
                    names = list(SCAN_QUERY_CANDIDATES)
                    report.setdefault("source", "built-in list")
                report.update(await _scan(coordinator, names[:SCAN_QUERY_LIMIT]))
                result[coordinator.device.duid] = report
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY,
        _query,
        schema=_QUERY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    async def _app_strings(call: ServiceCall) -> ServiceResponse:
        result: dict[str, Any] = {}
        for device_id in call.data[ATTR_DEVICE_ID]:
            for coordinator in _coordinators_for_device(hass, device_id):
                runtime = coordinator.config_entry.runtime_data
                if runtime.api_client is None or runtime.user_data is None:
                    raise HomeAssistantError("No Roborock account data")
                found, errors = await async_find_strings(
                    hass,
                    runtime.api_client,
                    runtime.user_data,
                    coordinator.product,
                    call.data[ATTR_CONTAINS],
                )
                result[coordinator.device.duid] = {"strings": found, "errors": errors}
        return result

    async def _save_map_data(call: ServiceCall) -> ServiceResponse:
        result: dict[str, Any] = {}
        for device_id in call.data[ATTR_DEVICE_ID]:
            for coordinator in _coordinators_for_device(hass, device_id):
                result[coordinator.device.duid] = await _capture_map_data(
                    hass, coordinator, call.data[ATTR_WAIT]
                )
        return result

    hass.services.async_register(
        DOMAIN,
        SERVICE_APP_STRINGS,
        _app_strings,
        schema=_APP_STRINGS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_MAP_DATA,
        _save_map_data,
        schema=_SAVE_MAP_DATA_SCHEMA,
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
        api_client=client,
        user_data=user_data,
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
    retired = (
        # 0.1.1: the "Mow Area" select became one "mow zone" button per area.
        ("select", "mow_area"),
        # 0.3.0: read-only views of settings that now have a switch / number.
        ("binary_sensor", "keep_edge"),
        ("sensor", "mow_direction"),
        # 0.3.4: direction mode and rotation angle became selects.
        ("sensor", "direction_mode"),
        ("sensor", "rotation_angle"),
        # 0.4.0: the number of passes became a number to set.
        ("sensor", "mow_passes"),
    )
    for coordinator in runtime.coordinators:
        for platform, key in retired:
            if entity_id := registry.async_get_entity_id(
                platform, DOMAIN, f"{coordinator.device.duid}_{key}"
            ):
                registry.async_remove(entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: MowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: MowerConfigEntry) -> None:
    """Delete the persistent cache when the config entry is removed."""
    await MowerCacheStore(hass, entry.entry_id).async_remove()
