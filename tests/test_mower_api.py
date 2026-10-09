"""Unit tests for the DPS parsing and command logic in mower_api.

They exercise the integration's bundled python-roborock copy (vendor/), i.e.
exactly the code that runs inside Home Assistant.
"""

import asyncio

import pytest

from custom_components.roborock_mower import mower_api
from custom_components.roborock_mower.vendor.roborock.data import RoborockCategory
from custom_components.roborock_mower.vendor.roborock.exceptions import (
    RoborockException,
)


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
    assert mower_api.EFF_MODE_LABELS == {1: "daily", 2: "efficient", 3: "manicure"}
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

    async def _raise(_payload: object, **_kwargs: object) -> None:
        raise RoborockException(f"Unexpected API Result: {_PREF_CONFIG_JSON}")

    api._send_remote_msg = _raise  # type: ignore[method-assign]
    result = asyncio.run(api._query({"type": "GET_MOW_PREFERENCE_CONFIG"}))
    assert result["type"] == "MOW_PREFERENCE_CONFIG"
    assert result["preference_config"]["global"]["effective"] == "DAILY"


def test_query_reraises_real_errors() -> None:
    api = _api()

    async def _raise(_payload: object, **_kwargs: object) -> None:
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

    async def _query(_payload: object, **_kwargs: object) -> dict:
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


def test_preference_write_refuses_without_current_preference() -> None:
    """A partial preference would reset passes, direction, edge cut ..."""
    api = _api()
    sent: list[dict] = []

    async def _get_pref() -> None:
        return None

    async def _send(payload: dict) -> str:
        sent.append(payload)
        return "ok"

    api._get_global_mow_preference = _get_pref  # type: ignore[method-assign]
    api._send_remote_msg = _send  # type: ignore[method-assign]
    with pytest.raises(RoborockException):
        asyncio.run(api.set_mow_eff_mode(2))
    with pytest.raises(RoborockException):
        asyncio.run(api.set_mow_preference(keep_edge=0))
    assert sent == []


def test_preference_write_updates_the_known_preference() -> None:
    api = _api()
    api.preference_config = {"global": {"keep_edge": 1, "direction": 90}, "mode": "GLOBAL"}

    async def _get_pref() -> dict:
        return dict(api.preference_config["global"])

    async def _send(payload: dict) -> list:
        return ["ok"]

    api._get_global_mow_preference = _get_pref  # type: ignore[method-assign]
    api._send_remote_msg = _send  # type: ignore[method-assign]
    asyncio.run(api.set_mow_preference(keep_edge=0, direction=45))
    assert api.preference_config["global"] == {"keep_edge": 0, "direction": 45}


class _FakeProduct:
    def __init__(self, category: object = None, model: str = "") -> None:
        self.category = category
        self.model = model


def test_is_mower_matches_category_or_model() -> None:
    # Correct category -> mower.
    assert mower_api.is_mower(_FakeProduct(category=RoborockCategory.MOWER))
    # Right model prefix even if the category is wrong/UNKNOWN (version-robust).
    assert mower_api.is_mower(
        _FakeProduct(category=RoborockCategory.UNKNOWN, model="roborock.mower.a222")
    )
    assert mower_api.is_mower(_FakeProduct(model="roborock.mower.a111"))
    # A vacuum is not a mower.
    assert not mower_api.is_mower(
        _FakeProduct(category=RoborockCategory.VACUUM, model="roborock.vacuum.a104")
    )
    assert not mower_api.is_mower(_FakeProduct(model=""))


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


class _FakeRpc:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def send_command(self, method: str, params: dict) -> object:
        self.calls.append((method, params))
        return self.result


class _FakeChannel:
    def __init__(self, result: object) -> None:
        self.rpc_channel = _FakeRpc(result)


def _api_with_result(result: object) -> "mower_api.MowerApi":
    return mower_api.MowerApi(
        product=None, channel=_FakeChannel(result), web_api=None, duid="test"
    )


def test_rejected_command_raises() -> None:
    api = _api_with_result(["fail"])
    try:
        asyncio.run(api.start())
    except RoborockException as err:
        assert "rejected" in str(err)
        assert "MOW_GLOBAL" in str(err)
    else:
        raise AssertionError("expected RoborockException for ['fail']")


