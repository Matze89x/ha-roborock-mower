"""Setup, restart and failure-path tests for the Roborock Mower integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.lawn_mower import LawnMowerActivity
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.package import is_installed

from custom_components.roborock_mower.const import (
    DOMAIN,
    STALE_SNAPSHOT_REFRESH_INTERVAL,
    UPDATE_INTERVAL,
)
from custom_components.roborock_mower.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.roborock_mower.home_data import (
    SOURCE_CACHE_FALLBACK,
    SOURCE_CLOUD,
)
from custom_components.roborock_mower.vendor.roborock.data.containers import (
    HomeDataScene,
)
from custom_components.roborock_mower.vendor.roborock.exceptions import (
    RoborockException,
    RoborockInvalidCredentials,
    RoborockRateLimit,
)
from custom_components.roborock_mower.vendor.roborock.mqtt.session import (
    MqttSessionUnauthorized,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .conftest import MOWER_DUID, FakeChannel, FakeMessage, make_home_data

MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / DOMAIN
    / "manifest.json"
)

PREF_CONFIG_JSON = (
    '{"type":"MOW_PREFERENCE_CONFIG","preference_config":{'
    '"global":{"effective":"DAILY","mode":"GLOBAL"},'
    '"custom":[{"area_name":"A1","area_id":2},{"area_name":"A2","area_id":3}]}}'
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries()}:
        entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{MOWER_DUID}_{key}"
    )
    assert entity_id, f"{platform} {key} not registered"
    return entity_id


async def _wait_for_state(
    hass: HomeAssistant, entry: MockConfigEntry, state: ConfigEntryState
) -> None:
    """Let a timer-triggered setup retry finish (its executor jobs need a tick)."""
    for _ in range(40):
        await hass.async_block_till_done()
        if entry.state is state:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"entry stuck in {entry.state}, expected {state}")


def _coordinator(entry: MockConfigEntry):
    return entry.runtime_data.coordinators[0]


async def test_requirements_never_conflict_with_core() -> None:
    """Requirements must accept whatever Home Assistant already has installed.

    A pinned/upper-bounded requirement made HA reinstall a different
    python-roborock on every start, breaking the official Roborock integration.
    python-roborock itself is bundled now, so it must not be required at all.
    """
    requirements = json.loads(MANIFEST.read_text())["requirements"]
    assert not [r for r in requirements if r.startswith("python-roborock")]
    for req in requirements:
        assert "==" not in req and "<" not in req, req
        assert is_installed(req), f"{req} not satisfied by the installed version"


async def test_setup_and_unload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    channel: FakeChannel,
    mqtt_session: MagicMock,
    home_data_mock: AsyncMock,
) -> None:
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert home_data_mock.await_count == 1
    assert config_entry.runtime_data.home_data.source == SOURCE_CLOUD
    assert _coordinator(config_entry).update_interval == UPDATE_INTERVAL

    assert hass.states.get(_entity_id(hass, "lawn_mower", "lawn_mower")).state == (
        LawnMowerActivity.DOCKED
    )
    assert hass.states.get(_entity_id(hass, "sensor", "battery")).state == "100"
    assert hass.states.get(_entity_id(hass, "number", "mow_height")).state == "40"
    assert hass.states.get(_entity_id(hass, "select", "mow_eff_mode")).state == "daily"
    # Translated enum sensors (labels are translation keys).
    assert hass.states.get(_entity_id(hass, "sensor", "charge_state")).state == (
        "charge_completed"
    )
    assert hass.states.get(_entity_id(hass, "sensor", "mow_state")).state == "idle"
    assert hass.states.get(_entity_id(hass, "sensor", "error_code")).state == "0"

    # Only the mower becomes a device; the vacuum belongs to the core integration.
    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), config_entry.entry_id
    )
    assert [d.model_id for d in devices] == ["roborock.mower.a222"]

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    channel.unsubscribe.assert_called_once()
    mqtt_session.close.assert_awaited_once()


async def test_restart_reuses_snapshot_then_refreshes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    home_data_mock: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A quick restart must not spend the shared home_data budget again."""
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert home_data_mock.await_count == 1
    coordinator = _coordinator(config_entry)
    assert coordinator.update_interval == STALE_SNAPSHOT_REFRESH_INTERVAL

    # Pushes missed while "down" are caught up by one real refresh shortly after.
    home_data_mock.return_value = make_home_data({"121": 87, "123": 0})
    freezer.tick(STALE_SNAPSHOT_REFRESH_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert home_data_mock.await_count == 2
    assert coordinator.update_interval == UPDATE_INTERVAL
    assert hass.states.get(_entity_id(hass, "sensor", "battery")).state == "87"


async def test_rate_limit_collision_with_core_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, home_data_mock: AsyncMock
) -> None:
    """The official integration fetched in the same second: retry once."""
    home_data_mock.side_effect = [RoborockRateLimit("1/s"), make_home_data()]
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert home_data_mock.await_count == 2
    assert config_entry.runtime_data.home_data.source == SOURCE_CLOUD


