# Roborock Mower Protocol Reference (reverse-engineered)

This is the verified control/status protocol for the Roborock **mower** family
(`roborock.mower.a222` / RockNeo, RockMow), reverse-engineered from the
decompiled Roborock app bundles (`com.roborock.mower` and `no_package/RockNeo Q1`)
and cross-checked against a live RockNeo Q105. Everything below was read from
literal protobuf schema strings in the app (`module_879.js` schema / `module_877.js`
call sites for `com.roborock.mower`; `module_1046.js` / `module_1086.js` / `module_1037.js`
for RockNeo — the two bundles are identical for every fact here).

> **Key fact:** the mower is a Tuya-style device for *status* (data points, DPS)
> but is **commanded almost entirely through one RPC method: `remote_pb`**. The
> official app never writes command DPS. `remote_pb` carries a
> `rock.common.remote.RemoteMsg` protobuf, sent over the cloud as its protobufjs
> `toJSON()` form — **string enum names**, `id` as a decimal string.

---

## 1. The `remote_pb` command mechanism

The app builds a `RemoteMsg`, calls `msg.toJSON()` (enum fields become their
**names**, 64-bit `id` becomes a string), and sends it as a **bare JSON object**:

```
callMethod('remote_pb', { id: "1699...", type: "APP_BUTTON", app_button: "MOW_GLOBAL" })
```

`python-roborock`'s V1 `send_command("remote_pb", params=<dict>)` produces the
exact same wire form: the dict is placed verbatim under `dps.101` as
`{"id":<reqId>,"method":"remote_pb","params":<dict>,...}`. So the integration
sends string enum names as JSON — **app-faithful, no protobuf/base64 needed**.
(A separate BLE path base64-encodes `RemoteMsg.encode()`; not used for cloud.)

A success reply is `"ok"` (python-roborock maps it to `{}`). Query replies
(map, preferences) come back as **base64 protobuf** (`RobotMsg`) — see §7.

### `RemoteMsg` fields (subset in use)

| # | field | type | used for |
|---|-------|------|----------|
| 1 | `id` | uint64 (string in JSON) | request id (ms timestamp) |
| 2 | `type` | enum `RemoteMsg.Type` | the command discriminator |
| 5 | `app_button` | enum `AppButton.Type` | button commands (`type=APP_BUTTON`) |
| 8 | `modify_map` | message `Map` | boundaries for edge/select mow, map name for queries |
| 13 | `remote_cmd` | message `RemoteCmd` | cutting height (`type=REMOTE_CMD`) |
| 14 | `mow_preference` | message `MowPreference` | efficiency mode etc. (`type=SET_MOW_PREFERENCE`) |

### `RemoteMsg.Type` (the command verbs used here)

`APP_BUTTON=6`, `REMOTE_CMD=17`, `SET_MOW_PREFERENCE=20`,
`GET_MOW_PREFERENCE_CONFIG=26`, `GET_MAP_NAMES=11`, `GET_FULL_MAP=2`,
`GET_ROBOT_STATUS=3`, `REMOTE_CONTROL=5` (joystick). (Numbers are FYI — the
wire carries the **name**.)

---

## 2. Controls — `AppButton.Type` (sent as `app_button`, `type=APP_BUTTON`)

The app's mow flow uses the **task-scoped** `MOW_*` values (not the generic
`START/PAUSE/STOP/RESUME`):

| app_button | # | action | integration method |
|-----------|---|--------|---------------------|
| `MOW_GLOBAL` | 14 | start full-lawn mow | `start()` |
| `MOW_EDGE` | 15 | start edge / perimeter cut | `edge_cut()` |
| `MOW_SELECT` | 16 | start select-area (zone) mow (needs `modify_map.boundaries`) | `start_area_mow()` |
| `MOW_PAUSE` | 20 | pause the running mow | `pause()` |
| `MOW_RESUME` | 22 | resume a paused mow | `resume()` |
| `MOW_END` | 24 | stop / end the mow task | `stop()` |
| `CHARGE` | 5 | return to dock & charge | `dock()` |
| `DOCK_END` | 25 | **cancel** docking (not dock!) | — |

