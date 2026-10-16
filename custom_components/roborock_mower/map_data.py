"""The mower's map: reading the app's ``get_map_diff`` answer and drawing it.

The answer (asked through the cloud map channel, see
:meth:`MowerApi.get_map_rpc`) is ``pb`` followed by a protobuf message. No
schema is published; the fields below were matched against the app and the
JSON status (``GET_ROBOT_STATUS``) of a RockNeo Q105:

====  ==============================================================
4     map info: 3 height, 4 width (pixels), 5 resolution (m),
      6 origin y, 7 origin x (m) -- the same values as the status'
      ``navigation.rgb_map_info``
10    a mowing area (repeated): 1 id, 4 boundary points {4 x, 5 y},
      5 short name, 7 zone {1 id, 3 name, 4 area m², 5 time s,
      6 points}, 8 area m²
20    poses {4 x, 5 y, 6 yaw}: 1 mower, 2 charging station
29    lawn area (m²), 30 expected time (s)
33    zone list: 1 {1 id, 2 name} -- the names the app shows
34    time the map file changed (``2026-10-09-12-36-43``)
====  ==============================================================

Coordinates are metres in the mower's own map frame (no geographic
position). Live mower positions come from the full status
(``navigation.map.robot_pose``) and, while mowing, from the status stream
the mower sends every two seconds (protocol 702, the same status as
protobuf: field 5 -> 12 navigation -> 6 map -> 8 robot pose).

That stream also carries the GPS position as 64-bit numbers. Nothing here
keeps or shows those: :func:`readable_tree` replaces every 64-bit value.

No Home Assistant imports, so it can be tested on its own.
"""

from __future__ import annotations

import base64
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from html import escape
from typing import Any

from .robot_status import FRAME_SHOW_LIMIT, REDACTED, as_number, dig, frame_content

PROTOBUF_PREFIXES = (b"pb", b"PB")
# Protocol of the status stream sent while mowing.
PROTOCOL_STATUS_STREAM = 702

# Varint, 64-bit, length-delimited, 32-bit.
_VARINT, _FIXED64, _LENGTH, _FIXED32 = 0, 1, 2, 5
_MAX_DEPTH = 12
# Anything further away from the map origin is not a position on a lawn.
_MAX_COORDINATE = 5000.0


class ProtobufError(ValueError):
    """The bytes are not a protobuf message."""


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ProtobufError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return result, pos


def decode(data: bytes) -> list[tuple[int, int, Any]]:
    """The fields of a protobuf message as ``(number, wire type, value)``.

    Values: ``int`` (varint), ``bytes`` (8 / 4 bytes for the fixed types, the
    content for length-delimited ones). Raises :class:`ProtobufError` if the
    bytes don't form a message.
    """
    fields: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ProtobufError("field number 0")
        if wire == _VARINT:
            value, pos = _varint(data, pos)
        elif wire == _FIXED64:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == _FIXED32:
            value, pos = data[pos : pos + 4], pos + 4
        elif wire == _LENGTH:
            size, pos = _varint(data, pos)
            value, pos = data[pos : pos + size], pos + size
        else:
            raise ProtobufError(f"wire type {wire}")
        if pos > len(data):
            raise ProtobufError("truncated field")
        fields.append((number, wire, value))
    return fields


def _message(data: Any) -> dict[int, list[tuple[int, Any]]]:
    """Fields by number (``{number: [(wire, value), ...]}``); empty if not a message."""
    if not isinstance(data, (bytes, bytearray)):
        return {}
    try:
        decoded = decode(bytes(data))
    except ProtobufError:
        return {}
    fields: dict[int, list[tuple[int, Any]]] = {}
    for number, wire, value in decoded:
        fields.setdefault(number, []).append((wire, value))
    return fields


def _first(fields: dict[int, list[tuple[int, Any]]], number: int) -> tuple[int, Any] | None:
    values = fields.get(number)
    return values[0] if values else None


def _sub(fields: dict[int, list[tuple[int, Any]]], *path: int) -> dict[int, list[tuple[int, Any]]]:
    for number in path:
        item = _first(fields, number)
        if item is None or item[0] != _LENGTH:
            return {}
        fields = _message(item[1])
    return fields


def _float(fields: dict[int, list[tuple[int, Any]]], number: int) -> float | None:
    item = _first(fields, number)
    if item is None:
        return None
    wire, value = item
    if wire == _FIXED32 and len(value) == 4:
        result = struct.unpack("<f", value)[0]
    elif wire == _VARINT:
        result = float(value)
    else:
        # 64-bit values are left alone: in the status stream they are GPS.
        return None
    return result if math.isfinite(result) else None


