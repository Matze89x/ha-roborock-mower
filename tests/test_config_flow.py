"""Config flow tests for the Roborock Mower integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, PropertyMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.roborock_mower.const import (
    CONF_BASE_URL,
    CONF_ENTRY_CODE,
    CONF_USER_DATA,
    DOMAIN,
)
from custom_components.roborock_mower.vendor.roborock.data import UserData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import USER_DATA

BASE_URL = "https://euiot.roborock.com"
WEB_API = "custom_components.roborock_mower.vendor.roborock.web_api.RoborockApiClient"


def _login_patches(user_data: dict):
    async def _base_url():
        return BASE_URL

    return (
        patch(f"{WEB_API}.request_code_v4", AsyncMock()),
        patch(
            f"{WEB_API}.code_login_v4",
            AsyncMock(return_value=UserData.from_dict(user_data)),
        ),
        patch(
            f"{WEB_API}.base_url",
            new_callable=PropertyMock,
            side_effect=lambda: _base_url(),
        ),
        patch(
            "custom_components.roborock_mower.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    )


async def test_user_flow_creates_entry(hass: HomeAssistant) -> None:
    request, login, base_url, setup = _login_patches(USER_DATA)
    with request, login, base_url, setup:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "user@example.com", "region": "eu"}
        )
        assert result["step_id"] == "code"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ENTRY_CODE: "123456"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BASE_URL] == BASE_URL
    assert result["result"].unique_id == USER_DATA["rruid"]


async def test_reauth_updates_entry(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_DATA["rruid"],
        data={
            CONF_USERNAME: "user@example.com",
            CONF_USER_DATA: {**USER_DATA, "token": "expired"},
            CONF_BASE_URL: BASE_URL,
        },
    )
    entry.add_to_hass(hass)

    request, login, base_url, setup = _login_patches(USER_DATA)
    with request, login, base_url, setup:
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "code"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ENTRY_CODE: "123456"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_USER_DATA]["token"] == USER_DATA["token"]


async def test_reauth_with_other_account_aborts(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_DATA["rruid"],
        data={
            CONF_USERNAME: "user@example.com",
            CONF_USER_DATA: USER_DATA,
            CONF_BASE_URL: BASE_URL,
        },
    )
    entry.add_to_hass(hass)

    request, login, base_url, setup = _login_patches(
        {**USER_DATA, "rruid": "someone_else"}
    )
    with request, login, base_url, setup:
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ENTRY_CODE: "123456"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