Confirmed call sites: `startGlobalMowing/startEdgeMowing/startAreaMowing`,
`pauseMowing/resumeMowing/stopMowing`, `backToCharger`, `stopDocking`.

> There is **no DPS-write command path** in the app. Legacy DPS 201–205
> (`start/dock/pause/resume/stop`) may be honoured by firmware but are not what
> the app uses; the integration drives everything through `remote_pb`.

---

## 3. Cutting height — `RemoteCmd` (`type=REMOTE_CMD`)

```json
{"type":"REMOTE_CMD","remote_cmd":{"type":"MAIN_CUTTER_HEIGHT","main_cutter_height":40}}
```

`RemoteCmd.Type.MAIN_CUTTER_HEIGHT=3`; `main_cutter_height` is a **uint32 in raw
millimetres, no scaling** (the app sends the slider value straight through). The
valid range is device-reported (`HeightMotorParameter{max,min,step}` via
`GET_HEIGHT_MOTOR_PARAMETER=34`), which is not in the DPS snapshot, so the
integration uses a 20–70 mm / step-1 fallback.

---

## 4. Efficiency mode — `MowPreference` (`type=SET_MOW_PREFERENCE`)

Efficiency mode is `MowPreference.effective`, **not** a command DPS:

| `MowPreference.Effective` | # |
|---------------------------|---|
| `DAILY` | 1 |
| `EFFICIENT` | 2 |
| `MANICURE` | 3 |

(`0=UNKNOWN`.) DP 133 reports the same 1/2/3 value. The integration reads the
current value from DP 133 and writes via a read-modify-write of the global
preference:

```
GET_MOW_PREFERENCE_CONFIG -> mow_preference_config.global (a MowPreference)
   set .effective = <1|2|3>
SET_MOW_PREFERENCE with that MowPreference
```

`MowPreference` also carries `mow_times`, `direction`/`direction_type`,
`height`, `keep_edge`, `area_id`, `mode` — not yet surfaced as entities.

---

## 5. Status data points (DPS) — read

DPS ids are `rock.iot.DpKey.Type`. Live values arrive in `home_data`
`device_status` and via MQTT push. The integration maps:

| DP | code | notes |
|----|------|-------|
| 120 | error_code | 0 = none |
| 121 | battery | % (real) |
| 122 | mow_type | see §6 |
| 123 | mow_state | **`RobotDetailState`**, see §6 |
| 124/125 | mapping_type / mapping_state | |
| 126 | ota_state | |
| 127 | charge_state | `ChargeStateDpValue` |
| 128 | dock_state | `DockStateDpValue` (0 idle,1 moving,2 docking,3 suspended) |
| 129 | charge_type | reason it docked, `ChargeTypeDpValue` |
| 130 | pend_type | pause reason, `PendTypeDpValue` |
| 131 | remote_state | |
| 132 | mow_start_type | app/button/schedule |
| 133 | mow_eff_mode | efficiency mode (see §4) |
| 134 | mow_height | mm |
| 135 | mow_direction_angle | |
| 136 | mow_patten | |
| 138 | offline_status | |
| 139 | mow_progress | % (real) |
| 140 | blade_lifespan | % |
| 142 | gps_coordinate | base64 protobuf |
| 143 | off_dock_no_task_status | 3 = returning to dock |
| 144/145 | afs_status / network_channel | |

> Do **not** use `python-roborock`'s vacuum DPS enum (`RoborockDataProtocol`) —
> it mis-maps the mower (e.g. treats 121 as STATE) and drops ids > 135. Parse
> the raw `payload["dps"]` (see `parse_dps_push`).

---

## 6. State enums

### `mow_state` (DP 123) = `rock.fsm.RobotDetailState.Type`

The value the device actually reports (matches observed 0/51/56/57/58). Coarse
activity mapping used by the lawn-mower entity:

