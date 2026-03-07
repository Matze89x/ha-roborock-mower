"""Constants for the Roborock Mower integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "roborock_mower"

PLATFORMS: list[Platform] = [
    Platform.LAWN_MOWER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

CONF_USER_DATA = "user_data"
CONF_BASE_URL = "base_url"
CONF_ENTRY_CODE = "code"

UPDATE_INTERVAL = timedelta(seconds=30)

REGION_OPTIONS = ["auto", "us", "eu", "ru", "cn"]
