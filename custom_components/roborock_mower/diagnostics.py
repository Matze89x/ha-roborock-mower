"""Diagnostics for the Roborock Mower integration.

Download from Settings > Devices & services > Roborock Mower > ... > Download
diagnostics. Credentials, the account e-mail, serial numbers, local keys and the
mower's GPS position are redacted. ``history`` lists the last commands, the
mower's answers and every data-point change with local timestamps -- enough to
follow a test run without a debug log.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_USERNAME
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CONF_BASE_URL, DOMAIN
from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .mower_api import DPS_GPS_COORDINATE, derive_activity, redact_dps
from .vendor import ROBOROCK_VERSION

TO_REDACT = {"gps_coordinate", "local_key", "sn", "duid", "lat", "lon"}

# Read-only queries asked live when diagnostics are downloaded, to discover data
# the integration does not decode yet (e.g. consumables, statistics).
PROBE_QUERIES = (
    "GET_ROBOT_STATUS",
    "GET_MOW_PREFERENCE_CONFIG",
    "GET_HEIGHT_MOTOR_PARAMETER",
    "GET_MAP_NAMES",
)
PROBE_TIMEOUT = 8
_PROBE_REDACT_HINTS = ("gps", "lat", "lon", "position", "coordinate")


def _redact_probe(value: Any) -> Any:
    """Drop anything that looks like a geographic position from a probe answer."""
    if isinstance(value, dict):
        return {
            key: "**REDACTED**"
            if any(hint in str(key).lower() for hint in _PROBE_REDACT_HINTS)
            else _redact_probe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_probe(item) for item in value]
    if isinstance(value, str) and len(value) > 2000:
        return f"<{len(value)} characters>"
    return value


async def _probe(api: Any, query_type: str) -> Any:
    try:
        return _redact_probe(
            await asyncio.wait_for(api.query(query_type), PROBE_TIMEOUT)
        )
    except TimeoutError:
        return {"error": "no answer (mower asleep or out of range?)"}
    except Exception as err:  # noqa: BLE001 - diagnostics must never fail
        return {"error": f"{type(err).__name__}: {err}"[:300]}


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _mower_diagnostics(
    coordinator: RoborockMowerCoordinator, probes: dict[str, Any]
) -> dict[str, Any]:
    api = coordinator.mower_api
    device = coordinator.device
    product = coordinator.product
    status = coordinator.data or api.status
    channel = api.channel
    status_dict = asdict(status)
    status_dict.pop("raw_dps", None)
    since_push = api.seconds_since_push
    return {
        "name": device.name,
        "model": product.model,
        "product_name": product.name,
        "category": str(product.category),
        "firmware": device.fv,
        "protocol_version": device.pv,
        "online_in_cloud": api.online,
        "connection": {
            "connected": getattr(channel, "is_connected", None),
            "local_connected": getattr(channel, "is_local_connected", None),
            "push_count": api.push_count,
            "seconds_since_last_push": round(since_push) if since_push is not None else None,
            "last_update_success": coordinator.last_update_success,
            "update_interval_s": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "activity": derive_activity(status, api.return_pending, api.task_pending),
        "return_pending": api.return_pending,
        "task_pending": api.task_pending,
        "message_counts": dict(api.message_counts),
        "probes": probes,
        "areas": api.areas,
        "status": async_redact_data(status_dict, TO_REDACT),
        "mow_state_label": status.mow_state_label,
        "raw_dps": {
            str(code): value
            for code, value in redact_dps(status.raw_dps).items()
            if code != DPS_GPS_COORDINATE
        },
        "product_schema": [
            {
                key: getattr(item, key, None)
                for key in ("id", "name", "code", "mode", "type", "property")
            }
            for item in (product.schema or [])
        ],
        "history": list(api.history),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MowerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    snapshot_age = runtime.home_data.snapshot_age
    mowers = []
    for coordinator in runtime.coordinators:
        answers = await asyncio.gather(
            *(_probe(coordinator.mower_api, query) for query in PROBE_QUERIES)
        )
        probes = dict(zip(PROBE_QUERIES, answers, strict=True))
        mowers.append(_mower_diagnostics(coordinator, probes))
    return {
        "versions": {
            "integration": str(integration.version),
            "home_assistant": HA_VERSION,
            "python_roborock_bundled": ROBOROCK_VERSION,
            # The one Home Assistant installed for the official integration;
            # informational only, this integration does not use it.
            "python_roborock_installed": _package_version("python-roborock"),
        },
        "config_entry": {
            "base_url": entry.data.get(CONF_BASE_URL),
            "username_set": bool(entry.data.get(CONF_USERNAME)),
        },
        "official_roborock_integration_entries": len(
            hass.config_entries.async_entries("roborock")
        ),
        "home_data": {
            "source": runtime.home_data.source,
            "snapshot_age_s": round(snapshot_age) if snapshot_age is not None else None,
            "last_error": runtime.home_data.last_error,
        },
        "mqtt_session_connected": getattr(runtime.mqtt_session, "connected", None),
        "mowers": mowers,
    }
