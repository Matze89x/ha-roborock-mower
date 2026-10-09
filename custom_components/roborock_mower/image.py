"""Image platform for the Roborock mower: the map, drawn like in the app.

The map (``get_map_diff``) shows the zones' boundaries, their names and the
charging station; the mower's position and the track of its run follow
while it mows. Drawn as SVG, so it stays sharp at any size.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import MAP_IMAGE_MIN_INTERVAL
from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity
from .map_data import render_svg


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mower's map image."""
    async_add_entities(
        RoborockMowerMapImage(hass, coordinator)
        for coordinator in entry.runtime_data.coordinators
    )


class RoborockMowerMapImage(RoborockMowerEntity, ImageEntity):
    """The mower's map with its position."""

    _attr_translation_key = "map"
    _attr_content_type = "image/svg+xml"

    def __init__(self, hass: HomeAssistant, coordinator: RoborockMowerCoordinator) -> None:
        RoborockMowerEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{self._device.duid}_map"
        self._redraw_later: CALLBACK_TYPE | None = None
        self._drawn_at: datetime | None = None

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.mower_map is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        mower_map = self.coordinator.mower_map
        if mower_map is None:
            return None
        return {
            "zones": [zone.name for zone in mower_map.zones if zone.name],
            "map_changed": mower_map.changed,
            "track_points": len(self.coordinator.track),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_map_listener(self._map_changed))
        self.async_on_remove(self._cancel_redraw)
        if self.coordinator.mower_map is not None:
            self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _cancel_redraw(self) -> None:
        if self._redraw_later is not None:
            self._redraw_later()
            self._redraw_later = None

    @callback
    def _map_changed(self) -> None:
        """The map or the position changed: show it, at most every 10 seconds."""
        if self._redraw_later is not None:
            return
        now = dt_util.utcnow()
        if self._drawn_at is not None:
            wait = MAP_IMAGE_MIN_INTERVAL - (now - self._drawn_at).total_seconds()
            if wait > 0:
                self._redraw_later = async_call_later(self.hass, wait, self._redraw)
                return
        self._redraw(now)

    @callback
    def _redraw(self, now: datetime | None = None) -> None:
        self._redraw_later = None
        self._drawn_at = dt_util.utcnow()
        self._attr_image_last_updated = self._drawn_at
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        view = self.coordinator.map_view
        return render_svg(view) if view is not None else None
