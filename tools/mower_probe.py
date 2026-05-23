r"""Standalone reverse-engineering probe for Roborock mowers.

Talks to the mower directly via python-roborock (same transport the HA
integration uses), so payloads are decrypted for you. Use it to capture the
real `get_status` fields and to discover which RPC method names the firmware
accepts — no Home Assistant restart loop required.

Run from the repo root with the project venv:

    .\.venv\Scripts\python.exe tools\mower_probe.py                   # interactive REPL
    .\.venv\Scripts\python.exe tools\mower_probe.py status            # one-shot get_status
    .\.venv\Scripts\python.exe tools\mower_probe.py send app_start    # one-shot command
    .\.venv\Scripts\python.exe tools\mower_probe.py watch 20          # listen 20s for pushes
    .\.venv\Scripts\python.exe tools\mower_probe.py --debug status    # any mode + verbose logs

First run asks for your Roborock email + the emailed code, then caches the
session to .mower_session.json so later runs skip login. Captured output is
also appended to tools/mower_capture.log so you can share it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
from pathlib import Path

import aiohttp
from roborock.callbacks import decoder_callback
from roborock.data import RoborockCategory, UserData
from roborock.devices.cache import DeviceCache, NoCache
from roborock.devices.rpc.v1_channel import create_v1_channel
from roborock.exceptions import RoborockException, RoborockUnsupportedFeature
from roborock.mqtt.roborock_session import create_lazy_mqtt_session
from roborock.protocol import create_mqtt_params
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol
from roborock.web_api import RoborockApiClient, UserWebApiClient

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = REPO_ROOT / ".mower_session.json"
CAPTURE_LOG = Path(__file__).resolve().parent / "mower_capture.log"

_LOGGER = logging.getLogger("mower_probe")

MENU = """
Commands:
  s | status         Dump get_status (read-only) -- the key status payload
  watch [secs]       Listen for push messages for N seconds (default 20)
  <method> [json]    Send a raw RPC method, optional JSON params, e.g.
                       app_start
                       set_mow_height {"height": 40}
                       set_mow_eff_mode {"mode": 0}
  help | ?           Show this menu
  q | quit           Exit

WARNING: movement methods physically move the mower. Be next to it and ready
to stop. Probe the real vacuum verbs first, then the *_mow variants:
  start: app_start | app_start_mow | app_mow_start
  pause: app_pause | app_pause_mow
  stop:  app_stop  | app_stop_mow
  dock:  app_charge | app_dock
