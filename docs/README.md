# Daten & Protokoll · Data & protocol

**Deutsch** – Hier liegen echte, bereinigte Antworten von Roborock-Mährobotern, damit
jeder sie nutzen kann: für eigene Integrationen, Skripte oder zum Vergleichen mit
dem eigenen Mäher. GPS-Position, MAC- und IP-Adressen, WLAN-Name, Bluetooth-Kennung,
Seriennummern und Geräte-IDs sind entfernt (`**REDACTED**`). Wie die Felder
zusammenhängen und was die Integration daraus macht, steht in
[PROTOCOL.md](../PROTOCOL.md).

**English** – Real, cleaned-up answers from Roborock robot mowers, free for anyone
to use: for other integrations, scripts or to compare with your own mower. GPS
position, MAC and IP addresses, Wi-Fi name, Bluetooth id, serial numbers and
device ids are removed (`**REDACTED**`). [PROTOCOL.md](../PROTOCOL.md) explains the
fields and how the integration uses them.

## Inhalt · Contents

| Ordner · folder | Mäher · mower | Firmware |
|---|---|---|
| [`captures/rockneo-q105_fw-02.72.44`](captures/rockneo-q105_fw-02.72.44) | RockNeo Q105 (`roborock.mower.a222`) with edge trimmer, RTK station | 02.72.44 |

Each folder holds:

| Datei · file | Inhalt · content |
|---|---|
| `GET_ROBOT_STATUS.json` | full status: last mow, next schedule, lawn area, Wi-Fi/4G/RTK/LoRa, state machines |
| `GET_ROBOT_INFO.json` | the same structure without network details; here with a controller error (`hardware.mcu_error`) |
| `GET_MOW_PREFERENCE_CONFIG.json` | mowing preferences (global + per zone): passes, direction, efficiency, edge |
| `GET_USER_MODE_CONFIG.json` | settings: rain protection and delay, do-not-disturb time, anti-theft, navigation options |
| `GET_FAULT_RECORDS.json` | fault history: error code, count, dates and task |
| `GET_ZONES_PLAN_INFO.json` | schedule ids per zone |
| `GET_FEATURE_INFO.json`, `GET_SKU_INFO.json`, `GET_MCU_VERSION.json` | model, rated / maximum area, battery, blade disc, positioning, controller firmware |
| `GET_MAP_NAMES.json`, `GET_HEIGHT_MOTOR_PARAMETER.json` | saved maps; cutting-height motor (empty on the Q105) |
| other `GET_*.json` | everything else the mower answered (dock pairing, eSIM, self checks, …) |
| `data_points.json` | the product schema (data points 101–206, Chinese names translated) and their values when docked |
| `query_scan.json` | which `GET_*` request names the mower accepts and which it rejects |

How the requests are sent: a `RemoteMsg` as JSON through the `remote_pb` RPC, e.g.
`{"id": "<ms timestamp>", "type": "GET_ROBOT_STATUS"}`; the mower answers with a
JSON string. Details in [PROTOCOL.md](../PROTOCOL.md).

## Eigene Daten beisteuern · Contribute your mower's data

Andere Modelle (RockMow Z1/S1, RockNeo Q1/Q110, …) oder andere Firmware helfen sehr.
Other models or firmware versions help a lot:

1. Install the integration, then run **Developer tools → Actions →
   `roborock_mower.scan_queries`** with your mower. It takes the request names
   from the official Roborock app plugin and asks the mower each one (read-only).
   The answer is already cleaned of position and network data.
2. Download the **diagnostics** (Settings → Devices & services → Roborock Mower →
   ⋮ → Download diagnostics). They are cleaned the same way.
3. Open an issue with the form **"Mäher-Daten teilen / Share mower data"** and
   paste both. Please glance over them once before posting.

Die Daten hier dürfen frei verwendet werden; ein Hinweis auf dieses Repository freut uns.
The data here is free to use; a mention of this repository is appreciated.
