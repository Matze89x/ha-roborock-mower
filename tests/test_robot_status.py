"""The mower's full status (GET_ROBOT_STATUS): helpers, entities, privacy."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.roborock_mower.binary_sensor import BINARY_SENSORS
from custom_components.roborock_mower.const import DOMAIN
from custom_components.roborock_mower.mower_api import MowerApi
from custom_components.roborock_mower.robot_status import (
    REDACTED,
    RobotInfo,
    as_flag,
    as_utc_file_time,
    dig,
    next_plan_end,
    next_plan_start,
    redact_private,
)
from custom_components.roborock_mower.sensor import ROBOT_STATUS_SENSORS
from custom_components.roborock_mower.vendor.roborock.exceptions import (
    RoborockException,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOWER_DUID, FakeChannel, FakeMessage

BERLIN = ZoneInfo("Europe/Berlin")

# GET_ROBOT_STATUS of a docked RockNeo Q105 (firmware 02.72.44) right after a
# short mow ended from Home Assistant. Position and network ids are made up.
LIVE_STATUS: dict[str, Any] = {
    "id": "1791509654127",
    "type": "ROBOT_STATUS",
    "map_names": ["APP_MAP1.bin"],
    "slam": {"type": "INIT"},
    "navigation": {
        "type": "ERROR",
        "nav_sub_state": "UNKNOWN",
        "map": {"robot_pose": {"f_x": 1.65, "f_y": -0.13, "f_yaw": -2.56}},
        "nav_error": {"delete_errors": ["NO_MAP", "MAP_EXCEEDS_LIMIT"]},
        "ai_obs_cmd": {"generic_obs_avoidance": True, "class_obs_avoidance": True},
        "robot_gps": {"latitude": 1.234567, "longitude": 2.345678},
        "geofence_error": {"delete_errors": ["OUTSIDE_E_FENCE"]},
        "detail_state": "IDLE",
        "living_obstacle": {},
        "map_editing": False,
    },
    "hardware": {
        "battery": {"charged": True, "percent": 100},
        "mcu_state": "MCU_LOW_POWER",
        "cutter_info": {"type": "MAIN_CUTTER_SPEED", "has_edge_cutter": True},
        "wheel": {},
        "safety_lock_status": False,
    },
    "network": {
        "mac": "02:00:00:00:00:01",
        "ip": "192.0.2.10",
        "ssid": "Example 2,4 GHz",
        "bssid": "02:00:00:00:00:02",
        "rssi": -62,
        "wifi_band": "2.4G",
    },
    "fsm_map_state": "IDLE",
    "fsm_mow_state": "IDLE",
    "fsm_dock_state": "IDLE",
    "fsm_ota_state": "IDLE",
    "fsm_charge_state": "CHARGE_COMPLETED",
    "fsm_mcu_state": "MCU_LOW_POWER",
    "bluetooth": {"mac": "02:00:00:00:00:03", "name": "rr-a222_EE-0001"},
    "mow_progress": {
        "mow_all_area": 79.1225052,
        "expected_time": 2464.03125,
        "cur_mow_progress": 3.21021199,
        "regions": [{"id": 2, "coverage_rate": 1.59878659, "cur_coverage_rate": 3.21}],
    },
    "rtk": {
        "position_type": "FIXED_SOLUTION",
        "nrtk": {
            "result": "SWITCHSUCCESS",
            "rtk_mode": "BASE_RTK",
            "dock_nrtk_status": "DOCK_NRTK_DISABLE",
            "account_mode": "NO_ACCOUNT",
        },
    },
    "runtime_state": "NORMAL",
    "wireless_devices": {
        "rtk_position": "FIXED_SOLUTION",
        "route": "WLAN0",
        "wifi": {"state": "CONNECTED", "level": "GOOD"},
        "mobile_4g": {"state": "CONNECTED"},
    },
    "mow_working_state": "IDLE",
    "fsm_anti_theft_state": "CLOSED",
    "robot_status_event": ["MOW_TASK_FINISH"],
    "fsm_rtk_state": "ACTIVE",
    "robot_task": {"working_state": "IDLE", "robot_detail_state": "FREE"},
    "mowing_zones": [{"id": 2}],
    "map_abstracts": [
        {"name": "APP_MAP1.bin", "file_change_time": "2026-10-09-08-15-21"}
    ],
    "next_plan": {
        "id": 2204435469,
        "start": "1791028800",
        "end": "1791043200",
        "days": [{"type": "FRIDAY"}],
        "mode": "GLOBAL",
        "fsm_state": "MOW_GLOBAL",
    },
    "fsm_energy_state": "SLEEP",
    "last_mow_abstract": {
        "start": {"time": "1791533613"},
        "end": {"type": "APP_END", "time": "1791533671"},
        "area": 1.2750001,
        "fsm_state": "MOW_GLOBAL",
        "seconds": 43,
        "percentage": 3.21021199,
        "abnormal_end": False,
    },
    "lora_status": "PAIRED",
}

LIVE_PREFERENCE: dict[str, Any] = {
    "type": "MOW_PREFERENCE_CONFIG",
    "preference_config": {
        "global": {
            "mow_times": 1,
            "effective": "DAILY",
            "direction": 90,
            "keep_edge": 1,
            "mode": "GLOBAL",
            "direction_type": "AUTO_DEFLECTION",
            "boundary_perception": "INTELLIGENCE",
            "rotation_angle": 15,
        },
        "custom": [{"area_name": "Garten", "area_id": 2, "mode": "GLOBAL"}],
        "mode": "GLOBAL",
    },
}

PRIVATE_VALUES = ("1.234567", "02:00:00:00:00:0", "192.0.2.10", "Example 2,4", "EE-0001")


def _info(now: datetime | None = None) -> RobotInfo:
    return RobotInfo(
        status=redact_private(LIVE_STATUS),
        preference=LIVE_PREFERENCE["preference_config"]["global"],
        now=now or datetime(2026, 10, 9, 10, 17, tzinfo=BERLIN),
        updated=datetime(2026, 10, 9, 8, 17, tzinfo=UTC),
        local_connected=True,
    )


# -- helpers --------------------------------------------------------------------


def test_redact_private_hides_position_and_network_ids() -> None:
    redacted = redact_private(LIVE_STATUS)
    text = json.dumps(redacted)
    for value in PRIVATE_VALUES:
        assert value not in text
    assert redacted["navigation"]["robot_gps"] == REDACTED
    assert redacted["network"]["rssi"] == -62
    assert redacted["network"]["mac"] == REDACTED
    assert redacted["bluetooth"] == REDACTED
    assert redacted["rtk"]["position_type"] == "FIXED_SOLUTION"
    # The original is left alone.
    assert LIVE_STATUS["network"]["ip"] == "192.0.2.10"


def test_dig() -> None:
    assert dig(LIVE_STATUS, "last_mow_abstract", "end", "type") == "APP_END"
    assert dig(LIVE_STATUS, "map_abstracts", 0, "name") == "APP_MAP1.bin"
    assert dig(LIVE_STATUS, "map_abstracts", 5, "name") is None
    assert dig(LIVE_STATUS, "missing", "deeper") is None
    assert dig(LIVE_STATUS, "lora_status", "x") is None
    assert dig(None, "x") is None


def test_next_plan_start_uses_time_of_day_and_weekdays() -> None:
    plan = LIVE_STATUS["next_plan"]
    # Friday morning: today 14:00 (the template is a Saturday, 14:00 CEST).
    morning = datetime(2026, 10, 9, 10, 17, tzinfo=BERLIN)
    assert next_plan_start(plan, morning) == datetime(2026, 10, 9, 14, 0, tzinfo=BERLIN)
    # Friday evening: next Friday, still 14:00 local after the clocks change.
    evening = datetime(2026, 10, 9, 15, 0, tzinfo=BERLIN)
    start = next_plan_start(plan, evening)
    assert start == datetime(2026, 10, 16, 14, 0, tzinfo=BERLIN)
    assert next_plan_end(plan, start) == datetime(2026, 10, 16, 18, 0, tzinfo=BERLIN)
    winter = datetime(2026, 11, 1, 9, 0, tzinfo=BERLIN)
    assert next_plan_start(plan, winter) == datetime(2026, 11, 6, 14, 0, tzinfo=BERLIN)


def test_next_plan_start_one_off_and_missing() -> None:
    one_off = {"start": "1791028800", "end": "1791043200", "days": []}
    before = datetime(2026, 10, 1, tzinfo=UTC)
    assert next_plan_start(one_off, before) == datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    assert next_plan_start(one_off, datetime(2026, 10, 4, tzinfo=UTC)) is None
    assert next_plan_start(None, before) is None
    assert next_plan_start({"days": [{"type": "MONDAY"}]}, before) is None


def test_small_converters() -> None:
    assert as_utc_file_time("2026-10-09-08-15-21") == datetime(
        2026, 10, 9, 8, 15, 21, tzinfo=UTC
    )
    assert as_utc_file_time("yesterday") is None
    assert as_flag(True) is True
    assert as_flag(1) is True
    assert as_flag(0) is False
    assert as_flag("false") is False
    assert as_flag(None) is None
    assert as_flag("maybe") is None


def test_sensor_values_from_the_live_status() -> None:
    info = _info()
    values = {desc.key: desc.value_fn(info) for desc in ROBOT_STATUS_SENSORS}
    assert values == {
        "lawn_area": pytest.approx(79.1225052),
        "estimated_mow_time": pytest.approx(2464.03125),
        "next_mow": datetime(2026, 10, 9, 14, 0, tzinfo=BERLIN),
        "last_mow_start": datetime(2026, 10, 9, 8, 13, 33, tzinfo=UTC),
        "last_mow_end": datetime(2026, 10, 9, 8, 14, 31, tzinfo=UTC),
        "last_mow_duration": 43,
        "last_mow_area": pytest.approx(1.2750001),
        "last_mow_coverage": pytest.approx(3.21021199),
        "last_mow_end_reason": "app_end",
        "last_event": "mow_task_finish",
        "wifi_signal": -62,
        "network_route": "wlan0",
        "mobile_network": "connected",
        "rtk_position": "fixed_solution",
        "wifi_quality": "good",
        "wifi_state": "connected",
        "wifi_band": "2.4G",
        "rtk_mode": "base_rtk",
        "rtk_state": "active",
        "network_rtk": "dock_nrtk_disable",
        "lora_status": "paired",
        "anti_theft": "closed",
        "energy_state": "sleep",
        "runtime_state": "normal",
        "ota_state": "idle",
        "working_state": "idle",
        "docking_state": "idle",
        "mapping_state": "idle",
        "navigation_state": "error",
        "slam_state": "init",
        "mcu_state": "mcu_low_power",
        "map_name": "APP_MAP1",
        "map_updated": datetime(2026, 10, 9, 8, 15, 21, tzinfo=UTC),
        "status_updated": datetime(2026, 10, 9, 8, 17, tzinfo=UTC),
        "mow_passes": 1,
        "mow_direction": 90,
        "direction_mode": "auto_deflection",
        "rotation_angle": 15,
        "boundary_perception": "intelligence",
    }
    by_key = {desc.key: desc for desc in ROBOT_STATUS_SENSORS}
    assert by_key["next_mow"].attrs_fn(info) == {
        "end": datetime(2026, 10, 9, 18, 0, tzinfo=BERLIN).isoformat(),
        "days": ["friday"],
        "mode": "global",
    }


def test_sensor_values_before_the_mower_answered() -> None:
    empty = RobotInfo(status={}, preference={}, now=dt_util.now())
    for desc in ROBOT_STATUS_SENSORS:
        assert desc.value_fn(empty) is None, desc.key
        if desc.attrs_fn is not None:
            assert desc.attrs_fn(empty) is None, desc.key
    for desc in BINARY_SENSORS:
        assert desc.value_fn(empty) is None, desc.key


def test_binary_sensor_values_from_the_live_status() -> None:
    info = _info()
    assert {desc.key: desc.value_fn(info) for desc in BINARY_SENSORS} == {
        "last_mow_aborted": False,
        "local_connection": True,
        "obstacle_avoidance": True,
        "object_recognition": True,
        "edge_cutter": True,
        "safety_lock": False,
        "map_editing": False,
        "keep_edge": True,
    }


def test_translations_cover_every_entity() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "custom_components" / DOMAIN
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        entity = json.loads((root / name).read_text())["entity"]
        for desc in ROBOT_STATUS_SENSORS:
            assert desc.translation_key in entity["sensor"], (name, desc.key)
        for desc in BINARY_SENSORS:
            assert desc.translation_key in entity["binary_sensor"], (name, desc.key)


def test_message_counts_use_protocol_names() -> None:
    api = MowerApi.__new__(MowerApi)
    api.message_counts = {}
    assert api.note_message(4) is True
    assert api.note_message(4) is False
    api.note_message(102)
    api.note_message(999)
    assert api.message_counts == {"general_request": 2, "rpc_response": 1, "999": 1}


@pytest.mark.parametrize("as_text", [True, False])
def test_query_answers_stay_out_of_log_and_history(
    caplog: pytest.LogCaptureFixture, as_text: bool
) -> None:
    channel = FakeChannel()
    if as_text:  # how the Q105 answers
        channel.rpc_channel.send_command = AsyncMock(
            side_effect=RoborockException(
                f"Unexpected API Result: {json.dumps(LIVE_STATUS)}"
            )
        )
    else:
        channel.rpc_channel.send_command = AsyncMock(return_value=LIVE_STATUS)
    api = MowerApi(MagicMock(), channel, MagicMock(), "duid")
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(api.query("GET_ROBOT_STATUS")) == LIVE_STATUS
    history = json.dumps(list(api.history))
    assert "<answer," in history
    for value in PRIVATE_VALUES:
        assert value not in caplog.text
        assert value not in history


# -- Home Assistant -------------------------------------------------------------


def _answering(calls: list[str], status: dict[str, Any] | None = None):
    """A send_command that answers the queries like the live mower."""

    async def _send(method: str, params: dict) -> object:
        calls.append(params["type"])
        if params["type"] == "GET_ROBOT_STATUS":
            answer = status if status is not None else LIVE_STATUS
            raise RoborockException(f"Unexpected API Result: {json.dumps(answer)}")
        if params["type"] == "GET_MOW_PREFERENCE_CONFIG":
            raise RoborockException(
                f"Unexpected API Result: {json.dumps(LIVE_PREFERENCE)}"
            )
        return ["ok"]

    return _send


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _wait_until(hass: HomeAssistant, check) -> None:
    for _ in range(100):
        await hass.async_block_till_done()
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{MOWER_DUID}_{key}"
    )
    assert entity_id, f"{platform} {key} not registered"
    return entity_id


async def test_status_entities_follow_the_mower(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    calls: list[str] = []
    channel.rpc_channel.send_command.side_effect = _answering(calls)
    await _setup(hass, config_entry)

    lawn_area = _entity_id(hass, "sensor", "lawn_area")
    await _wait_until(hass, lambda: hass.states.get(lawn_area).state != "unknown")

    assert float(hass.states.get(lawn_area).state) == pytest.approx(79.1225052)
    assert hass.states.get(lawn_area).attributes["unit_of_measurement"] == "m²"
    last_end = hass.states.get(_entity_id(hass, "sensor", "last_mow_end"))
    assert dt_util.parse_datetime(last_end.state) == datetime(
        2026, 10, 9, 8, 14, 31, tzinfo=UTC
    )
    assert last_end.attributes["mode"] == "mow_global"
    next_mow = hass.states.get(_entity_id(hass, "sensor", "next_mow"))
    start = dt_util.as_local(dt_util.parse_datetime(next_mow.state))
    assert (start.weekday(), start.hour, start.minute) == (4, 14, 0)
    assert next_mow.attributes["days"] == ["friday"]
    assert hass.states.get(_entity_id(hass, "sensor", "rtk_position")).state == (
        "fixed_solution"
    )
    assert float(hass.states.get(_entity_id(hass, "sensor", "wifi_signal")).state) == -62
    assert (
        hass.states.get(_entity_id(hass, "binary_sensor", "last_mow_aborted")).state
        == "off"
    )

    # Details are registered but disabled until the user enables them.
    registry = er.async_get(hass)
    for platform, key in (("sensor", "lora_status"), ("binary_sensor", "keep_edge")):
        entry = registry.async_get(_entity_id(hass, platform, key))
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category == "diagnostic"
        assert hass.states.get(entry.entity_id) is None

    # Routine polls stay out of the event history; nothing private is kept.
    coordinator = config_entry.runtime_data.coordinators[0]
    history = json.dumps(list(coordinator.mower_api.history))
    assert "GET_ROBOT_STATUS" not in history
    stored = json.dumps(coordinator.robot_status)
    for value in PRIVATE_VALUES:
        assert value not in stored
    assert "GET_MOW_PREFERENCE_CONFIG" in calls

    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_state_change_push_reads_the_status_again(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    calls: list[str] = []
    mowing = copy.deepcopy(LIVE_STATUS)
    channel.rpc_channel.send_command.side_effect = _answering(calls)
    with patch(
        "custom_components.roborock_mower.coordinator.ROBOT_STATUS_SETTLE_DELAY", 0
    ):
        await _setup(hass, config_entry)
        lawn_area = _entity_id(hass, "sensor", "lawn_area")
        await _wait_until(hass, lambda: calls.count("GET_ROBOT_STATUS") == 1)

        # Only the battery changed: no new query.
        channel.callback(FakeMessage(b'{"dps":{"121":99}}'))
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)
        assert calls.count("GET_ROBOT_STATUS") == 1

        # A task started: ask again right away.
        mowing["mow_progress"]["mow_all_area"] = 80.5
        channel.rpc_channel.send_command.side_effect = _answering(calls, mowing)
        channel.callback(FakeMessage(b'{"dps":{"123":55}}'))
        await _wait_until(hass, lambda: calls.count("GET_ROBOT_STATUS") == 2)
        await _wait_until(
            hass, lambda: hass.states.get(lawn_area).state.startswith("80.5")
        )
        assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_query_and_scan_actions_redact_private_data(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    calls: list[str] = []
    answer_status = _answering(calls)

    async def _send(method: str, params: dict) -> object:
        if params["type"] == "GET_CONSUMABLES":
            raise RoborockException('Unexpected API Result: {"blade":{"percent":71}}')
        if params["type"] == "GET_NOTHING":
            raise RoborockException("Unsupported request")
        return await answer_status(method, params)

    channel.rpc_channel.send_command.side_effect = _send
    await _setup(hass, config_entry)
    [device] = dr.async_entries_for_config_entry(
        dr.async_get(hass), config_entry.entry_id
    )

    response = await hass.services.async_call(
        DOMAIN,
        "query",
        {"device_id": device.id, "query_type": "GET_ROBOT_STATUS"},
        blocking=True,
        return_response=True,
    )
    text = json.dumps(response)
    for value in PRIVATE_VALUES:
        assert value not in text
    assert response[MOWER_DUID]["answer"]["network"]["rssi"] == -62

    response = await hass.services.async_call(
        DOMAIN,
        "scan_queries",
        {
            "device_id": device.id,
            "query_types": ["get_consumables", "GET_NOTHING", "GET_ROBOT_STATUS"],
        },
        blocking=True,
        return_response=True,
    )
    result = response[MOWER_DUID]
    assert result["answered"]["GET_CONSUMABLES"] == {"blade": {"percent": 71}}
    assert result["answered"]["GET_ROBOT_STATUS"]["network"]["mac"] == REDACTED
    assert "Unsupported" in result["failed"]["GET_NOTHING"]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "scan_queries",
            {"device_id": device.id, "query_types": ["APP_BUTTON"]},
            blocking=True,
            return_response=True,
        )
