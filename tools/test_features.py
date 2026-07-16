r"""End-to-end feature test for the Roborock Mower integration (v0.4.0).

Exercises every new feature against a REAL mower using the integration's own
``MowerApi`` (imported by path; no Home Assistant needed) and prints a PASS /
FAIL / SKIP summary. YOU log in once (cached to ``.mower_session.json``); no
credentials are handled by anyone but you.

    # non-driving checks only (safe while docked) -- status, zones, preference
    # read, efficiency-mode write+restore, height write, map capture:
    .\.venv\Scripts\python.exe tools\test_features.py

    # ALSO run the driving commands -- MOVES THE MOWER. Asks y/N per step and
    # docks at the end. Stand next to the mower.
    .\.venv\Scripts\python.exe tools\test_features.py --drive

Reads use the MQTT push + remote_pb (not the rate-limited cloud poll); the only
cloud ``get_home_data`` call is the one-time device discovery at startup.
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

_spec = importlib.util.spec_from_file_location(
    "mower_api", REPO_ROOT / "custom_components" / "roborock_mower" / "mower_api.py"
)
mower_api = importlib.util.module_from_spec(_spec)
sys.modules["mower_api"] = mower_api
_spec.loader.exec_module(mower_api)

RESULTS: list[tuple[str, str, str]] = []  # (name, status, detail)


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    print(f"  [{status:4}] {name}" + (f" -- {detail}" if detail else ""))


def derive_activity(s: "mower_api.MowerStatus") -> str:
    ms = s.mow_state
    if s.error_code or (ms is not None and ms in mower_api.MOW_STATES_ERROR):
        return "error"
    if ms in mower_api.MOW_STATES_PAUSED:
        return "paused"
    if ms in mower_api.MOW_STATES_RETURNING or s.dock_state in (1, 2):
        return "returning"
    if ms in mower_api.MOW_STATES_MOWING:
        return "mowing"
    if ms in mower_api.MOW_STATES_DOCKED or ms in (None, 0):
        return "docked"
    return f"mowing(unmapped {ms})"


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


async def confirm(prompt: str) -> bool:
    return (await ask(f"{prompt} [y/N]: ")).lower() in ("y", "yes")


async def login(session: aiohttp.ClientSession):
    if SESSION_FILE.exists():
        blob = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        print(f"Using cached session for {blob['username']}")
        return blob["username"], blob.get("base_url"), UserData.from_dict(
            blob["user_data"]
        )
    username = await ask("Roborock email: ")
    region = (await ask("Region [auto/us/eu/ru/cn]: ")) or "auto"
    base_url = None if region == "auto" else f"https://{region}iot.roborock.com"
    client = RoborockApiClient(username, base_url=base_url, session=session)
    await client.request_code_v4()
    code = await ask("Enter the emailed code: ")
    user_data = await client.code_login_v4(code)
    base_url = await client.base_url
    SESSION_FILE.write_text(
        json.dumps(
            {"username": username, "base_url": base_url, "user_data": user_data.as_dict()}
        ),
        encoding="utf-8",
    )
    return username, base_url, user_data


async def settle(api: "mower_api.MowerApi", secs: float = 6.0) -> str:
    """Wait for the MQTT push to reflect a command, return the activity."""
    await asyncio.sleep(secs)
    return derive_activity(api.status)


# --- non-driving feature checks -------------------------------------------


async def test_status_decode(api: "mower_api.MowerApi") -> None:
    s = api.status
    if s.battery is None and s.mow_state is None:
        record("status decode", "FAIL", "no battery/mow_state in snapshot")
        return
    record(
        "status decode",
        "PASS",
        f"battery={s.battery}% state={s.mow_state}({s.mow_state_label}) "
        f"activity={derive_activity(s)} charge={mower_api.CHARGE_STATE_LABELS.get(s.charge_state)}",
    )


async def test_zone_discovery(api: "mower_api.MowerApi") -> None:
    areas = await api.get_areas()
    if areas and all("id" in a for a in areas):
        record("zone discovery (get_areas)", "PASS", f"{len(areas)} zones: {areas}")
    else:
        record("zone discovery (get_areas)", "FAIL", f"got {areas!r}")
    names = await api.get_map_names()
    record("map names", "PASS" if names else "FAIL", f"{names}")


async def test_preference_read(api: "mower_api.MowerApi") -> None:
    cfg = await api.get_mow_preference_config()
    if isinstance(cfg, dict) and isinstance(cfg.get("global"), dict):
        record("preference read", "PASS", f"global={cfg['global']}")
    else:
        record("preference read", "FAIL", f"got {cfg!r}")


async def test_eff_mode_write(api: "mower_api.MowerApi") -> None:
    cfg = await api.get_mow_preference_config()
    orig = (cfg or {}).get("global", {}).get("effective")
    orig_code = {v: k for k, v in mower_api.EFF_MODE_WIRE.items()}.get(orig)
    target = 2 if orig_code != 2 else 1
    await api.set_mow_eff_mode(target)
    await asyncio.sleep(4)
    cfg2 = await api.get_mow_preference_config()
    now = (cfg2 or {}).get("global", {}).get("effective")
    ok = now == mower_api.EFF_MODE_WIRE[target]
    record(
        "efficiency-mode write (SET_MOW_PREFERENCE)",
        "PASS" if ok else "FAIL",
        f"{orig} -> {now} (wanted {mower_api.EFF_MODE_WIRE[target]})",
    )
    if orig_code:  # restore
        await api.set_mow_eff_mode(orig_code)
        await asyncio.sleep(3)
        record("efficiency-mode restore", "INFO", f"back to {orig}")


async def test_height_write(api: "mower_api.MowerApi") -> None:
    cfg = await api.get_mow_preference_config()
    orig = (cfg or {}).get("global", {}).get("height")
    target = 45 if orig != 45 else 40
    await api.set_mow_height(target)
    await asyncio.sleep(4)
    cfg2 = await api.get_mow_preference_config()
    now = (cfg2 or {}).get("global", {}).get("height")
    ok = now == target
    record(
        "cutting-height write (REMOTE_CMD + persist)",
        "PASS" if ok else "FAIL",
        f"pref.height {orig} -> {now} (wanted {target})",
    )
    if isinstance(orig, int):  # restore
        await api.set_mow_height(orig)
        await asyncio.sleep(3)
        record("cutting-height restore", "INFO", f"back to {orig}")


async def test_map_capture(api: "mower_api.MowerApi") -> None:
    raw = await api.get_map_raw()
    if isinstance(raw, str):
        record("map capture (get_map_raw)", "INFO", f"base64 protobuf, {len(raw)} chars (FDS file)")
    elif isinstance(raw, dict):
        record("map capture (get_map_raw)", "INFO", f"json keys={list(raw)}")
    else:
        record("map capture (get_map_raw)", "INFO", f"ack only ({raw!r}); map is an out-of-band FDS file")


# --- driving commands (gated) ---------------------------------------------


async def test_drives(api: "mower_api.MowerApi") -> None:
    print("\n-- DRIVING COMMANDS (these MOVE the mower; y/N each) --")

    async def step(name: str, action, expect: str) -> None:
        if not await confirm(f"Run: {name}?"):
            record(name, "SKIP")
            return
        await action()
        activity = await settle(api)
        ok = expect in activity or expect == "any"
        record(name, "PASS" if ok else "FAIL",
               f"activity={activity} (expected {expect}), state={api.status.mow_state}")

    await step("start full mow (MOW_GLOBAL)", api.start, "mowing")
    await step("pause (MOW_PAUSE)", api.pause, "paused")
    await step("resume (MOW_RESUME)", api.resume, "mowing")
    await step("edge cut (MOW_EDGE)", api.edge_cut, "mowing")
    await step("stop / end task (MOW_END)", api.stop, "any")
    await step(
        "area mow A1 [id=2] (MOW_SELECT)",
        lambda: api.start_area_mow([{"id": 2, "name": "A1"}]),
        "mowing",
    )
    await step("return to dock (CHARGE)", api.dock, "any")


async def main() -> None:
    drive = "--drive" in sys.argv[1:]
    async with aiohttp.ClientSession() as session:
        username, base_url, user_data = await login(session)
        client = RoborockApiClient(username, base_url=base_url, session=session)
        web_api = UserWebApiClient(client, user_data)
        try:
            home_data = await web_api.get_home_data()
        except RoborockRateLimit:
            print("home_data rate-limited (5/hour). Wait a few minutes and retry.")
            return
        mowers = [
            (d, p)
            for d, p in home_data.device_products.values()
            if p.category == RoborockCategory.MOWER
        ]
        if not mowers:
            print("No mower on this account.")
            return
        device, product = mowers[0]
        print(f"Mower: {device.name}  fw={device.fv}\n")

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
            print("== READ / DECODE ==")
            await test_status_decode(api)
            await test_zone_discovery(api)
            await test_preference_read(api)
            print("\n== SETTING WRITES (non-driving) ==")
            await test_eff_mode_write(api)
            await test_height_write(api)
            print("\n== MAP ==")
            await test_map_capture(api)
            if drive:
                await test_drives(api)
            else:
                print("\n(add --drive to also test the driving commands)")
        finally:
            unsub()
            await mqtt_session.close()

    print("\n===================== SUMMARY =====================")
    width = max((len(n) for n, _, _ in RESULTS), default=0)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "INFO": 0}
    for name, status, _ in RESULTS:
        counts[status] = counts.get(status, 0) + 1
        print(f"  {status:4}  {name.ljust(width)}")
    print("  " + "  ".join(f"{k}={v}" for k, v in counts.items() if v))
    print("===================================================")


if __name__ == "__main__":
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
