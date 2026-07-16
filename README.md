# Roborock Mower - Home Assistant Integration

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=christiantroldmand&repository=Roborock-mower-support-preview&category=integration)

Custom Home Assistant integration for **Roborock mower** devices (RockNeo / `roborock.mower.a222`).

The official Roborock integration does not yet support mower devices. This integration fills that gap using the same `python-roborock` library for authentication and device communication.

---

## Features

- **Lawn Mower entity** -- start (full-lawn mow), pause, resume, and return-to-dock, with live activity (mowing / paused / returning / docked / error)
- **Edge Cut button** -- start a perimeter / edge cut
- **Stop button** -- end the current mow task
- **Cancel Dock button** -- abort an in-progress return-to-dock
- **Battery sensor** -- current battery percentage
- **Mow Progress sensor** -- completion percentage of the current session
- **Mow State sensor** -- decoded activity (mowing / edge / paused / returning / charging / fault / ...)
- **Mow Mode sensor** -- full mow / edge / selection / zoning / remote / random
- **Charge State / Dock Reason / Pause Reason / Error Code / Blade Lifespan sensors** -- decoded status (some diagnostic / disabled by default)
- **Routine buttons** -- any Roborock routines/scenes you create for the mower appear as buttons
- **Mow Height** control (sets cutting height via the `remote_pb` command)
- **Efficiency Mode** selector (Daily / Efficient / Manicure)
- **Mow Area** selector -- pick a saved zone (auto-discovered) to start a select-area mow
- **Zone mowing service** (`roborock_mower.mow_areas`) -- start a select-area mow by boundary id

## Mowing

Start a **full-lawn mow** from the lawn mower entity's *Start* action, an **edge cut**
from the **Edge Cut** button, and **pause / resume / return-to-dock / stop** from the
entity and the Stop button. Everything is sent through the same `remote_pb` protobuf RPC
the official app uses (reverse-engineered) -- see [PROTOCOL.md](PROTOCOL.md).

**Area / zone mowing:** call the **`roborock_mower.mow_areas`** service with the ids of the
saved boundaries you want to mow (target your mower device). Discovering those ids from
Home Assistant is experimental (the mower returns its map as base64 protobuf that the
`python-roborock` cloud path may not decode); the `roborock_mower.list_areas` service
tries, but the reliable source of ids is the Roborock app. Any **routines** you create in
the app also appear here as buttons and are a convenient way to run saved zone mows.

## Requirements

- Home Assistant 2024.1.0 or later
- [HACS](https://hacs.xyz/) installed
- A Roborock account with a mower device (e.g. RockNeo)

## Installation

### Via HACS (recommended)

1. Click the button above, or in HACS go to **Integrations** > **3-dot menu** > **Custom repositories**
2. Add `https://github.com/christiantroldmand/Roborock-mower-support-preview` as an **Integration**
3. Search for "Roborock Mower" and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/roborock_mower` folder into your Home Assistant `custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings** > **Devices & Services** > **Add Integration**
2. Search for "Roborock Mower"
3. Enter your Roborock account email and select your region
4. Enter the verification code sent to your email
5. The integration will discover your mower device(s) automatically

## Map

The **lawn / zone map** (the outline and cutting zones as an image) is **not**
rendered, and it turns out it can't be easily: the map is stored as a *file*
(`APP_MAP1.bin`) in Roborock's cloud **file store (FDS)** and fetched by the
app's **native SDK** — `GET_FULL_MAP` over the API only returns an `ok`
acknowledgement, the bytes come out-of-band. That download (URL, signing) lives
in the app's native layer, isn't in the reverse-engineered material, and has no
equivalent in `python-roborock`. Once those bytes are obtained the rest is easy
— it's a plain protobuf **vector** map (boundary polygons + charger/robot
points, no encryption/compression) — but obtaining them needs a separate effort
(capturing the app's HTTPS traffic, or the APK's native code). Meanwhile the
saved **zones** (A1/A2/A3 …) are available via the Mow Area selector. See
[PROTOCOL.md](PROTOCOL.md) §7.

## Coexistence with official Roborock integration

This integration can run alongside the official Roborock integration without conflict. The official integration skips mower devices (unsupported), and this integration only picks up mower devices.

You will need to enter your credentials separately for each integration.

## How it works

The Roborock mower has no official Home Assistant support and no public API, so this
integration was built by reverse-engineering the device. It is a Roborock **V1** device
whose **status** is exposed through Tuya **data points (DPS)** rather than the RPC
commands used by vacuums (read with live MQTT push + a periodic cloud snapshot).
**Commands**, however, all use the app's **`remote_pb`** protobuf RPC — a `RemoteMsg`
sent as JSON with string enum names: start / edge / select-area via `app_button`,
cutting height via `remote_cmd`, efficiency mode via `mow_preference`. Unknown state
codes are logged as warnings so they can be reported and mapped in future updates. The
full reverse-engineered protocol is documented in [PROTOCOL.md](PROTOCOL.md).

## Development

For the architecture, the reverse-engineered **Tuya DPS** protocol (full data-point
map, state codes, command mechanism), the integration's data flow, the probe tool, and
how to set up a dev environment, see **[DEVELOPING.md](DEVELOPING.md)**.

## License

This project is provided as-is for community use.