async def test_rate_limited_without_cache_retries_later(
    hass: HomeAssistant, config_entry: MockConfigEntry, home_data_mock: AsyncMock
) -> None:
    home_data_mock.side_effect = RoborockRateLimit("5/hour")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_rate_limited_with_cache_uses_cache(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    home_data_mock: AsyncMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    # Next start, an hour later, with the cloud budget exhausted.
    freezer.tick(timedelta(hours=1))
    home_data_mock.side_effect = RoborockRateLimit("5/hour")
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.home_data.source == SOURCE_CACHE_FALLBACK
    assert _coordinator(config_entry).update_interval == STALE_SNAPSHOT_REFRESH_INTERVAL
    assert "using the cached device list" in caplog.text
    assert hass.states.get(_entity_id(hass, "sensor", "battery")).state == "100"


async def test_mqtt_failure_retries_and_does_not_leak(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    channel: FakeChannel,
    mqtt_session: MagicMock,
    home_data_mock: AsyncMock,
) -> None:
    """Network not up yet after a reboot: retry instead of failing for good."""
    channel.subscribe_error = RoborockException("broker unreachable")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    mqtt_session.close.assert_awaited_once()

    channel.subscribe_error = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await _wait_for_state(hass, config_entry, ConfigEntryState.LOADED)
    # The retry reused the snapshot instead of calling the cloud again.
    assert home_data_mock.await_count == 1


async def test_mqtt_unauthorized_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    channel: FakeChannel,
    mqtt_session: MagicMock,
) -> None:
    channel.subscribe_error = MqttSessionUnauthorized("rc 135")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    mqtt_session.close.assert_awaited_once()
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_invalid_credentials_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, home_data_mock: AsyncMock
) -> None:
    home_data_mock.side_effect = RoborockInvalidCredentials("code 2010")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_no_mower_stops_instead_of_retry_loop(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    home_data_mock: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    home_data_mock.return_value = make_home_data(with_mower=False)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    freezer.tick(timedelta(minutes=10))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert home_data_mock.await_count == 1


async def test_live_push_updates_entities(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await _setup(hass, config_entry)
    assert channel.callback is not None
    channel.callback(FakeMessage(b'{"t":1,"dps":{"123":55,"139":12,"121":97}}'))
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "lawn_mower", "lawn_mower")).state == (
        LawnMowerActivity.MOWING
    )
    assert hass.states.get(_entity_id(hass, "sensor", "mow_progress")).state == "12"
    assert hass.states.get(_entity_id(hass, "sensor", "mow_state")).state == "mowing"
    # Non-DPS traffic (e.g. binary map frames) is ignored.
    channel.callback(FakeMessage(b"\x00\x01binary"))
    await hass.async_block_till_done()