"""


def log(text: str) -> None:
    """Print and append to the shareable capture log."""
    print(text)
    with CAPTURE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


async def ask(prompt: str) -> str:
    """Non-blocking input so push messages can still arrive while we wait."""
    return (await asyncio.to_thread(input, prompt)).strip()


def _dumps(value: object) -> str:
    return json.dumps(value, indent=2, default=str)


async def login(session: aiohttp.ClientSession) -> tuple[str, str | None, UserData]:
    if SESSION_FILE.exists():
        try:
            blob = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            user_data = UserData.from_dict(blob["user_data"])
            print(f"Loaded cached session for {blob['username']}")
            return blob["username"], blob.get("base_url"), user_data
        except (KeyError, ValueError) as err:
            print(f"Cached session unusable ({err}); logging in fresh")

    username = await ask("Roborock email: ")
    region = (await ask("Region [auto/us/eu/ru/cn] (default auto): ")) or "auto"
    base_url = None if region == "auto" else f"https://{region}iot.roborock.com"

    client = RoborockApiClient(username, base_url=base_url, session=session)
    await client.request_code_v4()
    print("A verification code was emailed to you.")
    code = await ask("Enter code: ")
    user_data = await client.code_login_v4(code)
    base_url = await client.base_url

    SESSION_FILE.write_text(
        json.dumps(
            {
                "username": username,
                "base_url": base_url,
                "user_data": user_data.as_dict(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Logged in; session cached to {SESSION_FILE.name}")
    return username, base_url, user_data


async def pick_mower(web_api: UserWebApiClient):
    home_data = await web_api.get_home_data()
    products = list(home_data.device_products.values())
    mowers = [(d, p) for d, p in products if p.category == RoborockCategory.MOWER]

    if not mowers:
        print("No mower devices found. Devices on this account:")
        for device, product in products:
            print(f"  {device.name}: category={product.category} model={product.model}")
        return None

    if len(mowers) == 1:
        return mowers[0]

    for index, (device, product) in enumerate(mowers):
        print(f"  [{index}] {device.name} ({product.model})")
    choice = await ask("Pick mower index (default 0): ")
    return mowers[int(choice) if choice.isdigit() else 0]


async def dump_status(channel) -> None:
    try:
        result = await channel.rpc_channel.send_command("get_status")
    except RoborockException as err:
        log(f"get_status FAILED -> {type(err).__name__}: {err}")
        return
    log("get_status ->\n" + _dumps(result))


async def send(channel, method: str, params: object) -> None:
    log(f"\nSending: {method}  params={params!r}")
    try:
        result = await channel.rpc_channel.send_command(method, params=params)
    except RoborockException as err:
        log(f"  REJECTED -> {type(err).__name__}: {err}")
    except Exception as err:  # noqa: BLE001 - dev tool, surface anything
        log(f"  ERROR -> {type(err).__name__}: {err}")
    else:
        log("  OK ->\n" + _dumps(result))


async def repl(channel) -> None:
    print(MENU)
    while True:
        line = await ask("> ")
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            return
        if line in ("h", "help", "?"):
            print(MENU)
            continue
        if line in ("s", "status"):
            await dump_status(channel)
            continue
        if line.split()[0] == "watch":
            parts = line.split()
            secs = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
            print(f"Listening {secs}s for push messages (drive the mower from the app now)...")
            await asyncio.sleep(secs)
            continue

        parts = line.split(maxsplit=1)
        method = parts[0]
        params: object = None
        if len(parts) > 1:
            try:
                params = json.loads(parts[1])
            except json.JSONDecodeError as err:
                print(f"Bad JSON params: {err}")
                continue
        await send(channel, method, params)


async def probe(channel, methods: list[str]) -> None:
    """Try each method; stop at the first the device does NOT reject as unknown."""
    for method in methods:
        try:
            result = await channel.rpc_channel.send_command(method)
        except RoborockUnsupportedFeature:
            log(f"  {method}: unknown_method")
            continue
        except RoborockException as err:
            log(f"  {method}: {type(err).__name__}: {err}  <-- EXISTS (errored)")
            return
        log(f"  {method}: ACCEPTED -> {_dumps(result)}")
        log(f"\n*** '{method}' is a real method ***")
        await asyncio.sleep(3)
        return
    log("\nAll candidates returned unknown_method.")


async def tryall(channel, methods: list[str]) -> None:
    """Try each read-only method and log every result (does not stop on success)."""
    for method in methods:
        try:
            result = await channel.rpc_channel.send_command(method)
        except RoborockUnsupportedFeature:
            log(f"  {method}: unknown_method")
        except RoborockException as err:
            log(f"  {method}: {type(err).__name__}: {err}")
        else:
            log(f"  {method}: -> {_dumps(result)}")


async def poll(channel, secs: int, interval: float) -> None:
    """Snapshot get_status repeatedly so we capture state changes driven from the app."""
    log(f"Polling get_status every {interval}s for {secs}s -- drive the mower from the app now.")
    loop = asyncio.get_event_loop()
    end = loop.time() + secs
    while loop.time() < end:
        try:
            result = await channel.rpc_channel.send_command("get_status")
            log(f"[poll t+{int(secs - (end - loop.time()))}s] {result}")
        except RoborockException as err:
            log(f"[poll] error: {type(err).__name__}: {err}")
        await asyncio.sleep(interval)


def _proto_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_remote_msg(app_button: int, msg_type: int = 6) -> bytes:
    """RemoteMsg: field1 id (uint64 ms), field2 type (enum, APP_BUTTON=6), field5 app_button."""
    return (
        b"\x08"
        + _proto_varint(int(time.time() * 1000))
        + b"\x10"
        + _proto_varint(msg_type)
        + b"\x28"
        + _proto_varint(app_button)
    )


def _parse_value(raw: str):
    try:
        return int(raw)
    except ValueError:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw


async def dpsset(channel, dps_id: int, value) -> None:
    """Write a single DPS by publishing a V1 {"dps":{id:value}} message."""
    payload = json.dumps(
        {"dps": {str(dps_id): value}, "t": int(time.time())}, separators=(",", ":")
    ).encode()
    message = RoborockMessage(
        protocol=RoborockMessageProtocol.RPC_REQUEST, payload=payload, version=b"1.0"
    )
    log(f"\nDPS write -> {dps_id}={value!r}")
    await channel._mqtt_channel.publish(message)
    await asyncio.sleep(5)  # let on_push surface any reaction


async def run_once(channel, web_api, device, product, args: list[str]) -> None:
    cmd = args[0]
    if cmd == "routines":
        routines = await web_api.get_routines(device.duid)
        if not routines:
            log("No routines/scenes defined for this device.")
        for routine in routines:
            log(
                f"routine id={getattr(routine, 'id', None)} "
                f"name={getattr(routine, 'name', None)!r} :: {routine}"
            )
        return
    if cmd == "runroutine":
        await web_api.execute_routine(int(args[1]))
        log(f"Executed routine {args[1]}")
        await asyncio.sleep(5)
        return
    if cmd == "watchin":
        secs = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        mqtt_ch = channel._mqtt_channel

        def _on_input(message: object) -> None:
            log(f"[INPUT] {message!r}")
            payload = getattr(message, "payload", None)
            if payload:
                log(f"  [INPUT-PAYLOAD] {payload.decode(errors='replace')}")

        dispatch = decoder_callback(mqtt_ch._decoder, _on_input, _LOGGER)
        unsub = await mqtt_ch._mqtt_session.subscribe(mqtt_ch._publish_topic, dispatch)
        log(f"Watching INPUT topic {mqtt_ch._publish_topic} for {secs}s -- trigger in the app NOW.")
        try:
            await asyncio.sleep(secs)
        finally:
            unsub()
        return
    if cmd == "info":
        log(f"pv={device.pv}  category={product.category}  model={product.model}  fv={device.fv}")
        try:
            log("device: " + _dumps(device.as_dict()))
            log("product: " + _dumps(product.as_dict()))
        except Exception as err:  # noqa: BLE001
            log(f"(as_dict unavailable: {err})")
        return
    if cmd in ("s", "status"):
        await dump_status(channel)
        return
    if cmd == "devstatus":
        log(f"device_status = {device.device_status}")
        return
    if cmd == "probe":
        await probe(channel, args[1:])
        return
    if cmd == "tryall":
        await tryall(channel, args[1:])
        return
    if cmd == "dpsset":
        await dpsset(channel, int(args[1]), _parse_value(args[2]))
        return
    if cmd == "remotepb":
        button: object = args[1]
        if isinstance(button, str) and button.lstrip("-").isdigit():
            button = int(button)
        fmt = args[2] if len(args) > 2 else "json"
        msg = {"id": str(int(time.time() * 1000)), "type": "APP_BUTTON", "app_button": button}
        if fmt == "b64":
            params: object = [base64.b64encode(_encode_remote_msg(int(button))).decode()]
        elif fmt == "list":
            params = [msg]
        else:
            params = msg
        log(f"\nremote_pb fmt={fmt} params={params!r}")
        try:
            result = await channel.rpc_channel.send_command("remote_pb", params=params)
        except Exception as err:  # noqa: BLE001
            log(f"  error -> {type(err).__name__}: {err}")
        else:
            log(f"  result -> {_dumps(result)}")
        await asyncio.sleep(5)
        return
    if cmd == "poll":
        secs = int(args[1]) if len(args) > 1 and args[1].isdigit() else 60
        interval = float(args[2]) if len(args) > 2 else 3.0
        await poll(channel, secs, interval)
        return
    if cmd == "watch":
        secs = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        log(f"Listening {secs}s for push messages...")
        await asyncio.sleep(secs)
        return
    if cmd in ("send", "raw"):
        method = args[1]
        params = json.loads(args[2]) if len(args) > 2 else None
    else:
        method = cmd
        params = json.loads(args[1]) if len(args) > 1 else None
    await send(channel, method, params)
    # give the device a few seconds to emit a status push reflecting the change
    await asyncio.sleep(3)


async def main() -> None:
    if "--debug" in sys.argv:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("roborock").setLevel(logging.DEBUG)

    async with aiohttp.ClientSession() as session:
        username, base_url, user_data = await login(session)
        client = RoborockApiClient(username, base_url=base_url, session=session)
        web_api = UserWebApiClient(client, user_data)

        selected = await pick_mower(web_api)
        if selected is None:
            return
        device, product = selected
        log(f"Using mower: {device.name}  model={product.model}  duid={device.duid}  fw={device.fv}")

        mqtt_params = create_mqtt_params(user_data.rriot)
        mqtt_session = await create_lazy_mqtt_session(mqtt_params)
        device_cache = DeviceCache(device.duid, NoCache())
        channel = create_v1_channel(
            user_data, mqtt_params, mqtt_session, device, device_cache
        )

        def on_push(message: object) -> None:
            log(f"[PUSH] {message!r}")
            payload = getattr(message, "payload", None)
            if not payload:
                return
            try:
                data = json.loads(payload.decode())
            except (ValueError, AttributeError, UnicodeDecodeError):
                return
            dps = data.get("dps") if isinstance(data, dict) else None
            if isinstance(dps, dict):
                status_dps = {k: v for k, v in dps.items() if k not in ("101", "102")}
                if status_dps:
                    log(f"  [DPS] {status_dps}")

        unsubscribe = await channel.subscribe(on_push)
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        try:
            if args:
                await run_once(channel, web_api, device, product, args)
            else:
                await repl(channel)
        finally:
            unsubscribe()
            await mqtt_session.close()


if __name__ == "__main__":
    # The Roborock MQTT client needs loop.add_reader/add_writer, which Windows'
    # default Proactor loop lacks; force the Selector loop there.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
