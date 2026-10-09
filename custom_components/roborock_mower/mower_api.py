"""DPS + remote_pb API for Roborock mower devices.

The RockNeo mower (roborock.mower.a222) is a V1 device, but unlike vacuums it
exposes *status* through Tuya data points (DPS) rather than RPC verbs. Status DPS
arrive in home_data ``device_status`` and via live MQTT push. **Commands**, on the
other hand, are the same ``remote_pb`` protobuf RPC the official app uses: a
``rock.common.remote.RemoteMsg`` sent as its protobufjs ``toJSON`` form (string
enum names, ``id`` as a string). The legacy ``get_status`` RPC is a stub and is
not used.

The protocol here was reverse-engineered from the decompiled Roborock app
(``com.roborock.mower`` / ``no_package/RockNeo Q1`` bundles) and cross-checked
against a live RockNeo Q105. Enum *names* are sent on the wire (not numbers), so
the integration is robust to the app's several parallel enum numberings; the
numbers in comments are for reference only. See DEVELOPING.md for the full map.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import time
from typing import Any

try:
    # The integration's own bundled copy of python-roborock (see vendor/).
    from .vendor.roborock.data import RoborockCategory
    from .vendor.roborock.data.containers import HomeDataProduct, HomeDataScene
    from .vendor.roborock.devices.rpc.v1_channel import V1Channel
    from .vendor.roborock.exceptions import RoborockException
    from .vendor.roborock.roborock_message import (
        RoborockMessage,
        RoborockMessageProtocol,
    )
    from .vendor.roborock.web_api import UserWebApiClient
except ImportError:  # loaded standalone by the tools/ probe scripts
    from roborock.data import RoborockCategory
    from roborock.data.containers import HomeDataProduct, HomeDataScene
    from roborock.devices.rpc.v1_channel import V1Channel
    from roborock.exceptions import RoborockException
    from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol
    from roborock.web_api import UserWebApiClient

_LOGGER = logging.getLogger(__name__)

# --- Status DPS (read-only). rock.iot.DpKey.Type in the app schema. ----------
DPS_ERROR_CODE = 120
DPS_BATTERY = 121
DPS_MOW_TYPE = 122
DPS_MOW_STATE = 123
DPS_MAPPING_TYPE = 124
DPS_MAPPING_STATE = 125
DPS_OTA_STATE = 126
DPS_CHARGE_STATE = 127
DPS_DOCK_STATE = 128
DPS_CHARGE_TYPE = 129
DPS_PEND_TYPE = 130
DPS_REMOTE_STATE = 131
DPS_MOW_START_TYPE = 132
DPS_MOW_EFF_MODE = 133
DPS_MOW_HEIGHT = 134
DPS_MOW_DIRECTION_ANGLE = 135
DPS_MOW_PATTEN = 136
DPS_MOW_CONF_MODE = 137
DPS_OFFLINE_STATUS = 138
DPS_MOW_PROGRESS = 139
DPS_BLADE_LIFESPAN = 140
DPS_FC_STATE = 141
DPS_GPS_COORDINATE = 142
DPS_OFF_DOCK_NO_TASK_STATUS = 143
DPS_AFS_STATUS = 144
DPS_NETWORK_CHANNEL = 145

# --- Legacy command DPS (write value 1). Reverse-engineered as functional on
# the a222, but the official app does NOT use them -- it drives every control
# through remote_pb app_button (see the commands below). Kept for reference /
# as an alternate mechanism only; nothing here writes them by default.
DPS_START = 201
DPS_DOCK = 202
DPS_PAUSE = 203
DPS_RESUME = 204
DPS_STOP = 205

# rpc_request / rpc_response data points, ignored when reading status pushes.
_RPC_DPS = {101, 102}

# The mower's device model prefix (e.g. roborock.mower.a222). Used to identify
# mowers robustly — see is_mower().
MOWER_MODEL_PREFIX = "roborock.mower"


def is_mower(product: Any) -> bool:
    """Whether a home_data product is a mower.

    Prefer the ``MOWER`` category, but also match the model prefix: some
    ``python-roborock`` versions classify the mower under a different/UNKNOWN
    category, so relying on the category alone can miss it. The model
    (e.g. ``roborock.mower.a222``) is stable across versions.
    """
    if getattr(product, "category", None) == RoborockCategory.MOWER:
        return True
    model = getattr(product, "model", "") or ""
    return model.startswith(MOWER_MODEL_PREFIX)

# --- RemoteMsg.Type (top-level command discriminator). Sent as the name. ------
TYPE_APP_BUTTON = "APP_BUTTON"
TYPE_REMOTE_CMD = "REMOTE_CMD"
TYPE_SET_MOW_PREFERENCE = "SET_MOW_PREFERENCE"
TYPE_GET_MOW_PREFERENCE_CONFIG = "GET_MOW_PREFERENCE_CONFIG"
TYPE_GET_MAP_NAMES = "GET_MAP_NAMES"
TYPE_GET_FULL_MAP = "GET_FULL_MAP"

# --- AppButton.Type values the app uses for the mow flow (names on the wire).
BUTTON_MOW_GLOBAL = "MOW_GLOBAL"  # start full-lawn mow
BUTTON_MOW_EDGE = "MOW_EDGE"  # start edge / perimeter cut
BUTTON_MOW_SELECT = "MOW_SELECT"  # start select-area (zone) mow
BUTTON_MOW_PAUSE = "MOW_PAUSE"  # pause the running mow
BUTTON_MOW_RESUME = "MOW_RESUME"  # resume a paused mow
BUTTON_MOW_END = "MOW_END"  # stop / end the mow task
BUTTON_CHARGE = "CHARGE"  # return to dock & charge
BUTTON_DOCK_END = "DOCK_END"  # cancel an in-progress return-to-dock

# --- MowPreference.Effective enum (the "efficiency mode"). ------------------
# DPS 133 reports the same 1/2/3 value (rock.iot MowEffModeDpValue). The UI
# labels below drive the select; EFF_MODE_WIRE is the protobuf enum *name* the
# device uses in the preference config (confirmed live: effective:"DAILY").
# Option keys; the select translates them (e.g. "daily" -> "Täglich").
EFF_MODE_LABELS: dict[int, str] = {1: "daily", 2: "efficient", 3: "manicure"}
EFF_MODE_REVERSE: dict[str, int] = {v: k for k, v in EFF_MODE_LABELS.items()}
EFF_MODE_WIRE: dict[int, str] = {1: "DAILY", 2: "EFFICIENT", 3: "MANICURE"}

# remote_pb answers a command with ["ok"] when the mower acts and ["fail"] when
# it rejects it (e.g. start while the lid is open or the mower is off the map).
_REJECTED_RESULT = "fail"

# python-roborock's V1 decoder only passes through an "ok"/dict/list/int RPC
# result; the mower answers GET_* queries with a JSON *string*, which the
# library surfaces as this exception. We recover the payload from its text.
_UNEXPECTED_RESULT_PREFIX = "Unexpected API Result: "

# --- DP value enums (rock.iot.*DpValue), decoded to human labels for sensors.
MOW_TYPE_LABELS: dict[int, str] = {
    0: "none",
    1: "full_mow",
    2: "edge_cut",
    3: "selection",
    4: "zoning",
    5: "remote",
    6: "random",
}
CHARGE_STATE_LABELS: dict[int, str] = {
    0: "not_charging",
    1: "charging",
    2: "charge_completed",
    3: "waiting_charge",
    4: "low_power",
    5: "charge_error",
}
CHARGE_TYPE_LABELS: dict[int, str] = {
    0: "none",
    1: "manual_dock",
    2: "rain_dock",
    3: "do_not_disturb_dock",
    4: "standby_timeout_dock",
    5: "low_battery_dock",
    6: "task_finished_dock",
    7: "mow_fault_dock",
}
PEND_TYPE_LABELS: dict[int, str] = {
    0: "not_suspended",
    1: "app_pause",
    2: "emergency_stop",
    3: "fault",
    4: "breakpoint_pause",
    5: "other",
}

# --- rock.fsm.RobotDetailState.Type -- the enum DP 123 (mow_state) carries. ---
# Human labels for the state sensor; the activity frozensets below drive the
# lawn_mower entity's coarse activity.
ROBOT_DETAIL_STATE_LABELS: dict[int, str] = {
    0: "idle",
    # MAP_* mapping mode
    1: "map_initializing",
    2: "map_undocking",
    3: "map_undock_fault",
    4: "map_locating",
    5: "map_prepare_boundary",
    6: "map_prepare_island",
    7: "map_prepare_path",
    8: "map_boundary",
    9: "map_island",
    10: "map_path",
    11: "map_boundary_auto",
    12: "map_erasing",
    13: "map_save",
    14: "map_wait",
    15: "map_recoverable_fault",
    16: "map_fault",
    17: "map_emergency_stop",
    18: "map_waiting_fault",
    # MOW_*
    51: "mow_initializing",
    52: "mow_undocking",
    53: "mow_locating",
    54: "mow_adjust_cutter",
    55: "mowing",  # MOW_ZIG_ZAG -- primary full-area mowing
    56: "mowing_edge",  # MOW_EDGE
    57: "mow_goto",
    58: "paused",  # MOW_SUSPEND
    59: "recoverable_fault",
    60: "fault",
    61: "docked_rainfall",
    62: "docked_notdisturb",
    63: "docked_low_battery",
    64: "waiting",
    65: "prepare_remote",
    66: "remote_mowing",
    67: "emergency_stop",
    68: "docked_manual",
    69: "dock_fault",
    70: "remote_undocking",
    71: "returning",  # MOW_TO_DOCK_INITIALIZING
    72: "returning",  # MOW_TO_DOCK_LOCATING
    73: "returning_recoverable_fault",
    74: "returning_fault",
    75: "returning_emergency_stop",
    76: "charging",  # MOW_TO_DOCK_CHARGING
    77: "charge_completed",  # MOW_TO_DOCK_CHARGE_COMPLETED
    # FREE_*
    101: "free",
    102: "free_initializing",
    103: "free_locating",
    104: "docked_manual",
    105: "docked_mow_end",
    106: "docked_plan_end",
    107: "emergency_stop",
    108: "recoverable_fault",
    109: "fault",
    # Charge block
    151: "charging",
    152: "charge_completed",
    153: "waiting_charge",
    154: "charge_fault",
}

# Coarse activity classification of mow_state (DP 123). A code in none of
# these sets is reported (once) and treated as mowing, so new firmware codes
# surface instead of silently showing a wrong state.
MOW_STATES_MOWING = frozenset({51, 52, 53, 54, 55, 56, 57, 64, 65, 66, 70})
MOW_STATES_PAUSED = frozenset({17, 58, 67, 75, 107})
MOW_STATES_RETURNING = frozenset({71, 72})
MOW_STATES_ERROR = frozenset({3, 15, 16, 18, 59, 60, 69, 73, 74, 108, 109, 154})
# Map-building task (MAP_*): the mower is out driving.
MOW_STATES_MAPPING = frozenset({1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14})
# No task running. Whether the mower sits on the dock is told by charge_state.
MOW_STATES_IDLE = frozenset({0, 101, 102, 103})
# Docking-related end states (reason or charging). Off the dock they mean the
# mower is still on its way there.
MOW_STATES_DOCKED = frozenset(
    {61, 62, 63, 68, 76, 77, 104, 105, 106, 151, 152, 153}
)

# charge_state (DP 127) values reported only while the mower is on the dock.
CHARGE_STATES_ON_DOCK = frozenset({1, 2, 3, 5})
# dock_state (DP 128) DockStateDpValue: 1 MOVING_TO_TARGET, 2 DOCKING.
DOCK_STATES_RETURNING = frozenset({1, 2})
# off_dock_no_task_status (DP 143): 3 = DOCKING (returning to dock).
OFF_DOCK_DOCKING = 3

ACTIVITY_MOWING = "mowing"
ACTIVITY_PAUSED = "paused"
ACTIVITY_RETURNING = "returning"
ACTIVITY_DOCKED = "docked"
ACTIVITY_IDLE = "idle"
ACTIVITY_ERROR = "error"

# How long a return-to-dock sent from Home Assistant counts as "returning"
# while the mower has not reached the dock yet.
RETURN_PENDING_TIMEOUT = 30 * 60

# Entries kept in the in-memory event history (shown in the diagnostics).
HISTORY_SIZE = 300


def derive_activity(status: MowerStatus, return_pending: bool = False) -> str | None:
    """Coarse activity (ACTIVITY_*) from the mower's data points.

    ``mow_state`` (DP 123) says what task runs; ``charge_state`` (DP 127) says
    whether the mower sits on the dock. Without the latter, an idle mower
    driving back would wrongly read as docked. Returns None for an unknown
    ``mow_state`` code.
    """
    ms = status.mow_state
    if status.error_code or ms in MOW_STATES_ERROR:
        return ACTIVITY_ERROR
    if ms in MOW_STATES_PAUSED:
        return ACTIVITY_PAUSED
    if (
        ms in MOW_STATES_RETURNING
        or status.dock_state in DOCK_STATES_RETURNING
        or status.off_dock_no_task_status == OFF_DOCK_DOCKING
    ):
        return ACTIVITY_RETURNING
    if ms in MOW_STATES_MOWING or ms in MOW_STATES_MAPPING:
        return ACTIVITY_MOWING
    if ms is not None and ms not in MOW_STATES_IDLE and ms not in MOW_STATES_DOCKED:
        return None
    charge_state = status.charge_state
    if charge_state is None:
        # Firmware without DP 127: trust the state code alone.
        return ACTIVITY_DOCKED
    if charge_state in CHARGE_STATES_ON_DOCK:
        return ACTIVITY_DOCKED
    # Off the dock and no mowing task: on its way back, or simply stopped.
    if return_pending or ms in MOW_STATES_DOCKED or status.charge_type:
        return ACTIVITY_RETURNING
    return ACTIVITY_IDLE


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
    dock_state: int | None = None
    charge_type: int | None = None
    pend_type: int | None = None
    remote_state: int | None = None
    mow_start_type: int | None = None
    mow_eff_mode: int | None = None
    mow_height: int | None = None
    mow_direction_angle: int | None = None
    mow_patten: int | None = None
    mow_conf_mode: int | None = None
    offline_status: Any = None
    mow_progress: int | None = None
    blade_lifespan: int | None = None
    fc_state: int | None = None
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
            dock_state=dps.get(DPS_DOCK_STATE),
            charge_type=dps.get(DPS_CHARGE_TYPE),
            pend_type=dps.get(DPS_PEND_TYPE),
            remote_state=dps.get(DPS_REMOTE_STATE),
            mow_start_type=dps.get(DPS_MOW_START_TYPE),
            mow_eff_mode=dps.get(DPS_MOW_EFF_MODE),
            mow_height=dps.get(DPS_MOW_HEIGHT),
            mow_direction_angle=dps.get(DPS_MOW_DIRECTION_ANGLE),
            mow_patten=dps.get(DPS_MOW_PATTEN),
            mow_conf_mode=dps.get(DPS_MOW_CONF_MODE),
            offline_status=dps.get(DPS_OFFLINE_STATUS),
            mow_progress=dps.get(DPS_MOW_PROGRESS),
            blade_lifespan=dps.get(DPS_BLADE_LIFESPAN),
            fc_state=dps.get(DPS_FC_STATE),
            gps_coordinate=dps.get(DPS_GPS_COORDINATE),
            off_dock_no_task_status=dps.get(DPS_OFF_DOCK_NO_TASK_STATUS),
            afs_status=dps.get(DPS_AFS_STATUS),
            network_channel=dps.get(DPS_NETWORK_CHANNEL),
            raw_dps=dict(dps),
        )

    @property
    def on_dock(self) -> bool:
        """Whether charge_state says the mower sits on its dock."""
        return self.charge_state in CHARGE_STATES_ON_DOCK

    @property
    def mow_state_label(self) -> str | None:
        """Human label for mow_state (RobotDetailState); raw string if unknown."""
        if self.mow_state is None:
            return None
        return ROBOT_DETAIL_STATE_LABELS.get(self.mow_state, str(self.mow_state))


def coerce_dps(raw: dict[Any, Any] | None) -> dict[int, Any]:
    """Convert a DPS dict with arbitrary keys to int-keyed values."""
    result: dict[int, Any] = {}
    for key, value in (raw or {}).items():
        try:
            result[int(key)] = value
        except (ValueError, TypeError):
            continue
    return result


def redact_dps(dps: dict[int, Any]) -> dict[int, Any]:
    """Return a copy of a DPS dict with the GPS coordinate masked for logging.

    DPS 142 is the mower's position; keep it out of logs so a debug log can't
    expose the owner's location.
    """
    if DPS_GPS_COORDINATE not in dps:
        return dps
    return {
        code: ("<gps redacted>" if code == DPS_GPS_COORDINATE else value)
        for code, value in dps.items()
    }


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


def areas_from_preference_config(cfg: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract the saved areas ({id, name}) from a mowing preference config."""
    areas: list[dict[str, Any]] = []
    if isinstance(cfg, dict):
        for custom in cfg.get("custom") or []:
            if isinstance(custom, dict) and custom.get("area_id") is not None:
                areas.append(
                    {"id": custom["area_id"], "name": custom.get("area_name", "")}
                )
    return areas


