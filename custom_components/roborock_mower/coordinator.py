"""Data update coordinator for Roborock Mower."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    HOME_DATA_REUSE_AGE,
    MAP_RETRY_INTERVAL,
    MAP_TIMEOUT,
    PREFERENCE_REFRESH_INTERVAL,
    ROBOT_STATUS_ACTIVE_INTERVAL,
    ROBOT_STATUS_IDLE_INTERVAL,
    ROBOT_STATUS_SETTLE_DELAY,
    ROBOT_STATUS_TIMEOUT,
    STALE_SNAPSHOT_REFRESH_INTERVAL,
    TRACK_MAX_POINTS,
    TRACK_MIN_STEP,
    TRACK_RESET_GAP,
    UPDATE_INTERVAL,
)
from .home_data import HomeDataProvider
from .map_data import MapView, MowerMap, Pose, parse_map, pose_from_status
from .mower_api import (
    ACTIVITY_MOWING,
    ACTIVITY_PAUSED,
    ACTIVITY_RETURNING,
    DPS_CHARGE_STATE,
    DPS_CHARGE_TYPE,
    DPS_ERROR_CODE,
    DPS_MOW_EFF_MODE,
    DPS_MOW_STATE,
    DPS_MOW_TYPE,
    DPS_PEND_TYPE,
    MowerApi,
    MowerStatus,
    derive_activity,
    redact_dps,
)
from .robot_status import dig, redact_private
from .storage import MowerCacheStore
from .vendor.roborock.data import HomeDataDevice, HomeDataProduct, UserData
from .vendor.roborock.exceptions import RoborockException, RoborockInvalidCredentials
from .vendor.roborock.mqtt.session import MqttSession
from .vendor.roborock.web_api import RoborockApiClient, UserWebApiClient

_LOGGER = logging.getLogger(__name__)


@dataclass
class MowerRuntimeData:
    """Everything a loaded config entry owns."""

    coordinators: list[RoborockMowerCoordinator]
    home_data: HomeDataProvider
    cache: MowerCacheStore
    mqtt_session: MqttSession
    web_api: UserWebApiClient
    # For the scan_queries action (reads the app plugin with this account).
    api_client: RoborockApiClient | None = None
    user_data: UserData | None = None
    unsubscribes: list[Callable[[], None]] = field(default_factory=list)


type MowerConfigEntry = ConfigEntry[MowerRuntimeData]

# Data points whose change makes the full status worth asking for again
# (a task started or ended, the mower docked, an error, settings changed).
STATUS_TRIGGER_DPS = frozenset(
    {
        DPS_ERROR_CODE,
        DPS_MOW_TYPE,
        DPS_MOW_STATE,
        DPS_CHARGE_STATE,
        DPS_CHARGE_TYPE,
        DPS_PEND_TYPE,
        DPS_MOW_EFF_MODE,
    }
)
_BUSY_ACTIVITIES = frozenset({ACTIVITY_MOWING, ACTIVITY_PAUSED, ACTIVITY_RETURNING})
# Read with the mowing preferences (every 30 minutes, after a settings change
# or an error): rain / do-not-disturb / anti-theft settings, the fault
# history and the schedules per zone. The product details only once.
SETTINGS_QUERIES = ("GET_USER_MODE_CONFIG", "GET_FAULT_RECORDS", "GET_ZONES_PLAN_INFO")
ONCE_QUERIES = ("GET_FEATURE_INFO",)


class RoborockMowerCoordinator(DataUpdateCoordinator[MowerStatus]):
    """Coordinator for a single Roborock mower.

    Status is push-first: live DPS arrive over MQTT and are fed in via
    ``async_set_updated_data``. The periodic poll of the cloud ``home_data``
    snapshot is only a safety net for missed pushes. Its rate limit is shared
    with the official Roborock integration, so it runs rarely, and a failed poll
    keeps the last known (push-fed) state instead of marking the mower
    unavailable.

    When setup had to use a stored snapshot (``stale``), pushes missed while
    Home Assistant was down are not reflected yet, so one real cloud refresh is
    done soon after setup before falling back to the normal interval.
    """

    config_entry: MowerConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MowerConfigEntry,
        device: HomeDataDevice,
        product: HomeDataProduct,
        mower_api: MowerApi,
        home_data: HomeDataProvider,
        stale: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{device.duid}",
            update_interval=(
                STALE_SNAPSHOT_REFRESH_INTERVAL if stale else UPDATE_INTERVAL
            ),
            always_update=False,
        )
        self.device = device
        self.product = product
        self.mower_api = mower_api
        self._home_data = home_data
        self._needs_cloud_refresh = stale
        self._poll_failing = False
        # The mower's full status (GET_ROBOT_STATUS), private fields redacted.
        self.robot_status: dict[str, Any] | None = None
        self.robot_status_time: datetime | None = None
        self._status_wakeup = asyncio.Event()
        self._status_failing = False
        # Further answers by type ("USER_MODE_CONFIG", ...), redacted.
        self.extra: dict[str, dict[str, Any]] = {}
        self._settings_due = True
        self._settings_read_at: float | None = None
        # The map (get_map_diff), the mower's latest position and the track
        # of its current or last run (positions in the map frame, metres).
        self.mower_map: MowerMap | None = None
        self.robot_pose: Pose | None = None
        self.track: list[tuple[float, float]] = []
        self._track_moved_at: float | None = None
        self._map_tried_at: float | None = None
        self._map_version: Any = None
        self._map_failing = False
        self._map_listeners: list[Callable[[], None]] = []

    async def _async_update_data(self) -> MowerStatus:
        api = self.mower_api
        try:
            home_data = await self._home_data.async_refresh(
                None if self._needs_cloud_refresh else HOME_DATA_REUSE_AGE
            )
        except RoborockInvalidCredentials as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except RoborockException as err:
            if not self._poll_failing:
                _LOGGER.warning(
                    "[%s] Cloud status poll failed (%s); keeping the live MQTT state",
                    self.device.duid,
                    err,
                )
            self._poll_failing = True
            return api.status

        if self._poll_failing:
            _LOGGER.info("[%s] Cloud status poll recovered", self.device.duid)
        self._poll_failing = False
        if self._needs_cloud_refresh:
            self._needs_cloud_refresh = False
            self.update_interval = UPDATE_INTERVAL
        status = api.apply_home_data(home_data)
        if status.raw_dps:
            _LOGGER.debug(
                "[%s] Mower DPS: %s", self.device.duid, redact_dps(status.raw_dps)
            )
        return status

    def supports_dp(self, code: int) -> bool:
        """Whether the product schema lists data point ``code``.

        Models differ (the RockNeo Q105 has no blade-life or pause-reason data
        point, a RockMow does), so entities for missing ones are not created.
        Without a schema everything counts as supported.
        """
        codes: set[int] = set()
        for item in self.product.schema or []:
            try:
                codes.add(int(item.id))
            except (TypeError, ValueError):
                continue
        return not codes or code in codes

    # -- full robot status (GET_ROBOT_STATUS) ----------------------------------

    @property
    def model_name(self) -> str:
        """The product name with the exact model (``RockNeo Q1`` -> ``RockNeo Q105``)."""
        name = self.product.name or self.product.model
        market = dig(self.extra.get("FEATURE_INFO"), "feature_info", "sku_info", "market_name")
        if not isinstance(market, str) or not market or market in name:
            return name
        family = name.rsplit(" ", 1)[0] if " " in name else name
        return f"{family} {market}"

    def _update_device_model(self) -> None:
        registry = dr.async_get(self.hass)
        identifier = (DOMAIN, self.device.duid)
        if lookup := getattr(registry, "async_get_device_by_identifier", None):
            device = lookup(identifier, self.config_entry.entry_id)
        else:  # Home Assistant before 2026.10
            device = registry.async_get_device(identifiers={identifier})
        if device is not None and device.model != self.model_name:
            registry.async_update_device(device.id, model=self.model_name)

    @property
    def mow_preference(self) -> dict[str, Any] | None:
        """The global mowing preference (passes, direction, ...), if known."""
        cfg = self.mower_api.preference_config
        pref = cfg.get("global") if isinstance(cfg, dict) else None
        return pref if isinstance(pref, dict) else None

    @property
    def zones_with_own_settings(self) -> list[str]:
        """Zones whose own mowing preferences replace the global ones.

        Changes from Home Assistant go to the global preferences, which these
        zones don't use (preference ``mode`` CUSTOM, set in the app).
        """
        cfg = self.mower_api.preference_config
        zones = cfg.get("custom") if isinstance(cfg, dict) else None
        return [
            str(zone.get("area_name") or zone.get("area_id"))
            for zone in zones or []
            if isinstance(zone, dict) and zone.get("mode") == "CUSTOM"
        ]

    def note_push(self, changes: frozenset[int]) -> None:
        """React to a live data-point push: re-read the full status if useful."""
        if not changes & STATUS_TRIGGER_DPS:
            return
        if changes & {DPS_MOW_EFF_MODE, DPS_ERROR_CODE}:
            self._settings_due = True
        self._status_wakeup.set()

    def request_settings_refresh(self) -> None:
        """Read status and settings again soon (after a settings change)."""
        self._settings_due = True
        self._status_wakeup.set()

    def _robot_status_interval(self) -> float:
        api = self.mower_api
        activity = derive_activity(api.status, api.return_pending, api.task_pending)
        interval = (
            ROBOT_STATUS_ACTIVE_INTERVAL
            if activity in _BUSY_ACTIVITIES
            else ROBOT_STATUS_IDLE_INTERVAL
        )
        return interval.total_seconds()

    async def async_poll_robot_status(self) -> None:
        """Keep the full status fresh for as long as the config entry runs.

        Polls every minute while a task runs, every 30 minutes otherwise, and
        a few seconds after the mower pushes a state change, so the "last mow"
        values follow right after a run ends.
        """
        while True:
            self._status_wakeup.clear()
            try:
                await self.async_refresh_robot_status()
            except Exception:
                # Never let one odd answer end the polling for good.
                _LOGGER.exception("[%s] Reading the mower status failed", self.device.duid)
            try:
                async with asyncio.timeout(self._robot_status_interval()):
                    await self._status_wakeup.wait()
            except TimeoutError:
                continue
            # Let a burst of pushes (task end, docking) settle first.
            await asyncio.sleep(ROBOT_STATUS_SETTLE_DELAY)

    async def async_refresh_robot_status(self) -> None:
        """Ask the mower for its full status (and now and then its settings)."""
        api = self.mower_api
        try:
            async with asyncio.timeout(ROBOT_STATUS_TIMEOUT):
                answer = await api.get_robot_status(record=False)
        except (RoborockException, TimeoutError) as err:
            if not self._status_failing:
                _LOGGER.debug(
                    "[%s] No status answer from the mower (asleep or out of "
                    "range?): %s",
                    self.device.duid,
                    err or type(err).__name__,
                )
            self._status_failing = True
            return
        if answer is None:
            return
        if self._status_failing:
            _LOGGER.debug("[%s] Mower answers status queries again", self.device.duid)
        self._status_failing = False
        self.robot_status = redact_private(answer)
        self.robot_status_time = dt_util.utcnow()
        if (pose := pose_from_status(answer)) is not None:
            self.note_pose(pose)
        version = dig(answer, "map_abstracts", 0, "file_change_time")
        if self._map_due(version):
            await self.async_refresh_map(version)
        if (
            self._settings_due
            or self._settings_read_at is None
            or time.monotonic() - self._settings_read_at
            > PREFERENCE_REFRESH_INTERVAL.total_seconds()
        ):
            await self._async_refresh_settings()
        self.async_update_listeners()

    async def _async_refresh_settings(self) -> None:
        """Read the preferences, settings, fault history and plans."""
        api = self.mower_api
        if await _quietly(api.get_mow_preference_config(record=False)) is not None:
            self._settings_due = False
            self._settings_read_at = time.monotonic()
        queries = SETTINGS_QUERIES + tuple(
            query for query in ONCE_QUERIES if query.removeprefix("GET_") not in self.extra
        )
        for query in queries:
            # Models that lack a query reject it; it is simply tried again later.
            answer = await _quietly(api.get_info(query, record=False))
            if answer is None:
                continue
            key = query.removeprefix("GET_")
            first = key not in self.extra
            self.extra[key] = redact_private(answer)
            if key == "FEATURE_INFO" and first:
                self._update_device_model()

    # -- map ----------------------------------------------------------------------

    @property
    def map_view(self) -> MapView | None:
        """The map with the latest position and track, once the map is read."""
        if self.mower_map is None:
            return None
        return MapView(self.mower_map, self.robot_pose, self.track)

    @callback
    def async_add_map_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` when the map or the mower's position changes."""
        self._map_listeners.append(listener)

        def _remove() -> None:
            if listener in self._map_listeners:
                self._map_listeners.remove(listener)

        return _remove

    def _notify_map(self) -> None:
        for listener in list(self._map_listeners):
            listener()

    def _map_due(self, version: Any) -> bool:
        """Whether to read the map: none yet, or the mower reports a newer one.

        ``version`` is the map file time of the full status. After a failed
        try the map is asked again at most every 30 minutes.
        """
        if self.mower_map is not None and version == self._map_version:
            return False
        if self._map_tried_at is None or not self._map_failing:
            return True
        return time.monotonic() - self._map_tried_at > MAP_RETRY_INTERVAL.total_seconds()

    async def async_refresh_map(self, version: Any = None) -> bool:
        """Read the map from the mower (through the cloud map channel)."""
        self._map_tried_at = time.monotonic()
        try:
            async with asyncio.timeout(MAP_TIMEOUT):
                data = await self.mower_api.get_map_rpc("get_map_diff", map_channel=True)
        except (RoborockException, TimeoutError) as err:
            reason = str(err) or type(err).__name__
            parsed = None
        else:
            parsed = parse_map(data) if isinstance(data, bytes) else None
            reason = "no map in the answer"
        if parsed is None:
            if not self._map_failing:
                _LOGGER.debug("[%s] Could not read the map: %s", self.device.duid, reason)
            self._map_failing = True
            return False
        if self._map_failing or self.mower_map is None:
            _LOGGER.debug(
                "[%s] Map read: %s", self.device.duid, parsed.summary
            )
        self._map_failing = False
        self._map_version = version
        self.mower_map = parsed
        if self.robot_pose is None:
            self.robot_pose = parsed.robot
        self._notify_map()
        return True

    @callback
    def note_pose(self, pose: Pose) -> None:
        """A new position of the mower (status poll or live status stream).

        While the mower is out mowing (or on its way back) the positions make
        up the track of the run; a new run starts a new track.
        """
        previous = self.robot_pose
        self.robot_pose = pose
        api = self.mower_api
        activity = derive_activity(api.status, api.return_pending, api.task_pending)
        now = time.monotonic()
        if activity in (ACTIVITY_MOWING, ACTIVITY_RETURNING):
            if (
                self._track_moved_at is None
                or now - self._track_moved_at > TRACK_RESET_GAP.total_seconds()
            ):
                self.track = []
            last = self.track[-1] if self.track else None
            if last is None or _distance(last, (pose.x, pose.y)) >= TRACK_MIN_STEP:
                if len(self.track) >= TRACK_MAX_POINTS:
                    # Keep the whole run in view: drop every second point.
                    self.track = self.track[::2]
                self.track.append((pose.x, pose.y))
                self._track_moved_at = now
        if previous is None or previous != pose:
            self._notify_map()


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


async def _quietly(call: Coroutine[Any, Any, Any]) -> Any:
    """An answer of the mower, or None if it fails or takes too long."""
    try:
        async with asyncio.timeout(ROBOT_STATUS_TIMEOUT):
            return await call
    except (RoborockException, TimeoutError):
        return None
