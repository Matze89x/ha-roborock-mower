"""Unit tests for the DPS parsing logic in mower_api.

Loaded by file path so the tests don't import the package __init__ (which pulls
in Home Assistant). mower_api itself only depends on python-roborock + stdlib.
"""

import importlib.util
import sys
from pathlib import Path

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
