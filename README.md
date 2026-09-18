# Lokales Diktat

`local-dictation` ist eine vollständig lokale Push-to-talk-Diktierhilfe für KDE
Plasma unter Wayland. Solange die Anwendung bereit und die Sitzung entsperrt
ist, ist die rechte Strg-Taste ausschließlich für Diktat reserviert:

1. Rechte Strg-Taste halten und sprechen.
2. Taste loslassen.
3. Der lokal erkannte deutsche Text wird ohne Enter in das fokussierte Feld
   eingesetzt.

Optional zeigt das Overlay schon während des Sprechens eine lokale
Live-Vorschau aus den jeweils letzten zwölf Sekunden. Zusätzlich gibt es einen
experimentellen Modus, der den wachsenden Entwurf direkt in das aktive Feld
schreibt: Ändert Whisper ältere Wörter, ersetzt die App nur den eigenen
geänderten Suffix. Nach dem Loslassen bleibt eine frische Transkription der
vollständigen Aufnahme das autoritative Endergebnis.

Die Erkennung läuft mit `whisper.cpp`, dem unquantisierten
`large-v3-turbo`-Modell und Vulkan auf der Radeon 860M. Es gibt weder einen
Cloud-Dienst noch eine LLM-Nachbearbeitung.

## Installation auf Arch/CachyOS

Das Projekt enthält ein lokales Arch-Paket. Im Release-Baum liegt der geprüfte
Quell-Tarball bereits neben dem PKGBUILD:

```bash
cd /home/steffen/Projekte/local-dictation/packaging
makepkg -Ccfsi
systemctl --user daemon-reload
systemctl --user start local-dictation.service
```

Wird statt des Release-Baums ein Arbeits-Checkout paketiert, erzeugt
`scripts/build-arch-package` zuerst den Python-Quell-Tarball, aktualisiert die
echte SHA-256-Prüfsumme im PKGBUILD und prüft die Quelle. Dafür werden zusätzlich
`python-build`, `python-setuptools`, `python-wheel` und `pacman-contrib`
benötigt.

Beim ersten Start lädt die App mit sichtbarem Fortschritt zwei fest gepinnte
Dateien in `~/.local/share/local-dictation/models`: das 1,625-GB-Whisper-Modell
und das kleine Silero-VAD-Modell. Beide Dateien werden vor der atomaren
Übernahme gegen die eingebettete Größe und SHA-256-Prüfsumme geprüft. Nach
diesem einmaligen Download funktioniert die Erkennung offline.

Ein Eintrag unter `/etc/xdg/autostart` startet den Userdienst bei der nächsten
KDE-Anmeldung. Für die aktuelle Sitzung genügen die oben gezeigten
`systemctl`-Befehle. Die App aktiviert keinen ydotool-Dienst dauerhaft: Sie nutzt
zuerst einen vorhandenen benutzereigenen Socket und startet nur bei Bedarf eine
bereits installierte `ydotoold.service` oder `ydotool.service`.

Für den experimentellen direkten Live-Text wird zusätzlich `kdotool` empfohlen.
Die App verwendet dessen KWin-Fensterkennung als Schutz davor, Korrekturen in
ein inzwischen anderes Fenster zu senden. Ohne ein verlässliches Zielfenster
fällt sie für das jeweilige Diktat automatisch auf die nichtdestruktive
Overlay-Vorschau zurück.

Der angemeldete Desktopbenutzer benötigt Lesezugriff auf `/dev/input/event*`
und Schreibzugriff auf `/dev/uinput`. Auf Arch/CachyOS werden diese Geräte über
die Gruppe `input` freigegeben. Der Zugriff lässt sich ohne Geräte zu öffnen
oder zu grabben prüfen:

```bash
id -nG | tr ' ' '\n' | grep -x input
find /dev/input -maxdepth 1 -name 'event*' -readable -print -quit
test -w /dev/uinput && echo '/dev/uinput ist beschreibbar'
```

