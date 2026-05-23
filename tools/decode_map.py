r"""Generic protobuf decoder for inspecting captured mower map frames.

Walks the wire format and prints a field tree, surfacing strings, doubles
(coordinates), and nested messages so we can spot Boundary / zone structures.

    .\.venv\Scripts\python.exe tools\decode_map.py tools\map_dump\frame_000_p6.bin
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while True:
        x = buf[i]
        i += 1
        val |= (x & 0x7F) << shift
        if not x & 0x80:
            return val, i
        shift += 7


def decode(buf: bytes, depth: int = 0, max_depth: int = 6) -> list:
    out: list = []
    i = 0
    n = len(buf)
    while i < n:
        try:
            tag, i = _read_varint(buf, i)
        except IndexError:
            break
        field, wt = tag >> 3, tag & 7
        if wt == 0:
            val, i = _read_varint(buf, i)
            out.append((field, "varint", val))
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            data = buf[i : i + ln]
            i += ln
            entry: dict = {"len": ln}
            try:
                s = data.decode("utf-8")
                if s.isprintable() and s:
                    entry["str"] = s
            except UnicodeDecodeError:
                pass
            if depth < max_depth and data:
                try:
                    sub = decode(data, depth + 1, max_depth)
                    if sub:
                        entry["sub"] = sub
                except (IndexError, struct.error):
                    pass
            if "str" not in entry and "sub" not in entry:
                entry["hex"] = data[:48].hex()
            out.append((field, "msg", entry))
        elif wt == 1:
            (val,) = struct.unpack("<d", buf[i : i + 8])
            i += 8
            out.append((field, "f64", round(val, 4)))
        elif wt == 5:
            (val,) = struct.unpack("<f", buf[i : i + 4])
            i += 4
            out.append((field, "f32", round(val, 4)))
        else:
            break
    return out


def show(tree: list, indent: int = 0) -> None:
    pad = "  " * indent
    for field, kind, val in tree:
        if kind == "msg" and isinstance(val, dict):
            head = f"{pad}#{field} msg(len={val['len']})"
            if "str" in val:
                head += f"  str={val['str']!r}"
            if "hex" in val:
                head += f"  hex={val['hex']}"
            print(head)
            if "sub" in val:
                show(val["sub"], indent + 1)
        else:
            print(f"{pad}#{field} {kind}={val}")


if __name__ == "__main__":
    path = Path(sys.argv[1])
    raw = path.read_bytes()
    if raw[:2] == b"PB":
        print(f"[stripped 'PB' magic; {len(raw) - 2} bytes]")
        raw = raw[2:]
    show(decode(raw))