class MowerCommandRejected(RoborockException):
    """The mower answered a command with ``["fail"]``."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _boundaries_payload(areas: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a modify_map payload from selected areas ({id, name} each)."""
    return {
        "boundaries": [
            {"id": area["id"], "name": area.get("name", "")} for area in areas
        ]
    }


class MowerApi:
    """Reads mower status from DPS and sends commands via the remote_pb RPC."""

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
        # Push bookkeeping: lets the coordinator skip the rate-limited cloud
        # poll while MQTT is demonstrably delivering, and feeds diagnostics.
        self.push_count = 0
        self.last_push_monotonic: float | None = None
        self.online: bool | None = None
        # Saved areas (zones) once discovered; used by the edge cut.
        self.areas: list[dict[str, Any]] | None = None
        # Commands, answers and data-point changes, newest last (diagnostics).
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)
        self._return_requested_at: float | None = None

    @property
    def duid(self) -> str:
        return self._duid

    @property
    def channel(self) -> V1Channel:
        return self._channel

    @property
    def product(self) -> HomeDataProduct:
        return self._product

    @property
    def status(self) -> MowerStatus:
        return MowerStatus.from_dps(self._dps)

    @property
    def seconds_since_push(self) -> float | None:
        """Seconds since the last live DPS push (None if none received yet)."""
        if self.last_push_monotonic is None:
            return None
        return time.monotonic() - self.last_push_monotonic

    @property
    def return_pending(self) -> bool:
        """A return-to-dock sent from Home Assistant has not arrived yet."""
        if self._return_requested_at is None:
            return False
        if time.monotonic() - self._return_requested_at > RETURN_PENDING_TIMEOUT:
            self._return_requested_at = None
        return self._return_requested_at is not None

    def _record(self, kind: str, **data: Any) -> None:
        self.history.append({"time": _now(), "kind": kind, **data})

    def _merge(self, dps: dict[int, Any], source: str) -> MowerStatus:
        changed = {
            str(code): value
            for code, value in dps.items()
            if self._dps.get(code) != value and code != DPS_GPS_COORDINATE
        }
        self._dps.update(dps)
        if changed:
            self._record(source, dps=changed)
        status = self.status
        if status.on_dock:
            self._return_requested_at = None
        return status

    def apply_push(self, dps: dict[int, Any]) -> MowerStatus:
        """Merge a live DPS push into current state and return updated status."""
        self.push_count += 1
        self.last_push_monotonic = time.monotonic()
        return self._merge(dps, "push")

    def apply_home_data(self, home_data: Any) -> MowerStatus:
        """Merge this mower's device_status from a home_data snapshot."""
        entry = home_data.device_products.get(self._duid)
        if entry is None:
            return self.status
        device, _product = entry
        self.online = getattr(device, "online", None)
        return self._merge(coerce_dps(device.device_status), "cloud_snapshot")

    async def poll_status(self) -> MowerStatus:
        """Refresh status from the cloud home_data device_status snapshot."""
        return self.apply_home_data(await self._web_api.get_home_data())

    # -- remote_pb transport ---------------------------------------------------

    async def _send_remote_msg(self, payload: dict[str, Any]) -> Any:
        """Send a RemoteMsg (a ``type`` plus its value field) via remote_pb.

        Mirrors the app's cloud path: a ``RemoteMsg`` as its protobufjs ``toJSON``
        object (string enum names, ``id`` as a decimal string), delivered through
        ``rpc_channel.send_command('remote_pb', params=<obj>)``.
        """
        message = {"id": str(int(time.time() * 1000)), **payload}
        command = " ".join(
            str(part) for part in (payload.get("type"), payload.get("app_button")) if part
        )
        entry: dict[str, Any] = {"command": command}
        if "modify_map" in payload:
            entry["areas"] = payload["modify_map"]
        try:
            result = await self._channel.rpc_channel.send_command(
                "remote_pb", params=message
            )
        except RoborockException as err:
            self._record("command", **entry, error=_short(str(err)))
            _LOGGER.debug("[%s] remote_pb %s failed: %s", self._duid, command, _short(str(err)))
            raise
        self._record("command", **entry, result=_short(result))
        _LOGGER.debug("[%s] remote_pb %s -> %s", self._duid, command, _short(result))
        if result == _REJECTED_RESULT or (
            isinstance(result, list) and _REJECTED_RESULT in result
        ):
            raise MowerCommandRejected(f"Mower rejected {command}")
        return result

    async def _send_button(self, app_button: str, **extra: Any) -> Any:
        return await self._send_remote_msg(
            {"type": TYPE_APP_BUTTON, "app_button": app_button, **extra}
        )

    async def _query(self, payload: dict[str, Any]) -> Any:
        """Send a remote_pb GET_* query and return its parsed JSON result.

        The mower replies to queries with a JSON string in the RPC ``result``,
        which python-roborock raises as ``Unexpected API Result: <json>`` (it
        only passes ``ok``/dict/list/int straight through). Recover the JSON
        from that exception; genuine errors are re-raised.
        """
        try:
            result = await self._send_remote_msg(payload)
        except RoborockException as err:
            text = str(err)
            marker = text.find(_UNEXPECTED_RESULT_PREFIX)
            if marker == -1:
                raise
            raw = text[marker + len(_UNEXPECTED_RESULT_PREFIX) :]
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise err from None
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError:
                return result
        return result

    # -- mowing controls (app-faithful remote_pb app_button) -------------------

    async def _send_task_button(self, app_button: str, **extra: Any) -> Any:
        """Send a button that starts/changes a task (ends a pending return)."""
        self._return_requested_at = None
        return await self._send_button(app_button, **extra)

    async def start(self) -> Any:
        """Start a full-lawn mow (AppButton MOW_GLOBAL)."""
        return await self._send_task_button(BUTTON_MOW_GLOBAL)

    async def edge_cut(self, areas: list[dict[str, Any]] | None = None) -> Any:
        """Start an edge / perimeter cut (AppButton MOW_EDGE).

        Like the app (which makes you pick the area first), the edge cut is sent
        for the saved areas -- all of them unless ``areas`` narrows it down. If
        no area is known, or the mower rejects the area list, it is sent bare,
        which edges the whole current map on firmware that accepts that.
        """
        if areas is None:
            areas = self.areas if self.areas is not None else await self.get_areas()
        if areas:
            try:
                return await self._send_task_button(
                    BUTTON_MOW_EDGE, modify_map=_boundaries_payload(areas)
                )
            except MowerCommandRejected:
                _LOGGER.debug("[%s] edge cut with areas rejected; retrying bare", self._duid)
        return await self._send_task_button(BUTTON_MOW_EDGE)

    async def start_area_mow(self, areas: list[dict[str, Any]]) -> Any:
        """Start a select-area (zone) mow (AppButton MOW_SELECT).

        ``areas`` is a non-empty list of ``{"id": <boundary/zone id>, "name": ...}``
        as returned by :meth:`get_full_map`. The app requires at least one area.
        """
        if not areas:
            raise ValueError("start_area_mow requires at least one area")
        return await self._send_task_button(
            BUTTON_MOW_SELECT, modify_map=_boundaries_payload(areas)
        )

    async def pause(self) -> Any:
        """Pause the running mow (AppButton MOW_PAUSE)."""
        return await self._send_task_button(BUTTON_MOW_PAUSE)

    async def resume(self) -> Any:
        """Resume a paused mow (AppButton MOW_RESUME)."""
        return await self._send_task_button(BUTTON_MOW_RESUME)

    async def stop(self) -> Any:
        """Stop / end the current mow task (AppButton MOW_END)."""
        return await self._send_task_button(BUTTON_MOW_END)

    async def dock(self) -> Any:
        """Return to the dock and charge (AppButton CHARGE)."""
        result = await self._send_button(BUTTON_CHARGE)
        if not self.status.on_dock:
            self._return_requested_at = time.monotonic()
        return result

    async def cancel_dock(self) -> Any:
        """Cancel an in-progress return-to-dock (AppButton DOCK_END)."""
        return await self._send_task_button(BUTTON_DOCK_END)

    # -- settings --------------------------------------------------------------

    async def set_mow_height(self, height: int) -> Any:
        """Set cutting height (mm) via the remote_pb REMOTE_CMD command.

        ``main_cutter_height`` is an unsigned integer in raw millimetres (no
        scaling); guard against negatives, which would wrap on the wire. The app
        pairs the live REMOTE_CMD with a preference write so the height persists
        across future mows, so we mirror that (best effort).
        """
        height = max(0, int(height))
        result = await self._send_remote_msg(
            {
                "type": TYPE_REMOTE_CMD,
                "remote_cmd": {
                    "type": "MAIN_CUTTER_HEIGHT",
                    "main_cutter_height": height,
                },
            }
        )
        try:
            pref = await self._get_global_mow_preference() or {}
            pref["height"] = height
            await self._send_remote_msg(
                {"type": TYPE_SET_MOW_PREFERENCE, "mow_preference": pref}
            )
        except RoborockException as err:
            _LOGGER.debug("[%s] height preference persist failed: %s", self._duid, err)
        return result

    async def set_mow_eff_mode(self, mode: int) -> Any:
        """Set the efficiency mode (MowPreference.effective) via SET_MOW_PREFERENCE.

        Efficiency mode is a field of the mowing *preference*, not a command DPS.
        We read-modify-write the global preference so height/direction/etc. are
        preserved; ``effective`` is written as the protobuf enum name the device
        itself reports (e.g. "DAILY"). Falls back to a minimal update if the
        current preference can't be read back.
        """
        effective = EFF_MODE_WIRE.get(mode)
        if effective is None:
            raise ValueError(f"Unknown efficiency mode: {mode}")
        pref = await self._get_global_mow_preference() or {}
        pref["effective"] = effective
        return await self._send_remote_msg(
            {"type": TYPE_SET_MOW_PREFERENCE, "mow_preference": pref}
        )

    async def get_mow_preference_config(self) -> dict[str, Any] | None:
        """Read the mowing preference config (global + custom areas), or None."""
        try:
            resp = await self._query({"type": TYPE_GET_MOW_PREFERENCE_CONFIG})
        except RoborockException as err:
            _LOGGER.debug("[%s] GET_MOW_PREFERENCE_CONFIG failed: %s", self._duid, err)
            return None
        if not isinstance(resp, dict):
            return None
        # The device wraps it under "preference_config"; accept a couple of names.
        cfg = (
            resp.get("preference_config")
            or resp.get("mow_preference_config")
            or resp
        )
        return cfg if isinstance(cfg, dict) else None

    async def _get_global_mow_preference(self) -> dict[str, Any] | None:
        """Best-effort read of the global MowPreference (None if unavailable)."""
        cfg = await self.get_mow_preference_config()
        global_pref = cfg.get("global") if isinstance(cfg, dict) else None
        return global_pref if isinstance(global_pref, dict) else None

    # -- map / zone discovery --------------------------------------------------
    # The mower answers GET_* queries with JSON (recovered by _query). The most
    # reliable zone source is the mowing preference config's custom[] areas;
    # GET_FULL_MAP is also parsed when the device returns it.

    async def get_areas(self) -> list[dict[str, Any]]:
        """List selectable saved areas (id + name) for zone mowing.

        Sourced from the mowing preference config's ``custom`` areas (each a
        saved zone with ``area_id`` / ``area_name``), which the device returns
        reliably. Feed an ``id`` to :meth:`start_area_mow` or the ``mow_areas``
        service.
        """
        cfg = await self.get_mow_preference_config()
        if cfg is None:
            return self.areas or []
        self.areas = areas_from_preference_config(cfg)
        return self.areas

    async def get_map_names(self) -> list[str]:
        """Best-effort list of saved map names (empty if unavailable)."""
        try:
            resp = await self._query({"type": TYPE_GET_MAP_NAMES})
        except RoborockException as err:
            _LOGGER.debug("[%s] GET_MAP_NAMES failed: %s", self._duid, err)
            return []
        if isinstance(resp, dict):
            names = resp.get("map_names") or resp.get("6")
            if isinstance(names, list):
                return [str(n) for n in names]
        return []

    async def get_map_raw(self, map_name: str = "") -> Any:
        """Raw GET_FULL_MAP payload (dict, base64 string, or None).

        Diagnostic / rendering entry point. Unlike the preference config, the
        full map may come back as a base64 protobuf string; this returns it
        as-is so callers can decode it.
        """
        payload = {"type": TYPE_GET_FULL_MAP, "modify_map": {"name": map_name}}
        try:
            return await self._send_remote_msg(payload)
        except RoborockException as err:
            text = str(err)
            marker = text.find(_UNEXPECTED_RESULT_PREFIX)
            if marker == -1:
                _LOGGER.debug("[%s] GET_FULL_MAP failed: %s", self._duid, err)
                return None
            raw = text[marker + len(_UNEXPECTED_RESULT_PREFIX) :]
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw  # base64 protobuf string

    async def get_full_map(self, map_name: str = "") -> list[dict[str, Any]]:
        """Best-effort catalog of map boundaries/zones for zone mowing.

        Returns ``{"id", "name", "zones": [{"id", "name"}, ...]}`` per boundary,
        or ``[]`` if the device doesn't return a decodable map. Prefer
        :meth:`get_areas` for the reliable saved-zone list.
        """
        resp = await self.get_map_raw(map_name)
        raw_map = resp.get("map") if isinstance(resp, dict) else None
        if not isinstance(raw_map, dict):
            _LOGGER.debug(
                "[%s] GET_FULL_MAP has no JSON 'map' (type %s); use get_areas()",
                self._duid,
                type(resp).__name__,
            )
            return []
        areas: list[dict[str, Any]] = []
        for boundary in raw_map.get("boundaries") or []:
            if not isinstance(boundary, dict):
                continue
            zones = [
                {"id": z.get("id"), "name": z.get("name", "")}
                for z in boundary.get("zones") or []
                if isinstance(z, dict)
            ]
            areas.append(
                {
                    "id": boundary.get("id"),
                    "name": boundary.get("name", ""),
                    "zones": zones,
                }
            )
        return areas

    # -- routines / scenes ------------------------------------------------------

    async def get_routines(self) -> list[HomeDataScene]:
        """Fetch app-defined routines/scenes for this device."""
        return await self._web_api.get_routines(self._duid)

    async def execute_routine(self, scene_id: int) -> None:
        """Trigger a routine/scene by id (used for start, edge cut, etc.)."""
        await self._web_api.execute_routine(scene_id)

    # -- legacy DPS write (alternate mechanism; unused by default) --------------

    async def _write_dps(self, dps_id: int, value: Any) -> None:
        """Publish a single DPS write (legacy command path; see the DPS_* notes).

        The official app never writes command DPS -- it uses remote_pb app_button
        (the methods above). This is retained only as a fallback mechanism.
        """
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
        # V1Channel exposes no public raw-publish; its MQTT sub-channel sends it.
        await self._channel._mqtt_channel.publish(message)  # noqa: SLF001