def _int(fields: dict[int, list[tuple[int, Any]]], number: int) -> int | None:
    item = _first(fields, number)
    return item[1] if item is not None and item[0] == _VARINT else None


def _text(fields: dict[int, list[tuple[int, Any]]], number: int) -> str | None:
    item = _first(fields, number)
    if item is None or item[0] != _LENGTH:
        return None
    try:
        return bytes(item[1]).decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True, slots=True)
class Pose:
    """A position in the map frame (metres) with heading (radians)."""

    x: float
    y: float
    yaw: float | None = None


def _pose(fields: dict[int, list[tuple[int, Any]]]) -> Pose | None:
    x, y = _float(fields, 4), _float(fields, 5)
    if x is None or y is None or abs(x) > _MAX_COORDINATE or abs(y) > _MAX_COORDINATE:
        return None  # _float() never returns NaN or infinity
    return Pose(x, y, _float(fields, 6))


def _points(fields: dict[int, list[tuple[int, Any]]], number: int) -> tuple[tuple[float, float], ...]:
    points = []
    for wire, value in fields.get(number, []):
        if wire == _LENGTH and (pose := _pose(_message(value))) is not None:
            points.append((pose.x, pose.y))
    return tuple(points)


@dataclass(frozen=True, slots=True)
class MapZone:
    """A mowing zone: its boundary and what the app shows for it."""

    id: int | None
    name: str
    points: tuple[tuple[float, float], ...]
    area: float | None = None


@dataclass(frozen=True, slots=True)
class MowerMap:
    """What the integration draws of the mower's map."""

    zones: tuple[MapZone, ...]
    dock: Pose | None = None
    robot: Pose | None = None
    width: int | None = None
    height: int | None = None
    resolution: float | None = None
    origin_x: float | None = None
    origin_y: float | None = None
    area: float | None = None
    changed: str | None = None

    @property
    def summary(self) -> dict[str, Any]:
        """Facts about the map without any coordinates (for diagnostics)."""
        return {
            "zones": [
                {
                    "id": zone.id,
                    "name": zone.name,
                    "area": round(zone.area, 2) if zone.area is not None else None,
                    "boundary_points": len(zone.points),
                }
                for zone in self.zones
            ],
            "charging_station": self.dock is not None,
            "size_pixels": [self.width, self.height],
            "resolution": self.resolution,
            "area": round(self.area, 2) if self.area is not None else None,
            "changed": self.changed,
        }


def _strip_prefix(data: bytes) -> bytes:
    return data[2:] if data[:2] in PROTOBUF_PREFIXES else data


def _zone_names(fields: dict[int, list[tuple[int, Any]]]) -> dict[int | None, str]:
    """Zone names by id from the map's zone list (field 33: ``{1 {1 id, 2 name}}``)."""
    names: dict[int | None, str] = {}
    for wire, value in fields.get(33, []):
        if wire != _LENGTH:
            continue
        for item_wire, item in _message(value).get(1, []):
            if item_wire != _LENGTH:
                continue
            entry = _message(item)
            if (name := _text(entry, 2)) and name.strip():
                names[_int(entry, 1)] = name.strip()
    return names


def parse_map(data: bytes) -> MowerMap | None:
    """The map of a ``get_map_diff`` answer, or None if it holds none."""
    fields = _message(_strip_prefix(bytes(data)))
    if not fields:
        return None
    names = _zone_names(fields)
    zones: list[MapZone] = []
    for wire, value in fields.get(10, []):
        if wire != _LENGTH:
            continue
        area = _message(value)
        zone = _sub(area, 7)
        points = _points(zone, 6) or _points(area, 4)
        if len(points) < 3:
            continue
        zone_id = _int(zone, 1) if zone else _int(area, 1)
        zones.append(
            MapZone(
                id=zone_id,
                # The zone list has the name the app shows; once the mower
                # rewrote the map at the start of a run, the zone itself only
                # carried the short name ("A1").
                name=(names.get(zone_id) or _text(zone, 3) or _text(area, 5) or "").strip(),
                points=points,
                area=_float(zone, 4) if zone else _float(area, 8),
            )
        )
    info = _sub(fields, 4)
    poses = _sub(fields, 20)
    dock = _pose(_sub(poses, 2)) if poses else None
    if not zones and dock is None:
        return None
    return MowerMap(
        zones=tuple(zones),
        dock=dock,
        robot=_pose(_sub(poses, 1)) if poses else None,
        width=_int(info, 4),
        height=_int(info, 3),
        resolution=_float(info, 5),
        origin_x=_float(info, 7),
        origin_y=_float(info, 6),
        area=_float(fields, 29),
        changed=_text(fields, 34),
    )


