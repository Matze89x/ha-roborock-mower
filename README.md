# Roborock Mower – Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Matze89x&repository=ha-roborock-mower&category=integration)

**Deutsch** · [English below](#english)

Home-Assistant-Integration für **Roborock-Mähroboter** (RockNeo, z. B. Q105 /
`roborock.mower.a222`) – läuft **parallel zur offiziellen Roborock-Integration**
(für Staubsauger).

> **Fork-Hinweis:** Dieses Projekt ist ein Fork von
> [christiantroldmand/Roborock-mower-support-preview-57e0e10b](https://github.com/christiantroldmand/Roborock-mower-support-preview-57e0e10b)
> und wird hier unter dem Namen **Roborock Mower** eigenständig weiterentwickelt.
> Die Versionierung beginnt neu bei **0.1.0**. Danke an das Original für das
> Reverse-Engineering des Mäher-Protokolls.

## Änderungen

Was sich in welcher Version geändert hat, steht in den
[Releases](https://github.com/Matze89x/ha-roborock-mower/releases).

## Funktionen

- **Rasenmäher-Entität** – Mähen starten (ganze Fläche), Pause, Fortsetzen,
  zurück zur Station; Status: mäht / pausiert / kehrt zurück / angedockt / untätig / Fehler
- **Kantenschnitt** (Taste) – startet den Kanten-/Randschnitt für die gespeicherten Zonen
- **Stopp** und **Rückkehr abbrechen** (Tasten)
- **Sensoren** – Akku, Mähfortschritt, Mähstatus, Mähmodus, Ladezustand, Fehlercode,
  Grund für Rückkehr, Pausengrund, Messer-Lebensdauer; Statuswerte auf Deutsch und Englisch
- **Infos aus dem Mäher** (wie bei der offiziellen Roborock-Integration) – Rasenfläche,
  geschätzte Mähdauer, nächster geplanter Mähvorgang, letztes Mähen (Beginn, Ende,
  Dauer, Fläche, Abdeckung, Endgrund, abgebrochen ja/nein); unter **Diagnose**
  WLAN-Signal, Verbindungsweg, Mobilfunk (4G), RTK-Positionsbestimmung, letztes
  Ereignis, letzter Fehler (Code und Datum aus dem Fehlerverlauf), Regenschutz und
  Wartezeit nach Regen, Nicht stören (und ob die Zeit gerade läuft),
  Diebstahlschutz, Anzahl der Mähpläne; das Gerät zeigt das genaue Modell (z. B.
  RockNeo Q105). Viele weitere Werte (WLAN-Qualität, RTK-Modus, LoRa, Diebstahlschutz,
  Energie- und Systemzustände, Karte, Mäheinstellungen wie Mährichtung und
  Durchgänge, Hinderniserkennung, Kantenschneider …) sind ebenfalls unter
  **Diagnose** vorhanden, aber standardmäßig deaktiviert. Der Mäher wird dafür
  selbst gefragt (lokal oder über MQTT, nicht über die begrenzte Cloud-Schnittstelle):
  jede Minute beim Mähen, sonst alle 30 Minuten und sofort nach einer Statusänderung.
- **Schnitthöhe** (Zahl) und **Effizienzmodus** (Auswahl: Täglich / Effizient / Feinschnitt)
- **Zone mähen: …** (eine Taste pro gespeicherter Zone) – startet das Zonenmähen
- **Routinen** aus der Roborock-App erscheinen als Tasten
- **Aktionen** `roborock_mower.mow_areas` (Zonen mähen),
  `roborock_mower.list_areas` (Zonen auflisten), `roborock_mower.query`
  (reine Lese-Abfrage an den Mäher, z. B. `GET_ROBOT_STATUS`, zum Finden neuer Werte)
  und `roborock_mower.scan_queries` (holt die Abfragenamen aus der offiziellen
  Roborock-App-Erweiterung und probiert sie nacheinander am Mäher aus)
- **Datenschutz:** GPS-Position, MAC- und IP-Adressen sowie der WLAN-Name des Mähers
  werden in Diagnose, Abfrage-Antworten und Logs geschwärzt

## Installation über HACS

1. HACS → ⋮ → **Benutzerdefinierte Repositories** →
   `https://github.com/Matze89x/ha-roborock-mower`, Typ **Integration** → Hinzufügen.
2. „Roborock Mower“ suchen → **Herunterladen**.
3. Home Assistant **neu starten**.
4. Einstellungen → Geräte & Dienste → **Integration hinzufügen** → „Roborock Mower“ →
   E-Mail + Region → Code aus der E-Mail eingeben.

Voraussetzung: Home Assistant **2026.4** oder neuer.

### Vom Original (christiantroldmand) auf diesen Fork wechseln

Die Integration heißt intern weiterhin `roborock_mower`. Deine Einrichtung,
Geräte, Entitäten und Automationen **bleiben erhalten** – nur die Dateien werden
ausgetauscht.

1. **Nicht** die Integration unter *Geräte & Dienste* löschen.
2. HACS → „Roborock Mower“ (Original) → ⋮ → **Entfernen**.
3. HACS → ⋮ → **Benutzerdefinierte Repositories** → das alte Repository
   (`christiantroldmand/...`) entfernen und
   `https://github.com/Matze89x/ha-roborock-mower` als **Integration** hinzufügen.
4. „Roborock Mower“ → **Herunterladen** → Home Assistant **neu starten**.
5. Prüfen: *Geräte & Dienste* → sowohl **Roborock** (Staubsauger) als auch
   **Roborock Mower** sind „geladen“. Hat die alte Version python-roborock
   heruntergestuft, installiert Home Assistant beim ersten Neustart die Version der
   offiziellen Integration selbst wieder; bei Bedarf einmal erneut neu starten.

### Manuell

Ordner `custom_components/roborock_mower` nach `<config>/custom_components/`
kopieren und Home Assistant neu starten.

## Fehlersuche – was du mir schicken kannst

1. **Test durchführen** (z. B. Mähen starten, Pause, Kantenschnitt, Rückkehr) und
   kurz notieren, **was der Mäher wann tatsächlich gemacht hat**.
2. Danach **Diagnose herunterladen:** Einstellungen → Geräte & Dienste → Roborock
   Mower → ⋮ → **Diagnose herunterladen**. Sie enthält einen **Verlauf** der
   letzten 300 Befehle, Antworten und Statusänderungen mit Uhrzeit, außerdem
   Versionen, Verbindungsstatus, Roh-Datenpunkte (DPS) und das Produktschema.
   Zugangsdaten, E-Mail, Seriennummer, Schlüssel und GPS-Position sind geschwärzt.
3. Nur falls nötig ein **Debug-Protokoll** über `configuration.yaml`, so ist auch
   der Start enthalten:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.roborock_mower: debug
   ```

   Die eingebaute Library bleibt dabei bewusst auf INFO, sonst entstehen
   hunderte MB. Warnungen „Unmapped mower mow_state …“ oder „Unknown … value“
   bitte immer mitschicken.

## Daten für alle

Bereinigte echte Antworten des Mähers (ohne Position und Netzwerkdaten) liegen
öffentlich unter [`docs/`](docs/) – zum freien Verwenden, z. B. für eigene
Integrationen. Wer ein anderes Modell oder eine andere Firmware hat, kann seine
Daten über das Issue-Formular **„Mäher-Daten teilen“** beisteuern.

## Karte

Die Rasen-/Zonenkarte wird (noch) nicht als Bild dargestellt: Die App lädt sie als
Datei aus dem Roborock-Cloudspeicher über ihr natives SDK, das noch nicht
nachgebaut ist. Die gespeicherten **Zonen** sind über die Tasten „Zone mähen: …“
nutzbar. Details: [PROTOCOL.md](PROTOCOL.md) §7.

## Funktionsweise (kurz)

Der Mäher ist ein Roborock-**V1**-Gerät. Den **Status** liefert er als Tuya-
Datenpunkte (DPS) per MQTT-Push (plus Cloud-Schnappschuss alle 2 Stunden als
Sicherheitsnetz); **Befehle** gehen über das `remote_pb`-RPC der App. Protokoll:
[PROTOCOL.md](PROTOCOL.md), Architektur & Entwicklung: [DEVELOPING.md](DEVELOPING.md).

## Lizenz

Bereitgestellt wie besehen für die Community. Der mitgelieferte Ordner
`custom_components/roborock_mower/vendor/` enthält Teile von
[python-roborock](https://github.com/Python-roborock/python-roborock)
(Apache-2.0, siehe `vendor/LICENSE-python-roborock`).

---

<a id="english"></a>

# English

Home Assistant integration for **Roborock robotic mowers** (RockNeo, e.g. Q105 /
`roborock.mower.a222`) that runs **side by side with the official Roborock
integration** (vacuums).

> **Fork notice:** this project is a fork of
> [christiantroldmand/Roborock-mower-support-preview-57e0e10b](https://github.com/christiantroldmand/Roborock-mower-support-preview-57e0e10b)
> and is developed further here as **Roborock Mower**. Versioning restarts at
> **0.1.0**. Thanks to the original for reverse-engineering the mower protocol.

## Changes

What changed in which version is listed in the
[releases](https://github.com/Matze89x/ha-roborock-mower/releases).

## Features

- **Lawn mower entity** – start (full lawn), pause, resume, return to dock;
  activity: mowing / paused / returning / docked / idle / error
- **Edge Cut** (for the saved areas), **Stop** and **Cancel Dock** buttons
- **Sensors** – battery, mow progress, mow state, mow mode, charge state, error
  code, dock reason, pause reason, blade lifespan; state values in English and German
- **Information from the mower** (like the official Roborock integration) – lawn
  area, estimated mowing time, next scheduled mow, last mow (start, end,
  duration, area, coverage, end reason, aborted yes/no); under **Diagnostic**
  Wi-Fi signal, connection route, mobile network (4G), RTK positioning, last
  event, last fault (code and date from the fault history), rain protection and
  wait after rain, do not disturb (and whether its time is now), anti-theft,
  number of schedules; the device shows the exact model (e.g. RockNeo Q105).
  Many more values (Wi-Fi quality, RTK mode, LoRa, anti-theft, energy and
  system states, map, mowing preferences such as direction and passes, obstacle
  avoidance, edge trimmer, …) are under **Diagnostic** too, disabled by default.
  They are asked from the mower itself (locally or via MQTT, never the
  rate-limited cloud API): every minute while mowing, otherwise every 30 minutes
  and right after a state change.
- **Mow Height** (number) and **Efficiency Mode** (select: Daily / Efficient / Manicure)
- **Mow zone: …** – one button per saved area starts a zone mow
- **Routines** from the Roborock app appear as buttons
- **Actions** `roborock_mower.mow_areas`, `roborock_mower.list_areas`,
  `roborock_mower.query` (read-only query to the mower, e.g. `GET_ROBOT_STATUS`,
  to discover new values) and `roborock_mower.scan_queries` (takes the request
  names from the official Roborock app plugin and tries them one by one, read-only)
- **Privacy:** the mower's GPS position, MAC and IP addresses and Wi-Fi name are
  redacted in diagnostics, query answers and logs

## Installation via HACS

1. HACS → ⋮ → **Custom repositories** → `https://github.com/Matze89x/ha-roborock-mower`,
   type **Integration** → Add.
2. Search "Roborock Mower" → **Download**.
3. **Restart** Home Assistant.
4. Settings → Devices & services → **Add integration** → "Roborock Mower" →
   e-mail + region → enter the code from the e-mail.

Requires Home Assistant **2026.4** or newer.

### Switching from the original (christiantroldmand)

The integration keeps its internal name `roborock_mower`, so your setup,
devices, entities and automations **are kept** – only the files are replaced.

1. Do **not** delete the integration under *Devices & services*.
2. HACS → "Roborock Mower" (original) → ⋮ → **Remove**.
3. HACS → ⋮ → **Custom repositories** → remove the old repository and add
   `https://github.com/Matze89x/ha-roborock-mower` as **Integration**.
4. "Roborock Mower" → **Download** → **restart** Home Assistant.
5. Check *Devices & services*: both **Roborock** (vacuums) and **Roborock Mower**
   are loaded. If the old version had downgraded python-roborock, Home Assistant
   reinstalls the official integration's version on the first restart; restart
   once more if needed.

### Manual

Copy `custom_components/roborock_mower` into `<config>/custom_components/` and
restart Home Assistant.

## Troubleshooting – what to send

1. **Run the test** (e.g. start, pause, edge cut, return to dock) and note **what
   the mower actually did and when**.
2. Then **download diagnostics:** Settings → Devices & services → Roborock Mower →
   ⋮ → **Download diagnostics**. It contains a **history** of the last 300
   commands, answers and state changes with timestamps, plus versions, connection
   state, raw data points and the product schema. Credentials, e-mail, serial,
   keys and GPS position are redacted.
3. Only if needed, a **debug log** via `configuration.yaml` (includes startup):

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.roborock_mower: debug
   ```

   The bundled library deliberately stays at INFO, otherwise the log grows by
   hundreds of MB. Always include "Unmapped mower mow_state …" or "Unknown …
   value" warnings.

## Data for everyone

Cleaned-up real answers of the mower (no position or network data) are public
in [`docs/`](docs/), free to use, e.g. for other integrations. Owners of other
models or firmware can contribute theirs with the **"Share mower data"** issue
form.

## Map

The lawn/zone map is not rendered yet: the app downloads it as a file from
Roborock's cloud storage via its native SDK, which has not been reverse-engineered.
Saved **zones** are available through the "Mow zone: …" buttons. See
[PROTOCOL.md](PROTOCOL.md) §7.

## How it works (short)

The mower is a Roborock **V1** device. **Status** arrives as Tuya data points
(DPS) via MQTT push (plus a cloud snapshot every 2 hours as a safety net);
**commands** use the app's `remote_pb` RPC. Protocol: [PROTOCOL.md](PROTOCOL.md);
architecture & development: [DEVELOPING.md](DEVELOPING.md).

## License

Provided as-is for community use. The bundled
`custom_components/roborock_mower/vendor/` folder contains parts of
[python-roborock](https://github.com/Python-roborock/python-roborock)
(Apache-2.0, see `vendor/LICENSE-python-roborock`).
