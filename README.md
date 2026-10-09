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

## Neu in 0.1.0 (Kurzfassung)

- **Läuft parallel zur offiziellen Roborock-Integration.** Die Integration bringt
  ihre **eigene Kopie von python-roborock 7.12.1** (neueste Version) mit und
  verlangt kein `python-roborock` mehr von Home Assistant. Vorher erzwang sie
  `python-roborock <6.0`, die offizielle Integration aber 7.x – HA hat bei jedem
  Neustart hin- und herinstalliert und die offizielle Integration brach mit
  `ImportError` ab. Das ist behoben.
- **Robuster Neustart:** Gerätedaten werden zwischengespeichert. Ist die Cloud
  beim Start nicht erreichbar oder das Roborock-Abruflimit erreicht, startet die
  Integration aus dem Cache statt mit Fehler. MQTT-Probleme direkt nach dem Booten
  führen zu einem automatischen Neuversuch (statt dauerhaftem Fehler) ohne
  hängende Verbindungen.
- **Weitere Fehler behoben:** „Unable to remove unknown job listener“ beim
  Neustart; ungültige `services.yaml`; Entitäten wurden nach einem
  fehlgeschlagenen Cloud-Abruf für eine Stunde „nicht verfügbar“; vom Mäher
  abgelehnte Befehle wurden still ignoriert (jetzt Fehlermeldung); Endlos-Neuversuche,
  wenn kein Mäher gefunden wird; Log-Spam bei unbekannten Statuscodes.
- **Neu:** Diagnose-Download, Re-Authentifizierung bei abgelaufener Anmeldung,
  deutsche Übersetzung, Routinen/Zonen werden im Hintergrund geladen (blockieren
  den Start nicht), automatische Tests + GitHub-Prüfung (hassfest, HACS).

Alle Details: [CHANGELOG.md](CHANGELOG.md).

## Funktionen

- **Rasenmäher-Entität** – Mähen starten (ganze Fläche), Pause, Fortsetzen,
  zurück zur Station; Status: mäht / pausiert / kehrt zurück / an der Station / Fehler
- **Kantenschnitt** (Taste) – startet den Kanten-/Randschnitt
- **Stopp** und **Rückkehr abbrechen** (Tasten)
- **Sensoren** – Akku, Mähfortschritt, Mähstatus, Mähmodus, Ladezustand, Fehlercode,
  Grund für Rückkehr, Pausengrund, Messer-Lebensdauer (teils Diagnose / standardmäßig aus)
- **Schnitthöhe** (Zahl) und **Effizienzmodus** (Auswahl: Daily / Efficient / Manicure)
- **Mähzone** (Auswahl) – gespeicherte Zone wählen = Zonenmähen starten
- **Routinen** aus der Roborock-App erscheinen als Tasten
- **Aktionen** `roborock_mower.mow_areas` (Zonen mähen) und
  `roborock_mower.list_areas` (Zonen auflisten)

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

1. **Diagnose herunterladen:** Einstellungen → Geräte & Dienste → Roborock Mower →
   ⋮ → **Diagnose herunterladen**. Enthält Versionen, Verbindungsstatus,
   Roh-Datenpunkte (DPS) und das Produktschema. Zugangsdaten, E-Mail,
   Seriennummer, Schlüssel und GPS-Position sind geschwärzt.
2. **Debug-Protokoll:** dort ⋮ → **Debug-Protokollierung aktivieren**, das
   Problem nachstellen (z. B. Mähen starten, Kantenschnitt, Rückkehr), dann
   deaktivieren – das Log wird heruntergeladen. Alternativ in `configuration.yaml`:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.roborock_mower: debug
   ```
3. Dazu kurz notieren, **was der Mäher wann tatsächlich gemacht hat** (mäht,
   Kante, pausiert, fährt zurück, Fehler) – damit lassen sich die Statuscodes
   zuordnen. Warnungen „Unmapped mower mow_state …“ bitte immer mitschicken.

## Karte

Die Rasen-/Zonenkarte wird (noch) nicht als Bild dargestellt: Die App lädt sie als
Datei aus dem Roborock-Cloudspeicher über ihr natives SDK, das noch nicht
nachgebaut ist. Die gespeicherten **Zonen** sind über die Auswahl „Mähzone“
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

## What's new in 0.1.0 (summary)

- **Runs alongside the official Roborock integration.** The integration bundles
  its **own copy of python-roborock 7.12.1** (latest) and no longer requires
  `python-roborock` from Home Assistant. It used to pin `python-roborock <6.0`
  while the official integration pins 7.x, so HA swapped versions on every
  restart and the official integration failed with `ImportError`s. Fixed.
- **Robust restarts:** device data is cached. If the cloud is unreachable or the
  Roborock request limit is reached at startup, the integration starts from the
  cache instead of failing. MQTT problems right after boot trigger an automatic
  retry (instead of a permanent error) without leaking connections.
- **More fixes:** "Unable to remove unknown job listener" on restart; invalid
  `services.yaml`; entities turning "unavailable" for an hour after one failed
  cloud poll; commands rejected by the mower were silently ignored (now an
  error); endless retries when no mower is found; log spam for unknown states.
- **New:** diagnostics download, re-authentication, German translation, routines
  and areas load in the background (never block startup), automated tests and
  GitHub validation (hassfest, HACS).

Full details: [CHANGELOG.md](CHANGELOG.md).

## Features

- **Lawn mower entity** – start (full lawn), pause, resume, return to dock;
  activity: mowing / paused / returning / docked / error
- **Edge Cut**, **Stop** and **Cancel Dock** buttons
- **Sensors** – battery, mow progress, mow state, mow mode, charge state, error
  code, dock reason, pause reason, blade lifespan (some diagnostic / disabled by default)
- **Mow Height** (number) and **Efficiency Mode** (select: Daily / Efficient / Manicure)
- **Mow Area** select – picking a saved area starts a zone mow
- **Routines** from the Roborock app appear as buttons
- **Actions** `roborock_mower.mow_areas` and `roborock_mower.list_areas`

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

1. **Download diagnostics:** Settings → Devices & services → Roborock Mower → ⋮ →
   **Download diagnostics** (versions, connection state, raw data points, product
   schema; credentials, e-mail, serial, keys and GPS position are redacted).
2. **Debug log:** ⋮ → **Enable debug logging**, reproduce the issue, disable it –
   the log downloads. Or in `configuration.yaml`:

   ```yaml
   logger:
     default: warning
     logs:
       custom_components.roborock_mower: debug
   ```
3. Note **what the mower actually did and when** so state codes can be mapped;
   always include "Unmapped mower mow_state …" warnings.

## Map

The lawn/zone map is not rendered yet: the app downloads it as a file from
Roborock's cloud storage via its native SDK, which has not been reverse-engineered.
Saved **zones** are available through the Mow Area select. See
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
