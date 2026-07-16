# Developer & Architecture Guide

This document is for developers working on the **Roborock Mower** Home Assistant
integration. It explains how the device actually works, how the integration is
built, and what is confirmed vs. still unknown. The mower has **no official Home
Assistant support and no public API**, so almost everything here was
reverse-engineered from a live device (RockNeo Q105, `roborock.mower.a222`).

> TL;DR: the mower is a Roborock **V1** device, but unlike vacuums its **status**
> comes from **Tuya data points (DPS)**, not RPC verbs. **Commands** — start, edge,
> select-area, pause, resume, stop, dock, cutting height, efficiency mode — all go
> through the app's **`remote_pb`** protobuf RPC (a `RemoteMsg` sent as JSON with
> string enum names). The full reverse-engineered protocol lives in
> **[PROTOCOL.md](PROTOCOL.md)**; this document covers the integration architecture.

> **Note (v0.4.0):** earlier versions of this integration sent pause/resume/dock as
> DPS writes (201–205). That was a reverse-engineering dead-end kept working on the
> a222, but the official app has **no DPS-write command path** — it drives every
> control through `remote_pb` `app_button`. The integration now matches the app.
> The DPS-write helper is retained only as a documented fallback.

---

## 1. Background: how `python-roborock` talks to devices

The integration depends on [`python-roborock`](https://github.com/Python-roborock/python-roborock).
That library selects a protocol per device based on the device's **`pv`** (protocol
version) field from `home_data`, **not** its product category
(`roborock/devices/device_manager.py`):

| `device.pv` | Protocol | Devices | Wire format |
| --- | --- | --- | --- |
| `1.0` | **V1** | Classic vacuums | JSON-RPC over MQTT (+ local TCP) |
| `A01` | A01 | Dyad wet/dry vac, Zeo washer | DPS over MQTT |
| `B01` | B01 | Q7 / Q10 vacuums | Protobuf over MQTT |

The mower reports `pv = "1.0"`, so it is a **V1** device. However, the library's
high-level V1 support is gated to `RoborockCategory.VACUUM`, so the device manager
**skips the mower** as unsupported. The mower still speaks the V1 transport — it just
uses a different application-level model (DPS), which is why this integration talks to
it directly rather than through the library's high-level device API.

---

## 2. The key insight: the mower is DPS-driven (Tuya model)

Vacuums expose status/commands via RPC methods (`get_status`, `app_start`, …) carried
in DPS `101`/`102` (`rpc_request`/`rpc_response`). The mower's firmware **only
implements a handful of RPC methods** (device-info queries); its real status and all
controls live in **discrete Tuya data points**.

Two facts make this workable without guessing:

1. **The DPS schema is published in `home_data`.** Each product has a
   `product.schema` listing every data point: `id`, `code`, `mode` (`ro`/`rw`/`wo`),
   and `type` (`VALUE`/`RAW`). This is the authoritative map of the device.
2. **Live values are in `device.device_status`.** `home_data` carries a
   `{ "<dps id>": value }` snapshot, and the device pushes the same data points over
   MQTT when they change.

The legacy `get_status` RPC **does** respond, but it is a permanent stub that always
returns `{"state": 0, "battery": 0}` even mid-mow. **Do not use it.**

---

## 3. Transport & security

- **Broker:** Roborock's regional MQTT broker (region derived from the account /
  `base_url`, e.g. EU).
- **Topics** (`roborock/devices/transport/mqtt_channel.py`):
  - Output (device → us): `rr/m/o/{rriot.u}/{username}/{duid}` — we subscribe here.
  - Input (cloud → device): `rr/m/i/{rriot.u}/{username}/{duid}` — commands are
    published here.
- **Encryption:** payloads are AES-encrypted with the device's `local_key`
  (from `home_data`). `python-roborock`'s `MqttChannel` encrypts on publish and
  decrypts on receive, so callbacks see **plaintext** `RoborockMessage` payloads.