Fehlt die Gruppe, kann sie mit `sudo usermod -aG input "$USER"` ergänzt werden.
Danach ist eine vollständige Ab- und Anmeldung erforderlich: Der
systemd-Usermanager übernimmt ergänzende Gruppen beim Login; `newgrp` in einem
einzelnen Terminal aktualisiert den bereits laufenden Userdienst nicht.

## Bedienung

Ein Doppelklick auf das Tray-Symbol öffnet die Einstellungen. Das Menü bietet:

- Bereitschafts- und Backendstatus
- temporäres Aktivieren/Deaktivieren des Diktats
- Wahl eines PipeWire-Mikrofons
- Initial-Prompt/Fachwortliste
- erneutes Kopieren des letzten Ergebnisses
- warmen Benchmark und Engine-Neustart
- Beenden

In den Einstellungen lässt sich außerdem ein ausdrücklich zu bestätigender
Diagnosemodus aktivieren. Er bewahrt eine begrenzte Anzahl von Aufnahmen samt
Transkript auf, damit wiederkehrende Erkennungsfehler anhand des tatsächlich
gehörten Audios nachvollzogen und die Fachwortliste gezielt ergänzt werden kann.

Die optionale Live-Transkription lässt sich dort ebenfalls einschalten und
zwischen einer und fünf Sekunden aktualisieren. Standardmäßig bleibt sie als
sichere Vorschau im Overlay: Whisper kann beim Hören weiterer Wörter auch
ältere Teile seines Entwurfs korrigieren.

Mit „Experimentell: Live-Text direkt ins aktive Feld einfügen und revidieren“
wird stattdessen jeder vollständige Zwischenstand am Cursor eingesetzt. Die App
vergleicht ihn auf Unicode-Graphemgrenzen mit ihrem zuletzt bestätigten Entwurf,
löscht ausschließlich dessen geänderten Suffix per Rücktaste und fügt den neuen
Suffix ein. Sie sendet dabei niemals Enter. Beim Loslassen gleicht sie den
Entwurf mit der vollständigen finalen Transkription ab und legt den kompletten
Endtext zusätzlich in die Zwischenablage.

Während dieses experimentellen Modus müssen Fenster, Textfeld und Cursor bis
zur Abschlussmeldung unverändert bleiben. Ein erkannter Fokuswechsel oder eine
andere physische Tasteneingabe beendet weitere Live-Korrekturen; der Endtext
wird dann nur noch sicher kopiert. Technische Grenze: KWin kann das aktive Fenster
erkennen, aber weder das konkrete Textfeld noch eine Cursorbewegung innerhalb
desselben Fensters. Maus-, IME- und programmatische Änderungen sind ebenfalls
nicht zuverlässig sichtbar. Bei langen Diktaten wird die direkte
Live-Aktualisierung nach 60 Sekunden eingefroren; der vollständige Endtext wird
trotzdem normal transkribiert.

Rot bedeutet Aufnahme, Gelb Transkription, Grün einen gesendeten
Einfügevorgang. Unmittelbar vor dem Einfügen wird das Overlay ausgeblendet,
damit KWin das zuvor aktive Textfeld wiederherstellt. „Einfügen gesendet“ ist
bewusst präzise: Unter Wayland kann die App nicht zurücklesen, ob jede
Zielanwendung den Tastendruck tatsächlich angenommen hat.

Das Mikrofon wird niemals automatisch entstummt. Ein als stumm erkanntes Gerät
wird entsprechend markiert; stumme oder sehr kurze Aufnahmen werden verworfen.
Die maximale Aufnahmezeit beträgt fünf Minuten.

## Befehle

```bash
# Dienst starten bzw. Einstellungen öffnen
local-dictation

# Warmen lokalen Backend-/Laufzeittest ausführen
local-dictation --benchmark
local-dictation --benchmark --json

# Dienst stoppen und verwaltete Benutzerdateien nach Rückfrage löschen
local-dictation --purge
local-dictation --purge --yes
```

Der Benchmark meldet Backend, Audiolänge, Inferenzzeit, Echtzeitfaktor und ob
eine 10–15-sekündige Fixture in höchstens fünf Sekunden verarbeitet wurde.
Bei einem kalten Dienststart wartet der Aufruf auf Modellprüfung, Download und
Engine-Initialisierung und führt den Benchmark danach automatisch aus.

