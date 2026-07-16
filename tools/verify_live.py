r"""Live verifier for the reverse-engineered Roborock mower integration.

Runs the integration's OWN ``MowerApi`` code (imported by file path, no Home
Assistant needed) against your real mower so you can confirm the v0.4.0 changes
on-device. YOU log in interactively (email + the code Roborock emails you); the
session is cached to ``.mower_session.json`` so later runs skip login.

    # read-only: status decode + zone discovery + preference read (SAFE)
    .\.venv\Scripts\python.exe tools\verify_live.py

    # setting writes only -- efficiency mode + cutting height. Does NOT drive
    # the mower; safe while docked. Reads back and restores eff mode.
    .\.venv\Scripts\python.exe tools\verify_live.py settings

    # map capture (diagnostic) -- dumps the raw map + decodes its protobuf tree
    # to tools\map_dump\ so the lawn map can be rendered in HA. Does NOT drive.
    .\.venv\Scripts\python.exe tools\verify_live.py map

    # driving commands -- MOVES THE MOWER; asks y/N before each one.
    # Stand next to the mower, ready to stop it.
    .\.venv\Scripts\python.exe tools\verify_live.py move

Paste the whole output back to whoever is helping you; it contains no secrets
(the session file and GPS are not printed).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import aiohttp
from roborock.data import RoborockCategory, UserData
from roborock.devices.cache import DeviceCache, NoCache
from roborock.devices.rpc.v1_channel import create_v1_channel
from roborock.exceptions import RoborockRateLimit
from roborock.mqtt.roborock_session import create_lazy_mqtt_session
from roborock.protocol import create_mqtt_params
from roborock.web_api import RoborockApiClient, UserWebApiClient

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = REPO_ROOT / ".mower_session.json"
MAP_DIR = REPO_ROOT / "tools" / "map_dump"

# Import the integration's protocol layer by path (avoids importing Home Assistant).
_MODULE_PATH = REPO_ROOT / "custom_components" / "roborock_mower" / "mower_api.py"
_spec = importlib.util.spec_from_file_location("mower_api", _MODULE_PATH)
mower_api = importlib.util.module_from_spec(_spec)
sys.modules["mower_api"] = mower_api
_spec.loader.exec_module(mower_api)

# The generic protobuf field-tree decoder (for inspecting raw map bytes).
_dm_path = Path(__file__).resolve().parent / "decode_map.py"
_dm_spec = importlib.util.spec_from_file_location("decode_map", _dm_path)
decode_map = importlib.util.module_from_spec(_dm_spec)
_dm_spec.loader.exec_module(decode_map)


def derive_activity(s: "mower_api.MowerStatus") -> str:
    """Mirror of lawn_mower._derive_activity (kept HA-free for this tool)."""
    ms = s.mow_state
    if s.error_code or (ms is not None and ms in mower_api.MOW_STATES_ERROR):
        return "error"
    if ms in mower_api.MOW_STATES_PAUSED:
        return "paused"
    if ms in mower_api.MOW_STATES_RETURNING or s.dock_state in (1, 2):
        return "returning"
    if ms in mower_api.MOW_STATES_MOWING:
        return "mowing"
    if ms in mower_api.MOW_STATES_DOCKED:
        if ms == 0 and s.off_dock_no_task_status == 3:
            return "returning"
        return "docked"
    if ms in (None, 0):
        return "returning" if s.off_dock_no_task_status == 3 else "docked"
    return f"mowing (UNMAPPED state {ms})"


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


async def confirm(prompt: str) -> bool:
    return (await ask(f"{prompt} [y/N]: ")).lower() in ("y", "yes")


async def login(session: aiohttp.ClientSession) -> tuple[str, str | None, UserData]:
    if SESSION_FILE.exists():
        try:
            blob = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            print(f"Using cached session for {blob['username']}")
            return blob["username"], blob.get("base_url"), UserData.from_dict(
                blob["user_data"]
            )
        except (KeyError, ValueError) as err:
            print(f"Cached session unusable ({err}); logging in fresh")

    username = await ask("Roborock email: ")
    region = (await ask("Region [auto/us/eu/ru/cn] (default auto): ")) or "auto"
    base_url = None if region == "auto" else f"https://{region}iot.roborock.com"
    client = RoborockApiClient(username, base_url=base_url, session=session)
    await client.request_code_v4()
    code = await ask("Enter the code Roborock just emailed you: ")
    user_data = await client.code_login_v4(code)
    base_url = await client.base_url
    SESSION_FILE.write_text(
        json.dumps(
            {"username": username, "base_url": base_url, "user_data": user_data.as_dict()},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Logged in; session cached to {SESSION_FILE.name}")
    return username, base_url, user_data


def print_status(api: "mower_api.MowerApi") -> None:
    s = api.status
    dp = dict(s.raw_dps)
    dp.pop(142, None)  # drop GPS (location)
    print("\n=== DECODED STATUS ===")
    print(f"  activity (lawn_mower) : {derive_activity(s)}")
    print(f"  mow_state  (123)      : {s.mow_state}  -> {s.mow_state_label}")
    print(f"  mow_type   (122)      : {s.mow_type}  -> "
          f"{mower_api.MOW_TYPE_LABELS.get(s.mow_type)}")
    print(f"  battery    (121)      : {s.battery}%")
    print(f"  mow_progress (139)    : {s.mow_progress}%")
    print(f"  charge_state (127)    : {s.charge_state} -> "
          f"{mower_api.CHARGE_STATE_LABELS.get(s.charge_state)}")
    print(f"  charge_type  (129)    : {s.charge_type} -> "
          f"{mower_api.CHARGE_TYPE_LABELS.get(s.charge_type)}")
    print(f"  pend_type    (130)    : {s.pend_type} -> "
          f"{mower_api.PEND_TYPE_LABELS.get(s.pend_type)}")
    print(f"  dock_state   (128)    : {s.dock_state}")
    print(f"  off_dock_no_task (143): {s.off_dock_no_task_status}")
    print(f"  mow_eff_mode (133)    : {s.mow_eff_mode} -> "
          f"{mower_api.EFF_MODE_LABELS.get(s.mow_eff_mode or 0)}")
    print(f"  mow_height   (134)    : {s.mow_height}")
    print(f"  error_code   (120)    : {s.error_code}")
    print(f"  raw dps (no GPS)      : {dp}")


async def read_only_checks(api: "mower_api.MowerApi") -> None:
    print("\n=== ZONE DISCOVERY ===")
    try:
        areas = await api.get_areas()
        print(f"  get_areas()     -> {areas}")
        if areas:
            print("    (use these ids with the roborock_mower.mow_areas service)")
    except Exception as err:  # noqa: BLE001
        print(f"  get_areas() ERROR -> {type(err).__name__}: {err}")
    try:
        print(f"  get_map_names() -> {await api.get_map_names()}")
    except Exception as err:  # noqa: BLE001
        print(f"  get_map_names() ERROR -> {type(err).__name__}: {err}")
    try:
        print(f"  get_full_map()  -> {await api.get_full_map()}")
    except Exception as err:  # noqa: BLE001
        print(f"  get_full_map() ERROR -> {type(err).__name__}: {err}")

    print("\n=== EFFICIENCY-MODE / PREFERENCE READ ===")
    try:
        cfg = await api.get_mow_preference_config()
        print(f"  get_mow_preference_config() -> {cfg}")
        pref = await api._get_global_mow_preference()  # noqa: SLF001
        print(f"  global preference -> {pref}")
    except Exception as err:  # noqa: BLE001
        print(f"  ERROR -> {type(err).__name__}: {err}")


async def _eff_label(api: "mower_api.MowerApi") -> str:
    cfg = await api.get_mow_preference_config()
    eff = (cfg or {}).get("global", {}).get("effective")
    return f"preference.global.effective={eff!r}, DP133(push)={api.status.mow_eff_mode}"


async def safe_writes(api: "mower_api.MowerApi") -> None:
    """Setting writes that do NOT drive the mower (safe while docked)."""
    print("\n=== SAFE SETTING WRITES (no driving) ===")
    print("A result with no exception means the device accepted the command.\n")

    orig = api.status.mow_eff_mode
    print(f"Efficiency mode now: {orig} ({mower_api.EFF_MODE_LABELS.get(orig or 0)})")
    if await confirm("Test efficiency-mode WRITE (set, read back, restore)?"):
        target = 2 if orig != 2 else 1
        try:
            r = await api.set_mow_eff_mode(target)
            print(f"  set -> {mower_api.EFF_MODE_WIRE[target]}  result={r!r}")
        except Exception as err:  # noqa: BLE001
            print(f"  ERROR -> {type(err).__name__}: {err}")
        await asyncio.sleep(4)
        print(f"  read back: {await _eff_label(api)}")
        if orig in (1, 2, 3):
            await api.set_mow_eff_mode(orig)
            await asyncio.sleep(3)
            print(f"  restored to {mower_api.EFF_MODE_WIRE[orig]}: {await _eff_label(api)}")
    print()

    if await confirm("Test cutting-height WRITE (set 45 mm, read back DP 134)?"):
        try:
            r = await api.set_mow_height(45)
            print(f"  set height 45 -> result={r!r}")
        except Exception as err:  # noqa: BLE001
            print(f"  ERROR -> {type(err).__name__}: {err}")
        await asyncio.sleep(4)
        print(f"  read back: DP134 mow_height={api.status.mow_height} (push)")
    print()

    if await confirm("Test dock button (CHARGE) acceptance (harmless while docked)?"):
        try:
            print(f"  dock (CHARGE) -> {await api.dock()!r}")
        except Exception as err:  # noqa: BLE001
            print(f"  ERROR -> {type(err).__name__}: {err}")


async def map_capture(api: "mower_api.MowerApi") -> None:
    """Diagnostic: pull the raw map and decode its protobuf field tree.

    Saves any base64 map bytes to tools/map_dump/ and prints the generic
    protobuf tree (strings = names, f32/f64 = coordinates) so the map's
    boundary/grid format can be turned into an HA image.
    """
    import base64
    import json

    print("\n=== MAP CAPTURE (diagnostic) ===")
    try:
        print("  map names:", await api.get_map_names())
    except Exception as err:  # noqa: BLE001
        print(f"  map names ERROR -> {type(err).__name__}: {err}")

    for name in ("", "APP_MAP1.bin"):
        try:
            raw = await api.get_map_raw(name)
        except Exception as err:  # noqa: BLE001
            print(f"\n  GET_FULL_MAP(name={name!r}) ERROR -> {type(err).__name__}: {err}")
            continue
        print(f"\n  GET_FULL_MAP(name={name!r}) -> type={type(raw).__name__}")
        if isinstance(raw, dict):
            print("    keys:", list(raw))
            print("    ", json.dumps(raw)[:800])
        elif isinstance(raw, str):
            print(f"    len={len(raw)} head={raw[:60]!r}")
            try:
                data = base64.b64decode(raw, validate=False)
            except Exception as err:  # noqa: BLE001
                print(f"    not base64 ({err}); raw string above")
                continue
            MAP_DIR.mkdir(parents=True, exist_ok=True)
            out = MAP_DIR / (f"fullmap_{name}" if name else "fullmap.bin")
            out.write_bytes(data)
            print(f"    decoded {len(data)} bytes -> {out}")
            try:
                decode_map.show(decode_map.decode(data))
            except Exception as err:  # noqa: BLE001
                print(f"    protobuf decode error: {type(err).__name__}: {err}")
        else:
            print("    value:", raw)
    print("\n  Share tools/map_dump/*.bin (or the tree above) to build the HA map.")


async def drive_commands(api: "mower_api.MowerApi") -> None:
    """Commands that MOVE the mower. Stand next to it."""
    print("\n=== DRIVE COMMANDS (these MOVE the mower off the dock) ===")
    print("Each asks y/N. A result with no exception means it was accepted.\n")

    async def act(label: str, action: object) -> None:
        if not await confirm(f"Send: {label}?"):
            print("  skipped\n")
            return
        try:
            print(f"  OK -> {await action()!r}")
        except Exception as err:  # noqa: BLE001
            print(f"  ERROR -> {type(err).__name__}: {err}")
        await asyncio.sleep(6)  # let the MQTT push deliver the new state
        s = api.status
        print(f"  state (push): {s.mow_state} ({s.mow_state_label}) "
              f"activity={derive_activity(s)}\n")

    await act("start full mow (MOW_GLOBAL)", api.start)
    await act("pause (MOW_PAUSE)", api.pause)
    await act("resume (MOW_RESUME)", api.resume)
    await act("stop / end task (MOW_END)", api.stop)
    await act(
        "area mow A1 [id=2] (MOW_SELECT)",
        lambda: api.start_area_mow([{"id": 2, "name": "A1"}]),
    )
    await act("return to dock (CHARGE)", api.dock)


async def main() -> None:
    args = sys.argv[1:]
    if "move" in args:
        mode = "move"
    elif "settings" in args:
        mode = "settings"
    elif "map" in args:
        mode = "map"
    else:
        mode = "read"
    async with aiohttp.ClientSession() as session:
        username, base_url, user_data = await login(session)
        client = RoborockApiClient(username, base_url=base_url, session=session)
        web_api = UserWebApiClient(client, user_data)

        try:
            home_data = await web_api.get_home_data()
        except RoborockRateLimit:
            print(
                "home_data is rate-limited right now (5/hour, shared with the "
                "official Roborock integration). Wait a few minutes and retry."
            )
            return
        mowers = [
            (d, p)
            for d, p in home_data.device_products.values()
            if p.category == RoborockCategory.MOWER
        ]
        if not mowers:
            print("No mower devices on this account. Devices found:")
            for d, p in home_data.device_products.values():
                print(f"  {d.name}: category={p.category} model={p.model}")
            return
        device, product = mowers[0]
        print(f"Mower: {device.name}  model={product.model}  fw={device.fv}  "
              f"pv={device.pv}")

        mqtt_params = create_mqtt_params(user_data.rriot)
        mqtt_session = await create_lazy_mqtt_session(mqtt_params)
        channel = create_v1_channel(
            user_data, mqtt_params, mqtt_session, device,
            DeviceCache(device.duid, NoCache()),
        )
        api = mower_api.MowerApi(
            product, channel, web_api, device.duid, device.device_status
        )

        def on_push(msg: object) -> None:
            dps = mower_api.parse_dps_push(msg)
            if dps:
                api.apply_push(dps)

        unsub = await channel.subscribe(on_push)
        try:
            # Initial snapshot came from device_status (no extra REST call);
            # live changes below are observed via the MQTT push above.
            print_status(api)
            await read_only_checks(api)
            if mode == "settings":
                await safe_writes(api)
            elif mode == "move":
                await drive_commands(api)
            elif mode == "map":
                await map_capture(api)
            else:
                print("\n(read-only run; add 'settings' for no-drive setting "
                      "writes, 'map' to capture the map, or 'move' to drive)")
        finally:
            unsub()
            await mqtt_session.close()


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