- **ACL note:** the broker only lets a client subscribe to the **output** topic.
  Attempting to subscribe to the *input* topic times out — so we **cannot** sniff the
  commands the app sends to the device. (Verified.)

A decrypted V1 message payload looks like:

```json
{ "t": 1779540626, "dps": { "123": 58 } }
```

`dps` is the data-point dictionary; keys are stringified DPS ids.

---

## 4. The DPS protocol (reverse-engineered)

### 4.1 Status data points (read)

Captured from `product.schema` and confirmed against live values.

| DPS | code | type | Meaning | Notes |
| --- | --- | --- | --- | --- |
| 101 | rpc_request | RAW | RPC channel (vacuum-style) | not used for control |
| 102 | rpc_response | RAW | RPC response | stub `get_status` lives here |
| 120 | error_code | VALUE | Error code | 0 = no error |
| 121 | battery | VALUE | Battery % | **real** (the stub get_status battery is fake) |
| 122 | mow_type | VALUE | Mow mode | 0 idle, **1 full mow, 2 edge cut** |
| 123 | mow_state | VALUE | Activity state | see table below |
| 124 | mapping_type | VALUE | Map-build task type | |
| 125 | mapping_state | VALUE | Map-build state | |
| 126 | ota_state | VALUE | OTA/upgrade state | |
| 127 | charge_state | VALUE | Charge state | 1 = charging on dock |
| 129 | charge_type | VALUE | Reason for returning to charge | 1 seen while returning |
| 132 | mow_start_type | VALUE | How the mow was started | |
| 133 | mow_eff_mode | rw VALUE | Efficiency mode | writable; label mapping unverified |
| 134 | mow_height | rw VALUE | Cutting height | writable; schema `scale: 100`, value semantics unverified |
| 135 | mow_direction_angle | rw VALUE | Cutting direction | writable |
| 138 | offline_status | RAW | Offline reason | |
| 139 | mow_progress | VALUE | Session progress % | **real** |
| 142 | gps_coordinate | RAW | Last position | base64-encoded protobuf |
| 143 | off_dock_no_task_status | VALUE | Off-dock-with-no-task status | non-zero while **returning** to dock |
| 144 | afs_status | VALUE | After-sales mode | |
| 145 | network_channel | VALUE | WAN connection type | |

### 4.2 Command data points (write-only) — LEGACY, not used

Historically the integration wrote these DPS (value `1`). **The official app never
writes them** — all commands go through `remote_pb` (see §5). They are retained in
`mower_api.py` only as a documented fallback mechanism; nothing writes them by
default.

| DPS | code | type | Note |
| --- | --- | --- | --- |
| 201 | start | VALUE | no-op for a scalar write (needs a task payload) |
| 202 | dock | RAW | previously worked; now use `remote_pb` `CHARGE` |
| 203 | pause | RAW | previously worked; now use `remote_pb` `MOW_PAUSE` |
| 204 | resume | RAW | previously worked; now use `remote_pb` `MOW_RESUME` |
| 205 | stop | RAW | now use `remote_pb` `MOW_END` |

### 4.3 `mow_state` (DPS 123) values

| Value | Meaning |
| --- | --- |
| 0 | Idle / docked |
| 51 | Transient (starting / resuming) |
| 56, 57 | Mowing (active code varies) |
| 58 | Paused |

Because active codes vary (56/57, and likely more), the integration treats **any
non-zero, non-58 value as "mowing"** rather than enumerating each. "Returning to
dock" presents as `mow_state == 0` **plus** `off_dock_no_task_status != 0`.

### 4.4 Sending a command (the exact mechanism)

Commands go through the `remote_pb` RPC (see §5), **not** DPS writes. The
integration's `_send_remote_msg` sends a `RemoteMsg` as a plain dict (string enum
names, `id` as a string), which `python-roborock` places under `dps.101`:

```python
await channel.rpc_channel.send_command(
    "remote_pb",
    params={"id": str(int(time.time() * 1000)), "type": "APP_BUTTON",
            "app_button": "MOW_PAUSE"},
)  # -> "ok" (mapped to {}) on success
```

