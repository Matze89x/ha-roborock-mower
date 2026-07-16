"""Unit tests for the DPS parsing logic in mower_api.

Loaded by file path so the tests don't import the package __init__ (which pulls
in Home Assistant). mower_api itself only depends on python-roborock + stdlib.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

from roborock.exceptions import RoborockException

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "roborock_mower"
    / "mower_api.py"
)
_spec = importlib.util.spec_from_file_location("mower_api", _MODULE_PATH)
mower_api = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass can resolve the module via sys.modules.
sys.modules["mower_api"] = mower_api
_spec.loader.exec_module(mower_api)


# Real device_status captured live from the RockNeo Q105 (paused mid-mow).
REAL_DEVICE_STATUS = {
    "121": 78,
    "122": 1,
    "123": 58,
    "124": 0,
    "125": 0,
    "126": 0,
    "127": 0,
    "129": 0,
    "132": 1,
    "133": 1,
    "135": 0,
    "138": 0,
    "139": 55,
    "142": "<redacted-gps>",  # real value is a base64 protobuf of the mower's position
    "143": 0,
    "144": 0,
    "145": 1,
}


class FakeMessage:
    """Minimal stand-in for a RoborockMessage (only .payload is read)."""

    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload


def test_coerce_dps_converts_string_keys() -> None:
    assert mower_api.coerce_dps({"121": 86, "123": 56}) == {121: 86, 123: 56}


def test_coerce_dps_handles_none_and_bad_keys() -> None:
    assert mower_api.coerce_dps(None) == {}
    assert mower_api.coerce_dps({"x": 1, "122": 2}) == {122: 2}


def test_from_dps_maps_real_payload() -> None:
    status = mower_api.MowerStatus.from_dps(mower_api.coerce_dps(REAL_DEVICE_STATUS))
    assert status.battery == 78
    assert status.mow_state == 58
    assert status.mow_progress == 55
    assert status.mow_eff_mode == 1
    assert status.mow_type == 1
    assert status.network_channel == 1
    assert status.charge_state == 0
    assert status.error_code is None  # dps 120 absent from payload
    assert status.mow_height is None  # dps 134 absent from payload
    assert status.gps_coordinate == REAL_DEVICE_STATUS["142"]
    # New DPS not present in this capture default to None.
    assert status.dock_state is None  # dps 128
    assert status.pend_type is None  # dps 130
    assert status.blade_lifespan is None  # dps 140


def test_mow_state_label() -> None:
    def label(code: int) -> str | None:
        return mower_api.MowerStatus.from_dps({mower_api.DPS_MOW_STATE: code}).mow_state_label

    assert label(0) == "idle"
    assert label(55) == "mowing"  # MOW_ZIG_ZAG
    assert label(56) == "mowing_edge"  # MOW_EDGE
    assert label(58) == "paused"  # MOW_SUSPEND
    assert label(71) == "returning"  # MOW_TO_DOCK_INITIALIZING
    assert label(60) == "fault"
    assert label(151) == "charging"
    # Unknown code falls back to the raw number as a string.
    assert label(9999) == "9999"
    assert mower_api.MowerStatus.from_dps({}).mow_state_label is None


def test_mow_state_activity_frozensets() -> None:
    # No code appears in more than one activity bucket.
    buckets = [
        mower_api.MOW_STATES_MOWING,
        mower_api.MOW_STATES_PAUSED,
        mower_api.MOW_STATES_RETURNING,
        mower_api.MOW_STATES_ERROR,
        mower_api.MOW_STATES_DOCKED,
    ]
    for i, first in enumerate(buckets):
        for second in buckets[i + 1 :]:
            assert not (first & second), f"overlap: {first & second}"
    assert 55 in mower_api.MOW_STATES_MOWING
    assert 58 in mower_api.MOW_STATES_PAUSED
    assert {71, 72} <= mower_api.MOW_STATES_RETURNING
    assert {59, 60, 69, 73, 74, 154} <= mower_api.MOW_STATES_ERROR
    assert {76, 151} <= mower_api.MOW_STATES_DOCKED


def test_dp_value_label_maps() -> None:
    assert mower_api.MOW_TYPE_LABELS[1] == "full_mow"
    assert mower_api.MOW_TYPE_LABELS[2] == "edge_cut"
    assert mower_api.MOW_TYPE_LABELS[3] == "selection"
    assert mower_api.CHARGE_STATE_LABELS[1] == "charging"
    assert mower_api.CHARGE_TYPE_LABELS[2] == "rain_dock"
    assert mower_api.PEND_TYPE_LABELS[1] == "app_pause"


def test_efficiency_mode_maps_round_trip() -> None:
    assert mower_api.EFF_MODE_LABELS == {1: "Daily", 2: "Efficient", 3: "Manicure"}
    for code, label in mower_api.EFF_MODE_LABELS.items():
        assert mower_api.EFF_MODE_REVERSE[label] == code


def test_boundaries_payload() -> None:
    payload = mower_api._boundaries_payload(
        [{"id": 1, "name": "Front"}, {"id": 2}]
    )
    assert payload == {
        "boundaries": [
            {"id": 1, "name": "Front"},
            {"id": 2, "name": ""},
        ]
    }


def _api() -> "mower_api.MowerApi":
    """A MowerApi with no real channel (methods under test are monkeypatched)."""
    return mower_api.MowerApi(
        product=None, channel=None, web_api=None, duid="test"
    )


# Captured live from a RockNeo Q105 (the device returns GET_* as a JSON string).
_PREF_CONFIG_JSON = (
    '{"type":"MOW_PREFERENCE_CONFIG","preference_config":{'
    '"global":{"mow_times":1,"effective":"DAILY","direction":5,"keep_edge":1,'
    '"mode":"GLOBAL","direction_type":"AUTO_DEFLECTION"},'
    '"custom":[{"effective":"MANICURE","area_name":"A1","area_id":2,"mode":"CUSTOM"},'
    '{"effective":"MANICURE","area_name":"A2","area_id":3,"mode":"CUSTOM"},'
    '{"effective":"DAILY","area_name":"A3","area_id":4,"mode":"GLOBAL"}],'
    '"mode":"GLOBAL"}}'
)


def test_query_recovers_json_from_unexpected_result() -> None:
    api = _api()

    async def _raise(_payload: object) -> None:
        raise RoborockException(f"Unexpected API Result: {_PREF_CONFIG_JSON}")

    api._send_remote_msg = _raise  # type: ignore[method-assign]
    result = asyncio.run(api._query({"type": "GET_MOW_PREFERENCE_CONFIG"}))
    assert result["type"] == "MOW_PREFERENCE_CONFIG"
    assert result["preference_config"]["global"]["effective"] == "DAILY"


def test_query_reraises_real_errors() -> None:
    api = _api()

    async def _raise(_payload: object) -> None:
        raise RoborockException("device offline")

    api._send_remote_msg = _raise  # type: ignore[method-assign]
    try:
        asyncio.run(api._query({"type": "GET_MAP_NAMES"}))
    except RoborockException as err:
        assert "offline" in str(err)
    else:
        raise AssertionError("expected RoborockException to propagate")


def test_get_areas_from_preference_custom() -> None:
    api = _api()

    async def _query(_payload: object) -> dict:
        import json as _json

        return _json.loads(_PREF_CONFIG_JSON)

    api._query = _query  # type: ignore[method-assign]
    areas = asyncio.run(api.get_areas())
    assert areas == [
        {"id": 2, "name": "A1"},
        {"id": 3, "name": "A2"},
        {"id": 4, "name": "A3"},
    ]


def test_cancel_dock_sends_dock_end() -> None:
    api = _api()
    sent: dict = {}

    async def _send(payload: dict) -> str:
        sent.update(payload)
        return "ok"

    api._send_remote_msg = _send  # type: ignore[method-assign]
    asyncio.run(api.cancel_dock())
    assert sent == {"type": "APP_BUTTON", "app_button": "DOCK_END"}


def test_set_mow_height_actuates_then_persists() -> None:
    api = _api()
    calls: list[dict] = []

    async def _get_pref() -> dict:
        return {"direction": 5}

    async def _send(payload: dict) -> str:
        calls.append(payload)
        return "ok"

    api._get_global_mow_preference = _get_pref  # type: ignore[method-assign]
    api._send_remote_msg = _send  # type: ignore[method-assign]
    asyncio.run(api.set_mow_height(45))
    # First the live REMOTE_CMD, then the preference persist with the same value.
    assert calls[0]["type"] == "REMOTE_CMD"
    assert calls[0]["remote_cmd"]["main_cutter_height"] == 45
    assert calls[1]["type"] == "SET_MOW_PREFERENCE"
    assert calls[1]["mow_preference"]["height"] == 45


def test_set_mow_height_clamps_negative() -> None:
    api = _api()
    calls: list[dict] = []

    async def _get_pref() -> None:
        return None

    async def _send(payload: dict) -> str:
        calls.append(payload)
        return "ok"

    api._get_global_mow_preference = _get_pref  # type: ignore[method-assign]
    api._send_remote_msg = _send  # type: ignore[method-assign]
    asyncio.run(api.set_mow_height(-5))
    assert calls[0]["remote_cmd"]["main_cutter_height"] == 0


def test_set_mow_eff_mode_builds_string_enum_payload() -> None:
    api = _api()
    sent: dict = {}

    async def _get_pref() -> dict:
        return {"effective": "MANICURE", "direction": 5, "mode": "GLOBAL"}

    async def _send(payload: dict) -> str:
        sent.update(payload)
        return "ok"

    api._get_global_mow_preference = _get_pref  # type: ignore[method-assign]
    api._send_remote_msg = _send  # type: ignore[method-assign]
    asyncio.run(api.set_mow_eff_mode(1))
    assert sent["type"] == "SET_MOW_PREFERENCE"
    # effective written as the protobuf enum NAME, other fields preserved.
    assert sent["mow_preference"]["effective"] == "DAILY"
    assert sent["mow_preference"]["direction"] == 5
    assert sent["mow_preference"]["mode"] == "GLOBAL"


def test_redact_dps_masks_gps() -> None:
    dps = {mower_api.DPS_BATTERY: 80, mower_api.DPS_GPS_COORDINATE: "base64gps=="}
    red = mower_api.redact_dps(dps)
    assert red[mower_api.DPS_BATTERY] == 80
    assert red[mower_api.DPS_GPS_COORDINATE] == "<gps redacted>"
    # Original is untouched; a dict without GPS is returned unchanged.
    assert dps[mower_api.DPS_GPS_COORDINATE] == "base64gps=="
    assert mower_api.redact_dps({mower_api.DPS_BATTERY: 80}) == {
        mower_api.DPS_BATTERY: 80
    }


def test_parse_dps_push_extracts_status() -> None:
    msg = FakeMessage(b'{"dps":{"123":58},"t":1779540626}')
    assert mower_api.parse_dps_push(msg) == {123: 58}


def test_parse_dps_push_excludes_rpc_dps() -> None:
    msg = FakeMessage(b'{"dps":{"102":"{id:1}"}}')
    assert mower_api.parse_dps_push(msg) == {}


def test_parse_dps_push_handles_invalid() -> None:
    assert mower_api.parse_dps_push(FakeMessage(None)) == {}
    assert mower_api.parse_dps_push(FakeMessage(b"not json")) == {}
    assert mower_api.parse_dps_push(FakeMessage(b'{"no_dps":1}')) == {}
