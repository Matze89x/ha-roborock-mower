# Changelog

Source of the release notes: each version's section becomes the text of its
[GitHub release](https://github.com/Matze89x/ha-roborock-mower/releases).

## 0.3.2 – Map through the right channel, live values while mowing

**Kurzfassung (Deutsch):** Die Aufzeichnung von 0.3.1 hat gezeigt, warum keine
Karte kam: Über die lokale Verbindung antwortet der Mäher auf Kartenanfragen
nur mit „ok“. Die Karte schickt er verschlüsselt über die Cloud an den, der
dort gefragt hat. Genau so holt die Bibliothek auch die Karten der
Roborock-Sauger. „Kartendaten speichern“ fragt jetzt auf diesem Weg und
liefert die entschlüsselte Karte als Base64 mit.

Neu sind außerdem **Verbleibende Mähzeit** (geschätzt), **Messerdrehzahl**
und **Fahrgeschwindigkeit** während des Mähens. Eine Warnung von Home
Assistant 2026.10 (veraltete Geräte-Suche) ist behoben.

### New

- `MowerApi.get_map_data()`: `GET_FULL_MAP`, `GET_MAP_MOW_SNAPSHOT` and
  `GET_MAP_DIFFS` go through the bundled library's map channel (MQTT with
  the security endpoint and nonce); the protocol-301 answer comes back
  decrypted and unpacked. `save_map_data` uses it with the saved map's name
  and returns each map as base64 (and saves `<query>_map.bin`).
- Sensors: remaining mowing time (expected time of the running task × what
  is left, 0 without a task); diagnostic: blade speed (rpm) and driving
  speed (km/h), 0 when standing.
- Translation for `mow_global` (working / system state while mowing).
- Capture of the full status while mowing in `docs/`.

### Fixes

- Use `async_get_device_by_identifier` (Home Assistant 2026.10) instead of
  the deprecated `async_get_device`, which logged a warning at startup.

## 0.3.1 – Map data to copy

**Kurzfassung (Deutsch):** „Kartendaten speichern“ liefert die
aufgezeichneten Daten jetzt direkt in der Antwort der Aktion: JSON lesbar,
Binäres als Base64. Sie stehen auch in der Diagnose. Man muss die Dateien
also nicht mehr aus dem Konfigurationsordner holen, was mit dem File Editor
nicht ging. Private Felder in lesbaren Nachrichten werden geschwärzt.

### Changes

- `save_map_data` answers with every recorded message: JSON with the RPC
  answer nested in data point 102 unpacked and private fields redacted,
  anything else as base64 (up to 512 KB each), plus the query answers. The
  last recording is part of the diagnostics (`map_capture`). The files are
  still written.

## 0.3.0 – Settings to switch, fault history, schedules, stop

**Kurzfassung (Deutsch):** Einstellungen lassen sich jetzt auch ändern, nicht
nur ablesen:
- **Kantenschnitt beim Mähen** als Schalter
- **Mährichtung (Winkel)** in 5°-Schritten
- **Mäheffizienz** heißt wie in der App: Standard / Effizient, und „Feinschnitt“
  bleibt als dritte Option, die die App beim Q105 nicht anbietet

Geschrieben wird immer die ganze Mäheinstellung. Kann sie nicht gelesen
werden, wird nichts geändert, damit nichts anderes zurückgesetzt wird. Die
Rasenmäher-Entität kann jetzt auch **Stopp** (Home Assistant 2026.10).

Zwei neue Werkzeuge:
- **App-Texte suchen:** findet z. B. die genauen Namen der Richtungsmodi
  „Optimal“ und „Benutzerdefiniert“.
- **Kartendaten speichern:** zeichnet die Kartendaten des Mähers als Dateien
  auf, um das Kartenformat zu entschlüsseln.

Mit den Abfragenamen aus der Roborock-App hat der Mäher 37 Abfragen
beantwortet. Daraus kommen jetzt:
- **letzter Fehler:** Code und Datum aus dem Fehlerverlauf
- **Regenschutz** und **Wartezeit nach Regen**
- **Nicht stören** mit Zeitfenster, dazu ein Sensor, ob die Nicht-stören-Zeit
  gerade läuft
- **Diebstahlschutz** (an/aus)
- **Anzahl der Mähpläne**