def test_accepted_command_returns_result() -> None:
    api = _api_with_result(["ok"])
    assert asyncio.run(api.pause()) == ["ok"]
    method, params = api.channel.rpc_channel.calls[0]
    assert method == "remote_pb"
    assert params["app_button"] == "MOW_PAUSE"
    assert params["id"].isdigit()


def test_push_bookkeeping() -> None:
    api = _api()
    assert api.seconds_since_push is None
    status = api.apply_push({mower_api.DPS_MOW_STATE: 55})
    assert status.mow_state == 55
    assert api.push_count == 1
    assert api.seconds_since_push is not None


def test_new_library_dps_are_decoded() -> None:
    # 137 / 141 are named in python-roborock's RoborockMowerDataProtocol.
    status = mower_api.MowerStatus.from_dps({137: 2, 141: 1})
    assert status.mow_conf_mode == 2
    assert status.fc_state == 1


def test_apply_home_data_merges_device_status() -> None:
    class _Device:
        device_status = {"121": 55, "123": 76}
        online = True

    class _HomeData:
        device_products = {"test": (_Device(), None)}

    api = _api()
    api.apply_push({mower_api.DPS_MOW_PROGRESS: 10})
    status = api.apply_home_data(_HomeData())
    assert status.battery == 55
    assert status.mow_state == 76
    assert status.mow_progress == 10  # push-only DPS kept
    assert api.online is True


def test_areas_from_preference_config_ignores_garbage() -> None:
    assert mower_api.areas_from_preference_config(None) == []
    assert mower_api.areas_from_preference_config({"custom": "x"}) == []
    assert mower_api.areas_from_preference_config(
        {"custom": [{"area_id": 5, "area_name": "Hinten"}, {"area_name": "no id"}]}
    ) == [{"id": 5, "name": "Hinten"}]


def test_dps_ids_match_bundled_python_roborock() -> None:
    """Our DPS map must agree with the newest library's mower protocol."""
    from custom_components.roborock_mower.vendor.roborock.roborock_message import (
        RoborockMowerDataProtocol as P,
    )

    ours = {
        "ERROR_CODE": mower_api.DPS_ERROR_CODE,
        "BATTERY": mower_api.DPS_BATTERY,
        "MOW_TYPE": mower_api.DPS_MOW_TYPE,
        "MOW_STATE": mower_api.DPS_MOW_STATE,
        "CHARGE_STATE": mower_api.DPS_CHARGE_STATE,
        "DOCK_STATE": mower_api.DPS_DOCK_STATE,
        "CHARGE_TYPE": mower_api.DPS_CHARGE_TYPE,
        "PEND_TYPE": mower_api.DPS_PEND_TYPE,
        "MOW_EFF_MODE": mower_api.DPS_MOW_EFF_MODE,
        "MOW_HEIGHT": mower_api.DPS_MOW_HEIGHT,
        "MOW_PATTERN": mower_api.DPS_MOW_PATTEN,
        "MOW_CONF_MODE": mower_api.DPS_MOW_CONF_MODE,
        "MOW_PROGRESS": mower_api.DPS_MOW_PROGRESS,
        "BLADE_LIFESPAN": mower_api.DPS_BLADE_LIFESPAN,
        "FC_STATE": mower_api.DPS_FC_STATE,
        "GPS_COORDINATE": mower_api.DPS_GPS_COORDINATE,
        "OFF_DOCK_NO_TASK_STATUS": mower_api.DPS_OFF_DOCK_NO_TASK_STATUS,
        "NETWORK_CHANNEL": mower_api.DPS_NETWORK_CHANNEL,
    }
    for name, dps in ours.items():
        assert getattr(P, name).value == dps, name


def test_state_codes_cover_bundled_python_roborock() -> None:
    """Every mower state the newest library knows has a label here."""
    from custom_components.roborock_mower.vendor.roborock.data.mower import (
        RoborockMowerStateCode,
    )

    for code in RoborockMowerStateCode:
        if code.value >= 0:
            assert code.value in mower_api.ROBOT_DETAIL_STATE_LABELS, code



