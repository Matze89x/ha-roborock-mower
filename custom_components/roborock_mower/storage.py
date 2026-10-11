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


class MowerMapStore:
    """The map and the track of the last run of each mower, across restarts.

    Kept by mower (duid): ``map`` (the raw ``get_map_diff`` answer, base64),
    ``map_version`` (the status' map file time it belongs to), ``track``
    (``[[x, y], ...]`` in the map frame) and ``track_time`` (epoch seconds
    of the last track point). It shows the garden, so it stays local: never
    in diagnostics.
    """

    SAVE_DELAY = 60  # seconds; the track grows every few seconds while mowing

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.map", private=True
        )
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001 - a bad file must never block setup
            _LOGGER.warning("Ignoring unreadable Roborock Mower map store", exc_info=True)
            stored = None
        if not isinstance(stored, dict):
            stored = {}
        self._data = {
            duid: item for duid, item in stored.items() if isinstance(item, dict)
        }

    def get(self, duid: str) -> dict[str, Any]:
        return self._data.get(duid, {})

    def update(self, duid: str, **values: Any) -> None:
        """Change what is kept for a mower; written within a minute."""
        self._data.setdefault(duid, {}).update(values)
        self._store.async_delay_save(self._snapshot, self.SAVE_DELAY)

    def _snapshot(self) -> dict[str, Any]:
        # Copied on the event loop: the file is written in a worker thread
        # while the track keeps growing.
        return {
            duid: {
                key: list(value) if isinstance(value, list) else value
                for key, value in item.items()
            }
            for duid, item in self._data.items()
        }

    async def async_remove(self) -> None:
        await self._store.async_remove()