Viele Details sind ebenfalls dabei, standardmäßig deaktiviert: Regenstatus,
Diebstahlschutz-Radius, Navigationsmodus, Kamera-Optionen, gespeicherte Fehler,
Ortung, empfohlene und maximale Fläche, Messerscheibe, Akkukapazität. Das Gerät
zeigt jetzt das genaue Modell (z. B. „RockNeo Q105“ statt „RockNeo Q1“). Die
Suche nach Abfragenamen liest die App jetzt exakt aus. Bisher waren dabei
manche Namen mit ihren Nachbarn verklebt (z. B. `GET_FEATURESET_NEW_PIN_CODE`).
SIM- und Modemkennungen sowie der RTK-Positionswert werden zusätzlich
geschwärzt.

### New

- **Controls** (configuration entities, written with `SET_MOW_PREFERENCE`):
  switch "Edge cut while mowing" (`keep_edge`), number "Mowing direction
  (angle)" (`direction`, 0–180° in 5° steps). The mowing efficiency select is
  named like in the app ("Standard", "Efficient") and keeps "Manicure (not in
  the app)". After a change the settings are read again.
- **Lawn mower "stop"** (`lawn_mower.stop`, Home Assistant 2026.10+): ends
  the task where the mower is.
- **Action `app_strings`:** returns the texts of the official app plugin
  that contain given words (e.g. `DEFLECTION`), to find exact setting values.
- **Action `save_map_data`:** asks the mower for its map, records every
  message it sends for a given time and saves them in
  `<config>/roborock_mower/map_<time>/` – the first step towards showing the
  map.
- Settings from `GET_USER_MODE_CONFIG`, fault history from
  `GET_FAULT_RECORDS`, schedules per zone from `GET_ZONES_PLAN_INFO`, read
  together with the mowing preferences (every 30 minutes, after a settings
  change or a new error); product details from `GET_FEATURE_INFO` once.
- Sensors: mowing schedules (count, zones as attribute); diagnostic: last
  fault (code; date, task and the whole history as attributes), last fault
  date, wait after rain; disabled: rain status, anti-theft radius, navigation
  mode, stored faults, positioning, rated / maximum lawn area, blade disc
  diameter, battery capacity.
- Binary sensors (diagnostic): rain protection, do not disturb (start / end
  as attributes), do-not-disturb time now, anti-theft enabled; disabled:
  obstacle detection on paths, camera avoidance at the edge / in passages,
  obstacle photo privacy.
- The device model is the exact one from the mower (`sku_info.market_name`).
- `scan_queries`: names come from the Hermes string table of the app plugin
  (exact, also for overlapping strings), with the old split as fallback;
  answers that are just `["ok"]` are listed under `acknowledged`.
- Diagnostics probe the new queries too. More captures in `docs/`.

### Changes

- Preference writes (efficiency, cutting height, edge cut, direction) always
  send the whole current preference and are refused when it can't be read;
  before, a failed read sent a partial one, which could reset other settings.
- The read-only "keep edge" binary sensor and "mowing direction" sensor are
  replaced by the switch and number and removed.

### Fixes

- Redact SIM / modem identifiers (`iccid`, `imei`, `imsi`, `eid`, `msisdn`),
  `ip_addr`, serial numbers and `random_gngga_config` in every answer.

## 0.2.1 – Real query names, hardware errors, open data

**Kurzfassung (Deutsch):** Die Aktion „Mäher-Abfragen durchsuchen“ rät nicht
mehr: Sie lädt – wie die Roborock-App – mit deinem eigenen Konto die offizielle
App-Erweiterung für deinen Mäher, liest daraus alle Abfragenamen (`GET_*`) und
fragt den Mäher genau diese. So finden wir Zeitpläne, Verschleißteile und
Statistik. Neu ist der Diagnose-Sensor **Hardware-Fehler**: Er zeigt Fehler der
Steuerung an (z. B. `MAIN_CUTTER_DRIVER_IC_FAULT`, Störung des Mähmotor-Treibers),
sonst „Keiner“. Sensoren für Datenpunkte, die dein Modell laut Roborock gar nicht
hat (beim Q105: Messer-Lebensdauer, Pausengrund), werden nicht mehr angelegt und
alte, immer leere Einträge entfernt. Im Ruhezustand wird der Mäher nur noch alle
30 statt 10 Minuten gefragt (beim Start und Ende einer Fahrt weiterhin sofort).
Bereinigte echte Mäher-Antworten liegen jetzt öffentlich unter `docs/`, und es
gibt Issue-Formulare zum Teilen von Daten und Melden von Fehlern.

### New

