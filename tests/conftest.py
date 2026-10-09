"""Fixtures for the Roborock Mower Home Assistant tests."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_USERNAME

from custom_components.roborock_mower.const import (
    CONF_BASE_URL,
    CONF_USER_DATA,
    DOMAIN,
)
from custom_components.roborock_mower.vendor.roborock.data import HomeData
from pytest_homeassistant_custom_component.common import MockConfigEntry

MOWER_DUID = "mower_duid_1"

USER_DATA = {
    "uid": 123456,
    "tokentype": "",
    "token": "secret-token",
    "rruid": "rr_uid_1",
    "region": "eu",
    "countrycode": "49",
    "country": "DE",
    "nickname": "test",
    "rriot": {
        "u": "rriot_u",
        "s": "rriot_s",
        "h": "rriot_h",
        "k": "rriot_k",
        "r": {
            "r": "EU",
            "a": "https://api-eu.roborock.com",
            "m": "ssl://mqtt-eu.roborock.com:8883",
            "l": "https://wood-eu.roborock.com",
        },
    },
}

# Live capture from a docked RockNeo Q105 (GPS replaced).
DEVICE_STATUS = {
    "120": 0,
    "121": 100,
    "122": 0,
    "123": 0,
    "127": 2,
    "133": 1,
    "134": 40,
    "139": 0,
    "142": "base64-gps==",
    "143": 0,
}


# Data points a RockNeo Q105 lists in its product schema (fw 02.72.44): no
# 128 dock_state, 130 pend_type, 131, 136, 137, 140 blade_lifespan, 141.
Q105_SCHEMA = [
    {"id": str(code), "name": name, "code": name, "mode": mode, "type": "VALUE"}
    for code, name, mode in (
        (101, "rpc_request", "rw"),
        (102, "rpc_response", "rw"),
        (103, "dps_report", "ro"),
        (120, "error_code", "ro"),
        (121, "battery", "ro"),
        (122, "mow_type", "ro"),
        (123, "mow_state", "ro"),
        (124, "mapping_type", "ro"),
        (125, "mapping_state", "ro"),
        (126, "ota_state", "ro"),
        (127, "charge_state", "ro"),
        (129, "charge_type", "ro"),
        (132, "mow_start_type", "ro"),
        (133, "mow_eff_mode", "rw"),
        (134, "mow_height", "rw"),
        (135, "mow_direction_angle", "rw"),
        (138, "offline_status", "ro"),
        (139, "mow_progress", "ro"),
        (142, "gps_coordinate", "ro"),
        (143, "off_dock_no_task_status", "ro"),
        (144, "afs_status", "ro"),
        (145, "network_channel", "ro"),
        (201, "start", "wo"),
        (202, "dock", "wo"),
        (203, "pause", "wo"),
        (204, "resume", "wo"),
        (205, "stop", "wo"),
        (206, "auth_status", "ro"),
    )
]


def make_home_data(
    device_status: dict[str, Any] | None = None, *, with_mower: bool = True
) -> HomeData:
    """Build a home_data payload with a RockNeo Q105 (and a vacuum)."""
    products = [
        {
            "id": "prod_vacuum",
            "name": "Roborock S8",
            "model": "roborock.vacuum.a51",
            "category": "robot.vacuum.cleaner",
        }
    ]
    devices = [
        {
            "duid": "vacuum_duid",
            "name": "Vacuum",
            "localKey": "vacuum_key",
            "productId": "prod_vacuum",
            "pv": "1.0",
            "online": True,
        }
    ]
    if with_mower:
        products.append(
            {
                "id": "prod_mower",
                "name": "RockNeo Q105",
                "model": "roborock.mower.a222",
                "category": "roborock.mower",
                "schema": Q105_SCHEMA,
            }
        )
        devices.append(
            {
                "duid": MOWER_DUID,
                "name": "Rasenmäher",
                "localKey": "mower_key",
                "productId": "prod_mower",
                "fv": "02.18.42",
                "pv": "1.0",
                "sn": "SN123",
                "online": True,
                "deviceStatus": dict(device_status or DEVICE_STATUS),
            }
        )
    return HomeData.from_dict(
        {
            "id": 1,
            "name": "Home",
            "products": products,
            "devices": devices,
            "receivedDevices": [],
        }
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the custom integration in every test."""


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id=USER_DATA["rruid"],
        data={
            CONF_USERNAME: "user@example.com",
            CONF_USER_DATA: USER_DATA,
            CONF_BASE_URL: "https://euiot.roborock.com",
        },
    )


class FakeChannel:
    """Stands in for python-roborock's V1Channel."""

    def __init__(self) -> None:
        self.callback: Callable[[Any], None] | None = None
        self.unsubscribe = MagicMock()
        self.subscribe_error: Exception | None = None
        self.rpc_channel = MagicMock()
        self.rpc_channel.send_command = AsyncMock(return_value=["ok"])
        self.is_connected = True
        self.is_local_connected = False

    async def subscribe(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        if self.subscribe_error is not None:
            raise self.subscribe_error
        self.callback = callback
        return self.unsubscribe


class FakeMessage:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def mqtt_session() -> MagicMock:
    session = MagicMock()
    session.close = AsyncMock()
    session.connected = True
    return session


@pytest.fixture
def home_data_mock() -> AsyncMock:
    return AsyncMock(return_value=make_home_data())


@pytest.fixture
def routines_mock() -> AsyncMock:
    return AsyncMock(return_value=[])


@pytest.fixture(autouse=True)
def mock_roborock(
    channel: FakeChannel,
    mqtt_session: MagicMock,
    home_data_mock: AsyncMock,
    routines_mock: AsyncMock,
) -> Generator[None]:
    """Patch every network-facing piece of python-roborock."""
    with (
        patch(
            "custom_components.roborock_mower.vendor.roborock.web_api.RoborockApiClient.get_home_data_v3", home_data_mock
        ),
        patch(
            "custom_components.roborock_mower.vendor.roborock.web_api.UserWebApiClient.get_routines", routines_mock
        ),
        patch(
            "custom_components.roborock_mower.create_lazy_mqtt_session",
            AsyncMock(return_value=mqtt_session),
        ),
        patch(
            "custom_components.roborock_mower.create_v1_channel",
            return_value=channel,
        ),
        patch(
            "custom_components.roborock_mower.home_data.HOME_DATA_RATE_LIMIT_RETRY_DELAY",
            0,
        ),
        patch(
            "custom_components.roborock_mower.button.AREA_DISCOVERY_RETRY_DELAYS",
            (0, 0, 0, 0),
        ),
    ):
        yield