- **Idle/Docked** (`DOCKED`): 0, 61 (rain), 62 (do-not-disturb), 63 (low-battery),
  68/104 (manual), 76/151 (charging), 77/152 (charge complete), 153 (waiting),
  101/105/106 (free/dock-end/plan-end)
- **Mowing** (`MOWING`): 51 init, 52 undocking, 53 locating, 54 adjust-cutter,
  **55 zig-zag (main)**, 56 edge, 57 goto, 64 wait, 65/66 remote, 70 remote-undock
- **Paused** (`PAUSED`): 58 suspend, 67/75/107/17 emergency-stop
- **Returning** (`RETURNING`): 71 to-dock init, 72 to-dock locating
- **Error** (`ERROR`): 15/16 map-fault, 59/60 fault, 69 dock-fault, 73/74
  to-dock-fault, 108/109 free-fault, 154 charge-fault

Full label table lives in `mower_api.ROBOT_DETAIL_STATE_LABELS`.

### Other DP value enums

- `mow_type` (122): 0 none, **1 full**, **2 edge**, 3 selection, 4 zoning, 5 remote, 6 random
- `charge_state` (127): 0 not-charging, 1 charging, 2 complete, 3 waiting, 4 low-power, 5 error
- `charge_type` (129): 0 unknown, 1 manual, 2 rain, 3 do-not-disturb, 4 standby-timeout, 5 low-battery, 6 task-finished, 7 mow-fault
- `pend_type` (130): 0 none, 1 app-pause, 2 emergency-stop, 3 fault, 4 breakpoint, 5 other

---

## 7. Zone / area mowing & the map (experimental)

Select-area mow (`MOW_SELECT`) needs a non-empty list of saved-map boundaries:

```json
{"type":"APP_BUTTON","app_button":"MOW_SELECT",
 "modify_map":{"boundaries":[{"id":<id>,"name":"<name>"}]}}
```

The **send** path is high-confidence (same JSON transport as start/edge).

**Query responses are JSON, not base64.** Live capture (RockNeo Q105) shows the
mower answers `GET_*` queries with a **JSON string** in the RPC `result` (e.g.
`GET_MOW_PREFERENCE_CONFIG` → `{"type":"MOW_PREFERENCE_CONFIG","preference_config":{...}}`).
`python-roborock` only passes `ok`/dict/list/int through and raises
`Unexpected API Result: <json>` for a string, so `MowerApi._query()` recovers the
JSON from that exception. (It was never base64 protobuf on the cloud path.)

**Reliable zone discovery — the mowing preference config.**
`GET_MOW_PREFERENCE_CONFIG` returns `preference_config.custom[]`, one entry per
saved zone with `area_id` + `area_name` (live: `A1`=2, `A2`=3, `A3`=4). This is
what `MowerApi.get_areas()` uses, and it is the reliable source of zone ids.
`GET_MAP_NAMES` / `GET_FULL_MAP` are also parsed when present.

Zone-mow flow: `get_areas()` → pick id(s) → `MOW_SELECT` with
`modify_map.boundaries:[{id,name}]` (the `mow_areas` service).

**Open question (needs a live select-mow):** whether the firmware expects the
`area_id` from the preference config, a top-level `Boundary.id`, or a nested
`SubBoundary` (zone) id in `modify_map.boundaries[].id`. Whole-lawn edge cut
(bare `MOW_EDGE`, no boundaries) is confirmed working on the a222.

---

## 8. Live-verification status (RockNeo Q105, fw 02.68.36)

**Confirmed live:**
- Status decode: `mow_state=55`→mowing, `mow_type=1`→full_mow, `mow_eff_mode=1`→Daily,
  activity=mowing — all correct against a mid-mow snapshot.
- `GET_MOW_PREFERENCE_CONFIG` returns JSON; `_query()` recovers it; `get_areas()`
  enumerates the saved zones (A1/A2/A3).

