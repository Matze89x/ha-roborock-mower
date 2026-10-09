"""Config flow for the Roborock Mower integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BASE_URL,
    CONF_ENTRY_CODE,
    CONF_USER_DATA,
    DOMAIN,
    REGION_OPTIONS,
)
from .vendor.roborock.exceptions import (
    RoborockAccountDoesNotExist,
    RoborockException,
    RoborockInvalidCode,
    RoborockInvalidEmail,
    RoborockTooFrequentCodeRequests,
    RoborockUrlException,
)
from .vendor.roborock.web_api import RoborockApiClient

_LOGGER = logging.getLogger(__name__)

CONF_REGION = "region"


class RoborockMowerFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Roborock Mower."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._base_url: str | None = None
        self._client: RoborockApiClient | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user step (email + region)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            region = user_input[CONF_REGION]
            self._username = username

            base_url = None
            if region != "auto":
                base_url = f"https://{region}iot.roborock.com"

            self._client = RoborockApiClient(
                username,
                base_url=base_url,
                session=async_get_clientsession(self.hass),
            )
            errors = await self._request_code()
            if not errors:
                return await self.async_step_code()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_REGION, default="auto"): SelectSelector(
                        SelectSelectorConfig(
                            options=REGION_OPTIONS,
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="region",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication when the stored login stopped working."""
        self._username = entry_data[CONF_USERNAME]
        self._base_url = entry_data.get(CONF_BASE_URL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication and send a new verification code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._username
            self._client = RoborockApiClient(
                self._username,
                base_url=self._base_url,
                session=async_get_clientsession(self.hass),
            )
            errors = await self._request_code()
            if not errors:
                return await self.async_step_code()
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"username": self._username or ""},
            errors=errors,
        )

    async def _request_code(self) -> dict[str, str]:
        assert self._client
        errors: dict[str, str] = {}
        try:
            await self._client.request_code_v4()
        except RoborockAccountDoesNotExist:
            errors["base"] = "invalid_email"
        except RoborockUrlException:
            errors["base"] = "unknown_url"
        except RoborockInvalidEmail:
            errors["base"] = "invalid_email_format"
        except RoborockTooFrequentCodeRequests:
            errors["base"] = "too_frequent_code_requests"
        except RoborockException:
            _LOGGER.exception("Unexpected Roborock exception during code request")
            errors["base"] = "unknown_roborock"
        except Exception:
            _LOGGER.exception("Unexpected exception during code request")
            errors["base"] = "unknown"
        return errors

    async def async_step_code(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the verification code step."""
        errors: dict[str, str] = {}
        assert self._client
        assert self._username

        if user_input is not None:
            code = user_input[CONF_ENTRY_CODE]
            try:
                user_data = await self._client.code_login_v4(code)
            except RoborockInvalidCode:
                errors["base"] = "invalid_code"
            except RoborockAccountDoesNotExist:
                errors["base"] = "invalid_email_or_region"
            except RoborockException:
                _LOGGER.exception("Unexpected Roborock exception during login")
                errors["base"] = "unknown_roborock"
            except Exception:
                _LOGGER.exception("Unexpected exception during login")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_data.rruid)
                data = {
                    CONF_USERNAME: self._username,
                    CONF_USER_DATA: user_data.as_dict(),
                    CONF_BASE_URL: await self._client.base_url,
                }
                if self.source == SOURCE_REAUTH:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data_updates=data
                    )
                self._abort_if_unique_id_configured(
                    error="already_configured_account"
                )
                return self.async_create_entry(title=self._username, data=data)

        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema({vol.Required(CONF_ENTRY_CODE): str}),
            errors=errors,
        )
