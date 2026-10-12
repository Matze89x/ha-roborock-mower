"""Switch platform for the Roborock mower.

Mowing preferences and settings that are on/off: edge cut, rain protection,
do not disturb, anti-theft and the navigation options; and one switch per
mowing plan of the app, added and removed as the plans come and go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import MowerConfigEntry, RoborockMowerCoordinator
from .entity import RoborockMowerEntity, RoborockPreferenceEntity, RoborockUserModeEntity
from .mower_api import PLAN_OPEN, MowerSettingNotApplied, find_plan
from .robot_status import as_flag, plan_clock, plan_days, plan_days_label
from .vendor.roborock.exceptions import RoborockException


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the mower's switches."""
    entities: list[SwitchEntity] = []
    for coordinator in entry.runtime_data.coordinators:
        entities.append(RoborockEdgeCutSwitch(coordinator))
        entities.extend(
            RoborockUserModeSwitch(coordinator, description)
            for description in USER_MODE_SWITCHES
        )
    async_add_entities(entities)
    for coordinator in entry.runtime_data.coordinators:
        _follow_plans(hass, entry, coordinator, async_add_entities)


def _follow_plans(
    hass: HomeAssistant,
    entry: MowerConfigEntry,
    coordinator: RoborockMowerCoordinator,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One switch per mowing plan: new plans get one, deleted ones lose it."""
    prefix = f"{coordinator.device.duid}_plan_"
    shown: set[str] = set()
    last_seen: list[object] = [None]

    @callback
    def _sync() -> None:
        schedule = coordinator.extra.get("MOW_SCHEDULE")
        plans = coordinator.mow_plans
        if schedule is last_seen[0] or plans is None:
            return  # nothing new, or the plans were not read (yet)
        last_seen[0] = schedule
        ids = {str(plan["id"]) for plan in plans}
        if new := sorted(ids - shown):
            shown.update(new)
            async_add_entities(RoborockPlanSwitch(coordinator, plan_id) for plan_id in new)
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                entity.domain == "switch"
                and entity.unique_id.startswith(prefix)
                and entity.unique_id.removeprefix(prefix) not in ids
            ):
                registry.async_remove(entity.entity_id)
        shown.intersection_update(ids)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


@dataclass(frozen=True, kw_only=True)
class UserModeSwitchDescription(SwitchEntityDescription):
    """An on/off field of the mower's settings (``GET_USER_MODE_CONFIG``)."""

    part: str
    field: str


USER_MODE_SWITCHES = (
    UserModeSwitchDescription(
        key="rain_protection",
        translation_key="rain_protection",
        icon="mdi:weather-rainy",
        entity_category=EntityCategory.CONFIG,
        part="rainfall_config",
        field="enable",
    ),
    UserModeSwitchDescription(
        key="do_not_disturb",
        translation_key="do_not_disturb",
        icon="mdi:minus-circle-outline",
        entity_category=EntityCategory.CONFIG,
        part="not_disturb_config",
        field="enable",
    ),
    UserModeSwitchDescription(
        key="anti_theft",
        translation_key="anti_theft",
        icon="mdi:shield-lock-outline",
        entity_category=EntityCategory.CONFIG,
        part="anti_theft_config",
        field="enable",
    ),
    UserModeSwitchDescription(
        key="path_obstacle_detection",
        translation_key="path_obstacle_detection",
        icon="mdi:road-variant",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="path_detect_avoid",
    ),
    UserModeSwitchDescription(
        key="edge_camera_avoidance",
        translation_key="edge_camera_avoidance",
        icon="mdi:camera-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="edge_vision_avoid",
    ),
    UserModeSwitchDescription(
        key="passage_camera_avoidance",
        translation_key="passage_camera_avoidance",
        icon="mdi:camera-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="channel_vision_avoid",
    ),
    UserModeSwitchDescription(
        key="obstacle_photo_privacy",
        translation_key="obstacle_photo_privacy",
        icon="mdi:image-off-outline",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        part="nav_common_config",
        field="obstacle_image_privacy",
    ),
)


class RoborockEdgeCutSwitch(RoborockPreferenceEntity, SwitchEntity):
    """Cut the edges as part of every mow (preference ``keep_edge``).

    Called "Kantenschnitt" in the app. Written with ``SET_MOW_PREFERENCE``.
    """

    _attr_translation_key = "edge_cut_while_mowing"
    _attr_icon = "mdi:border-outside"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: RoborockMowerCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._device.duid}_edge_cut_while_mowing"

    @property
    def is_on(self) -> bool | None:
        return as_flag(self.preference.get("keep_edge"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_preference("Set edge cut", keep_edge=1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_preference("Set edge cut", keep_edge=0)


class RoborockUserModeSwitch(RoborockUserModeEntity, SwitchEntity):
    """A setting of the mower that is on or off (rain protection, ...).

    Written with the app's command for its part (``SET_RAINFALL`` ...), the
    part sent whole; the mower is asked again to confirm it took the change.
    """

    entity_description: UserModeSwitchDescription

    def __init__(
        self, coordinator: RoborockMowerCoordinator, description: UserModeSwitchDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._device.duid}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        desc = self.entity_description
        part = self.user_mode(desc.part)
        if not part:
            return None
        # The mower leaves out fields that are off.
        return bool(as_flag(part.get(desc.field)))

    async def _async_turn(self, on: bool) -> None:
        desc = self.entity_description
        await self._async_set_user_mode(
            f"Set {desc.key}", desc.part, **{desc.field: on}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_turn(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_turn(False)


class RoborockPlanSwitch(RoborockMowerEntity, SwitchEntity):
    """A mowing plan of the app: on (``OPEN``) mows as planned, off does not.

    Plans have no names; the switch is named after the plan's time and days,
    like the app shows them ("Mähplan 08:00–10:35 (Mo–Sa)"). Switched with
    ``CHANGE_MOWING_PLAN``, the plan sent whole; the plans are read again to
    confirm.
    """

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: RoborockMowerCoordinator, plan_id: str) -> None:
        super().__init__(coordinator)
        self._plan_id = plan_id
        self._attr_unique_id = f"{self._device.duid}_plan_{plan_id}"
        self._attr_name = self._label()

    @property
    def _plan(self) -> dict[str, Any] | None:
        return find_plan(self.coordinator.extra.get("MOW_SCHEDULE"), self._plan_id)

    def _time_zone(self) -> Any:
        name = (self.coordinator.extra.get("MOW_SCHEDULE") or {}).get("mow_schedule", {}).get(
            "time_zone"
        )
        return (dt_util.get_time_zone(name) if isinstance(name, str) else None) or (
            dt_util.get_default_time_zone()
        )

    def _label(self) -> str:
        plan = self._plan or {}
        # Known before the entity is added, so its entity id is in the
        # language of Home Assistant too.
        language = self.coordinator.hass.config.language or "en"
        word = "Mähplan" if language.startswith("de") else "Mowing plan"
        clock = plan_clock(plan, self._time_zone())
        if clock is None:
            return f"{word} {self._plan_id}"
        days = plan_days_label(plan, language)
        return f"{word} {clock[0]}–{clock[1]}" + (f" ({days})" if days else "")

    @callback
    def _handle_coordinator_update(self) -> None:
        # A plan changed in the app keeps its id: follow its time and days.
        if self._plan is not None:
            self._attr_name = self._label()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        return super().available and self._plan is not None

    @property
    def is_on(self) -> bool | None:
        plan = self._plan
        return None if plan is None else plan.get("status") == PLAN_OPEN

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        plan = self._plan
        if plan is None:
            return None
        clock = plan_clock(plan, self._time_zone())
        return {
            "plan_id": self._plan_id,
            "start": clock[0] if clock else None,
            "end": clock[1] if clock else None,
            "days": plan_days(plan),
            "mode": str(plan.get("mode", "")).lower() or None,
        }

    async def _async_switch(self, on: bool) -> None:
        try:
            await self.coordinator.async_set_plan_open(self._plan_id, on)
        except MowerSettingNotApplied as err:
            raise HomeAssistantError(
                "Switch mowing plan: the mower did not take the change"
            ) from err
        except RoborockException as err:
            raise HomeAssistantError(f"Switch mowing plan failed: {err}") from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_switch(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_switch(False)
