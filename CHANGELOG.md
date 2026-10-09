# Changelog

Source of the release notes: each version's section becomes the text of its
[GitHub release](https://github.com/Matze89x/ha-roborock-mower/releases).

## 0.1.2 – Roborock logo, cleaner start, discovery tools

**Kurzfassung (Deutsch):** Das Gerät zeigt jetzt das Roborock-Logo (wie die
offizielle Integration). Beim Losfahren steht nicht mehr kurz „Leerlauf“, sondern
gleich „Mäht“. Der Mähstatus „idle“ heißt jetzt „Keine Aufgabe“ statt „Bereit“,
weil der Mäher ihn auch während der Rückfahrt meldet. Neu: Die Diagnose fragt den
Mäher beim Herunterladen live ab (z. B. `GET_ROBOT_STATUS`), und die Aktion
`roborock_mower.query` stellt eigene Lese-Abfragen. Damit suchen wir Wartungswerte
wie die Messer-Lebensdauer, die der Mäher nicht von sich aus sendet.

### Fixes

- **Brief "Idle" when the mower set off:** leaving the dock reports "not
  charging" a few seconds before the task code (live: 10:13:43 → 10:13:49). After
  a start from Home Assistant, or for 60 s after leaving the dock, the mower now
  reads **mowing** until the task code arrives.
- Data point 143 = **104** was seen live while driving back; it now counts as
  returning (also for returns started from the app).
- The mow state `idle` is shown as **"No task" / "Keine Aufgabe"** (the mower
  also reports it while driving back); `free` as "Free" / "Frei".

### New

- **Roborock logo and icon** for the device and integration pages, shipped in
  the integration (`brand/`, Home Assistant 2026 loads it locally).
- **Diagnostics probe the mower live** when downloaded (`GET_ROBOT_STATUS`,
  `GET_MOW_PREFERENCE_CONFIG`, `GET_HEIGHT_MOTOR_PARAMETER`, `GET_MAP_NAMES`),
  position-like fields redacted, plus a count of every message type received.
- **Action `roborock_mower.query`**: sends a read-only `GET_*` query and returns
  the raw answer, to find data the integration does not decode yet (e.g. blade
  and module wear shown in the app's maintenance page). Anything not starting
  with `GET_` is refused, so it can never move the mower.
- The first message of each kind the mower sends is logged at debug level
  (type and size), so a debug log shows what arrives without hundreds of MB.

## 0.1.1 – fixes from the first live test (RockNeo Q105)

**Kurzfassung (Deutsch):** Statuswerte erscheinen jetzt übersetzt (Deutsch/Englisch)
statt als Rohtexte wie `charge_completed`. Die Rückfahrt zur Station wird nicht mehr
als „Angedockt“ angezeigt, ein im Garten stehender Mäher als „Untätig“. Der
Kantenschnitt schickt – wie die App – die gespeicherten Zonen mit. Pro Zone gibt es
eine Taste „Zone mähen: …“, sie ersetzt die Auswahl „Mähzone“. Die Diagnose-Datei
enthält einen Verlauf aller Befehle und Statusänderungen, und die eingebaute
Library schreibt nicht mehr hunderte MB ins Debug-Log.

### Fixes

- **Returning to the dock showed "Docked".** The activity now also uses
  `charge_state` (DP 127): an idle mower that is not charging is not on the dock.
  After "return to dock" from Home Assistant it reads **Returning** until it
  arrives; a mower stopped in the garden reads **Idle** (new in HA's lawn mower).
  Docking-reason states (e.g. "docked: mowing finished") off the dock, and a
  return reason (DP 129) while not charging, also read as returning.
- **Edge cut did nothing.** Like the app (which makes you pick the area first),
  the edge cut now sends the saved areas (`modify_map.boundaries`); if the mower
  rejects that, it falls back to the bare command.
- **Raw state texts** (`not_charging`, `charge_completed`, `mowing_edge`, …):
  Mow State, Mow Mode, Charge State, Dock Reason and Pause Reason are enum
  sensors with German and English translations, like the official Roborock
  integration. Unknown codes read as unknown and are logged once.
- **Diagnostic sensors stuck on "Unknown":** the mower only reports some data
  points after they change. Error code, dock reason and pause reason now show
  their "nothing" value (0 / none / not paused) until then. The dock reason
  value 0 was named `unknown`, which Home Assistant always displays as
  "Unknown"; it is now `none`.
- **Huge debug logs (200+ MB):** the bundled python-roborock logged every raw
  message (local pings every 10 s, map/path frames while mowing, Wi-Fi details).
  Its logger now stays at INFO unless configured explicitly
  (`custom_components.roborock_mower.vendor: debug`).
- **"Mow Area" select always read "unknown":** replaced by one **"Mow zone:
  <area>"** button per saved area. The old select is removed automatically.

### New

- Diagnostics contain a **history** of the last 300 commands, the mower's answers
  and every data-point change (GPS excluded) with local timestamps, plus the
  derived activity and the saved areas – enough to follow a test run without a
  debug log.
- Efficiency mode options are translated (Daily/Täglich, Efficient/Effizient,
  Manicure/Feinschnitt). Their state values are now lowercase (`daily`, …);
  update automations that compared against `Daily`.
- GitHub releases (`.github/workflows/release.yml`): as soon as a new version
  reaches `main`, the tag and the release are created automatically with this
  version's notes; HACS then offers the versions.

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