def pose_from_status(status: dict[str, Any] | None) -> Pose | None:
    """The mower's position in the full JSON status, if it has one."""
    pose = dig(status, "navigation", "map", "robot_pose")
    if not isinstance(pose, dict):
        return None
    x, y = as_number(pose.get("f_x")), as_number(pose.get("f_y"))
    if (
        x is None
        or y is None
        or not abs(x) <= _MAX_COORDINATE
        or not abs(y) <= _MAX_COORDINATE
    ):
        return None
    yaw = as_number(pose.get("f_yaw"))
    return Pose(
        float(x), float(y), float(yaw) if yaw is not None and math.isfinite(yaw) else None
    )


def pose_from_stream(payload: bytes | None) -> Pose | None:
    """The mower's position in a status-stream message (protocol 702)."""
    if not payload or payload[:2] not in PROTOBUF_PREFIXES:
        return None
    pose = _sub(_message(payload[2:]), 5, 12, 6, 8)
    return _pose(pose) if pose else None


def readable_tree(data: bytes, depth: int = 0) -> dict[str, Any] | None:
    """A protobuf message as JSON-ready data, every 64-bit value replaced.

    For looking at map and stream messages: field numbers as keys, repeated
    fields as lists, 32-bit values as floats, texts as text. 64-bit values
    can be the GPS position, so they are never shown; nor are texts that look
    like a GNSS sentence. None if the bytes aren't a message.
    """
    try:
        decoded = decode(_strip_prefix(data) if depth == 0 else data)
    except ProtobufError:
        return None
    tree: dict[str, list[Any]] = {}
    for number, wire, value in decoded:
        if wire == _VARINT:
            item: Any = value
        elif wire == _FIXED64:
            item = REDACTED
        elif wire == _FIXED32:
            number_value = struct.unpack("<f", value)[0]
            item = round(number_value, 4) if math.isfinite(number_value) else None
        else:
            item = _readable_bytes(value, depth)
        tree.setdefault(str(number), []).append(item)
    return {key: items[0] if len(items) == 1 else items for key, items in tree.items()}


def readable_frame(payload: bytes) -> dict[str, Any]:
    """A raw message in readable form, protobuf included (64-bit values hidden)."""
    if payload[:2] not in PROTOBUF_PREFIXES:
        return frame_content(payload)
    tree = readable_tree(payload) if len(payload) <= FRAME_SHOW_LIMIT else None
    if tree is None:
        return {"omitted": "binary message, see the saved file"}
    return {"protobuf": tree}


def _readable_bytes(value: bytes, depth: int) -> Any:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and text.isprintable():
        return REDACTED if "GGA" in text or "$GN" in text or "$GP" in text else text
    if depth < _MAX_DEPTH and value and (nested := readable_tree(value, depth + 1)) is not None:
        return nested
    return {"base64": base64.b64encode(value).decode()} if value else ""


# -- drawing -------------------------------------------------------------------

_MARGIN = 1.0  # metres around the drawn things when the map size is unknown
_PIXELS_PER_METRE = 40
_COLORS = {
    # Like the Roborock app: dark lawn, white lines, light station place.
    "lawn": "#3f7a2e",
    "lawn_edge": "#e9f5e3",
    "track": "#ffffff",
    "grid": "#8a8a8a",
    "station_place": "#1f2a1f",
    "station_place_edge": "#ffffff",
    "station": "#4a4a4a",
    "mower_body": "#3b3b3b",
    "mower_top": "#5c5c5c",
    "mower_line": "#b0b0b0",
    "label": "#b8eadb",
    "label_halo": "#24501a",
    "scale": "#2f4f2a",
}# What can be shown, and what is shown unless switched otherwise.
MAP_LAYERS = ("track", "labels", "dock", "mower", "grid")
DEFAULT_MAP_LAYERS: dict[str, bool] = {
    "track": True,
    "labels": True,
    "dock": True,
    "mower": True,
    "grid": False,
}


def map_layer_option(layer: str) -> str:
    """The config entry option that shows or hides a layer (``map_track`` ...)."""
    return f"map_{layer}"