def _status(**dps: int) -> "mower_api.MowerStatus":
    codes = {
        "mow_state": mower_api.DPS_MOW_STATE,
        "charge_state": mower_api.DPS_CHARGE_STATE,
        "charge_type": mower_api.DPS_CHARGE_TYPE,
        "error_code": mower_api.DPS_ERROR_CODE,
        "dock_state": mower_api.DPS_DOCK_STATE,
    }
    return mower_api.MowerStatus.from_dps({codes[k]: v for k, v in dps.items()})


def test_derive_activity() -> None:
    derive = mower_api.derive_activity
    # Docked and charged (live: idle + charge_completed).
    assert derive(_status(mow_state=0, charge_state=2)) == "docked"
    assert derive(_status(mow_state=0, charge_state=3)) == "docked"  # waiting_charge
    # Driving back after "return to dock": idle task, not charging -> not docked.
    assert derive(_status(mow_state=0, charge_state=0), return_pending=True) == "returning"
    assert derive(_status(mow_state=0, charge_state=0, charge_type=1)) == "returning"
    assert derive(_status(mow_state=105, charge_state=0)) == "returning"
    assert derive(_status(mow_state=105, charge_state=1)) == "docked"
    # Stopped in the garden.
    assert derive(_status(mow_state=0, charge_state=0)) == "idle"
    assert derive(_status(mow_state=101, charge_state=0)) == "idle"
    # Task states.
    assert derive(_status(mow_state=55, charge_state=0)) == "mowing"
    assert derive(_status(mow_state=56, charge_state=0)) == "mowing"  # edge cut
    assert derive(_status(mow_state=8)) == "mowing"  # mapping
    assert derive(_status(mow_state=58)) == "paused"
    assert derive(_status(mow_state=71)) == "returning"
    assert derive(_status(mow_state=0, dock_state=1)) == "returning"
    assert derive(_status(mow_state=60)) == "error"
    assert derive(_status(mow_state=0, charge_state=2, error_code=5)) == "error"
    # Firmware without charge_state: state code alone.
    assert derive(_status(mow_state=0)) == "docked"
    assert derive(mower_api.MowerStatus()) == "docked"
    # Unknown code: caller decides.
    assert derive(_status(mow_state=250)) is None


def test_history_records_changes_without_gps() -> None:
    api = _api()
    api.apply_push({mower_api.DPS_MOW_STATE: 55, mower_api.DPS_GPS_COORDINATE: "x"})
    api.apply_push({mower_api.DPS_MOW_STATE: 55, mower_api.DPS_GPS_COORDINATE: "y"})
    api.apply_push({mower_api.DPS_BATTERY: 80})
    entries = list(api.history)
    assert [e["dps"] for e in entries] == [{"123": 55}, {"121": 80}]
    assert all(e["kind"] == "push" and e["time"] for e in entries)


def test_command_is_recorded_with_result() -> None:
    api = _api_with_result(["ok"])
    asyncio.run(api.pause())
    [entry] = list(api.history)
    assert entry["kind"] == "command"
    assert entry["command"] == "APP_BUTTON MOW_PAUSE"
    assert entry["result"] == "['ok']"


def test_edge_cut_sends_saved_areas_like_the_app() -> None:
    api = _api_with_result(["ok"])
    api.areas = [{"id": 2, "name": "Garten"}]
    asyncio.run(api.edge_cut())
    _method, params = api.channel.rpc_channel.calls[-1]
    assert params["app_button"] == "MOW_EDGE"
    assert params["modify_map"] == {"boundaries": [{"id": 2, "name": "Garten"}]}


def test_edge_cut_falls_back_to_bare_when_areas_rejected() -> None:
    api = _api_with_result(None)
    replies = [["fail"], ["ok"]]

    async def _send(method: str, params: dict) -> object:
        api.channel.rpc_channel.calls.append((method, params))
        return replies.pop(0)

    api.channel.rpc_channel.send_command = _send
    api.areas = [{"id": 2, "name": "Garten"}]
    assert asyncio.run(api.edge_cut()) == ["ok"]
    first, second = (params for _m, params in api.channel.rpc_channel.calls)
    assert "modify_map" in first
    assert "modify_map" not in second


def test_edge_cut_without_known_areas_is_bare() -> None:
    api = _api_with_result(["ok"])
    api.areas = []
    asyncio.run(api.edge_cut())
    _method, params = api.channel.rpc_channel.calls[-1]
    assert params == {"id": params["id"], "type": "APP_BUTTON", "app_button": "MOW_EDGE"}