The legacy DPS-write mechanism (kept only as a fallback in `_write_dps`) looked
like this — the app does not use it:

```python
import json, time
from roborock.roborock_message import RoborockMessage, RoborockMessageProtocol

payload = json.dumps({"dps": {"203": 1}, "t": int(time.time())}).encode()
msg = RoborockMessage(protocol=RoborockMessageProtocol.RPC_REQUEST, payload=payload, version=b"1.0")
await mqtt_channel.publish(msg)
```

### 4.5 Reading DPS — important caveat

Do **not** use the library's `V1Channel.add_dps_listener` /
`decode_data_protocol_message`. Those map data points through the **vacuum**
`RoborockDataProtocol` enum (e.g. `121 = STATE`, `122 = BATTERY`), which **mis-maps**
the mower (`121 = battery`, `122 = mow_type`) and silently drops ids > 135. Instead,
parse the raw `payload["dps"]` yourself — see `parse_dps_push()` in `mower_api.py`.

---

## 5. Start / edge cut — the `remote_pb` command

Start, edge cut, and area mowing are **not** DPS writes (scalar DPS-201 writes are a
no-op — that earlier dead end). The app sends a `rock.common.remote.RemoteMsg` protobuf
via the RPC method **`remote_pb`**. We send it as the protobuf's JSON form (protobufjs
`toJSON`: string enum names, id as string):

```python
rpc_channel.send_command("remote_pb", params={
    "id": str(int(time.time() * 1000)),
    "type": "APP_BUTTON",
    "app_button": "MOW_GLOBAL",   # MOW_EDGE = edge cut, MOW_SELECT = area
})
# -> ["ok"]  (mower acts);  ["fail"] = rejected
```

