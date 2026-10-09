"""Building protobuf messages like the mower's map and status stream.

Made-up shapes and numbers (a 10 x 8 m lawn), no data of a real garden.
"""

from __future__ import annotations

import struct


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def f32(number: int, value: float) -> bytes:
    return _varint(number << 3 | 5) + struct.pack("<f", value)


def f64(number: int, value: float) -> bytes:
    return _varint(number << 3 | 1) + struct.pack("<d", value)


def sub(number: int, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return _varint(number << 3 | 2) + _varint(len(body)) + body


def text(number: int, value: str) -> bytes:
    return sub(number, value.encode())


def point(number: int, x: float, y: float, yaw: float | None = None) -> bytes:
    parts = [f32(4, x), f32(5, y)]
    if yaw is not None:
        parts.append(f32(6, yaw))
    return sub(number, *parts)


LAWN = [(0.0, 0.0), (10.0, 0.0), (10.0, 8.0), (0.0, 8.0)]

MAP = b"pb" + b"".join(
    [
        sub(1, varint(2, 1700000000000)),
        sub(
            4,
            varint(3, 200),  # height in pixels
            varint(4, 240),  # width in pixels
            f32(5, 0.05),
            f32(6, -1.5),  # origin y
            f32(7, -0.5),  # origin x
        ),
        sub(
            10,
            varint(1, 2),
            *(point(4, x, y) for x, y in LAWN),
            text(5, "A1"),
            sub(
                7,
                varint(1, 2),
                varint(2, 2),
                text(3, "Wiese <hinten>"),
                f32(4, 80.0),
                f32(5, 2400.0),
                *(point(6, x, y) for x, y in LAWN),
            ),
            f32(8, 80.0),
        ),
        sub(20, point(1, 1.0, 1.0, 0.5), point(2, 0.5, 0.5, -1.0)),
        f32(29, 80.0),
        f32(30, 2400.0),
        text(34, "2026-10-09-12-36-43"),
    ]
)

def stream_frame(x: float, y: float, yaw: float) -> bytes:
    """The status stream while mowing (protocol 702): the position in the
    map, plus a GPS position as 64-bit numbers."""
    return b"PB" + b"".join(
        [
            varint(1, 1791552332),
            text(2, "702"),
            sub(
                5,
                varint(1, 1700000000000),
                sub(
                    12,
                    varint(2, 11),
                    sub(6, point(8, x, y, yaw)),
                    sub(23, f64(1, 49.3312), f64(2, 7.1123)),
                ),
            ),
        ]
    )


STREAM_FRAME = stream_frame(3.5, -2.25, 1.5)
