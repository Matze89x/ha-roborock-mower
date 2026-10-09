# Changelog

## 0.1.0 – first release of the **Roborock Mower** fork

Fork of [christiantroldmand/Roborock-mower-support-preview-57e0e10b](https://github.com/christiantroldmand/Roborock-mower-support-preview-57e0e10b)
(last upstream version 0.4.3). Versioning restarts at 0.1.0.

### Coexistence with the official Roborock integration

- **Bundled python-roborock 7.12.1** (latest release) in
  `custom_components/roborock_mower/vendor/` with all imports made relative, so it
  never loads or replaces the `roborock` package of the official integration.
  `python-roborock` is no longer a requirement. Root cause of the restart errors:
  the old `python-roborock>=5.12.0,<6.0.0` pin conflicted with the official
  integration's `==7.12.0` (Home Assistant 2026.9+), so Home Assistant reinstalled
  a different version on every start and the official integration failed with
  `ImportError`s.
- Remaining requirements (`aiomqtt`, `construct`, `paho-mqtt`, `pycryptodome`,
  `pyrate-limiter`) only have lower bounds, so Home Assistant's own versions
  always satisfy them.
- `script/vendor_roborock.py` regenerates the bundled copy for a given version.

### Startup / restart robustness

- Persistent cache (`.storage/roborock_mower.<entry_id>`) of the device list and
  the mower's network info. If the cloud fails or is rate-limited at startup, the
  integration starts from the cache and fetches a fresh snapshot 5 minutes later.
- One retry after 2 s when the per-second `home_data` limit is hit.
- Snapshots younger than 30 minutes are reused (setup retries, several mowers).
- MQTT errors during setup now raise `ConfigEntryNotReady` (automatic retry)
  instead of failing permanently, and the MQTT session is closed (no leaked
  reconnect loop).
- Fixed "Unable to remove unknown job listener" logged when Home Assistant
  stopped while the entry was set up.
- Routines (cloud) and saved areas (mower) are discovered in background tasks;
  platform setup never blocks. Areas are retried after 2, 10 and 30 minutes.
- No endless retry loop when the account lists no mower (`ConfigEntryError` with
  a clear log message) – the loop used to burn the shared `home_data` budget.

### Fixes

- `services.yaml` was invalid (device filter on `target`), so Home Assistant
  could not load the action descriptions; now a `device_id` field with a device
  selector.
- A failed safety-net cloud poll marked every entity unavailable until the next
  poll; the last (MQTT-fed) state is now kept.
- Commands the mower rejects (`["fail"]`) raised no error; they now raise a
  readable Home Assistant error. Other command failures are also surfaced as
  readable errors.
- "Unmapped mower mow_state" is logged once per code instead of on every state
  write.
- Safety-net poll every 2 hours instead of hourly (12 instead of 24 of the
  account's 40 daily `home_data` calls).

### New

- Re-authentication flow when the Roborock login expires or the MQTT broker
  rejects it.
- Diagnostics download (versions, connection, raw DPS, product schema; secrets,
  e-mail, serial, keys and GPS redacted).
- German translation.
- DPS 137 (`mow_conf_mode`) and 141 (`fc_state`) decoded (named in
  python-roborock 7.12.1).
- Device info shows product name, model id and serial number.
- Tests run in a real Home Assistant 2026.10 next to the official Roborock
  integration (57 tests); GitHub workflow runs hassfest, HACS validation and the
  tests.