## Datenschutz und Sicherheit

- Während der Aufnahme liegt das rohe Audiosignal in einem begrenzten
  Arbeitsspeicherpuffer. Private WAV-Snapshots für die Live-Vorschau und die
  abschließende WAV liegen nur in `$XDG_RUNTIME_DIR/local-dictation`, einem
  privaten tmpfs-Verzeichnis, und werden nach Erfolg oder Fehler entfernt.
- Standardmäßig werden Transkripte, Prompts und HTTP-Antworten nicht
  protokolliert; das letzte Ergebnis lebt nur im Arbeitsspeicher.
- Nur nach ausdrücklichem Opt-in speichert der Diagnosemodus Audio und
  Transkription dauerhaft lokal. Anwendungs- und Journal-Logs enthalten auch
  dann keinen erkannten Text.
- `wl-copy --sensitive` versieht den Text mit dem KDE-Hinweis für sensible
  Clipboard-Daten, sodass Klipper ihn nicht in seinen Verlauf übernimmt.
- Vor dem Einfügen werden Steuer-, Zeilen- und Absatztrennzeichen durch
  Leerzeichen ersetzt. Die App sendet ausschließlich Shift+Insert und niemals
  Enter.
- `whisper-server` bindet nur an `127.0.0.1`; Port und private Route werden bei
  jedem Start zufällig gewählt. Es nimmt ausschließlich WAV-Daten der App an
  und wird nicht als Administrator ausgeführt.
- Während Sperrbildschirm und Suspend gibt der Input-Proxy sämtliche Geräte
  frei. Er liest daher keine Passworteingabe am Sperrbildschirm.
- Ein Heartbeat-Wächter gibt alle gegriffenen Geräte nach spätestens 2,5
  Sekunden frei, falls der steuernde Hauptprozess hängt oder verschwindet.
- Der Userdienst ist an `graphical-session.target` gebunden und wird beim Ende
  der KDE-Sitzung mitsamt Input-Proxy und `whisper-server` beendet.

Andere Clipboard-Manager können den KDE-Sensitive-Hinweis ignorieren; auch
Swap-Verhalten liegt außerhalb der absichtlichen App-Speicherung.

## Konfiguration

Die versionierte Datei liegt unter
`~/.config/local-dictation/config.json`. Wichtige Felder sind die stabile
PipeWire-`node.name`, beide Modellpfade, `backend` (`vulkan` oder explizit
`cpu`), der Initial-Prompt, die Aufnahmegrenzen, `live.enabled`,
`live.direct_insert`, `live.interval_ms` sowie `diagnostics.enabled` und
`diagnostics.retention_entries`. Unbekannte zukünftige Schemaversionen werden
nicht überschrieben. Das mitgelieferte Standardvokabular enthält unter anderem
`Dicio`, `Eigenpfad`, `GrapheneOS`, `Pixel 8 Pro` und `Innerkofler Straße`;
selbst angepasste Prompts werden bei einer Konfigurationsmigration nicht
überschrieben.

Im Vulkan-Modus gilt die Engine erst dann als bereit, wenn
`whisper-server` ausdrücklich ein `Vulkan… backend` bestätigt. Ein stiller
CPU-Fallback wird beendet; CPU kann anschließend bewusst im Fehlerdialog oder
in den Einstellungen gewählt werden.

## Diagnose

Der optionale Diagnosemodus legt pro Diktat ein Verzeichnis unter
`~/.local/share/local-dictation/diagnostics` an:

- `audio.wav`: die unveränderte lokale Aufnahme
- `transcript.txt`: der normalisierte Whisper-Text, bei Fehlern leer
- `metadata.json`: Status, Zeitstempel, Audio-/Inferenzdauer und Fehlerphase
- `insertion.json`: Clipboard-Ergebnis und ob Shift+Insert an `ydotool` gesendet wurde

`shortcut_sent: true` bestätigt bewusst nur, dass der lokale Eingabedienst den
Tastenakkord angenommen hat. Wayland meldet nicht zurück, ob das fokussierte
Programm den Text tatsächlich übernommen hat.