- **`scan_queries` reads the request names from the official app plugin**
  (option "Names from the Roborock app", on by default): it downloads the
  product plugin and the mower category plugin with the user's own account,
  collects every `GET_*` name and asks the mower each one, one at a time. The
  answer now lists `answered`, `rejected` (the mower's `["fail"]`) and
  `failed` separately, says where the names came from (`source`) and what went
  wrong while reading the plugin (`app_errors`, without links). Without the
  plugin the built-in list is used. Long strings in answers are shortened.
- **Diagnostic sensor "Hardware error"** from `hardware.mcu_error.errors`
  (first error as state, all of them as the `errors` attribute; "None" when
  the controller reports none).
- **Open data:** cleaned-up captures of a RockNeo Q105 (firmware 02.72.44) in
  `docs/captures/`: `GET_ROBOT_STATUS`, `GET_ROBOT_INFO`,
  `GET_MOW_PREFERENCE_CONFIG`, `GET_MAP_NAMES`, `GET_HEIGHT_MOTOR_PARAMETER`, the
  product schema with translated data point names, and the query scan. Issue
  forms "Share mower data" and "Report a bug".
- Translations for the states `run` (controller) and `prepare` (energy).

### Changes

- Data point sensors (and the cutting height) are only created when the
  model's product schema lists the data point; entities of earlier versions
  that this model can never fill are removed (Q105: blade lifespan, pause
  reason).
- The full status is read every 30 minutes while idle (was 10); every minute
  during a run and right after state changes as before.

## 0.2.0 – Information from the mower

**Kurzfassung (Deutsch):** Die Integration fragt den Mäher jetzt selbst nach
seinem vollständigen Status – wie die offizielle Roborock-Integration bei den
Saugern. Neu sind Rasenfläche, geschätzte Mähdauer, nächster geplanter
Mähvorgang und das letzte Mähen (Beginn, Ende, Dauer, Fläche, Abdeckung). Unter
**Diagnose** stehen WLAN-Signal, Verbindungsweg, Mobilfunk (4G),
RTK-Positionsbestimmung, Endgrund und letztes Ereignis. Viele weitere Werte
(WLAN-Qualität, RTK-Modus, LoRa, Diebstahlschutz, Energie- und Systemzustände,
Karte, Mäheinstellungen, Hinderniserkennung, Kantenschneider …) sind ebenfalls
unter Diagnose vorhanden, aber standardmäßig deaktiviert. Abgefragt wird nur der
Mäher (lokal oder über MQTT, nie über die begrenzte Cloud-Schnittstelle): jede
Minute beim Mähen, sonst alle 10 Minuten und sofort nach einer Statusänderung.
Alle Texte auf Deutsch und Englisch. Außerdem werden GPS-Position, MAC- und
IP-Adressen und der WLAN-Name jetzt überall geschwärzt.

### New

- **Status from the mower:** the coordinator asks the mower for
  `GET_ROBOT_STATUS` (every minute while a task runs, every 10 minutes
  otherwise, and 5 s after a pushed state change) and for its mowing
  preferences (every 30 minutes or after a settings change). Over the local
  connection when there is one, else MQTT – the rate-limited cloud API is not
  used. These routine polls stay out of the diagnostics history.
- **Sensors:** lawn area, estimated mowing time, next scheduled mow (with
  end, weekdays and mode as attributes), last mow start / end / duration /
  area / coverage.
- **Diagnostic sensors (enabled):** last mow end reason, last event, Wi-Fi
  signal (dBm), connection route, mobile network (4G), RTK positioning;
  binary sensor "last mow aborted".
- **Diagnostic entities (disabled by default):** Wi-Fi quality / connection /
  band, RTK mode and state, network RTK at the station, LoRa connection,
  anti-theft, energy / system / firmware-update / working / docking /
  mapping / navigation / localization / controller states, map name, map
  changed, status read, mowing passes, mowing direction, direction mode,
  direction change per mow, boundary detection; binary sensors for local
  connection, obstacle avoidance, object recognition (AI), edge trimmer
  fitted, safety lock, map being edited and mowing along the edge.
- State values whose full list is unknown are shown as reported (e.g. a new
  event name) instead of breaking the sensor; known values are translated
  (English and German).
- **Action `roborock_mower.scan_queries`:** tries a list of read-only `GET_*`
  queries one after another and returns which ones the mower answers – to
  find blade wear, statistics and similar values the app shows.

### Fixes

- **Privacy:** the mower's network identifiers (Wi-Fi and Bluetooth MAC, IP
  address, Wi-Fi name, BSSID) were not redacted in the diagnostics probes and
  the `query` answer; they are now, together with the GPS position. The RTK
  `position_type` is no longer redacted by mistake.
- Query answers are no longer written to the debug log or the history (only
  their size), since they contain the position.
- The diagnostics' message counts show protocol names (`general_request`,
  `rpc_response`, ...) instead of numbers.

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
