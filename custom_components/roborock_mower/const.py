"""Constants for the Roborock Mower integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "roborock_mower"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
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
# safety net. Roborock rate-limits home_data per ACCOUNT (python-roborock models
# it as 1/s, 3/min, 5/hour, 40/day). Our bundled python-roborock has its own
# client-side counter, but the account budget on Roborock's side is shared with
# the official Roborock integration and the app. Poll rarely -- 12/day leaves
# most of the budget for restarts. Do not lower this.
UPDATE_INTERVAL = timedelta(hours=2)

# A home_data snapshot younger than this is reused instead of fetched again
# (setup retries after ConfigEntryNotReady, several mowers on one account).
HOME_DATA_REUSE_AGE = timedelta(minutes=30)

# If setup had to use a stored snapshot, fetch a fresh one this soon (and keep
# retrying at this pace while the cloud refuses), then go back to the interval.
STALE_SNAPSHOT_REFRESH_INTERVAL = timedelta(minutes=5)

# When the per-second home_data limit is hit, wait this long and try exactly
# once more before falling back to the cache.
HOME_DATA_RATE_LIMIT_RETRY_DELAY = 2.0

# Delays between attempts to discover saved mowing areas at startup. The mower
# is often out of Wi-Fi range or asleep right after a Home Assistant restart.
AREA_DISCOVERY_RETRY_DELAYS = (0, 120, 600, 1800)

# The mower's full status (GET_ROBOT_STATUS: last mow, schedule, lawn area,
# Wi-Fi/4G/RTK, ...) is asked from the mower itself -- over the local
# connection when there is one, else MQTT; never the rate-limited cloud API.
# Every minute while a task runs, otherwise every 30 minutes (little changes
# then, and the mower may sleep), and a few seconds after the mower reports a
# state change -- that is when a run starts or ends.
ROBOT_STATUS_ACTIVE_INTERVAL = timedelta(minutes=1)
ROBOT_STATUS_IDLE_INTERVAL = timedelta(minutes=30)
ROBOT_STATUS_SETTLE_DELAY = 5.0
ROBOT_STATUS_TIMEOUT = 15.0
# The mowing preferences (passes, direction, ...) rarely change.
PREFERENCE_REFRESH_INTERVAL = timedelta(minutes=30)

STORAGE_VERSION = 1

REGION_OPTIONS = ["auto", "us", "eu", "ru", "cn"]

# Services (zone / area mowing).
SERVICE_MOW_AREAS = "mow_areas"
SERVICE_LIST_AREAS = "list_areas"
SERVICE_QUERY = "query"
SERVICE_SCAN_QUERIES = "scan_queries"
ATTR_QUERY_TYPES = "query_types"
ATTR_FROM_APP = "from_app"
ATTR_QUERY_TYPE = "query_type"
ATTR_PAYLOAD = "payload"
ATTR_DEVICE_ID = "device_id"
ATTR_AREA_IDS = "area_ids"
ATTR_AREA_NAMES = "area_names"
ATTR_MAP_NAME = "map_name"
