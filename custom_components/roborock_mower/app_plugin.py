"""Find the mower's read-only query names in the official Roborock app plugin.

The Roborock app drives a mower through a plugin it downloads per product
(React Native code). The plugin's protocol definitions name every request
the mower understands, e.g. ``GET_ROBOT_STATUS``. The ``scan_queries`` action
downloads the same plugin with the user's own account -- just like the app
does -- and pulls out the ``GET_*`` names, so the mower is asked exactly the
queries the app knows instead of guesses. Nothing is stored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import io
import re
import struct
from typing import Any
import zipfile

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .vendor.roborock.data import HomeDataProduct, UserData
from .vendor.roborock.web_api import RoborockApiClient

# A request type name as a whole word: GET_ROBOT_STATUS, not xGET_Y or GET_Ya.
QUERY_NAME_RE = re.compile(rb"(?<![A-Za-z0-9_])GET_[A-Z][A-Z0-9_]{1,62}(?![A-Za-z0-9_])")
# Compiled (Hermes) bundles keep their strings back to back, even
# overlapping ("GET_FEATURE" + "SET_NEW_PIN_CODE" = "GET_FEATURESET_NEW_PIN_CODE").
# Their string table says where each string starts and ends.
HERMES_MAGIC = b"\xc6\x1f\xbc\x03\xc1\x03\x19\x1f"
_HERMES_HEADER_SIZE = 128
_HERMES_FUNCTION_HEADER_SIZE = 16
_OVERFLOWED = 255
# Fallback when the table can't be read: every run after "GET_", split at "GET_".
_RUN_RE = re.compile(rb"GET_[A-Z0-9_]+")
_SPLIT_RE = re.compile(rb"(?=GET_)")
_NAME_RE = re.compile(rb"GET_[A-Z][A-Z0-9_]{1,62}")
_NAME_STR_RE = re.compile(r"GET_[A-Z][A-Z0-9_]{1,62}")
# Known and huge (the whole map as base64); not worth a scan.
SKIP_QUERIES = frozenset({"GET_FULL_MAP"})
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT = 180


def hermes_strings(blob: bytes) -> list[str] | None:
    """The (ASCII) strings of a Hermes bytecode file, from its string table.

    None when the file does not have the expected layout. Header: magic,
    version, source hash, file length, global code index, then the counts
    read here; sections follow the 128-byte header in this order.
    """
    if len(blob) < _HERMES_HEADER_SIZE or not blob.startswith(HERMES_MAGIC):
        return None
    functions, kinds, identifiers, count, overflow_count, storage_size = struct.unpack_from(
        "<6I", blob, 40
    )
    small_at = (
        _HERMES_HEADER_SIZE
        + functions * _HERMES_FUNCTION_HEADER_SIZE
        + kinds * 4
        + identifiers * 4
    )
    overflow_at = small_at + count * 4
    storage_at = overflow_at + overflow_count * 8
    if storage_at + storage_size > len(blob):
        return None
    overflow = struct.unpack_from(f"<{overflow_count * 2}I", blob, overflow_at)
    strings: list[str] = []
    for (entry,) in struct.iter_unpack("<I", blob[small_at:overflow_at]):
        utf16, offset, length = entry & 1, (entry >> 1) & 0x7FFFFF, entry >> 24
        if length == _OVERFLOWED:
            if offset >= overflow_count:
                return None
            offset, length = overflow[offset * 2], overflow[offset * 2 + 1]
        size = length * 2 if utf16 else length
        if offset + size > storage_size:
            return None
        if not utf16:  # request names are ASCII
            start = storage_at + offset
            strings.append(blob[start : start + size].decode("latin-1"))
    return strings


def _names_in(blob: bytes) -> set[str]:
    if not blob.startswith(HERMES_MAGIC):
        return {match.decode() for match in QUERY_NAME_RE.findall(blob)}
    if (strings := hermes_strings(blob)) is not None:
        return {text for text in strings if _NAME_STR_RE.fullmatch(text)}
    names: set[str] = set()
    for run in _RUN_RE.findall(blob):
        for part in _SPLIT_RE.split(run):
            part = part.rstrip(b"_")
            if _NAME_RE.fullmatch(part):
                names.add(part.decode())
    return names


def _files(data: bytes, depth: int = 0) -> Iterator[bytes]:
    """The files of a plugin archive (zip, also nested), or the data itself."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        yield data
        return
    unpacked = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            unpacked += info.file_size
            if unpacked > MAX_UNPACKED_BYTES:
                break
            member = archive.read(info)
            if depth < 2 and info.filename.lower().endswith(".zip"):
                yield from _files(member, depth + 1)
            else:
                yield member


