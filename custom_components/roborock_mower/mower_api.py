"""Mower API trait for Roborock mower devices."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from roborock.data.containers import HomeDataProduct
from roborock.protocols.v1_protocol import V1RpcChannel

_LOGGER = logging.getLogger(__name__)


@dataclass
class MowerStatus:
    """Represents the status of a Roborock mower."""

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
    raw_data: dict[str, Any] = field(default_factory=dict)


class MowerApi:
    """API for controlling a Roborock mower device via V1 RPC."""

    def __init__(self, product: HomeDataProduct, rpc_channel: V1RpcChannel) -> None:
        self._product = product
        self._rpc_channel = rpc_channel

    @property
    def product(self) -> HomeDataProduct:
        return self._product

    async def refresh(self) -> MowerStatus:
        """Fetch current mower status."""
        try:
            result = await self._rpc_channel.send_command("get_status")
            if result and isinstance(result, list) and len(result) > 0:
                data = result[0]
                return MowerStatus(
                    error_code=data.get("error_code"),
                    battery=data.get("battery"),
                    mow_type=data.get("mow_type"),
                    mow_state=data.get("mow_state"),
                    mapping_type=data.get("mapping_type"),
                    mapping_state=data.get("mapping_state"),
                    ota_state=data.get("ota_state"),
                    charge_state=data.get("charge_state"),
                    charge_type=data.get("charge_type"),
                    mow_start_type=data.get("mow_start_type"),
                    mow_eff_mode=data.get("mow_eff_mode"),
                    mow_height=data.get("mow_height"),
                    mow_direction_angle=data.get("mow_direction_angle"),
                    offline_status=data.get("offline_status"),
                    mow_progress=data.get("mow_progress"),
                    gps_coordinate=data.get("gps_coordinate"),
                    off_dock_no_task_status=data.get("off_dock_no_task_status"),
                    afs_status=data.get("afs_status"),
                    network_channel=data.get("network_channel"),
                    raw_data=data,
                )
        except Exception:
            _LOGGER.exception("Error getting mower status")
        return MowerStatus()

    async def start(self) -> Any:
        """Start mowing."""
        return await self._rpc_channel.send_command("app_start")

    async def stop(self) -> Any:
        """Stop mowing."""
        return await self._rpc_channel.send_command("app_stop")

    async def pause(self) -> Any:
        """Pause mowing."""
        return await self._rpc_channel.send_command("app_pause")

    async def resume(self) -> Any:
        """Resume mowing."""
        return await self._rpc_channel.send_command("app_resume")

    async def dock(self) -> Any:
        """Return to dock."""
        return await self._rpc_channel.send_command("app_dock")

    async def set_mow_height(self, height: int) -> Any:
        """Set mowing height."""
        return await self._rpc_channel.send_command(
            "set_mow_height", {"height": height}
        )

    async def set_mow_eff_mode(self, mode: int) -> Any:
        """Set mowing efficiency mode."""
        return await self._rpc_channel.send_command(
            "set_mow_eff_mode", {"mode": mode}
        )
