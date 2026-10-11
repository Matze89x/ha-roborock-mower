"""Time platform for the Roborock mower: the do-not-disturb time."""

from __future__ import annotations

from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockUserModeEntity
from .robot_status import as_number

# Used when the mower has no do-not-disturb time yet (the app's default).
_DEFAULT_WINDOW = {"start": {"hour": 20}, "end": {"hour": 8}}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mower's time entities."""
    async_add_entities(
        RoborockDndTime(coordinator, end)
        for coordinator in entry.runtime_data.coordinators
        for end in ("start", "end")
    )


class RoborockDndTime(RoborockUserModeEntity, TimeEntity):
    """Start or end of the do-not-disturb time (no mowing in between).

    ``not_disturb_config.time[0]``, written with ``SET_NOT_DISTURB``.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: RoborockMowerCoordinator, end: str) -> None:
        super().__init__(coordinator)
        self._end = end
        self._attr_translation_key = f"do_not_disturb_{end}"
        self._attr_unique_id = f"{self._device.duid}_do_not_disturb_{end}"

    def _window(self) -> dict[str, Any] | None:
        times = self.user_mode("not_disturb_config").get("time")
        if isinstance(times, list) and times and isinstance(times[0], dict):
            return times[0]
        return None

    @property
    def native_value(self) -> time | None:
        window = self._window()
        if window is None:
            return None
        clock = window.get(self._end)
        if not isinstance(clock, dict):
            clock = {}
        # The mower leaves out zeros: {"hour": 8} is 08:00.
        hour = as_number(clock.get("hour")) or 0
        minute = as_number(clock.get("minute")) or 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return time(int(hour), int(minute))

    async def async_set_value(self, value: time) -> None:
        times = self.user_mode("not_disturb_config").get("time")
        times = [dict(item) for item in times] if isinstance(times, list) and times else []
        window = times[0] if times else dict(_DEFAULT_WINDOW)
        window[self._end] = {"hour": value.hour, "minute": value.minute}
        await self._async_set_user_mode(
            f"Set do-not-disturb {self._end}",
            "not_disturb_config",
            time=[window, *times[1:]],
        )