def map_layers_from_options(options: Mapping[str, Any]) -> dict[str, bool]:
    """Which layers to show, from the integration's options."""
    return {
        layer: bool(options.get(map_layer_option(layer), shown))
        for layer, shown in DEFAULT_MAP_LAYERS.items()
    }


# The mower drawn from above (shape of a RockNeo, drawn about 1 m long like
# in the app) and the place around the charging station, in metres. Both
# grow on large maps so they stay visible.
_MOWER_LENGTH, _MOWER_WIDTH = 1.0, 0.74
# Track positions further apart than this (metres) are not joined by a line.
TRACK_MAX_GAP = 3.0
# The garage place, in the station's frame (x along the dock's heading, which
# is where a docked mower looks, into the garage). The dock position lies at the
# garage's entrance: a docked mower stands _DOCKED_AHEAD ahead of it, flush
# with the back. Like in the app the place reaches 1 m beyond the mower in
# front and about as far to both sides, and not behind it.
_DOCKED_AHEAD = 0.75
_PLACE_BEHIND = 0.1
_PLACE_FRONT = 1.0
_PLACE_SIDE = 0.9


@dataclass(slots=True)
class MapView:
    """The map with the mower's latest position and the track of its run."""

    map: MowerMap
    robot: Pose | None = None
    track: list[tuple[float, float]] = field(default_factory=list)
    layers: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_MAP_LAYERS))

    def shows(self, layer: str) -> bool:
        return self.layers.get(layer, DEFAULT_MAP_LAYERS.get(layer, False))


