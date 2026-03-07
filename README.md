# Roborock Mower - Home Assistant Integration

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=christiantroldmand&repository=Roborock-mower-support-preview&category=integration)

Custom Home Assistant integration for **Roborock mower** devices (RockNeo / `roborock.mower.a222`).

The official Roborock integration does not yet support mower devices. This integration fills that gap using the same `python-roborock` library for authentication and device communication.

---

## Features

- **Lawn Mower entity** -- start, pause, dock with proper state mapping (mowing, paused, docked, error)
- **Battery sensor** -- current battery percentage
- **Mow Progress sensor** -- completion percentage of current mowing session
- **Mow State / Charge State / Error Code sensors** -- raw status values from the device
- **Mow Height control** -- adjustable cutting height (number slider, mm)
- **Efficiency Mode selector** -- Standard / Efficient / Quiet

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

## State Mapping

The mower state values are mapped to Home Assistant lawn mower states on a best-effort basis. Unknown state codes are logged as warnings so they can be reported and added in future updates.

## License

This project is provided as-is for community use.
