"""The map: decoding, drawing, the image entity and the live position."""

from __future__ import annotations

import copy
from datetime import timedelta
import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from homeassistant.components.image import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.roborock_mower.map_data import (
    MapView,
    MapZone,
    MowerMap,
    Pose,
    parse_map,
    pose_from_status,
    pose_from_stream,
    readable_frame,
    readable_tree,
    render_svg,
)
from custom_components.roborock_mower.robot_status import REDACTED
from custom_components.roborock_mower.vendor.roborock.exceptions import (
    RoborockException,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .conftest import FakeChannel, FakeMessage
from .protobuf import LAWN, MAP, STREAM_FRAME, stream_frame, sub, text
from .test_robot_status import LIVE_STATUS, _answering, _entity_id, _setup, _wait_until

# -- decoding --------------------------------------------------------------------


def test_the_map_is_read() -> None:
    mower_map = parse_map(MAP)
    assert mower_map is not None
    [zone] = mower_map.zones
    assert zone == MapZone(id=2, name="Wiese <hinten>", points=tuple(LAWN), area=80.0)
    assert mower_map.dock == Pose(0.5, 0.5, -1.0)
    assert mower_map.robot == Pose(1.0, 1.0, 0.5)
    assert (mower_map.width, mower_map.height) == (240, 200)
    assert mower_map.resolution == pytest.approx(0.05)
    assert (mower_map.origin_x, mower_map.origin_y) == (-0.5, -1.5)
    assert mower_map.area == 80.0
    assert mower_map.changed == "2026-10-09-12-36-43"
    # The summary (for diagnostics) has no coordinates.
    assert mower_map.summary == {
        "zones": [{"id": 2, "name": "Wiese <hinten>", "area": 80.0, "boundary_points": 4}],
        "charging_station": True,
        "size_pixels": [240, 200],
        "resolution": pytest.approx(0.05),
        "area": 80.0,
        "changed": "2026-10-09-12-36-43",
    }


@pytest.mark.parametrize(
    "data",
    [b"", b"pb", b"pb\x01\x02", b"not a map", b"pb" + text(34, "2026-10-09")],
)
def test_no_map_in_odd_answers(data: bytes) -> None:
    assert parse_map(data) is None


def test_positions() -> None:
    assert pose_from_stream(STREAM_FRAME) == Pose(3.5, -2.25, 1.5)
    assert pose_from_stream(None) is None
    assert pose_from_stream(b"PB\x01") is None
    assert pose_from_stream(b'{"dps": {}}') is None
    live = LIVE_STATUS["navigation"]["map"]["robot_pose"]
    assert pose_from_status(LIVE_STATUS) == Pose(live["f_x"], live["f_y"], live["f_yaw"])
    assert pose_from_status(
        {"navigation": {"map": {"robot_pose": {"f_x": "2.5", "f_y": 1}}}}
    ) == Pose(2.5, 1.0, None)
    assert pose_from_status({"navigation": {"map": {"robot_pose": {"f_x": "nan", "f_y": 1}}}}) is None
    assert pose_from_status({"navigation": {}}) is None
    assert pose_from_status(None) is None


def test_readable_frames_hide_the_gps_position() -> None:
    tree = readable_tree(STREAM_FRAME)
    assert tree["2"] == "702"
    assert tree["5"]["12"]["23"] == {"1": REDACTED, "2": REDACTED}
    assert "49.33" not in json.dumps(tree)
    # GNSS sentences in texts are hidden too.
    nmea = b"PB" + text(3, "$GNGGA,083000.00,4919.87,N,00706.74,E,4,30")
    assert readable_tree(nmea) == {"3": REDACTED}
    assert readable_frame(STREAM_FRAME) == {"protobuf": tree}
    assert readable_frame(b"PB\x01\x02") == {"omitted": "binary message, see the saved file"}
    assert readable_frame(b'{"a": 1}') == {"json": {"a": 1}}
    # Repeated fields become lists.
    assert readable_tree(sub(1, text(2, "a")) + sub(1, text(2, "b"))) == {
        "1": [{"2": "a"}, {"2": "b"}]
    }


# -- drawing ---------------------------------------------------------------------


def test_the_map_is_drawn() -> None:
    view = MapView(parse_map(MAP), Pose(5.0, 4.0, 0.0), [(1.0, 1.0), (5.0, 4.0)])
    svg = render_svg(view).decode()
    root = ElementTree.fromstring(svg)
    # The map frame: 240 x 200 pixels of 5 cm.
    assert root.get("viewBox") == "0 0 12.000 10.000"
    assert root.get("width") == "480"
    tags = [child.tag.rsplit("}", 1)[-1] for child in root]
    assert tags == ["polygon", "polyline", "text", "rect", "circle", "line"]
    assert "Wiese &lt;hinten&gt; · 80 m²" in svg
    # North up: y = 8 m (top of the lawn) is near the top of the picture.
    polygon = root[0].get("points").split()
    assert polygon[2] == "10.500,0.500"


def test_drawing_without_map_size() -> None:
    zone = MapZone(id=1, name="", points=((0.0, 0.0), (4.0, 0.0), (4.0, 3.0)))
    svg = render_svg(MapView(MowerMap(zones=(zone,)))).decode()
    root = ElementTree.fromstring(svg)
    # Fitted around the lawn with a metre on each side.
    assert root.get("viewBox") == "0 0 6.000 5.000"
    assert "<text" not in svg and "<circle" not in svg


# -- in Home Assistant -------------------------------------------------------------


def _map_channel(answers: list[bytes | Exception], calls: list[str]):
    async def _send(method: str, params: dict | None = None) -> bytes:
        calls.append(method)
        answer = answers[0] if len(answers) == 1 else answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return _send


async def test_map_image_follows_the_mower(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    status = copy.deepcopy(LIVE_STATUS)
    channel.rpc_channel.send_command.side_effect = _answering([], status)
    map_calls: list[str] = []
    channel.map_rpc_channel.send_command.side_effect = _map_channel([MAP], map_calls)
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinators[0]
    await _wait_until(hass, lambda: coordinator.mower_map is not None)
    assert map_calls == ["get_map_diff"]

    image = _entity_id(hass, "image", "map")
    await _wait_until(hass, lambda: hass.states.get(image).state != "unavailable")
    state = hass.states.get(image)
    assert state.attributes["zones"] == ["Wiese <hinten>"]
    assert state.attributes["track_points"] == 0
    picture = await async_get_image(hass, image)
    assert picture.content_type == "image/svg+xml"
    assert b"Wiese &lt;hinten&gt;" in picture.content
    assert b"<polyline" not in picture.content
    # Docked: the position comes from the full status.
    assert coordinator.robot_pose.x == LIVE_STATUS["navigation"]["map"]["robot_pose"]["f_x"]

    # Mowing: the status stream moves the mower and draws its track.
    channel.callback(FakeMessage(b'{"dps":{"123":55,"127":0}}'))
    channel.callback(SimpleNamespace(protocol=702, payload=STREAM_FRAME))
    channel.callback(SimpleNamespace(protocol=702, payload=stream_frame(3.55, -2.25, 1.5)))
    channel.callback(SimpleNamespace(protocol=702, payload=stream_frame(5.0, -2.25, 1.5)))
    await _wait_until(hass, lambda: coordinator.robot_pose == Pose(5.0, -2.25, 1.5))
    # Steps under 15 cm are left out.
    assert coordinator.track == [(3.5, -2.25), (5.0, -2.25)]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await _wait_until(hass, lambda: hass.states.get(image).attributes["track_points"] == 2)
    picture = await async_get_image(hass, image)
    assert b"<polyline" in picture.content

    # The same map file: not read again; a changed one: read again.
    await coordinator.async_refresh_robot_status()
    assert map_calls == ["get_map_diff"]
    status["map_abstracts"][0]["file_change_time"] = "2026-10-10-07-00-00"
    await coordinator.async_refresh_robot_status()
    assert map_calls == ["get_map_diff", "get_map_diff"]
    assert await hass.config_entries.async_unload(config_entry.entry_id)


async def test_map_retried_after_a_while(
    hass: HomeAssistant, config_entry: MockConfigEntry, channel: FakeChannel
) -> None:
    channel.rpc_channel.send_command.side_effect = _answering([])
    map_calls: list[str] = []
    channel.map_rpc_channel.send_command.side_effect = _map_channel(
        [RoborockException("Command timed out after 10.0s"), b"pb", MAP], map_calls
    )
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinators[0]
    await _wait_until(hass, lambda: map_calls == ["get_map_diff"])
    image = _entity_id(hass, "image", "map")
    assert hass.states.get(image).state == "unavailable"

    # Not asked again at every status read ...
    await coordinator.async_refresh_robot_status()
    assert len(map_calls) == 1
    # ... but after 30 minutes (an answer without a map counts as failed).
    coordinator._map_tried_at -= 31 * 60
    await coordinator.async_refresh_robot_status()
    assert len(map_calls) == 2 and coordinator.mower_map is None
    coordinator._map_tried_at -= 31 * 60
    await coordinator.async_refresh_robot_status()
    assert coordinator.mower_map is not None
    await _wait_until(hass, lambda: hass.states.get(image).state != "unavailable")
    assert await hass.config_entries.async_unload(config_entry.entry_id)
