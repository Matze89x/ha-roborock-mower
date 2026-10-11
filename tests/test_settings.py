"""Settings of the mower written from Home Assistant (rain, do not disturb, ...)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.roborock_mower.const import DOMAIN
from custom_components.roborock_mower.mower_api import USER_MODE_COMMANDS
from custom_components.roborock_mower.robot_status import REDACTED
from custom_components.roborock_mower.vendor.roborock.exceptions import (
    RoborockException,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOWER_DUID, FakeChannel, enable_entities
from .test_robot_status import LIVE_EXTRA, _answering, _entity_id, _setup, _wait_until

SETTINGS = LIVE_EXTRA["USER_MODE_CONFIG"]["user_mode_config"]


def _mower(sent: list[dict[str, Any]], *, takes: bool = True, rejects: bool = False):
    """A mower that keeps its settings and applies the app's SET_* commands."""
    settings = copy.deepcopy(LIVE_EXTRA["USER_MODE_CONFIG"])
    answer = _answering([])

    async def _send(method: str, params: dict | None = None) -> object:
        kind = (params or {}).get("type")
        if kind in USER_MODE_COMMANDS.values():
            sent.append(params)
            if rejects:
                return ["fail"]
            part = next(part for part, cmd in USER_MODE_COMMANDS.items() if cmd == kind)
            if takes:
                # Like protobuf: zero minutes are left out of the answer.
                settings["user_mode_config"][part] = json.loads(
                    json.dumps(params[part]).replace(', "minute": 0', "")
                )
            return ["ok"]
        if kind == "GET_USER_MODE_CONFIG":
            raise RoborockException(f"Unexpected API Result: {json.dumps(settings)}")
        return await answer(method, params)

    return _send


async def test_settings_are_written_whole_and_confirmed(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    sent: list[dict[str, Any]] = []
    channel.rpc_channel.send_command.side_effect = _mower(sent)
    enable_entities(hass, config_entry, ("switch", "path_obstacle_detection"))
    await _setup(hass, config_entry)
    rain = _entity_id(hass, "switch", "rain_protection")
    await _wait_until(hass, lambda: hass.states.get(rain).state == "on")
    coordinator = config_entry.runtime_data.coordinators[0]

    # Rain protection off: the whole rain part goes back with the change.
    await hass.services.async_call("switch", "turn_off", {"entity_id": rain}, blocking=True)
    assert sent[-1]["type"] == "SET_RAINFALL"
    assert sent[-1]["rainfall_config"] == {**SETTINGS["rainfall_config"], "enable": False}
    assert hass.states.get(rain).state == "off"
    # What the mower reports afterwards is shown -- private fields redacted.
    assert (
        coordinator.extra["USER_MODE_CONFIG"]["user_mode_config"]["random_gngga_config"]
        == REDACTED
    )

    delay = _entity_id(hass, "number", "rain_delay")
    assert float(hass.states.get(delay).state) == 8
    await hass.services.async_call(
        "number", "set_value", {"entity_id": delay, "value": 4}, blocking=True
    )
    assert sent[-1]["rainfall_config"] == {
        **SETTINGS["rainfall_config"],
        "enable": False,
        "delay_time": 4,
    }
    assert float(hass.states.get(delay).state) == 4

    # Do not disturb from 21:15: the end (08:00) stays as the mower had it.
    start = _entity_id(hass, "time", "do_not_disturb_start")
    end = _entity_id(hass, "time", "do_not_disturb_end")
    assert hass.states.get(start).state == "20:30:00"
    assert hass.states.get(end).state == "08:00:00"
    await hass.services.async_call(
        "time", "set_value", {"entity_id": start, "time": "21:15:00"}, blocking=True
    )
    assert sent[-1]["type"] == "SET_NOT_DISTURB"
    assert sent[-1]["not_disturb_config"] == {
        "enable": True,
        "time": [{"start": {"hour": 21, "minute": 15}, "end": {"hour": 8}}],
    }
    assert hass.states.get(start).state == "21:15:00"
    assert hass.states.get(end).state == "08:00:00"
    # 07:00 is confirmed although the mower answers {"hour": 7}.
    await hass.services.async_call(
        "time", "set_value", {"entity_id": end, "time": "07:00:00"}, blocking=True
    )
    assert hass.states.get(end).state == "07:00:00"

    # A navigation option: the whole navigation part.
    path = _entity_id(hass, "switch", "path_obstacle_detection")
    assert hass.states.get(path).state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": path}, blocking=True)
    assert sent[-1]["type"] == "SET_NAV_COMMON_CONFIG"
    assert sent[-1]["nav_common_config"] == {
        **SETTINGS["nav_common_config"],
        "path_detect_avoid": False,
    }
    assert hass.states.get(path).state == "off"

    anti_theft = _entity_id(hass, "switch", "anti_theft")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": anti_theft}, blocking=True
    )
    assert sent[-1]["type"] == "SET_ANTI_THEFT"
    assert sent[-1]["anti_theft_config"]["enable"] is True
    assert hass.states.get(anti_theft).state == "on"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    ("takes", "rejects", "message"),
    [(False, False, "did not take"), (True, True, "rejected")],
)
async def test_a_setting_the_mower_does_not_take_is_an_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    channel: FakeChannel,
    takes: bool,
    rejects: bool,
    message: str,
) -> None:
    sent: list[dict[str, Any]] = []
    channel.rpc_channel.send_command.side_effect = _mower(sent, takes=takes, rejects=rejects)
    await _setup(hass, config_entry)
    rain = _entity_id(hass, "switch", "rain_protection")
    await _wait_until(hass, lambda: hass.states.get(rain).state == "on")
    with pytest.raises(HomeAssistantError, match=message):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": rain}, blocking=True
        )
    assert len(sent) == 1
    assert hass.states.get(rain).state == "on"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_query_through_the_map_channel(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    """Answers the mower sends separately can be asked through the cloud."""
    channel.rpc_channel.send_command.side_effect = _answering([])
    asked: list[dict[str, Any]] = []

    async def _map(method: str, params: dict | None = None) -> bytes:
        if params is None:
            raise RoborockException("Command timed out after 10.0s")
        asked.append(params)
        return json.dumps(
            {"type": "MOW_SCHEDULE", "mow_schedule": {"plans": [{"id": 7}]}}
        ).encode()

    channel.map_rpc_channel.send_command.side_effect = _map
    await _setup(hass, config_entry)
    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    response = await hass.services.async_call(
        DOMAIN,
        "query",
        {"device_id": device.id, "query_type": "GET_MOW_SCHEDULE", "channel": "map"},
        blocking=True,
        return_response=True,
    )
    assert asked[-1]["type"] == "GET_MOW_SCHEDULE"
    answer = response[MOWER_DUID]["answer"]
    assert answer["json"]["mow_schedule"]["plans"] == [{"id": 7}]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "query",
            {"device_id": device.id, "query_type": "SET_RAINFALL", "channel": "map"},
            blocking=True,
            return_response=True,
        )
    assert await hass.config_entries.async_unload(config_entry.entry_id)