async def test_failed_poll_keeps_entities_available(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    home_data_mock: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, config_entry)
    battery = _entity_id(hass, "sensor", "battery")

    home_data_mock.side_effect = RoborockException("cloud down")
    freezer.tick(UPDATE_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert home_data_mock.await_count == 2
    assert hass.states.get(battery).state == "100"

    home_data_mock.side_effect = None
    home_data_mock.return_value = make_home_data({"121": 64})
    freezer.tick(UPDATE_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(battery).state == "64"
    assert hass.states.get(battery).state != STATE_UNAVAILABLE


async def test_start_command_uses_remote_pb(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await _setup(hass, config_entry)
    await hass.services.async_call(
        "lawn_mower",
        "start_mowing",
        {"entity_id": _entity_id(hass, "lawn_mower", "lawn_mower")},
        blocking=True,
    )
    method = channel.rpc_channel.send_command.await_args.args[0]
    params = channel.rpc_channel.send_command.await_args.kwargs["params"]
    assert method == "remote_pb"
    assert params["type"] == "APP_BUTTON"
    assert params["app_button"] == "MOW_GLOBAL"


async def test_rejected_command_raises_readable_error(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await _setup(hass, config_entry)
    channel.rpc_channel.send_command.return_value = ["fail"]
    with pytest.raises(HomeAssistantError, match="Edge cut failed"):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": _entity_id(hass, "button", "edge_cut")},
            blocking=True,
        )


async def test_areas_and_routines_are_discovered_in_background(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    channel: FakeChannel,
    routines_mock: AsyncMock,
) -> None:
    replies = [
        RoborockException("Command timed out after 10.0s"),  # mower asleep
        RoborockException(f"Unexpected API Result: {PREF_CONFIG_JSON}"),
    ]

    async def _send(method: str, params: dict) -> object:
        if params["type"] == "GET_MOW_PREFERENCE_CONFIG":
            raise replies.pop(0)
        return ["ok"]

    channel.rpc_channel.send_command.side_effect = _send
    routines_mock.return_value = [HomeDataScene(id=7, name="Vorgarten")]
    await _setup(hass, config_entry)

    for area_id in (2, 3):
        assert er.async_get(hass).async_get_entity_id(
            "button", DOMAIN, f"{MOWER_DUID}_mow_area_{area_id}"
        )
    assert er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{MOWER_DUID}_routine_7"
    )
    assert config_entry.runtime_data.coordinators[0].mower_api.areas == [
        {"id": 2, "name": "A1"},
        {"id": 3, "name": "A2"},
    ]


async def test_routine_fetch_failure_does_not_break_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, routines_mock: AsyncMock
) -> None:
    routines_mock.side_effect = RoborockException("cloud down")
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(_entity_id(hass, "button", "edge_cut"))


async def test_homeassistant_stop_closes_session(
    hass: HomeAssistant, config_entry: MockConfigEntry, mqtt_session: MagicMock
) -> None:
    await _setup(hass, config_entry)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    mqtt_session.close.assert_awaited_once()
    # Unloading afterwards must not close twice or raise.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    mqtt_session.close.assert_awaited_once()


async def test_diagnostics_are_redacted(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await _setup(hass, config_entry)
    channel.callback(FakeMessage(b'{"dps":{"123":58}}'))
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = json.dumps(diag)
    for secret in (
        "secret-token",
        "rriot_s",
        "rriot_k",
        "base64-gps",
        "SN123",
        "mower_key",
        "user@example.com",
    ):
        assert secret not in text, secret
    mower = diag["mowers"][0]
    assert mower["model"] == "roborock.mower.a222"
    assert mower["mow_state_label"] == "paused"
    assert mower["connection"]["push_count"] == 1
    assert diag["versions"]["python_roborock_bundled"] == "7.12.1"
    assert diag["home_data"]["source"] == SOURCE_CLOUD


async def test_mow_areas_service(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await _setup(hass, config_entry)
    [device] = dr.async_entries_for_config_entry(
        dr.async_get(hass), config_entry.entry_id
    )
    await hass.services.async_call(
        DOMAIN,
        "mow_areas",
        {"device_id": device.id, "area_ids": [2, 3]},
        blocking=True,
    )
    params = channel.rpc_channel.send_command.await_args.kwargs["params"]
    assert params["app_button"] == "MOW_SELECT"
    assert [b["id"] for b in params["modify_map"]["boundaries"]] == [2, 3]


async def test_stop_during_setup_logs_no_listener_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    channel: FakeChannel,
    mqtt_session: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Restarting HA while the entry is set up must not log listener errors."""
    await _setup(hass, config_entry)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text
    mqtt_session.close.assert_awaited_once()


async def test_transient_empty_device_list_uses_previous_snapshot(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    home_data_mock: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    freezer.tick(timedelta(hours=1))
    home_data_mock.return_value = make_home_data(with_mower=False)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert home_data_mock.await_count == 2


async def test_returning_is_not_shown_as_docked(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    """Field report: driving back after "return to dock" read as "docked"."""
    await _setup(hass, config_entry)
    mower = _entity_id(hass, "lawn_mower", "lawn_mower")
    channel.callback(FakeMessage(b'{"dps":{"123":55,"127":0}}'))
    await hass.async_block_till_done()
    assert hass.states.get(mower).state == LawnMowerActivity.MOWING

    await hass.services.async_call(
        "lawn_mower", "dock", {"entity_id": mower}, blocking=True
    )
    # Task ends, mower drives back: idle task code, not charging yet.
    channel.callback(FakeMessage(b'{"dps":{"123":0,"127":0}}'))
    await hass.async_block_till_done()
    assert hass.states.get(mower).state == LawnMowerActivity.RETURNING

    channel.callback(FakeMessage(b'{"dps":{"127":3}}'))
    await hass.async_block_till_done()
    assert hass.states.get(mower).state == LawnMowerActivity.DOCKED


async def test_stopped_in_garden_is_idle(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await _setup(hass, config_entry)
    channel.callback(FakeMessage(b'{"dps":{"123":0,"127":0}}'))
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "lawn_mower", "lawn_mower")).state == (
        LawnMowerActivity.IDLE
    )


async def test_zone_button_starts_area_mow(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    async def _send(method: str, params: dict) -> object:
        if params["type"] == "GET_MOW_PREFERENCE_CONFIG":
            raise RoborockException(f"Unexpected API Result: {PREF_CONFIG_JSON}")
        return ["ok"]

    channel.rpc_channel.send_command.side_effect = _send
    await _setup(hass, config_entry)
    button = _entity_id(hass, "button", "mow_area_2")
    assert hass.states.get(button).attributes["friendly_name"].endswith(
        "Mow zone: A1"
    )
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    params = channel.rpc_channel.send_command.await_args.kwargs["params"]
    assert params["app_button"] == "MOW_SELECT"
    assert params["modify_map"] == {"boundaries": [{"id": 2, "name": "A1"}]}

    # The edge cut uses the same saved areas, as the app does.
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(hass, "button", "edge_cut")},
        blocking=True,
    )
    params = channel.rpc_channel.send_command.await_args.kwargs["params"]
    assert params["app_button"] == "MOW_EDGE"
    assert [b["id"] for b in params["modify_map"]["boundaries"]] == [2, 3]


async def test_retired_area_select_is_removed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "select", DOMAIN, f"{MOWER_DUID}_mow_area", config_entry=config_entry
    )
    await _setup(hass, config_entry)
    assert registry.async_get_entity_id("select", DOMAIN, f"{MOWER_DUID}_mow_area") is None


async def test_bundled_library_logs_quietly() -> None:
    import logging

    assert (
        logging.getLogger("custom_components.roborock_mower.vendor").level
        == logging.INFO
    )


async def test_diagnostics_include_history(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    await _setup(hass, config_entry)
    await hass.services.async_call(
        "lawn_mower",
        "pause",
        {"entity_id": _entity_id(hass, "lawn_mower", "lawn_mower")},
        blocking=True,
    )
    channel.callback(FakeMessage(b'{"dps":{"123":58,"142":"gps-secret"}}'))
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    history = diag["mowers"][0]["history"]
    # Last two entries: our pause command, then the mower's answer push.
    assert [e["kind"] for e in history[-2:]] == ["command", "push"]
    assert history[-2]["command"] == "APP_BUTTON MOW_PAUSE"
    assert history[-2]["result"] == "['ok']"
    assert history[-1]["dps"] == {"123": 58}
    assert any(e.get("command") == "GET_MOW_PREFERENCE_CONFIG" for e in history)
    assert "gps-secret" not in json.dumps(diag)
    assert diag["mowers"][0]["activity"] == "paused"
