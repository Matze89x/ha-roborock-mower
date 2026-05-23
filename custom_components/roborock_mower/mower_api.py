"""DPS-based API for Roborock mower devices.

The RockNeo mower (roborock.mower.a222) is a V1 device, but unlike vacuums it
exposes status and control through Tuya data points (DPS) rather than RPC verbs.
Status DPS arrive in home_data ``device_status`` and via live MQTT push; commands
are DPS writes. The legacy ``get_status`` RPC is a stub and is not used.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from roborock.data.containers import HomeDataProduct, HomeDataScene
from roborock.devices.rpc.v1_channel import V1Channel
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol
from roborock.web_api import UserWebApiClient

_LOGGER = logging.getLogger(__name__)

# Status DPS (read-only)
DPS_ERROR_CODE = 120
DPS_BATTERY = 121
DPS_MOW_TYPE = 122
DPS_MOW_STATE = 123
DPS_MAPPING_TYPE = 124
DPS_MAPPING_STATE = 125
DPS_OTA_STATE = 126
DPS_CHARGE_STATE = 127
DPS_CHARGE_TYPE = 129
DPS_MOW_START_TYPE = 132
DPS_MOW_EFF_MODE = 133
DPS_MOW_HEIGHT = 134
DPS_MOW_DIRECTION_ANGLE = 135
DPS_OFFLINE_STATUS = 138
DPS_MOW_PROGRESS = 139
DPS_GPS_COORDINATE = 142
DPS_OFF_DOCK_NO_TASK_STATUS = 143
DPS_AFS_STATUS = 144
DPS_NETWORK_CHANNEL = 145

# Command DPS (write-only). Write value 1 to trigger.
DPS_START = 201
DPS_DOCK = 202
DPS_PAUSE = 203
DPS_RESUME = 204
DPS_STOP = 205

# rpc_request / rpc_response data points, ignored when reading status pushes.
_RPC_DPS = {101, 102}


@dataclass
class MowerStatus:
    """Decoded mower status from DPS data points."""

    error_code: int | None = None
    battery: int | None = None
    mow_type: int | None = None
    mow_state: int | None = None
    mapping_type: int | None = None
    mapping_state: int | None = None
    ota_state: int | None = None
    charge_state: int | None = None
    charge_type: int | None = None
    mow_start_type: int | None = None
    mow_eff_mode: int | None = None
    mow_height: int | None = None
    mow_direction_angle: int | None = None
    offline_status: Any = None
    mow_progress: int | None = None
    gps_coordinate: Any = None
    off_dock_no_task_status: int | None = None
    afs_status: int | None = None
    network_channel: int | None = None
    raw_dps: dict[int, Any] = field(default_factory=dict)

    @classmethod
    def from_dps(cls, dps: dict[int, Any]) -> MowerStatus:
        """Build a status from an int-keyed DPS dict."""
        return cls(
            error_code=dps.get(DPS_ERROR_CODE),
            battery=dps.get(DPS_BATTERY),
            mow_type=dps.get(DPS_MOW_TYPE),
            mow_state=dps.get(DPS_MOW_STATE),
            mapping_type=dps.get(DPS_MAPPING_TYPE),
            mapping_state=dps.get(DPS_MAPPING_STATE),
            ota_state=dps.get(DPS_OTA_STATE),
            charge_state=dps.get(DPS_CHARGE_STATE),
            charge_type=dps.get(DPS_CHARGE_TYPE),
            mow_start_type=dps.get(DPS_MOW_START_TYPE),
            mow_eff_mode=dps.get(DPS_MOW_EFF_MODE),
            mow_height=dps.get(DPS_MOW_HEIGHT),
            mow_direction_angle=dps.get(DPS_MOW_DIRECTION_ANGLE),
            offline_status=dps.get(DPS_OFFLINE_STATUS),
            mow_progress=dps.get(DPS_MOW_PROGRESS),
            gps_coordinate=dps.get(DPS_GPS_COORDINATE),
            off_dock_no_task_status=dps.get(DPS_OFF_DOCK_NO_TASK_STATUS),
            afs_status=dps.get(DPS_AFS_STATUS),
            network_channel=dps.get(DPS_NETWORK_CHANNEL),
            raw_dps=dict(dps),
        )


def coerce_dps(raw: dict[Any, Any] | None) -> dict[int, Any]:
    """Convert a DPS dict with arbitrary keys to int-keyed values."""
    result: dict[int, Any] = {}
    for key, value in (raw or {}).items():
        try:
            result[int(key)] = value
        except (ValueError, TypeError):
            continue
    return result


def parse_dps_push(message: Any) -> dict[int, Any]:
    """Extract status DPS {id: value} from a raw V1 push message ({} if none)."""
    payload = getattr(message, "payload", None)
    if not payload:
        return {}
    try:
        data = json.loads(payload.decode())
    except (ValueError, AttributeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("dps"), dict):
        return {}
    return {
        code: value
        for code, value in coerce_dps(data["dps"]).items()
        if code not in _RPC_DPS
    }


class MowerApi:
    """Reads mower status from DPS and sends commands via DPS writes."""

    def __init__(
        self,
        product: HomeDataProduct,
        channel: V1Channel,
        web_api: UserWebApiClient,
        duid: str,
        initial_dps: dict[Any, Any] | None = None,
    ) -> None:
        self._product = product
        self._channel = channel
        self._web_api = web_api
        self._duid = duid
        self._dps: dict[int, Any] = coerce_dps(initial_dps)

    @property
    def product(self) -> HomeDataProduct:
        return self._product

    @property
    def status(self) -> MowerStatus:
        return MowerStatus.from_dps(self._dps)

    def apply_push(self, dps: dict[int, Any]) -> MowerStatus:
        """Merge a live DPS push into current state and return updated status."""
        self._dps.update(dps)
        return self.status

    async def poll_status(self) -> MowerStatus:
        """Refresh status from the cloud home_data device_status snapshot."""
        home_data = await self._web_api.get_home_data()
        entry = home_data.device_products.get(self._duid)
        if entry is not None:
            device, _product = entry
            self._dps.update(coerce_dps(device.device_status))
        return self.status

    async def _write_dps(self, dps_id: int, value: Any) -> None:
        """Publish a single DPS write to the device."""
        payload = json.dumps(
            {"dps": {str(dps_id): value}, "t": int(time.time())},
            separators=(",", ":"),
        ).encode()
        message = RoborockMessage(
            protocol=RoborockMessageProtocol.RPC_REQUEST,
            payload=payload,
            version=b"1.0",
        )
        _LOGGER.debug("[%s] DPS write %s=%s", self._duid, dps_id, value)
        # V1Channel exposes no public raw-publish; its MQTT sub-channel sends the dp write.
        await self._channel._mqtt_channel.publish(message)  # noqa: SLF001

    async def _send_remote_msg(self, payload: dict[str, Any]) -> Any:
        """Send a RemoteMsg (a `type` plus its value field) via the remote_pb RPC."""
        message = {"id": str(int(time.time() * 1000)), **payload}
        _LOGGER.debug("[%s] remote_pb %s", self._duid, payload.get("type"))
        return await self._channel.rpc_channel.send_command("remote_pb", params=message)

    async def _send_button(self, app_button: str) -> Any:
        return await self._send_remote_msg(
            {"type": "APP_BUTTON", "app_button": app_button}
        )

    async def start(self) -> Any:
        """Start a full-lawn mow."""
        return await self._send_button("MOW_GLOBAL")

    async def edge_cut(self) -> Any:
        """Start an edge cut."""
        return await self._send_button("MOW_EDGE")

    async def stop(self) -> None:
        await self._write_dps(DPS_STOP, 1)

    async def pause(self) -> None:
        await self._write_dps(DPS_PAUSE, 1)

    async def resume(self) -> None:
        await self._write_dps(DPS_RESUME, 1)

    async def dock(self) -> None:
        await self._write_dps(DPS_DOCK, 1)

    async def set_mow_height(self, height: int) -> Any:
        """Set cutting height via the remote_pb REMOTE_CMD command."""
        return await self._send_remote_msg(
            {
                "type": "REMOTE_CMD",
                "remote_cmd": {
                    "type": "MAIN_CUTTER_HEIGHT",
                    "main_cutter_height": height,
                },
            }
        )

    async def set_mow_eff_mode(self, mode: int) -> None:
        # NOTE: efficiency mode is part of the mow_preference config sent via
        # SET_MOW_PREFERENCE; this dps write is a placeholder pending that schema.
        await self._write_dps(DPS_MOW_EFF_MODE, mode)

    async def get_routines(self) -> list[HomeDataScene]:
        """Fetch app-defined routines/scenes for this device."""
        return await self._web_api.get_routines(self._duid)

    async def execute_routine(self, scene_id: int) -> None:
        """Trigger a routine/scene by id (used for start, edge cut, etc.)."""
        await self._web_api.execute_routine(scene_id)
