"""Constants for the Roborock Mower integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "roborock_mower"

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.LAWN_MOWER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

CONF_USER_DATA = "user_data"
CONF_BASE_URL = "base_url"
CONF_ENTRY_CODE = "code"

# Live updates arrive via MQTT DPS push; this poll is only a cloud-snapshot
# safety net. get_home_data is heavily rate-limited (5/hour, 40/day) and that
# budget is shared with the official Roborock integration on the same account,
# so poll infrequently and lean on the MQTT push for real-time state.
UPDATE_INTERVAL = timedelta(minutes=30)

REGION_OPTIONS = ["auto", "us", "eu", "ru", "cn"]

# Services (zone / area mowing).
SERVICE_MOW_AREAS = "mow_areas"
SERVICE_LIST_AREAS = "list_areas"
ATTR_DEVICE_ID = "device_id"
ATTR_AREA_IDS = "area_ids"
ATTR_AREA_NAMES = "area_names"
ATTR_MAP_NAME = "map_name"
