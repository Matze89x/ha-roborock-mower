"""The mower's full status (GET_ROBOT_STATUS): helpers, entities, privacy."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, date, datetime
import json
import logging
from pathlib import Path
import struct
from types import SimpleNamespace
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
from custom_components.roborock_mower.diagnostics import (
    async_get_config_entry_diagnostics,
)
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

from .conftest import MOWER_DUID, FakeChannel, FakeMessage, enable_entities
from .protobuf import STREAM_FRAME

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

# Further answers of the same Q105 (found with scan_queries from the app plugin).
LIVE_EXTRA: dict[str, dict[str, Any]] = {
    "USER_MODE_CONFIG": {
        "type": "USER_MODE_CONFIG",
        "user_mode_config": {
            "rainfall_config": {"type": "RAIN_NO", "enable": True, "delay_time": 8},
            "not_disturb_config": {
                "enable": True,
                "time": [{"start": {"hour": 20, "minute": 30}, "end": {"hour": 8}}],
            },
            "anti_theft_config": {
                "enable": False,
                "e_fence_range": 100,
                "anti_theft_state": "CLOSED",
            },
            "audio_config": {},
            "nav_common_config": {
                "channel_vision_avoid": False,
                "navigation_type": "MORE_COVERAGE",
                "path_detect_avoid": True,
                "edge_vision_avoid": False,
                "obstacle_image_privacy": True,
            },
            "rtk_mode_config": "BASE_RTK",
            "random_gngga_config": "1234567890123456789",
        },
    },
    "FAULT_RECORDS": {
        "type": "FAULT_RECORDS",
        "fault_records": {
            "cards": [
                {
                    "e_code": 16,
                    "occur_count": 2,
                    "items": [
                        {"fault_time": "2026-09-30", "task_type": "RUNTIME"},
                        {"fault_time": "2026-09-18", "task_type": "MOW"},
                    ],
                },
                {
                    "e_code": 35,
                    "occur_count": 3,
                    "items": [
                        {"fault_time": "2026-10-02", "task_type": "MOW"},
                        {"fault_time": "2026-09-23", "task_type": "MOW"},
                        {"fault_time": "2026-09-22", "task_type": "MOW"},
                    ],
                },
            ]
        },
    },
    "ZONES_PLAN_INFO": {
        "type": "ZONES_PLAN_INFO",
        "zones_plan_info": {
            "zones_plan_info": [
                {"id": 2, "name": "Garten", "plan_id": [2848231976, 2204435469, 471772864]}
            ]
        },
    },
    "FEATURE_INFO": {
        "type": "FEATURE_INFO",
        "feature_info": {
            "cutter_feature": {"main_cutter_radius": 0.11, "main_cutter_diameter_mm": 220},
            "nav_feature": {"solution": "RTK_VISION"},
            "drive_feature": {"mode": "TWO_WHEEL_DRIVE"},
            "mobile_4g_feature": {"mode": "EXTERNAL_ESIM"},
            "sku_info": {
                "market_name": "Q105",
                "declare_cut_area": 500,
                "charge_current": 2,
                "battery_capacity": 4,
                "region": "EU",
                "real_cut_area": 700,
            },
        },
    },
}

PRIVATE_VALUES = (
    "1.234567",
    "02:00:00:00:00:0",
    "192.0.2.10",
    "Example 2,4",
    "EE-0001",
    "1234567890123456789",
)


def _info(now: datetime | None = None) -> RobotInfo:
    return RobotInfo(
        status=redact_private(LIVE_STATUS),
        preference=LIVE_PREFERENCE["preference_config"]["global"],
        now=now or datetime(2026, 10, 9, 10, 17, tzinfo=BERLIN),
        updated=datetime(2026, 10, 9, 8, 17, tzinfo=UTC),
        local_connected=True,
        extra=redact_private(LIVE_EXTRA),
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


def by_key_attrs(info: RobotInfo, key: str) -> Any:
    desc = next(desc for desc in ROBOT_STATUS_SENSORS if desc.key == key)
    return desc.attrs_fn(info)


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
        "hardware_error": "none",
        "remaining_mow_time": 0,
        "blade_speed": 0,
        "speed": 0,
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
        "boundary_perception": "intelligence",
        "plan_count": 3,
        "last_fault": 35,
        "last_fault_date": date(2026, 10, 2),
        "rain_delay": 8,
        "rain_state": "rain_no",
        "anti_theft_range": 100,
        "navigation_mode": "more_coverage",
        "fault_count": 5,
        "positioning": "rtk_vision",
        "rated_area": 500,
        "max_area": 700,
        "blade_diameter": 220,
        "battery_capacity": 4,
    }
    assert by_key_attrs(info, "plan_count") == {"zones": {"Garten": 3}}
    assert by_key_attrs(info, "last_fault") == {
        "date": "2026-10-02",
        "task": "mow",
        "faults": [
            {"code": 35, "count": 3, "last": "2026-10-02", "task": "mow"},
            {"code": 16, "count": 2, "last": "2026-09-30", "task": "runtime"},
        ],
    }
    by_key = {desc.key: desc for desc in ROBOT_STATUS_SENSORS}
    assert by_key["next_mow"].attrs_fn(info) == {
        "end": datetime(2026, 10, 9, 18, 0, tzinfo=BERLIN).isoformat(),
        "days": ["friday"],
        "mode": "global",
    }


def test_live_values_while_mowing() -> None:
    """Captured on the Q105 at 80 % of a scheduled whole-lawn mow."""
    status = copy.deepcopy(LIVE_STATUS)
    status["navigation"]["nav_task_progress"] = {
        "task": "MOW",
        "percent": 80,
        "area": 79.1225052,
        "current_area": {"id": 2, "percent": 80, "percentage": 80.5270309},
        "percentage": 80.5270309,
        "expected_time": 2576.40625,
    }
    status["hardware"]["cutter_info"]["main_cutter_speed"] = -2799
    status["hardware"]["wheel"] = {"linear_velocity": 0.401152462, "angular_velocity": -0.1}
    info = RobotInfo(status=status, preference={}, now=dt_util.now())
    values = {desc.key: desc.value_fn(info) for desc in ROBOT_STATUS_SENSORS}
    assert values["remaining_mow_time"] == 502
    assert values["blade_speed"] == 2799
    assert values["speed"] == pytest.approx(0.401152462)


def test_hardware_error_from_the_controller() -> None:
    by_key = {desc.key: desc for desc in ROBOT_STATUS_SENSORS}
    hardware_error = by_key["hardware_error"]
    assert hardware_error.attrs_fn(_info()) == {"errors": []}
    # Seen live in GET_ROBOT_INFO on a Q105 waking up.
    status = copy.deepcopy(LIVE_STATUS)
    status["hardware"]["mcu_error"] = {"errors": ["MAIN_CUTTER_DRIVER_IC_FAULT"]}
    info = RobotInfo(status=status, preference={}, now=dt_util.now())
    assert hardware_error.value_fn(info) == "main_cutter_driver_ic_fault"
    assert hardware_error.attrs_fn(info) == {"errors": ["main_cutter_driver_ic_fault"]}


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
        "rain_protection": True,
        "do_not_disturb": True,
        "do_not_disturb_active": False,
        "anti_theft_enabled": False,
        "path_obstacle_detection": True,
        "edge_camera_avoidance": False,
        "passage_camera_avoidance": False,
        "obstacle_photo_privacy": True,
    }


def test_no_faults_and_no_plans_read_as_zero() -> None:
    """Empty lists are left out of the answers."""
    info = RobotInfo(
        status={},
        preference={},
        now=dt_util.now(),
        extra={
            "FAULT_RECORDS": {"type": "FAULT_RECORDS", "fault_records": {}},
            "ZONES_PLAN_INFO": {"type": "ZONES_PLAN_INFO", "zones_plan_info": {}},
        },
    )
    values = {desc.key: desc.value_fn(info) for desc in ROBOT_STATUS_SENSORS}
    assert values["fault_count"] == 0
    assert values["plan_count"] == 0
    assert values["last_fault"] is None
    assert values["last_fault_date"] is None


@pytest.mark.parametrize(
    ("hour", "minute", "active"),
    [(20, 29, False), (20, 30, True), (23, 59, True), (0, 0, True), (7, 59, True), (8, 0, False)],
)
def test_do_not_disturb_window_spans_midnight(hour: int, minute: int, active: bool) -> None:
    dnd_now = next(desc for desc in BINARY_SENSORS if desc.key == "do_not_disturb_active")
    info = _info(datetime(2026, 10, 9, hour, minute, tzinfo=BERLIN))
    assert dnd_now.value_fn(info) is active
    assert dnd_now.attrs_fn(info) == {"start": "20:30", "end": "08:00"}


def test_translations_cover_every_entity() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "custom_components" / DOMAIN
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        entity = json.loads((root / name).read_text())["entity"]
        for desc in ROBOT_STATUS_SENSORS:
            assert desc.translation_key in entity["sensor"], (name, desc.key)
        for desc in BINARY_SENSORS:
            assert desc.translation_key in entity["binary_sensor"], (name, desc.key)


def test_frame_content_unpacks_rpc_answers() -> None:
    from custom_components.roborock_mower.robot_status import frame_content

    inner = json.dumps({"id": 1, "result": json.dumps({"map": {"ip": "192.0.2.10"}})})
    payload = json.dumps({"dps": {"102": inner}, "t": 5}).encode()
    assert frame_content(payload) == {
        "json": {"dps": {"102": {"id": 1, "result": {"map": {"ip": REDACTED}}}}, "t": 5}
    }
    assert frame_content(b"\x08\x01") == {"base64": "CAE="}


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

    async def _send(method: str, params: dict | None = None) -> object:
        if params is None:  # a plain RPC such as get_map
            calls.append(method)
            return ["ok"]
        calls.append(params["type"])
        if params["type"] == "GET_ROBOT_STATUS":
            answer = status if status is not None else LIVE_STATUS
            raise RoborockException(f"Unexpected API Result: {json.dumps(answer)}")
        if params["type"] == "GET_MOW_PREFERENCE_CONFIG":
            raise RoborockException(
                f"Unexpected API Result: {json.dumps(LIVE_PREFERENCE)}"
            )
        if (extra := LIVE_EXTRA.get(params["type"].removeprefix("GET_"))) is not None:
            raise RoborockException(f"Unexpected API Result: {json.dumps(extra)}")
        return ["ok"]

    return _send


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
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
    enable_entities(
        hass,
        config_entry,
        ("sensor", "lawn_area"),
        ("sensor", "plan_count"),
        ("sensor", "last_fault"),
        ("binary_sensor", "rain_protection"),
    )
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
    assert hass.states.get(_entity_id(hass, "sensor", "plan_count")).state == "3"
    assert hass.states.get(_entity_id(hass, "sensor", "last_fault")).state == "35"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "rain_protection")).state == "on"
    # The exact model from the mower's product details.
    [device] = dr.async_entries_for_config_entry(
        dr.async_get(hass), config_entry.entry_id
    )
    assert device.model == "RockNeo Q105"

    # Details are registered but disabled until the user enables them.
    registry = er.async_get(hass)
    for platform, key in (("sensor", "lora_status"), ("binary_sensor", "map_editing")):
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
    enable_entities(hass, config_entry, ("sensor", "lawn_area"))
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
    assert result["source"] == "given"
    assert result["tried"] == 3
    assert result["answered"]["GET_CONSUMABLES"] == {"blade": {"percent": 71}}
    assert result["answered"]["GET_ROBOT_STATUS"]["network"]["mac"] == REDACTED
    assert "Unsupported" in result["failed"]["GET_NOTHING"]
    assert result["rejected"] == []

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "scan_queries",
            {"device_id": device.id, "query_types": ["APP_BUTTON"]},
            blocking=True,
            return_response=True,
        )


async def test_only_data_points_the_model_has_get_entities(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The Q105 schema has no blade-life (140) or pause-reason (130) point."""
    registry = er.async_get(hass)
    config_entry.add_to_hass(hass)
    # Created by 0.2.0 and earlier; never filled on this model.
    old = registry.async_get_or_create(
        "sensor", DOMAIN, f"{MOWER_DUID}_blade_lifespan", config_entry=config_entry
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(old.entity_id) is None
    for key in ("blade_lifespan", "pend_type"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{MOWER_DUID}_{key}") is None
    for key in ("battery", "charge_type", "mow_state", "error_code"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{MOWER_DUID}_{key}")
    assert registry.async_get_entity_id("number", DOMAIN, f"{MOWER_DUID}_mow_height")


async def test_settings_can_be_changed(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    """Edge cut and direction are written back as the whole preference."""
    calls: list[str] = []
    answer = _answering(calls)
    sent: list[dict] = []

    async def _send(method: str, params: dict) -> object:
        if params["type"] == "SET_MOW_PREFERENCE":
            sent.append(params["mow_preference"])
            return ["ok"]
        return await answer(method, params)

    channel.rpc_channel.send_command.side_effect = _send
    await _setup(hass, config_entry)
    edge = _entity_id(hass, "switch", "edge_cut_while_mowing")
    direction = _entity_id(hass, "number", "mow_direction_angle")
    await _wait_until(hass, lambda: hass.states.get(edge).state == "on")
    assert float(hass.states.get(direction).state) == 90

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": edge}, blocking=True
    )
    assert sent[-1] == {**LIVE_PREFERENCE["preference_config"]["global"], "keep_edge": 0}
    assert hass.states.get(edge).state == "off"

    await hass.services.async_call(
        "number", "set_value", {"entity_id": direction, "value": 45}, blocking=True
    )
    assert sent[-1]["direction"] == 45
    assert sent[-1]["direction_type"] == "AUTO_DEFLECTION"
    assert sent[-1]["mow_times"] == 1

    # Direction mode and the turn per mow, with the app's values.
    mode = _entity_id(hass, "select", "direction_mode")
    rotation = _entity_id(hass, "select", "rotation_angle_select")
    assert hass.states.get(mode).state == "auto"
    assert hass.states.get(rotation).state == "15"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": mode, "option": "optimal"}, blocking=True
    )
    assert sent[-1]["direction_type"] == "NAV_EFFICIENT"
    assert hass.states.get(mode).state == "optimal"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": mode, "option": "custom"}, blocking=True
    )
    assert sent[-1]["direction_type"] == "CUSTOM"
    await hass.services.async_call(
        "select", "select_option", {"entity_id": rotation, "option": "60"}, blocking=True
    )
    assert sent[-1]["rotation_angle"] == 60
    assert "zones_with_own_settings" not in hass.states.get(mode).attributes

    # The lawn mower entity can stop a task (Home Assistant 2026.10+).
    mower = _entity_id(hass, "lawn_mower", "lawn_mower")
    await hass.services.async_call(
        "lawn_mower", "stop", {"entity_id": mower}, blocking=True
    )
    assert channel.rpc_channel.send_command.await_args.kwargs["params"]["app_button"] == "MOW_END"
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_save_map_data_writes_the_captured_messages(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel, tmp_path
) -> None:
    calls: list[str] = []
    answer = _answering(calls)

    async def _send(method: str, params: dict | None = None) -> object:
        return await answer(method, params)

    async def _map(method: str, params: dict | None = None) -> bytes:
        # Asked through the map channel, the mower sends the map itself.
        if method == "get_map":
            # A protobuf answer with a 64-bit number (could be GPS).
            return b"PB\x08\x01\x11" + struct.pack("<d", 49.3312)
        if params is None:
            raise RoborockException("Command timed out after 10.0s")
        if params["type"] == "GET_FULL_MAP":
            assert params["modify_map"] == {"name": "APP_MAP1.bin"}
            return b"\x00\x01\x02"
        if params["type"] == "GET_MAP_MOW_SNAPSHOT":
            # Every raw message during the recording is kept (pings aside).
            channel.callback(SimpleNamespace(protocol=301, payload=b"\x08\x01map"))
            channel.callback(SimpleNamespace(protocol=2, payload=b"ping"))
            channel.callback(SimpleNamespace(protocol=702, payload=STREAM_FRAME))
        raise RoborockException("Command timed out after 10.0s")

    channel.rpc_channel.send_command.side_effect = _send
    channel.map_rpc_channel.send_command.side_effect = _map
    hass.config.config_dir = str(tmp_path)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinators[0]
    await _wait_until(hass, lambda: coordinator.robot_status is not None)
    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    response = await hass.services.async_call(
        DOMAIN,
        "save_map_data",
        {"device_id": device.id, "wait": 5},
        blocking=True,
        return_response=True,
    )
    result = response[MOWER_DUID]
    # The data is in the answer itself: binary as base64, JSON unpacked,
    # protobuf decoded without its 64-bit numbers (the GPS position).
    assert result["messages"][0] == {
        "time": result["messages"][0]["time"],
        "protocol": 301,
        "bytes": 5,
        "base64": "CAFtYXA=",
    }
    stream = result["messages"][1]
    assert stream["protocol"] == 702
    assert stream["protobuf"]["5"]["12"]["23"] == {"1": REDACTED, "2": REDACTED}
    assert stream["protobuf"]["5"]["12"]["6"]["8"] == {"4": 3.5, "5": -2.25, "6": 1.5}
    assert "49.33" not in json.dumps(result)
    assert result["map_name"] == "APP_MAP1.bin"
    assert result["answers"]["GET_FULL_MAP"] == {"bytes": 3, "base64": "AAEC"}
    assert "timed out" in result["answers"]["GET_MAP_DIFFS"]["error"]
    # The app's map RPCs: through the map channel and the normal one.
    assert result["answers"]["get_map_map"] == {
        "bytes": 13,
        "protobuf": {"1": 1, "2": REDACTED},
    }
    assert result["answers"]["get_map_rpc"] == ["ok"]
    assert "timed out" in result["answers"]["get_map_diff_map"]["error"]
    folder = Path(result["folder"])
    assert folder.parent == tmp_path / DOMAIN
    assert (folder / "000_p301.bin").read_bytes() == b"\x08\x01map"
    # The diagnostics only list what arrived.
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    capture = diag["mowers"][0]["map_capture"]
    assert capture["messages_by_protocol"] == {"301": 1, "702": 1}
    assert capture["answers"]["get_map_map"] == {"bytes": 13}
    assert "49.33" not in json.dumps(diag, default=str)
    assert (folder / "GET_FULL_MAP_map.bin").read_bytes() == b"\x00\x01\x02"
    assert set(result["files"]) >= {
        "000_p301.bin",
        "GET_FULL_MAP_map.bin",
        "get_map_map_map.bin",
    }
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_zones_with_own_settings_are_shown(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    """Seen live: after a change in the app, the zone kept its own settings."""
    calls: list[str] = []
    own = copy.deepcopy(LIVE_PREFERENCE)
    own["preference_config"]["global"]["direction_type"] = "NAV_EFFICIENT"
    own["preference_config"]["custom"][0].update(
        mode="CUSTOM", direction_type="AUTO_DEFLECTION"
    )
    answer = _answering(calls)

    async def _send(method: str, params: dict | None = None) -> object:
        if params and params["type"] == "GET_MOW_PREFERENCE_CONFIG":
            raise RoborockException(f"Unexpected API Result: {json.dumps(own)}")
        return await answer(method, params)

    channel.rpc_channel.send_command.side_effect = _send
    await _setup(hass, config_entry)
    mode = _entity_id(hass, "select", "direction_mode")
    await _wait_until(hass, lambda: hass.states.get(mode).state == "optimal")
    assert hass.states.get(mode).attributes["zones_with_own_settings"] == ["Garten"]
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_new_installation_is_not_flooded(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    """Only what most people look at is enabled when the mower is added."""
    channel.rpc_channel.send_command.side_effect = _answering([])
    await _setup(hass, config_entry)
    entries = er.async_entries_for_config_entry(er.async_get(hass), config_entry.entry_id)
    enabled = {
        (entry.domain, str(entry.entity_category or "-"), entry.unique_id.removeprefix(f"{MOWER_DUID}_"))
        for entry in entries
        if entry.disabled_by is None
    }
    assert enabled == {
        ("lawn_mower", "-", "lawn_mower"),
        ("image", "-", "map"),
        ("sensor", "-", "battery"),
        ("sensor", "-", "mow_state"),
        ("sensor", "-", "mow_progress"),
        ("sensor", "-", "remaining_mow_time"),
        ("sensor", "-", "next_mow"),
        ("sensor", "-", "last_mow_end"),
        ("sensor", "-", "last_mow_duration"),
        ("sensor", "-", "last_mow_area"),
        ("button", "-", "edge_cut"),
        ("button", "-", "mow_area_2"),
        ("select", "config", "mow_eff_mode"),
        ("select", "config", "direction_mode"),
        ("select", "config", "rotation_angle_select"),
        ("number", "config", "mow_height"),
        ("number", "config", "mow_direction_angle"),
        ("number", "config", "mow_passes"),
        ("switch", "config", "edge_cut_while_mowing"),
        ("sensor", "diagnostic", "error_code"),
        ("sensor", "diagnostic", "last_mow_end_reason"),
        ("sensor", "diagnostic", "wifi_signal"),
        ("sensor", "diagnostic", "rtk_position"),
        ("binary_sensor", "diagnostic", "last_mow_aborted"),
    }
    assert len(entries) > 2 * len(enabled)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
