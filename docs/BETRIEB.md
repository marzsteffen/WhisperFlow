# Betrieb, Datenschutz und Fehlerbehebung

## Bedienung

WhisperFlow läuft im Infobereich der Taskleiste beziehungsweise im System-Tray.
Zum Diktieren die **rechte Strg-Taste** gedrückt halten, sprechen und die Taste
loslassen. Nach der lokalen Erkennung wird der Text an der aktuellen
Cursorposition eingefügt.

Das Tray-Menü bietet Einstellungen, Aktivieren/Deaktivieren, Kopieren des
letzten Ergebnisses und Beenden. Im Einstellungsfenster lassen sich Mikrofon,
Modell, Fachwörter, Aufnahmegrenzen, Live-Vorschau und lokale Diagnose ändern.

## Modelle und Backend

Die integrierten Modelle Tiny, Base, Small, Medium und Large v3 Turbo werden bei
der Auswahl automatisch in den verwalteten Modellordner geladen und geprüft.
Small ist der ausgewogene Standard. Tiny und Base sind auf langsameren CPUs
schneller; Medium und Large v3 Turbo benötigen mehr Speicher und Rechenzeit.

Eine eigene Datei muss ein mit der eingesetzten `whisper.cpp`-Version
kompatibles GGML-Whisper-Modell (`.bin`) sein. Benutzerdateien außerhalb des
verwalteten Modellordners werden weder überschrieben noch beim Purge gelöscht.

Der mitgelieferte Engine-Build nutzt die CPU. Ein eigener Vulkan-fähiger
`whisper-server` kann von erfahrenen Nutzern verwendet werden; Treiber und
Kompatibilität liegen dann außerhalb der geprüften Standardinstallation.

## Datenschutz

- Aufnahme und Transkript bleiben lokal.
- Temporäre Aufnahmen werden nach dem Einfügen gelöscht.
- Der lokale `whisper-server` bindet ausschließlich an `127.0.0.1` und erhält
  pro Start einen zufälligen privaten URL-Pfad.
- Das letzte Ergebnis bleibt nur im Arbeitsspeicher, damit es über das
  Tray-Menü erneut kopiert werden kann.
- Erst nach ausdrücklicher Bestätigung speichert der Diagnosemodus eine
  begrenzte Anzahl lokaler Audio-/Textfälle. Speicherort und Löschfunktion sind
  im Einstellungsfenster sichtbar.

Bei direkter Live-Einfügung verändert WhisperFlow bereits während des
Sprechens das aktive Textfeld. Diese Option ist standardmäßig ausgeschaltet;
die reine Live-Vorschau ist die sicherere Wahl für Editoren mit komplexer
Eingabelogik.

## Nützliche Befehle

Linux:

```bash
whisperflow
whisperflow --benchmark
whisperflow --benchmark --json
systemctl --user status local-dictation.service
journalctl --user -u local-dictation.service -b
```

Windows PowerShell:

```powershell
& "$env:LOCALAPPDATA\WhisperFlow\venv\Scripts\python.exe" -m local_dictation
& "$env:LOCALAPPDATA\WhisperFlow\venv\Scripts\python.exe" -m local_dictation --benchmark
```

Der Benchmark verwendet die installierte lokale Engine und gibt Audiozeit,
Inferenzzeit und Echtzeitfaktor aus.

## Fehlerbehebung

### Die rechte Strg-Taste reagiert unter Linux nicht

Nach der ersten Installation vollständig ab- und wieder anmelden. Prüfen, ob
der Benutzer Mitglied der Gruppe `input` ist und ob der Dienst läuft:

```bash
id -nG
systemctl --user status local-dictation.service
```

Bei einer gesperrten Sitzung oder Suspend gibt WhisperFlow Eingabegeräte
absichtlich frei und verbindet sie anschließend neu.

### Kein Mikrofon wird angezeigt

- Windows: In den Datenschutzeinstellungen den Mikrofonzugriff für
  Desktop-Apps erlauben und WhisperFlow neu starten.
- Linux: Mit `wpctl status` oder `pw-dump` prüfen, ob PipeWire das Gerät sieht.
  Danach im Einstellungsfenster erneut das Mikrofon wählen.

### Text wird nur kopiert, aber nicht eingefügt

Der erkannte Text bleibt in der Zwischenablage, wenn das Zielprogramm keine
synthetische Eingabe annimmt. Das Ziel-Fenster fokussieren und einfügen. Unter
Linux zusätzlich prüfen, ob `ydotool` und `wl-copy` verfügbar sind.

### Modell oder Engine startet nicht

Das Installationsskript erneut ausführen. Beschädigte oder unvollständige
Downloads werden nicht übernommen. Bei einem eigenen GGML-Modell testweise auf
eines der integrierten Modelle und das CPU-Backend zurückwechseln.

### Das Tray-Symbol fehlt unter Windows

Windows blendet neue Symbole teilweise hinter dem Pfeil „Ausgeblendete Symbole"
ein. Dort WhisperFlow dauerhaft in den sichtbaren Bereich ziehen. Wenn keine
Instanz läuft, „WhisperFlow“ aus dem Startmenü öffnen.

### Linux-Dienst neu starten

```bash
systemctl --user restart local-dictation.service
journalctl --user -u local-dictation.service -b -n 100
```

## Datenpfade

| Daten | Windows | Linux (Standard) |
|---|---|---|
| Konfiguration | `%APPDATA%\WhisperFlow\config.json` | `~/.config/local-dictation/config.json` |
| Modelle und Engine | `%LOCALAPPDATA%\WhisperFlow` | `~/.local/share/local-dictation` |
| Diagnosedaten | `%LOCALAPPDATA%\WhisperFlow\diagnostics` | `~/.local/share/local-dictation/diagnostics` |

Unter Linux können Konfigurations- und Datenpfade über `XDG_CONFIG_HOME` und
`XDG_DATA_HOME` abweichen.