Der Ordner und die einzelnen Fälle sind nur für den Benutzer zugänglich
(`0700`), die Dateien erhalten `0600`. Standardmäßig werden höchstens 20 Fälle
behalten; die Grenze ist in den Einstellungen von 1 bis 100 wählbar und ältere
Fälle werden automatisch entfernt. Deaktivieren löscht bestehende Daten nicht.
Über „Alle Diagnosedaten vollständig löschen …“ werden sie nach einer zweiten
Bestätigung entfernt; Modelle und Einstellungen bleiben erhalten.

Für wiederkehrende Fehltranskriptionen kann man Audio und Text vergleichen und
korrekte Namen oder Fachbegriffe anschließend in die Fachwortliste aufnehmen.
Das verändert keine bereits gespeicherten Transkripte und trainiert das Modell
nicht nach, gibt Whisper aber für künftige Diktate passenden Schreibkontext.

Für technische Dienstdiagnose helfen weiterhin:

```bash
systemctl --user status local-dictation.service
journalctl --user -u local-dictation.service -b
wpctl status
vulkaninfo --summary
```

Die Logs enthalten technische Status- und Fehlermeldungen, aber keinen
erkannten Text.
Wenn keine Tastatur übernommen werden kann, prüfen Sie die Mitgliedschaft in
der Gruppe `input` und die Rechte von `/dev/input/event*` sowie `/dev/uinput`.
Die App wird erst bei `input-ready` bereit; `input-partial` bedeutet, dass
mindestens ein erkanntes Eingabegerät noch nicht sicher gegriffen werden
konnte. Das ist beim Start kurzzeitig normal, solange auf einer Tastatur noch
eine Taste gehalten wird. Dauerhafte Meldungen `input-grab-failed` im Journal
weisen meist auf fehlende Gruppenrechte oder einen konkurrierenden exklusiven
Grab hin.
Nach einem erzwungenen Prozessende gibt der Kernel die Grabs frei; der Dienst
startet absichtlich nicht automatisch neu.

## Tests

```bash
pytest -m 'not integration and not live'
LOCAL_DICTATION_RUN_INTEGRATION=1 pytest -m integration
systemd-analyze --user verify assets/local-dictation.service
desktop-file-validate assets/local-dictation.desktop assets/local-dictation-autostart.desktop
```

Der Integrationstest verwendet standardmäßig die beiden validierten Modelle
unter `~/.local/share/local-dictation/models`, fordert Vulkan ausdrücklich an
und prüft das Fünf-Sekunden-Ziel. Abweichende Pfade können mit
`LOCAL_DICTATION_MODEL_PATH` und `LOCAL_DICTATION_VAD_MODEL_PATH` gesetzt
werden. Die mitgelieferten deutschen Fixtures sind 16-kHz-Mono-S16-WAVs; ihre
Texte, Längen und Prüfsummen stehen in
`src/local_dictation/resources/fixtures.json`. Mit lokal
installiertem `espeak-ng` und `ffmpeg` lassen sie sich über
`scripts/generate-audio-fixtures` reproduzieren.

Die Live-Abnahme erfolgt in Konsole, Firefox, Kate und einer Electron-App. Für
direkten Live-Text werden dort außerdem Erweiterung, rückwirkende Korrektur,
Unicode, Fokuswechsel und Cursorbewegung geprüft. Zusätzlich werden externe
Tastatur, Gerätewechsel, Sperre/Suspend, fehlgeschlagenes ydotool, beschädigtes
Modell, erzwungener App-Abbruch und ein Start ohne externes Netzwerk geprüft.

## Deinstallation

Modelle, Benutzerkonfiguration und gegebenenfalls gespeicherte Diagnosedaten
gehören dem Benutzer und bleiben bei einer Paketentfernung erhalten:

```bash
systemctl --user stop local-dictation.service
sudo pacman -Rns local-dictation
```

Für eine vollständige Bereinigung zuerst `local-dictation --purge` ausführen
und danach das Paket entfernen. Ein Modellpfad außerhalb des von der App
verwalteten Datenverzeichnisses wird vom Purge-Befehl niemals gelöscht.
