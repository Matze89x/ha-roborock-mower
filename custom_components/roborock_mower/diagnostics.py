"""Diagnostics for the Roborock Mower integration.

Download from Settings > Devices & services > Roborock Mower > ... > Download
diagnostics. Credentials, the account e-mail, serial numbers, local keys and the
mower's GPS position are redacted. ``history`` lists the last commands, the
mower's answers and every data-point change with local timestamps -- enough to
follow a test run without a debug log.
"""

from __future__ import annotations

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


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _mower_diagnostics(coordinator: RoborockMowerCoordinator) -> dict[str, Any]:
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
        "activity": derive_activity(status, api.return_pending),
        "return_pending": api.return_pending,
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
        "mowers": [_mower_diagnostics(c) for c in runtime.coordinators],
    }
