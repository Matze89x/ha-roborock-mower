"""Rate-limit-aware access to the Roborock cloud home_data endpoint."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from .const import HOME_DATA_RATE_LIMIT_RETRY_DELAY, HOME_DATA_REUSE_AGE
from .storage import MowerCacheStore
from .vendor.roborock.data import HomeData, UserData
from .vendor.roborock.exceptions import (
    RoborockException,
    RoborockInvalidCredentials,
    RoborockRateLimit,
)
from .vendor.roborock.web_api import RoborockApiClient

_LOGGER = logging.getLogger(__name__)

SOURCE_CLOUD = "cloud"
SOURCE_RECENT = "recent_snapshot"
SOURCE_CACHE_FALLBACK = "cache_fallback"


class HomeDataProvider:
    """Fetches home_data for one account and shares/caches the result.

    Roborock limits home_data per account (python-roborock: 1/s, 3/min, 5/hour,
    40/day), and that account budget is shared with the official Roborock
    integration and the app. Everything that needs home_data therefore goes
    through here: one call at a time, recent snapshots are reused, and the last
    snapshot is persisted so a restart can fall back to it instead of failing.
    """

    def __init__(
        self,
        client: RoborockApiClient,
        user_data: UserData,
        cache: MowerCacheStore,
    ) -> None:
        self._client = client
        self._user_data = user_data
        self._cache = cache
        self._lock = asyncio.Lock()
        self.source: str | None = None
        self.last_error: str | None = None

    @property
    def snapshot_age(self) -> float | None:
        """Age of the last stored snapshot in seconds (None if never fetched)."""
        if self._cache.home_data_fetched_at is None:
            return None
        return time.time() - self._cache.home_data_fetched_at

    async def _fetch(self) -> HomeData:
        try:
            return await self._client.get_home_data_v3(self._user_data)
        except RoborockRateLimit:
            # The per-second slot frees up at once; the hourly/daily ones don't,
            # in which case the caller falls back to the cached snapshot.
            _LOGGER.debug(
                "home_data rate-limited; retrying once in %ss",
                HOME_DATA_RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(HOME_DATA_RATE_LIMIT_RETRY_DELAY)
            return await self._client.get_home_data_v3(self._user_data)

    async def async_refresh(self, max_age: timedelta | None = None) -> HomeData:
        """Return home_data from the cloud, or a stored snapshot younger than max_age.

        Raises RoborockException (incl. RoborockRateLimit) when the cloud call
        fails.
        """
        async with self._lock:
            cache_data = await self._cache.get()
            age = self.snapshot_age
            if (
                max_age is not None
                and cache_data.home_data is not None
                and age is not None
                and 0 <= age < max_age.total_seconds()
            ):
                self.source = SOURCE_RECENT
                return cache_data.home_data
            try:
                home_data = await self._fetch()
            except RoborockException as err:
                self.last_error = f"{type(err).__name__}: {err}"
                raise
            self.last_error = None
            self.source = SOURCE_CLOUD
            cache_data.home_data = home_data
            await self._cache.set(cache_data)
            self._cache.home_data_fetched_at = time.time()
            await self._cache.async_flush()
            return home_data

    async def async_get_for_setup(self) -> HomeData:
        """Return home_data for setup: fresh if possible, else the cached snapshot."""
        try:
            return await self.async_refresh(HOME_DATA_REUSE_AGE)
        except RoborockInvalidCredentials:
            raise
        except RoborockException as err:
            cached = await self.async_get_cached()
            if cached is None:
                raise
            _LOGGER.warning(
                "Could not fetch home data from the Roborock cloud (%s); using "
                "the cached device list. Live state arrives via MQTT",
                err,
            )
            self.source = SOURCE_CACHE_FALLBACK
            return cached

    async def async_get_cached(self) -> HomeData | None:
        """Return the stored snapshot without contacting the cloud."""
        return (await self._cache.get()).home_data
