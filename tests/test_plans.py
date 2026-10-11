"""Mowing plans of the app as switches."""

from __future__ import annotations

import copy
from datetime import datetime
import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.roborock_mower.const import DOMAIN
from custom_components.roborock_mower.robot_status import plan_clock, plan_days_label
from custom_components.roborock_mower.vendor.roborock.exceptions import (
    RoborockException,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOWER_DUID, FakeChannel
from .test_robot_status import _answering, _setup, _wait_until

BERLIN = ZoneInfo("Europe/Berlin")
WORKDAYS = [{"type": day} for day in ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")]


def _at(hour: int, minute: int) -> str:
    """A plan time as the mower sends it: seconds on an old date, as text."""
    return str(int(datetime(2026, 10, 3, hour, minute, tzinfo=BERLIN).timestamp()))


def _plan(plan_id: int, start: tuple[int, int], end: tuple[int, int], status: str) -> dict:
    return {
        "id": plan_id,
        "type": "WEEKLY",
        "status": status,
        "start": _at(*start),
        "end": _at(*end),
        "days": WORKDAYS,
        "mode": "GLOBAL",
        "config": {"mode": "GLOBAL"},
        "fsm_state": "MOW_GLOBAL",
    }


SCHEDULE = {
    "type": "MOW_SCHEDULE",
    "mow_schedule": {
        "plans": [
            _plan(11, (8, 0), (10, 30), "OPEN"),
            _plan(22, (14, 0), (18, 0), "CLOSE"),
        ],
        "time_zone": "Europe/Berlin",
    },
}


def test_plan_labels() -> None:
    plan = SCHEDULE["mow_schedule"]["plans"][0]
    assert plan_clock(plan, BERLIN) == ("08:00", "10:30")
    assert plan_days_label(plan, "de") == "Mo–Sa"
    assert plan_days_label(plan, "en") == "Mon–Sat"
    week = {"days": [{"type": day} for day in ("MONDAY", "WEDNESDAY", "FRIDAY", "SATURDAY", "SUNDAY")]}
    assert plan_days_label(week, "de") == "Mo, Mi, Fr–So"
    assert plan_days_label({"days": [{"type": "MONDAY"}, {"type": "TUESDAY"}]}, "de") == "Mo, Di"
    every = {"days": [{"type": day} for day in ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")]}
    assert plan_days_label(every, "de") == "täglich"
    assert plan_days_label({}, "de") == ""
    assert plan_clock({"start": "0"}, BERLIN) is None


def _mower(sent: list[dict[str, Any]], schedule: dict, *, takes: bool = True):
    """A mower that keeps its plans and applies CHANGE_MOWING_PLAN."""
    answer = _answering([])

    async def _send(method: str, params: dict | None = None) -> object:
        kind = (params or {}).get("type")
        if kind == "GET_MOW_SCHEDULE":
            raise RoborockException(f"Unexpected API Result: {json.dumps(schedule)}")
        if kind == "CHANGE_MOWING_PLAN":
            sent.append(params)
            if takes:
                plans = schedule["mow_schedule"]["plans"]
                for index, plan in enumerate(plans):
                    if plan["id"] == params["mowing_plan"]["id"]:
                        plans[index] = params["mowing_plan"]
            return ["ok"]
        return await answer(method, params)

    return _send


def _plan_entity(hass: HomeAssistant, plan_id: int) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{MOWER_DUID}_plan_{plan_id}"
    )


async def test_plans_are_switches(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    schedule = copy.deepcopy(SCHEDULE)
    sent: list[dict[str, Any]] = []
    channel.rpc_channel.send_command.side_effect = _mower(sent, schedule)
    await _setup(hass, config_entry)
    await _wait_until(hass, lambda: _plan_entity(hass, 22) is not None)
    morning, afternoon = _plan_entity(hass, 11), _plan_entity(hass, 22)
    await _wait_until(hass, lambda: hass.states.get(morning) is not None)

    state = hass.states.get(morning)
    assert state.state == "on"
    assert state.name.endswith("Mowing plan 08:00–10:30 (Mon–Sat)")
    assert state.attributes["start"] == "08:00"
    assert state.attributes["end"] == "10:30"
    assert state.attributes["days"] == ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
    assert hass.states.get(afternoon).state == "off"

    # Off for today's rain: the whole plan goes back with status CLOSE.
    await hass.services.async_call("switch", "turn_off", {"entity_id": morning}, blocking=True)
    assert sent[-1]["type"] == "CHANGE_MOWING_PLAN"
    assert sent[-1]["mowing_plan"] == {**SCHEDULE["mow_schedule"]["plans"][0], "status": "CLOSE"}
    assert hass.states.get(morning).state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": afternoon}, blocking=True)
    assert sent[-1]["mowing_plan"]["status"] == "OPEN"
    assert hass.states.get(afternoon).state == "on"

    # In the app: the afternoon plan deleted, an evening plan added.
    plans = schedule["mow_schedule"]["plans"]
    plans[1:] = [_plan(33, (18, 30), (20, 0), "OPEN")]
    coordinator = config_entry.runtime_data.coordinators[0]
    coordinator._settings_due = True  # read every 30 minutes and on changes
    await coordinator.async_refresh_robot_status()
    await hass.async_block_till_done()
    assert _plan_entity(hass, 22) is None
    assert hass.states.get(afternoon) is None
    evening = _plan_entity(hass, 33)
    assert evening is not None
    assert hass.states.get(evening).state == "on"

    # No plans left: all plan switches go.
    plans.clear()
    coordinator._settings_due = True
    await coordinator.async_refresh_robot_status()
    await hass.async_block_till_done()
    assert _plan_entity(hass, 11) is None and _plan_entity(hass, 33) is None
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_plan_change_the_mower_does_not_take_is_an_error(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    sent: list[dict[str, Any]] = []
    channel.rpc_channel.send_command.side_effect = _mower(
        sent, copy.deepcopy(SCHEDULE), takes=False
    )
    await _setup(hass, config_entry)
    await _wait_until(hass, lambda: _plan_entity(hass, 11) is not None)
    morning = _plan_entity(hass, 11)
    await _wait_until(hass, lambda: hass.states.get(morning) is not None)
    with pytest.raises(HomeAssistantError, match="did not take"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": morning}, blocking=True
        )
    assert len(sent) == 1
    assert hass.states.get(morning).state == "on"
    assert await hass.config_entries.async_unload(config_entry.entry_id)