def extract_query_names(data: bytes) -> list[str]:
    """``GET_*`` names in a plugin archive or bundle file."""
    names: set[str] = set()
    for blob in _files(data):
        names.update(_names_in(blob))
    return sorted(names - SKIP_QUERIES)


# Text constants of plain JS bundles; any word in a Hermes file without a table.
_QUOTED_RE = re.compile(rb"""["']([A-Za-z0-9_][A-Za-z0-9_ .:/-]{2,119})["']""")
_WORD_RE = re.compile(rb"[A-Za-z0-9_]{3,120}")
MAX_STRINGS = 500


def extract_strings(data: bytes, needles: list[str]) -> list[str]:
    """Texts of the plugin containing one of ``needles`` (any case)."""
    lowered = [needle.lower() for needle in needles if needle]
    found: set[str] = set()
    for blob in _files(data):
        if blob.startswith(HERMES_MAGIC):
            strings = hermes_strings(blob)
            texts = (
                strings
                if strings is not None
                else [match.decode() for match in _WORD_RE.findall(blob)]
            )
        else:
            texts = [match.decode() for match in _QUOTED_RE.findall(blob)]
        found.update(
            text
            for text in texts
            if len(text) <= 120 and any(needle in text.lower() for needle in lowered)
        )
    return sorted(found)[:MAX_STRINGS]


def _describe(err: BaseException) -> str:
    """A short error text without URLs (plugin links carry a signature)."""
    if isinstance(err, aiohttp.ClientResponseError):
        return f"HTTP {err.status}"
    if isinstance(err, aiohttp.ClientError) or not str(err):
        return type(err).__name__
    return f"{type(err).__name__}: {err}"[:200]


async def _plugin_urls(
    client: RoborockApiClient, user_data: UserData, product: HomeDataProduct
) -> tuple[list[str], list[str]]:
    """Download links of the product's own plugin and its category plugin."""
    urls: list[str] = []
    errors: list[str] = []
    try:
        products = await client.get_products(user_data)
        ids = [
            item.id
            for detail in products.category_detail_list
            for item in detail.product_list
            if item.model == product.model and item.id is not None
        ]
        if ids:
            urls.append(await client.download_code(user_data, ids[0]))
        else:
            errors.append(f"{product.model} is not in Roborock's product list")
    except Exception as err:  # noqa: BLE001 - unknown answer formats; report them
        errors.append(f"product plugin: {_describe(err)}")
    try:
        plugins: dict[str, Any] = await client.download_category_code(user_data)
        category = getattr(product.category, "value", product.category)
        if url := plugins.get(category):
            urls.append(url)
    except Exception as err:  # noqa: BLE001 - see above
        errors.append(f"category plugin: {_describe(err)}")
    return list(dict.fromkeys(url for url in urls if isinstance(url, str) and url)), errors


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    data = bytearray()
    async with asyncio.timeout(DOWNLOAD_TIMEOUT), session.get(url) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(1 << 16):
            data += chunk
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise ValueError("plugin larger than expected")
    return bytes(data)


async def _async_plugin_search(
    hass: HomeAssistant,
    client: RoborockApiClient,
    user_data: UserData,
    product: HomeDataProduct,
    search: Callable[[bytes], list[str]],
) -> tuple[list[str], list[str]]:
    """Download the plugins and run ``search`` on each (in the executor)."""
    urls, errors = await _plugin_urls(client, user_data, product)
    session = async_get_clientsession(hass)
    found: set[str] = set()
    for url in urls:
        try:
            data = await _download(session, url)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            errors.append(f"download: {_describe(err)}")
            continue
        found.update(await hass.async_add_executor_job(search, data))
    return sorted(found), errors


async def async_find_query_names(
    hass: HomeAssistant,
    client: RoborockApiClient,
    user_data: UserData,
    product: HomeDataProduct,
) -> tuple[list[str], list[str]]:
    """The ``GET_*`` names the app plugin knows, and what went wrong on the way."""
    return await _async_plugin_search(hass, client, user_data, product, extract_query_names)


async def async_find_strings(
    hass: HomeAssistant,
    client: RoborockApiClient,
    user_data: UserData,
    product: HomeDataProduct,
    needles: list[str],
) -> tuple[list[str], list[str]]:
    """Texts of the app plugin containing one of ``needles`` (e.g. enum names)."""
    found, errors = await _async_plugin_search(
        hass, client, user_data, product, lambda data: extract_strings(data, needles)
    )
    return found[:MAX_STRINGS], errors
