# Roborock Mower - Home Assistant Integration

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=christiantroldmand&repository=Roborock-mower-support-preview&category=integration)

Custom Home Assistant integration for **Roborock mower** devices (RockNeo / `roborock.mower.a222`).

The official Roborock integration does not yet support mower devices. This integration fills that gap using the same `python-roborock` library for authentication and device communication.

---

## Features

- **Lawn Mower entity** -- start (full-lawn mow), pause, resume, and return-to-dock, with live activity (mowing / paused / docked / error)
- **Edge Cut button** -- start a perimeter / edge cut
- **Battery sensor** -- current battery percentage
- **Mow Progress sensor** -- completion percentage of the current session
- **Mow Mode sensor** -- full mow vs edge cut
- **Mow State / Charge State / Error Code sensors** -- raw status values from the device
- **Routine buttons** -- any Roborock routines/scenes you create for the mower appear as buttons
- **Mow Height** and **Efficiency Mode** controls (experimental)

## Mowing

Start a **full-lawn mow** from the lawn mower entity's *Start* action, and an **edge cut**
from the **Edge Cut** button. Both are sent using the same `remote_pb` protobuf command the
official app uses (reverse-engineered). Pause, resume, and return-to-dock also work from
Home Assistant. **Area / zone mowing** (a specific saved zone) still needs to be started
from the Roborock app, since it requires selecting saved map boundaries -- and any
**routines** you create in the app appear here as buttons.

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

## Coexistence with official Roborock integration

This integration can run alongside the official Roborock integration without conflict. The official integration skips mower devices (unsupported), and this integration only picks up mower devices.

You will need to enter your credentials separately for each integration.

## How it works

The Roborock mower has no official Home Assistant support and no public API, so this
integration was built by reverse-engineering the device. It is a Roborock **V1** device
that exposes status and control through Tuya **data points (DPS)** rather than the RPC
commands used by vacuums. Status is read from the device's data points (with live MQTT
push and a periodic cloud snapshot). Pause / resume / dock are sent as DPS writes, while
start and edge cut use the app's `remote_pb` protobuf RPC (`RemoteMsg` with an
`APP_BUTTON` action). Unknown state codes are logged as warnings so they can be reported
and mapped in future updates.

## Development

For the architecture, the reverse-engineered **Tuya DPS** protocol (full data-point
map, state codes, command mechanism), the integration's data flow, the probe tool, and
how to set up a dev environment, see **[DEVELOPING.md](DEVELOPING.md)**.

## License

This project is provided as-is for community use.
