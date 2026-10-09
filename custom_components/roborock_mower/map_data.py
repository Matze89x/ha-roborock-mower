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


def parse_map(data: bytes) -> MowerMap | None:
    """The map of a ``get_map_diff`` answer, or None if it holds none."""
    fields = _message(_strip_prefix(bytes(data)))
    if not fields:
        return None
    zones: list[MapZone] = []
    for wire, value in fields.get(10, []):
        if wire != _LENGTH:
            continue
        area = _message(value)
        zone = _sub(area, 7)
        points = _points(zone, 6) or _points(area, 4)
        if len(points) < 3:
            continue
        zones.append(
            MapZone(
                id=_int(zone, 1) if zone else _int(area, 1),
                name=(_text(zone, 3) or _text(area, 5) or "").strip(),
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
    "lawn": "#7cc36b",
    "lawn_edge": "#2e7d32",
    "track": "#ffffff",
    "dock": "#1e88e5",
    "mower": "#ff7043",
    "label": "#1b3d1b",
}


@dataclass(slots=True)
class MapView:
    """The map with the mower's latest position and the track of its run."""

    map: MowerMap
    robot: Pose | None = None
    track: list[tuple[float, float]] = field(default_factory=list)


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
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
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


def render_svg(view: MapView) -> bytes:
    """The map as an SVG image: lawn, zone names, track, station and mower.

    North of the map frame (growing y) is up, as in the app.
    """
    min_x, min_y, max_x, max_y = _bounds(view)
    width, height = max_x - min_x, max_y - min_y
    scale = _PIXELS_PER_METRE

    def pt(x: float, y: float) -> str:
        return f"{(x - min_x):.3f},{(max_y - y):.3f}"

    def path(points: list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> str:
        return " ".join(pt(x, y) for x, y in points)

    unit = max(width, height) / 60  # line widths and sizes follow the map size
    parts = [
        (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width:.3f} {height:.3f}" '
            f'width="{round(width * scale)}" height="{round(height * scale)}">'
        )
    ]
    for zone in view.map.zones:
        parts.append(
            f'<polygon points="{path(zone.points)}" fill="{_COLORS["lawn"]}" '
            f'stroke="{_COLORS["lawn_edge"]}" stroke-width="{unit * 0.35:.3f}" '
            'stroke-linejoin="round"/>'
        )
    if len(view.track) > 1:
        parts.append(
            f'<polyline points="{path(view.track)}" fill="none" '
            f'stroke="{_COLORS["track"]}" stroke-opacity="0.75" '
            f'stroke-width="{unit * 0.3:.3f}" stroke-linecap="round" '
            'stroke-linejoin="round"/>'
        )
    for zone in view.map.zones:
        if not zone.name:
            continue
        cx, cy = _centroid(zone.points)
        label = escape(zone.name)
        if zone.area is not None:
            label += f" · {zone.area:.0f} m²"
        parts.append(
            f'<text x="{cx - min_x:.3f}" y="{max_y - cy:.3f}" '
            f'font-size="{unit * 1.6:.3f}" font-family="sans-serif" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'fill="{_COLORS["label"]}" stroke="#ffffff" '
            f'stroke-width="{unit * 0.25:.3f}" paint-order="stroke">{label}</text>'
        )
    if (dock := view.map.dock) is not None:
        size = unit * 1.2
        parts.append(
            f'<rect x="{dock.x - min_x - size / 2:.3f}" y="{max_y - dock.y - size / 2:.3f}" '
            f'width="{size:.3f}" height="{size:.3f}" rx="{size * 0.2:.3f}" '
            f'fill="{_COLORS["dock"]}" stroke="#ffffff" stroke-width="{unit * 0.15:.3f}"/>'
        )
    if (robot := view.robot or view.map.robot) is not None:
        radius = unit * 0.8
        x, y = robot.x - min_x, max_y - robot.y
        parts.append(
            f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{radius:.3f}" '
            f'fill="{_COLORS["mower"]}" stroke="#ffffff" stroke-width="{unit * 0.15:.3f}"/>'
        )
        if robot.yaw is not None:
            # Heading line; the image's y axis points down.
            hx = x + math.cos(robot.yaw) * radius * 1.8
            hy = y - math.sin(robot.yaw) * radius * 1.8
            parts.append(
                f'<line x1="{x:.3f}" y1="{y:.3f}" x2="{hx:.3f}" y2="{hy:.3f}" '
                f'stroke="{_COLORS["mower"]}" stroke-width="{unit * 0.3:.3f}" '
                'stroke-linecap="round"/>'
            )
    parts.append("</svg>")
    return "".join(parts).encode()