**Still needs a live action to confirm:**
- pause/resume/dock/stop over `remote_pb` (transport proven by start/edge; these
  specific buttons not yet actioned live).
- `SET_MOW_PREFERENCE` efficiency-mode *write* taking effect.
- Which id `MOW_SELECT` wants in `modify_map.boundaries[].id` (§7).
- DP 134 read unit parity with the `main_cutter_height` write unit.

---

## 9. Full status — `GET_ROBOT_STATUS` (read)

Answered as JSON (`{"type":"ROBOT_STATUS", ...}`, recovered like every query).
Live on a RockNeo Q105 (fw 02.72.44); the fields the entities use
(`sensor.py` / `binary_sensor.py`, helpers in `robot_status.py`):

| path | example | entity |
|------|---------|--------|
| `mow_progress.mow_all_area` | `79.12` (m²) | lawn area |
| `mow_progress.expected_time` | `2464.0` (s) | estimated mowing time |
| `last_mow_abstract.start.time` / `.end.time` | `"1791533671"` (unix s, string) | last mow start / end |
| `last_mow_abstract.seconds` / `.area` / `.percentage` | `43` / `1.28` / `3.21` | last mow duration / area / coverage |
| `last_mow_abstract.end.type` | `APP_END` (also for a dock command from HA) | last mow end reason |
| `last_mow_abstract.abnormal_end` | `false` | last mow aborted |
| `next_plan.start` / `.end` / `.days[].type` / `.mode` | template date (only the time of day counts), `FRIDAY`, `GLOBAL` | next scheduled mow |
| `robot_status_event[-1]` | `MOW_TASK_FINISH` | last event |
| `network.rssi` / `.wifi_band` | `-62` / `2.4G` | Wi-Fi signal / band |
| `wireless_devices.route` / `.wifi.state` / `.wifi.level` / `.mobile_4g.state` | `WLAN0` / `CONNECTED` / `GOOD` / `CONNECTED` | connection route, Wi-Fi, 4G |
| `rtk.position_type` (= `wireless_devices.rtk_position`) | `FIXED_SOLUTION` | RTK positioning |
| `rtk.nrtk.rtk_mode` / `.dock_nrtk_status` | `BASE_RTK` / `DOCK_NRTK_DISABLE` | RTK mode, network RTK |
| `fsm_rtk_state`, `lora_status`, `fsm_anti_theft_state`, `fsm_energy_state`, `runtime_state`, `fsm_ota_state`, `fsm_dock_state`, `fsm_map_state` | `ACTIVE`, `PAIRED`, `CLOSED` (= off), `SLEEP`, `NORMAL`, `IDLE` | state sensors |
| `robot_task.working_state`, `navigation.type`, `slam.type`, `hardware.mcu_state` | `IDLE`, `ERROR`, `INIT`, `MCU_LOW_POWER` | state sensors |
| `navigation.ai_obs_cmd.generic_obs_avoidance` / `.class_obs_avoidance` | `true` | obstacle avoidance / object recognition |
| `hardware.cutter_info.has_edge_cutter`, `hardware.safety_lock_status`, `navigation.map_editing` | `true`, `false`, `false` | binary sensors |
| `map_abstracts[0].name` / `.file_change_time` | `APP_MAP1.bin` / `2026-10-09-08-15-21` (UTC) | map, map changed |

Private and never shown: `navigation.robot_gps` (latitude/longitude),
`network.mac/ip/ssid/bssid`, `bluetooth` (MAC + a name derived from it) –
`robot_status.redact_private` removes them before anything is stored, shown or
exported. Not in this answer (still to find): blade/consumable wear, total
mowing statistics, rain and wildlife protection settings – the
`scan_queries` action tries likely query names for them.

`GET_MOW_PREFERENCE_CONFIG` → `preference_config.global`: `mow_times`
(passes), `direction` (°), `direction_type` (`AUTO_DEFLECTION`),
`rotation_angle` (° per mow), `boundary_perception` (`INTELLIGENCE`),
`keep_edge` (1/0), `effective` (efficiency mode).
