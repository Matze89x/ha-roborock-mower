"""Persistent cache for the Roborock Mower integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .vendor.roborock.devices.cache import Cache, CacheData

_LOGGER = logging.getLogger(__name__)


class MowerCacheStore(Cache):
    """python-roborock cache backed by a Home Assistant Store.

    Keeps the last home_data snapshot (device list, product schema and
    device_status) and each mower's network info across restarts. A restart or a
    setup retry then does not have to spend the account's home_data budget --
    which is shared with the official Roborock integration -- and the mower can
    still be set up while the cloud is rate-limiting or unreachable.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}", private=True
        )
        self._data: CacheData | None = None
        # Wall-clock time (epoch seconds) of the cached home_data snapshot.
        self.home_data_fetched_at: float | None = None

    async def get(self) -> CacheData:
        """Return the cached data, loading it from disk on first use."""
        if self._data is None:
            stored = await self._store.async_load() or {}
            try:
                self._data = CacheData.from_dict(stored.get("cache") or {})
            except Exception:  # noqa: BLE001 - a bad cache must never block setup
                _LOGGER.warning("Ignoring unreadable Roborock Mower cache", exc_info=True)
                self._data = None
            if self._data is None:
                self._data = CacheData()
            else:
                self.home_data_fetched_at = stored.get("home_data_fetched_at")
        return self._data

    async def set(self, value: CacheData) -> None:
        """Replace the cached data (persisted on the next flush)."""
        self._data = value

    async def async_flush(self) -> None:
        """Write the cached data to disk."""
        if self._data is None:
            return
        await self._store.async_save(
            {
                "cache": self._data.as_dict(),
                "home_data_fetched_at": self.home_data_fetched_at,
            }
        )

    async def async_remove(self) -> None:
        """Delete the cache file (config entry removed)."""
        await self._store.async_remove()