`RemoteMsg` fields: `id` (uint64 ms), `type` (enum, `APP_BUTTON`), `app_button` (enum:
`MOW_GLOBAL` / `MOW_EDGE` / `MOW_SELECT`), `modify_map` (a `Map` with `boundaries[]`,
for area/edge selection). **Confirmed live:** `MOW_GLOBAL` starts a full-lawn mow;
`MOW_EDGE` starts an edge cut (`mow_type`→2 when started from the dock). This is why
scalar DPS-201 failed — the firmware wants this `remote_pb` protobuf RPC. The schema is
defined in the decompiled app (`rock.iot.*` / `rock.common.*` in `module_879.js` of the
[Python-roborock/RR_API](https://github.com/Python-roborock/RR_API) repo); call sites
are in `module_877.js` (`startGlobalMowing`/`startEdgeMowing`).

Because `remote_pb` is an RPC, the integration uses the **V1 channel**
(`create_v1_channel(...).rpc_channel.send_command`), not a bare `MqttChannel`. Pause /
resume / dock / stop still go through DPS writes (via the channel's MQTT sub-channel).

**Area / zone mowing** needs `MOW_SELECT` + `modify_map.boundaries` (saved-map zone
ids) — still requires parsing the protobuf map (the `protocol` 6/7 `PB…` stream). The
Roborock-app **routines** path also remains available (see `button.py`).

---

## 6. Integration architecture

```
home_data (REST) ──► find devices where product.category == MOWER
                         │
                         ▼
        create_mqtt_channel(user_data, mqtt_params, mqtt_session, device)
                         │
        ┌────────────────┴───────────────────────┐
        ▼ subscribe (output topic)                ▼ publish (input topic)
   parse_dps_push(msg) ─► MowerApi.apply_push    MowerApi._write_dps (commands)
        │                                          ▲
        ▼                                          │
   coordinator.async_set_updated_data        lawn_mower / number / select
        ▲
        │ every 60s (safety net)
   coordinator._async_update_data ─► MowerApi.poll_status ─► home_data.device_status
```

Status is **push-first** (live DPS over MQTT) with a **60s cloud poll** as a safety
net. Both feed a single `DataUpdateCoordinator[MowerStatus]`.

### File-by-file (`custom_components/roborock_mower/`)

| File | Responsibility |
| --- | --- |
| `__init__.py` | Setup/teardown: fetch `home_data`, filter mowers, create one MQTT channel + `MowerApi` + coordinator per device, wire the DPS-push handler, manage the MQTT session lifecycle. |
| `mower_api.py` | The protocol layer. `MowerStatus` dataclass + DPS id constants, `from_dps`/`coerce_dps`/`parse_dps_push`, and `MowerApi` (poll via `device_status`, merge live pushes, write command DPS, routines). |
| `coordinator.py` | `DataUpdateCoordinator` that polls `MowerApi.poll_status()` every 60s. |
| `entity.py` | Base entity: device info, availability, `status` accessor. |
| `lawn_mower.py` | Lawn mower entity. Activity mapping; `start_mowing` resumes when paused (and warns that fresh start must come from the app); `pause`/`dock`. |
| `sensor.py` | Battery, Mow Progress, Mow Mode (`mow_type`), Mow State, Charge State, Error Code. |
| `number.py` | Mow Height (DPS 134) — experimental. |
| `select.py` | Efficiency Mode (DPS 133) — experimental, labels unverified. |
| `button.py` | One button per Roborock routine/scene (`get_routines`/`execute_routine`). |
| `config_flow.py` | Email + region → emailed code → `code_login_v4`. Stores `UserData`. |
| `const.py` | `DOMAIN`, `PLATFORMS`, `UPDATE_INTERVAL`, region options. |

---

## 7. The probe tool (`tools/mower_probe.py`)

A standalone CLI used to reverse-engineer and debug the device directly, without
running Home Assistant. It logs in (caching the session to `.mower_session.json`,
gitignored), connects, and runs one command per invocation. Output is also appended to
`tools/mower_capture.log`.

```powershell
.\.venv\Scripts\python.exe tools\mower_probe.py <command> [args]
```

| Command | What it does |
| --- | --- |
| `info` | Dump the full device + product (incl. the DPS `schema`). |
| `devstatus` | Print just `device.device_status` (the live DPS snapshot). |
| `status` | Call the (stub) `get_status` RPC. |
| `send <method> [json]` | Send a raw V1 RPC method. |
| `probe <m1> <m2> …` | Try RPC method names, stop at the first the device accepts. |
| `tryall <m1> <m2> …` | Try read-only RPC methods, print every result. |
| `poll [secs] [interval]` | Snapshot `get_status` repeatedly (legacy; status is the stub). |
| `watch [secs]` | Print incoming MQTT messages / DPS pushes. |
| `dpsset <id> <value>` | **Write a data point** (the command mechanism). |
| `routines` / `runroutine <id>` | List / execute Roborock routines. |
| `watchin [secs]` | Attempt to subscribe to the input topic (blocked by ACL — kept for reference). |

`--debug` enables verbose `roborock` logging on any command.

> ⚠️ `dpsset` and movement commands physically move the mower. Use with care.

---

## 8. Development setup & validation

Windows note: the `python` on PATH may be the Microsoft Store stub. Use the `py`
launcher or the project venv.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install "python-roborock>=5.12.0,<6.0.0" ruff pytest
```

Validate before committing:

```powershell
.\.venv\Scripts\ruff.exe check custom_components\roborock_mower tools tests
.\.venv\Scripts\python.exe -m pytest tests -q
```

- **Lint:** `ruff` (the integration is kept lint-clean).
- **Tests:** `tests/test_mower_api.py` covers the DPS parsing (`coerce_dps`,
  `MowerStatus.from_dps`, `parse_dps_push`) against a **real captured payload**. It
  loads `mower_api.py` by file path so it doesn't import Home Assistant.
- The integration cannot be import-tested without a Home Assistant install; the
  real test is loading it in HA against a live account.

Pin `python-roborock` to a narrow range. The `roborock.devices.*` namespace is new and
changes across versions — re-verify the imports in `__init__.py`/`mower_api.py` on
every bump.

---

## 9. Status: confirmed vs. open

**Confirmed live (RockNeo Q105, fw 02.68.36):**
- Auth, device discovery, MQTT transport.
- Status decode via the real `RobotDetailState` table — verified both mid-mow
  (`55`→mowing, `52`→undocking) and docked (`0`→idle, `charge_state 1`→charging).
  Explicit mowing / paused / returning / docked / error (no more heuristic).
- **Start (`MOW_GLOBAL`)** → `['ok']`, mower undocked. **Pause (`MOW_PAUSE`)** →
  `['ok']`, accepted. Edge cut (`MOW_EDGE`) via `remote_pb`.
- Query responses are **JSON** (recovered by `MowerApi._query` from
  python-roborock's "Unexpected API Result"). Efficiency-mode **read** works;
  **zone discovery** works via the preference config's `custom[]` (A1/A2/A3 →
  `area_id` 2/3/4), surfaced by `get_areas()` + the Mow Area select.

**Also confirmed live (via `tools/test_features.py`):**
- Efficiency-mode **write** (`SET_MOW_PREFERENCE`): `DAILY`→`EFFICIENT` verified
  changed and restored.
- Cutting-height **write** (`REMOTE_CMD` + preference persist): `pref.height`→45
  verified persisted. (Note: the mower may not report `height` back until it is
  first set, so the number entity can read empty initially.)

**Still pending a live action to confirm:**
- resume/dock/stop/edge/`cancel_dock` `app_button` writes (transport proven by
  start + pause, both confirmed `['ok']` live). Test via `test_features.py --drive`.
- Which id `MOW_SELECT` wants (`area_id` from the preference config vs a boundary
  id) — the `--drive` area-mow step answers this.

**`get_home_data` is rate-limited — 5/hour, 40/day PER ACCOUNT** (shared if the
official Roborock integration also runs). This bit hard in practice: on a
`python-roborock` version that classified the mower under a different category,
setup raised `ConfigEntryNotReady`, HA retried it, and **each retry spent one
`home_data` call — 5 retries hit the 5/hour cap in ~2.5 min** (the classic
"No mower found ×5 → Reached maximum requests" sequence). Mitigations, all
shipped:
- Match the mower by **model prefix** (`is_mower()`), not category alone, so
  setup succeeds across versions and never enters the retry loop.
- Setup costs **one** `home_data` call — seed the coordinator from the discovery
  `device_status` (`async_set_updated_data`) instead of a second poll in
  `async_config_entry_first_refresh`.
- The coordinator polls only **hourly** (24/day) and treats `RoborockRateLimit`
  as "keep the last push-fed state"; commands never trigger a REST refresh
  (state arrives over the MQTT DPS push). Do not lower `UPDATE_INTERVAL`.

**Open / future work**
- Full map grid (`GET_FULL_MAP`) still returns no decodable `map` on the cloud
  path; zone ids come from the preference config instead (sufficient for
  `MOW_SELECT`). See [PROTOCOL.md](PROTOCOL.md) §7.
- Cutting-height bounds are a 20–70 fallback (real `HeightMotorParameter` needs
  a `GET_HEIGHT_MOTOR_PARAMETER` query); DP 134 not reported on this device.
- Larger `remote_pb` surface (schedules, go-to, map edit, clear-task) unused.

---

## 10. References

- python-roborock: <https://github.com/Python-roborock/python-roborock>
  - `roborock/devices/device_manager.py` — protocol selection by `pv`.
  - `roborock/devices/transport/mqtt_channel.py` — topics, publish/subscribe.
  - `roborock/protocols/v1_protocol.py` — V1 payload encode/decode.
  - `roborock/data/containers.py` — `HomeDataDevice` (`device_status`), `HomeDataProduct` (`schema`), `HomeDataScene`.
  - `roborock/roborock_message.py` — `RoborockMessageProtocol`, `RoborockDataProtocol` (vacuum DPS enum).
- Home Assistant lawn mower entity: <https://developers.home-assistant.io/docs/core/entity/lawn-mower>
- HACS publishing: <https://hacs.xyz/docs/publish/integration>