def _bounds(view: MapView) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` of what is drawn, in metres.

    The map's own frame when known (it has a margin already), grown if
    something lies outside; else the drawn things with a metre around them.
    """
    m = view.map
    if (
        m.width
        and m.height
        and m.resolution
        and m.origin_x is not None
        and m.origin_y is not None
    ):
        min_x, min_y = m.origin_x, m.origin_y
        max_x, max_y = min_x + m.width * m.resolution, min_y + m.height * m.resolution
        margin = _MARGIN / 5
    else:
        min_x = min_y = math.inf
        max_x = max_y = -math.inf
        margin = _MARGIN
    points = [p for zone in m.zones for p in zone.points] + list(view.track)
    points += [(p.x, p.y) for p in (m.dock, view.robot or m.robot) if p is not None]
    for x, y in points:
        min_x, min_y = min(min_x, x - margin), min(min_y, y - margin)
        max_x, max_y = max(max_x, x + margin), max(max_y, y + margin)
    if not math.isfinite(min_x):
        return 0.0, 0.0, 1.0, 1.0
    return min_x, min_y, max_x, max_y


def _centroid(points: tuple[tuple[float, float], ...]) -> tuple[float, float]:
    area = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True):
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area) < 1e-9:
        return (
            sum(x for x, _ in points) / len(points),
            sum(y for _, y in points) / len(points),
        )
    return cx / (3 * area), cy / (3 * area)


def _scale_length(width: float) -> int:
    """A round length for the scale bar, about a fifth of the map's width."""
    for length in (1, 2, 5, 10, 20, 50, 100):
        if length >= width / 5:
            return length
    return 200


def render_svg(view: MapView) -> bytes:
    """The map as an SVG image in the style of the Roborock app.

    Dark green lawn with a light edge always; as switched: the track as white
    lines, zone names, the light place around the charging station, the mower
    from above turned where it heads, and a 1-metre grid with a scale bar.
    North of the map frame (growing y) is up, as in the app.
    """
    min_x, min_y, max_x, max_y = _bounds(view)
    width, height = max_x - min_x, max_y - min_y
    unit = max(width, height) / 60  # symbol sizes follow the map size

    def x_of(x: float) -> float:
        return x - min_x

    def y_of(y: float) -> float:
        return max_y - y

    def path(points: list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> str:
        return " ".join(f"{x_of(x):.3f},{y_of(y):.3f}" for x, y in points)

    parts = [
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width:.3f} {height:.3f}" '
            f'width="{round(width * _PIXELS_PER_METRE)}" '
            f'height="{round(height * _PIXELS_PER_METRE)}">'
        )
    ]
    zones = view.map.zones
    for zone in zones:
        parts.append(
            f'<polygon points="{path(zone.points)}" fill="{_COLORS["lawn"]}" '
            f'stroke="{_COLORS["lawn_edge"]}" stroke-width="{unit * 0.22:.3f}" '
            'stroke-linejoin="round"/>'
        )
    if view.shows("grid"):
        parts.extend(_grid(min_x, min_y, max_x, max_y, unit))
    dock = view.map.dock
    if view.shows("dock") and dock is not None:
        parts.append(_station_place(x_of(dock.x), y_of(dock.y), dock.yaw, unit))
    if view.shows("track"):
        for run in _track_runs(view.track):
            parts.append(
                f'<polyline points="{path(run)}" fill="none" '
                f'stroke="{_COLORS["track"]}" stroke-opacity="0.9" '
                f'stroke-width="{unit * 0.16:.3f}" stroke-linecap="round" '
                'stroke-linejoin="round"/>'
            )
    if view.shows("labels"):
        for zone in zones:
            if not zone.name:
                continue
            cx, cy = _centroid(zone.points)
            parts.append(
                f'<text x="{x_of(cx):.3f}" y="{y_of(cy):.3f}" '
                f'font-size="{unit * 1.7:.3f}" font-family="sans-serif" '
                'text-anchor="middle" dominant-baseline="middle" '
                f'fill="{_COLORS["label"]}" stroke="{_COLORS["label_halo"]}" '
                f'stroke-width="{unit * 0.2:.3f}" paint-order="stroke">'
                f"{escape(zone.name)}</text>"
            )
    robot = view.robot or view.map.robot
    show_mower = view.shows("mower") and robot is not None
    if view.shows("dock") and dock is not None:
        # The station where the mower docks, unless the mower stands there.
        scale = max(1.0, unit * 3.5 / _MOWER_LENGTH)
        spot_x, spot_y = _docked_spot(dock, scale)
        if not show_mower or math.hypot(robot.x - spot_x, robot.y - spot_y) > 0.6 * scale:
            parts.append(_station(x_of(spot_x), y_of(spot_y), dock.yaw, unit))
    if show_mower:
        parts.append(_mower(x_of(robot.x), y_of(robot.y), robot.yaw, unit))
    parts.append("</svg>")
    return "".join(parts).encode()


def _track_runs(track: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """The track in pieces that can be drawn as lines.

    Positions further apart than :data:`TRACK_MAX_GAP` were not driven in a
    straight line (the mower turns every few metres), so they are not joined:
    a line there would cut across the lawn.
    """
    runs: list[list[tuple[float, float]]] = []
    for point in track:
        if runs and math.dist(runs[-1][-1], point) <= TRACK_MAX_GAP:
            runs[-1].append(point)
        else:
            runs.append([point])
    return [run for run in runs if len(run) > 1]


def _turned(x: float, y: float, yaw: float | None) -> str:
    """Place a shape drawn along +x at ``(x, y)``, turned to ``yaw``.

    The image's y axis points down, so the map's angles turn the other way.
    """
    degrees = -math.degrees(yaw) if yaw is not None else 0.0
    return f'transform="translate({x:.3f} {y:.3f}) rotate({degrees:.1f})"'


def _docked_spot(dock: Pose, scale: float) -> tuple[float, float]:
    """Where a docked mower stands: ahead of the dock position, in its heading."""
    yaw = dock.yaw or 0.0
    return (
        dock.x + math.cos(yaw) * _DOCKED_AHEAD * scale,
        dock.y + math.sin(yaw) * _DOCKED_AHEAD * scale,
    )


def _station_place(x: float, y: float, yaw: float | None, unit: float) -> str:
    """The dark place of the garage, around where the mower docks.

    ``(x, y)`` is the dock position (the garage's entrance).
    """
    scale = max(1.0, unit * 3.5 / _MOWER_LENGTH)
    back = (_DOCKED_AHEAD + _MOWER_LENGTH / 2 + _PLACE_BEHIND) * scale
    front = (_DOCKED_AHEAD - _MOWER_LENGTH / 2 - _PLACE_FRONT) * scale
    half = (_MOWER_WIDTH / 2 + _PLACE_SIDE) * scale
    return (
        f'<rect {_turned(x, y, yaw)} x="{front:.3f}" y="{-half:.3f}" '
        f'width="{back - front:.3f}" height="{2 * half:.3f}" rx="{0.35 * scale:.3f}" '
        f'fill="{_COLORS["station_place"]}" fill-opacity="0.85" '
        f'stroke="{_COLORS["station_place_edge"]}" stroke-opacity="0.55" '
        f'stroke-width="{unit * 0.08:.3f}" stroke-dasharray="{unit * 0.5:.3f} {unit * 0.35:.3f}"/>'
    )


def _station(x: float, y: float, yaw: float | None, unit: float) -> str:
    """The charging station from above: a dark plate with its contact."""
    scale = max(1.0, unit * 3.5 / _MOWER_LENGTH)
    length, width = 0.7 * scale, 0.5 * scale
    return (
        f'<g {_turned(x, y, yaw)}>'
        f'<rect x="{-length / 2:.3f}" y="{-width / 2:.3f}" width="{length:.3f}" '
        f'height="{width:.3f}" rx="{width * 0.3:.3f}" fill="{_COLORS["station"]}" '
        f'stroke="#ffffff" stroke-width="{unit * 0.08:.3f}"/>'
        f'<circle cx="{length * 0.18:.3f}" cy="0" r="{width * 0.18:.3f}" '
        f'fill="none" stroke="{_COLORS["mower_line"]}" stroke-width="{unit * 0.08:.3f}"/>'
        "</g>"
    )


def _mower(x: float, y: float, yaw: float | None, unit: float) -> str:
    """The mower from above, its front to where it heads."""
    scale = max(1.0, unit * 3.5 / _MOWER_LENGTH)
    length, width = _MOWER_LENGTH * scale, _MOWER_WIDTH * scale
    return (
        f'<g {_turned(x, y, yaw)}>'
        f'<rect x="{-length / 2:.3f}" y="{-width / 2:.3f}" width="{length:.3f}" '
        f'height="{width:.3f}" rx="{width * 0.38:.3f}" fill="{_COLORS["mower_body"]}" '
        f'stroke="#1c1c1c" stroke-width="{unit * 0.08:.3f}"/>'
        f'<ellipse cx="{-length * 0.04:.3f}" cy="0" rx="{length * 0.3:.3f}" '
        f'ry="{width * 0.24:.3f}" fill="{_COLORS["mower_top"]}" '
        f'stroke="{_COLORS["mower_line"]}" stroke-width="{unit * 0.1:.3f}"/>'
        f'<ellipse cx="{-length * 0.04:.3f}" cy="0" rx="{length * 0.17:.3f}" '
        f'ry="{width * 0.1:.3f}" fill="none" stroke="{_COLORS["mower_line"]}" '
        f'stroke-width="{unit * 0.06:.3f}"/>'
        "</g>"
    )


def _grid(
    min_x: float, min_y: float, max_x: float, max_y: float, unit: float
) -> list[str]:
    """Lines every metre (every fifth stronger) and a scale bar."""
    width, height = max_x - min_x, max_y - min_y
    lines = []
    for metre in range(math.ceil(min_x), math.floor(max_x) + 1):
        lines.append((metre, f"M{metre - min_x:.3f} 0V{height:.3f}"))
    for metre in range(math.ceil(min_y), math.floor(max_y) + 1):
        lines.append((metre, f"M0 {max_y - metre:.3f}H{width:.3f}"))
    thin = "".join(step for metre, step in lines if metre % 5)
    strong = "".join(step for metre, step in lines if not metre % 5)
    parts = [
        f'<path d="{thin}" stroke="{_COLORS["grid"]}" stroke-opacity="0.25" '
        f'stroke-width="{unit * 0.06:.3f}"/>',
        f'<path d="{strong}" stroke="{_COLORS["grid"]}" stroke-opacity="0.45" '
        f'stroke-width="{unit * 0.1:.3f}"/>',
    ]
    length = _scale_length(width)
    x0, y0 = unit * 1.5, height - unit * 1.5
    bar = f"M{x0:.3f} {y0 - unit * 0.5:.3f}V{y0:.3f}H{x0 + length:.3f}V{y0 - unit * 0.5:.3f}"
    # A white halo keeps the bar readable on dark dashboards.
    parts.append(
        f'<path d="{bar}" fill="none" stroke="#ffffff" stroke-width="{unit * 0.45:.3f}"/>'
    )
    parts.append(
        f'<path d="{bar}" fill="none" stroke="{_COLORS["scale"]}" '
        f'stroke-width="{unit * 0.2:.3f}"/>'
    )
    parts.append(
        f'<text x="{x0 + length / 2:.3f}" y="{y0 - unit * 0.7:.3f}" '
        f'font-size="{unit * 1.2:.3f}" font-family="sans-serif" text-anchor="middle" '
        f'fill="{_COLORS["scale"]}" stroke="#ffffff" stroke-width="{unit * 0.2:.3f}" '
        f'paint-order="stroke">{length} m</text>'
    )
    return parts
