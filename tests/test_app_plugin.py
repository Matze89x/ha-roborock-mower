"""Query names from the official app plugin, and the scan_queries action."""

from __future__ import annotations

import io
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zipfile

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.roborock_mower.app_plugin import (
    HERMES_MAGIC,
    extract_query_names,
    hermes_strings,
)
from custom_components.roborock_mower.const import DOMAIN
from custom_components.roborock_mower.vendor.roborock.exceptions import (
    RoborockException,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from .conftest import MOWER_DUID, FakeChannel

# Shaped like protobufjs output in a minified React Native bundle.
BUNDLE = (
    b'var t={};t[t.GET_FULL_MAP=2]="GET_FULL_MAP";'
    b'e[r[3]="GET_ROBOT_STATUS"]=3,e[r[31]="GET_PLAN_INFO"]=31,'
    b'e[r[40]="GET_CONSUMABLE_STATE"]=40,e[r[6]="APP_BUTTON"]=6;'
    b'"xGET_NOT_A_NAME";"GET_LOWER_ok";fetch("GET_");'
)
PLUGIN_URL = "https://files.example.com/plugin/a222.zip"
CATEGORY_URL = "https://files.example.com/plugin/mower.zip"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_extract_query_names_from_a_plugin_archive() -> None:
    nested = _zip({"inner/main.hbc": b'..."GET_MOW_RECORD_LIST"...'})
    archive = _zip(
        {"index.android.bundle": BUNDLE, "assets/res.zip": nested, "img/": b""}
    )
    assert extract_query_names(archive) == [
        "GET_CONSUMABLE_STATE",
        "GET_MOW_RECORD_LIST",
        "GET_PLAN_INFO",
        "GET_ROBOT_STATUS",
    ]
    # A bare bundle works too; GET_FULL_MAP (huge, known) is left out.
    assert "GET_FULL_MAP" not in extract_query_names(BUNDLE)
    assert extract_query_names(b"nothing here") == []


def _hermes(storage: bytes, small: list[tuple[int, int]], overflow: list[tuple[int, int]]) -> bytes:
    """A minimal Hermes bytecode file: header, 1 function, 1 string kind, tables."""
    header = bytearray(128)
    header[:8] = HERMES_MAGIC
    struct.pack_into("<I", header, 8, 96)  # bytecode version
    struct.pack_into(
        "<6I", header, 40, 1, 1, 0, len(small), len(overflow), len(storage)
    )
    entries = b"".join(
        struct.pack("<I", (offset << 1) | (length << 24)) for offset, length in small
    )
    overflow_table = b"".join(struct.pack("<II", *entry) for entry in overflow)
    return (
        bytes(header)
        + bytes(16)  # function header
        + bytes(4)  # string kind
        + entries
        + overflow_table
        + storage
        + bytes(-len(storage) % 4)
    )


def test_extract_exact_names_from_the_hermes_string_table() -> None:
    """Overlapping strings: the table says where each one starts and ends."""
    storage = b"GET_FEATURESET_NEW_PIN_CODEisArrayGET_PLAN_INFO"
    blob = _hermes(
        storage,
        small=[(0, 11), (11, 16), (27, 7), (0, 255)],  # the last one overflows
        overflow=[(34, 13)],
    )
    assert hermes_strings(blob) == [
        "GET_FEATURE",
        "SET_NEW_PIN_CODE",
        "isArray",
        "GET_PLAN_INFO",
    ]
    assert extract_query_names(_zip({"main.hbc": blob})) == ["GET_FEATURE", "GET_PLAN_INFO"]
    # A table pointing past the strings is not trusted.
    assert hermes_strings(_hermes(storage, small=[(40, 20)], overflow=[])) is None


def test_extract_query_names_from_hermes_bytecode() -> None:
    """Without a readable table: runs after GET_, split at GET_."""
    blob = (
        HERMES_MAGIC
        + b"\x00\x01isArrayGET_ROBOT_STATUSGET_PLAN_INFOlengthGET_CONSUMABLE_STATE_"
        + b"\x00GET_FULL_MAPpushGET_"
    )
    assert extract_query_names(blob) == [
        "GET_CONSUMABLE_STATE",
        "GET_PLAN_INFO",
        "GET_ROBOT_STATUS",
    ]


def _products(model: str = "roborock.mower.a222") -> SimpleNamespace:
    return SimpleNamespace(
        category_detail_list=[
            SimpleNamespace(product_list=[SimpleNamespace(id=1234, model=model)])
        ]
    )


def _mower_answers(calls: list[str]):
    async def _send(method: str, params: dict) -> object:
        calls.append(params["type"])
        if params["type"] == "GET_PLAN_INFO":
            answer = {"type": "PLAN_INFO", "plans": [{"id": 1, "days": ["FRIDAY"]}]}
            raise RoborockException(f"Unexpected API Result: {json.dumps(answer)}")
        if params["type"] == "GET_ROBOT_STATUS":
            answer = {"type": "ROBOT_STATUS", "network": {"ip": "192.0.2.1"}}
            raise RoborockException(f"Unexpected API Result: {json.dumps(answer)}")
        if params["type"] == "GET_CONSUMABLE_STATE":
            return ["fail"]
        return ["ok"]

    return _send


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return device.id


async def _scan(hass: HomeAssistant, device_id: str, **data) -> dict:
    response = await hass.services.async_call(
        DOMAIN,
        "scan_queries",
        {"device_id": device_id, **data},
        blocking=True,
        return_response=True,
    )
    return response[MOWER_DUID]


async def test_scan_uses_the_names_from_the_app_plugin(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    channel: FakeChannel,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    calls: list[str] = []
    channel.rpc_channel.send_command.side_effect = _mower_answers(calls)
    aioclient_mock.get(PLUGIN_URL, content=_zip({"main.jsbundle": BUNDLE}))
    aioclient_mock.get(CATEGORY_URL, status=404)
    base = "custom_components.roborock_mower.vendor.roborock.web_api.RoborockApiClient"
    with (
        patch(f"{base}.get_products", AsyncMock(return_value=_products())),
        patch(f"{base}.download_code", AsyncMock(return_value=PLUGIN_URL)) as code,
        patch(
            f"{base}.download_category_code",
            AsyncMock(return_value={"roborock.mower": CATEGORY_URL}),
        ),
    ):
        device_id = await _setup(hass, config_entry)
        result = await _scan(hass, device_id)

    assert code.await_args.args[1] == 1234
    assert result["source"] == "app"
    assert result["app_errors"] == ["download: HTTP 404"]
    assert result["tried"] == 3
    assert result["answered"]["GET_PLAN_INFO"]["plans"][0]["days"] == ["FRIDAY"]
    assert result["answered"]["GET_ROBOT_STATUS"]["network"]["ip"] == "**REDACTED**"
    assert result["rejected"] == ["GET_CONSUMABLE_STATE"]
    assert result["acknowledged"] == []
    # Signed plugin links never end up in the answer.
    assert "files.example.com" not in json.dumps(result)


async def test_scan_falls_back_to_the_built_in_list(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    calls: list[str] = []
    channel.rpc_channel.send_command.side_effect = _mower_answers(calls)
    base = "custom_components.roborock_mower.vendor.roborock.web_api.RoborockApiClient"
    with (
        patch(f"{base}.get_products", AsyncMock(return_value=_products("other"))),
        patch(
            f"{base}.download_category_code",
            AsyncMock(side_effect=RoborockException("server says no")),
        ),
    ):
        device_id = await _setup(hass, config_entry)
        result = await _scan(hass, device_id)
        assert result["source"] == "built-in list"
        assert result["app_errors"] == [
            "roborock.mower.a222 is not in Roborock's product list",
            "category plugin: RoborockException: server says no",
        ]
        assert "GET_ROBOT_INFO" in calls

        calls.clear()
        result = await _scan(hass, device_id, from_app=False)
        assert "app_errors" not in result
        assert result["source"] == "built-in list"
