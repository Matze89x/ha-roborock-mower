"""The mower integration must run side by side with the official Roborock one.

The official integration (vacuums) uses the python-roborock that Home Assistant
installs; this integration ships its own private copy in ``vendor/``. These
tests pin down that the two can never interfere again:

* requirements: a pinned python-roborock made Home Assistant reinstall a
  different version on every start, breaking the official integration;
* imports: the bundled copy must never load or patch the top-level
  ``roborock`` package the official integration uses;
* home_data: the official integration's startup fetch must not block ours.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from roborock.data import UserData as OfficialUserData
import roborock.web_api as official_web_api

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant

from custom_components.roborock_mower.const import DOMAIN
from custom_components.roborock_mower.home_data import SOURCE_CLOUD
from custom_components.roborock_mower.vendor import ROBOROCK_VERSION
from custom_components.roborock_mower.vendor.roborock import web_api as bundled_web_api
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import USER_DATA, make_home_data

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "custom_components" / DOMAIN / "manifest.json"

# Captured before the autouse fixture patches it for the other tests.
REAL_GET_HOME_DATA_V3 = bundled_web_api.RoborockApiClient.get_home_data_v3


def test_does_not_require_python_roborock() -> None:
    requirements = json.loads(MANIFEST.read_text())["requirements"]
    assert not [r for r in requirements if r.startswith("python-roborock")]


def test_bundles_newest_python_roborock() -> None:
    assert ROBOROCK_VERSION == "7.12.1"


def test_bundled_copy_never_loads_top_level_roborock() -> None:
    """Load the integration in a fresh interpreter; ``roborock`` stays untouched."""
    code = (
        "import sys\n"
        "import custom_components.roborock_mower\n"
        "import custom_components.roborock_mower.diagnostics\n"
        "loaded = sorted(m for m in sys.modules if m == 'roborock' "
        "or m.startswith('roborock.'))\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_bundled_copy_has_its_own_rate_limiter() -> None:
    assert (
        bundled_web_api.RoborockApiClient._home_data_limiter
        is not official_web_api.RoborockApiClient._home_data_limiter
    )


async def test_both_integrations_load_together(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    core_entry = MockConfigEntry(
        domain="roborock",
        version=1,
        minor_version=2,
        unique_id=USER_DATA["rruid"],
        data={
            CONF_USERNAME: "user@example.com",
            "user_data": USER_DATA,
            "base_url": "https://euiot.roborock.com",
        },
    )
    core_entry.add_to_hass(hass)
    config_entry.add_to_hass(hass)

    device_manager = MagicMock()
    device_manager.get_devices = AsyncMock(return_value=[])
    device_manager.close = AsyncMock()
    with patch(
        "homeassistant.components.roborock.create_device_manager",
        AsyncMock(return_value=device_manager),
    ):
        assert await hass.config_entries.async_setup(core_entry.entry_id)
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert core_entry.state is ConfigEntryState.LOADED
    assert config_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert await hass.config_entries.async_unload(core_entry.entry_id)
    await hass.async_block_till_done()


async def test_official_startup_fetch_does_not_block_ours(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Both fetch home_data in the same second while Home Assistant starts."""
    payload = {"success": True, "result": make_home_data().as_dict()}
    with (
        patch.object(
            bundled_web_api.RoborockApiClient,
            "get_home_data_v3",
            REAL_GET_HOME_DATA_V3,
        ),
        patch.object(
            bundled_web_api.RoborockApiClient,
            "_get_home_id",
            AsyncMock(return_value=1),
        ),
        patch.object(
            bundled_web_api.PreparedRequest, "request", AsyncMock(return_value=payload)
        ),
        patch.object(
            official_web_api.RoborockApiClient,
            "_get_home_id",
            AsyncMock(return_value=1),
        ),
        patch.object(
            official_web_api.PreparedRequest,
            "request",
            AsyncMock(return_value=payload),
        ),
    ):
        # The official integration's startup fetch ...
        official = official_web_api.RoborockApiClient("user@example.com")
        await official.get_home_data_v3(OfficialUserData.from_dict(USER_DATA))
        # ... and ours in the same second: no waiting, straight from the cloud.
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.home_data.source == SOURCE_CLOUD