def test_return_pending_lifecycle() -> None:
    api = _api_with_result(["ok"])
    api.apply_push({mower_api.DPS_MOW_STATE: 58, mower_api.DPS_CHARGE_STATE: 0})
    asyncio.run(api.dock())
    assert api.return_pending
    # Arriving on the dock ends it ...
    api.apply_push({mower_api.DPS_CHARGE_STATE: 1})
    assert not api.return_pending
    # ... and so does any new task.
    api.apply_push({mower_api.DPS_CHARGE_STATE: 0})
    asyncio.run(api.dock())
    assert api.return_pending
    asyncio.run(api.start())
    assert not api.return_pending


def test_task_pending_bridges_start_until_task_reported() -> None:
    derive = mower_api.derive_activity
    idle_off_dock = _status(mow_state=0, charge_state=0)
    assert derive(idle_off_dock) == "idle"
    assert derive(idle_off_dock, task_pending=True) == "mowing"
    # A return request wins over a stale start.
    assert derive(idle_off_dock, return_pending=True, task_pending=True) == "returning"


def test_off_dock_value_104_means_returning() -> None:
    # Live (Q105): DP 143 = 104 while driving back after "return to dock".
    status = mower_api.MowerStatus.from_dps(
        {mower_api.DPS_MOW_STATE: 0, mower_api.DPS_CHARGE_STATE: 0, 143: 104}
    )
    assert mower_api.derive_activity(status) == "returning"


def test_leaving_the_dock_reads_mowing_until_task_state() -> None:
    api = _api()
    api.apply_push({mower_api.DPS_MOW_STATE: 0, mower_api.DPS_CHARGE_STATE: 2})
    assert not api.task_pending
    api.apply_push({mower_api.DPS_CHARGE_STATE: 0})  # live: 10:13:43, task not named yet
    assert api.task_pending
    assert mower_api.derive_activity(api.status, task_pending=api.task_pending) == "mowing"
    api.apply_push({mower_api.DPS_MOW_STATE: 57})  # "driving to zone"
    assert not api.task_pending


def test_start_command_sets_task_pending() -> None:
    api = _api_with_result(["ok"])
    api.apply_push({mower_api.DPS_MOW_STATE: 0, mower_api.DPS_CHARGE_STATE: 0})
    asyncio.run(api.start())
    assert api.task_pending
    asyncio.run(api.dock())
    assert not api.task_pending
    assert api.return_pending


def test_query_is_read_only() -> None:
    api = _api_with_result(None)
    for bad in ("APP_BUTTON", "SET_MOW_PREFERENCE", "remote_cmd"):
        try:
            asyncio.run(api.query(bad))
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad} must be rejected")
    sent: dict = {}

    async def _query(payload: dict) -> dict:
        sent.update(payload)
        return {"ok": True}

    api._query = _query  # type: ignore[method-assign]
    assert asyncio.run(
        api.query("get_robot_status", type="APP_BUTTON", app_button="MOW_GLOBAL", id="1")
    ) == {"ok": True}
    # The payload cannot turn a query into a command.
    assert sent["type"] == "GET_ROBOT_STATUS"
    assert "id" not in sent


def test_map_data_goes_through_the_map_channel() -> None:
    """Asked over the local connection the mower only answers ["ok"]."""
    from unittest.mock import AsyncMock, MagicMock

    channel = MagicMock()
    channel.map_rpc_channel.send_command = AsyncMock(return_value=b"MAP")
    api = mower_api.MowerApi(MagicMock(), channel, MagicMock(), "duid")
    assert asyncio.run(api.get_map_data("get_full_map", "APP_MAP1.bin")) == b"MAP"
    params = channel.map_rpc_channel.send_command.await_args.kwargs["params"]
    assert params["type"] == "GET_FULL_MAP"
    assert params["modify_map"] == {"name": "APP_MAP1.bin"}
    assert api.history[-1]["result"] == "<3 bytes>"
    with pytest.raises(ValueError):
        asyncio.run(api.get_map_data("SET_FULL_MAP"))
    channel.rpc_channel.send_command.assert_not_called()
