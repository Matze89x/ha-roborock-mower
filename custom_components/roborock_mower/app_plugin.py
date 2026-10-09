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
import io
import re
from typing import Any
import zipfile

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .vendor.roborock.data import HomeDataProduct, UserData
from .vendor.roborock.web_api import RoborockApiClient

# A request type name as a whole word: GET_ROBOT_STATUS, not xGET_Y or GET_Ya.
QUERY_NAME_RE = re.compile(rb"(?<![A-Za-z0-9_])GET_[A-Z][A-Z0-9_]{1,62}(?![A-Za-z0-9_])")
# Compiled (Hermes) bundles keep their strings back to back without quotes
# ("GET_AGET_BisArray"): take every run after "GET_" and split it at "GET_".
HERMES_MAGIC = b"\xc6\x1f\xbc\x03\xc1\x03\x19\x1f"
_RUN_RE = re.compile(rb"GET_[A-Z0-9_]+")
_SPLIT_RE = re.compile(rb"(?=GET_)")
_NAME_RE = re.compile(rb"GET_[A-Z][A-Z0-9_]{1,62}")
# Known and huge (the whole map as base64); not worth a scan.
SKIP_QUERIES = frozenset({"GET_FULL_MAP"})
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT = 180


def _names_in(blob: bytes) -> set[str]:
    if not blob.startswith(HERMES_MAGIC):
        return {match.decode() for match in QUERY_NAME_RE.findall(blob)}
    names: set[str] = set()
    for run in _RUN_RE.findall(blob):
        for part in _SPLIT_RE.split(run):
            part = part.rstrip(b"_")
            if _NAME_RE.fullmatch(part):
                names.add(part.decode())
    return names


def extract_query_names(data: bytes, depth: int = 0) -> list[str]:
    """``GET_*`` names in a plugin archive (zip, also nested) or a bundle file."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return sorted(_names_in(data) - SKIP_QUERIES)
    names: set[str] = set()
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
                names.update(extract_query_names(member, depth + 1))
            else:
                names.update(_names_in(member))
    return sorted(names - SKIP_QUERIES)


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


async def async_find_query_names(
    hass: HomeAssistant,
    client: RoborockApiClient,
    user_data: UserData,
    product: HomeDataProduct,
) -> tuple[list[str], list[str]]:
    """The ``GET_*`` names the app plugin knows, and what went wrong on the way."""
    urls, errors = await _plugin_urls(client, user_data, product)
    session = async_get_clientsession(hass)
    names: set[str] = set()
    for url in urls:
        try:
            data = await _download(session, url)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            errors.append(f"download: {_describe(err)}")
            continue
        names.update(await hass.async_add_executor_job(extract_query_names, data))
    return sorted(names), errors
