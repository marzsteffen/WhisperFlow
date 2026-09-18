# Installation, Aktualisierung und Deinstallation

WhisperFlow wird aus einem lokalen Checkout installiert. Der grafische
Installer richtet eine eigene Python-Umgebung, die geprüfte `whisper.cpp`-Engine,
das gewählte Whisper-Modell und auf Wunsch den Autostart ein.

## Voraussetzungen

- Windows 10/11 oder Linux mit Wayland und PipeWire
- 64-Bit-x86-Prozessor (`AMD64` beziehungsweise `x86_64`)
- Internetzugang während der Erstinstallation und beim Herunterladen eines
  anderen Modells
- genügend freier Speicher für Python-Umgebung, Engine und Modell; je nach
  Modell sollte mit ungefähr 1 bis 3 GB gerechnet werden

Andere CPU-Architekturen, macOS, X11 und Linux ohne PipeWire werden vom
Ein-Klick-Installer derzeit nicht unterstützt.
## Windows

1. Repository herunterladen oder klonen.
2. Im Projektordner `install.ps1` mit PowerShell ausführen:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

3. Im geöffneten Installer Modell und Autostart wählen.
4. Nach Abschluss im Einstellungsfenster das Mikrofon wählen.

Fehlt Python 3.11 oder neuer, installiert das Startskript Python 3.13 für den
aktuellen Benutzer. Der Download stammt von `python.org`; vor der Ausführung
wird die Windows-Signatur auf die Python Software Foundation geprüft.

Installierte Pfade:

| Inhalt | Pfad |
|---|---|
| App, Engine, Modelle und Python-Umgebung | `%LOCALAPPDATA%\WhisperFlow` |
| Konfiguration | `%APPDATA%\WhisperFlow\config.json` |
| Startmenü | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\WhisperFlow.cmd` |
| Autostart, falls gewählt | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WhisperFlow.cmd` |

## Linux

1. Repository herunterladen oder klonen.
2. Im Projektordner ausführen:

   ```bash
   bash ./install.sh
   ```

3. Die grafische Systemabfrage bestätigen, wenn Pakete fehlen.
4. Falls der Installer neue Eingaberechte eingerichtet hat, vollständig ab- und
   wieder anmelden. Danach WhisperFlow starten und das Mikrofon wählen.

Automatisch unterstützt werden Debian/Ubuntu (`apt`), Fedora (`dnf`) und
Arch-basierte Systeme (`pacman`). Installiert werden bei Bedarf Python,
PipeWire-Werkzeuge, WirePlumber, `wl-clipboard`, `ydotool` und die
OpenMP-Laufzeit. Für den exklusiven Tastaturzugriff kann der aktuelle Benutzer
der Gruppe `input` hinzugefügt werden.

Installierte Pfade:

| Inhalt | Standardpfad |
|---|---|
| Python-Umgebung und Downloads | `~/.local/share/whisperflow` |
| Engine und verwaltete Modelle | `~/.local/share/local-dictation` |
| Konfiguration | `~/.config/local-dictation/config.json` |
| Kommandozeilen-Wrapper | `~/.local/bin/whisperflow` |
| Desktop-Eintrag | `~/.local/share/applications/whisperflow.desktop` |
| Benutzer-Dienst | `~/.config/systemd/user/local-dictation.service` |

`XDG_DATA_HOME` und `XDG_CONFIG_HOME` werden für App-Daten beziehungsweise
Konfiguration berücksichtigt. Der Installationsordner der Python-Umgebung
bleibt unabhängig davon unter `~/.local/share/whisperflow`.

## Automatische Modell-Empfehlung

Bevor das Sprachmodell gewählt wird, liest der Installer die Hardware aus und
schlägt ein passendes Modell vor, das bereits vorausgewählt ist. Die Auswahl
bleibt jederzeit frei änderbar.

Gelesen werden auf Windows Grafikkarte und VRAM aus der Registry
(`HardwareInformation.qwMemorySize`), der Arbeitsspeicher über
`GlobalMemoryStatusEx` sowie Prozessor und Kernanzahl; `nvidia-smi` dient als
Ergänzung für den VRAM-Wert. Auf Linux stammen die Werte aus `sysfs`
(`/sys/class/drm`) bzw. `nvidia-smi`, `/proc/meminfo` und `/proc/cpuinfo`.
Es werden keine Daten übertragen; die Auswertung läuft vollständig lokal.

Die Empfehlung orientiert sich am verfügbaren Speicher:

| Situation | Empfehlung | Backend |
|---|---|---|
| Dedizierte GPU mit mindestens 5 GB VRAM | Large v3 Turbo | Vulkan |
| Dedizierte GPU mit mindestens 3 GB VRAM | Medium | Vulkan |
| Dedizierte GPU mit mindestens 2 GB VRAM | Small | Vulkan |
| Integrierte Grafik, 8 GB RAM oder mehr | Small bzw. Base | Vulkan (gemeinsamer Speicher) |
| Ohne GPU-Beschleunigung, 16 GB RAM oder mehr | Small | CPU |
| Ohne GPU-Beschleunigung, 8 GB RAM oder mehr | Base | CPU |
| Weniger Arbeitsspeicher | Tiny | CPU |

Bei sehr wenig VRAM einer dedizierten Karte fällt die Empfehlung auf die CPU
zurück. Schlägt die Systemanalyse fehl, bleibt Small vorausgewählt. Das
gewählte Backend wird in die `config.json` geschrieben und lässt sich später
im Einstellungsfenster ändern; scheitert Vulkan dort, weicht WhisperFlow
automatisch auf die CPU aus.

## Unterbrochene Downloads

Download von Engine und Modellen laufen mit automatischer Fortsetzung: Bei
einem Verbindungsabbruch macht der Installer bis zu fünf Versuche mit
steigender Wartezeit und fragt fehlende Daten per HTTP-Range-Anfrage nach,
statt von vorn zu beginnen. Der Status zeigt die Fortsetzung an, zum Beispiel
„Verbindung unterbrochen – Download wird bei 247 MB fortgesetzt …". Bricht der
Installer ganz ab, bleibt die teilweise geladene `.part`-Datei liegen und ein
erneuter Installer-Start nimmt den Download an derselben Stelle wieder auf.
Korrupte Teildateien werden verworfen; jede abgeschlossene Datei wird vor der
Übernahme erneut per SHA-256 geprüft.

## Was der Installer prüft

- Alle mitgelieferten Download-URLs sind auf feste Versionen festgelegt.
- Engine, VAD- und Sprachmodelle werden anhand erwarteter Dateigröße und
  SHA-256-Hash geprüft.
- Bereits vorhandene, gültige Downloads werden wiederverwendet.
- Das Projekt wird in eine isolierte virtuelle Python-Umgebung installiert.
- Zum Abschluss werden Python-Paket und Engine-Datei geprüft und die App wird
  gestartet, sofern keine erneute Anmeldung nötig ist.

Die Transkription benötigt nach erfolgreicher Installation keine
Internetverbindung. Ein Modellwechsel lädt das neue Modell einmalig herunter.

## Aktualisieren oder reparieren

Aktuellen Stand des Repositorys holen und dasselbe Installationsskript erneut
ausführen. Der Installer aktualisiert die isolierte Python-Umgebung, prüft
Engine und Modelle erneut und erzeugt die Startdateien neu. Dabei wird die
Konfiguration auf die im Installer gewählte Grundkonfiguration zurückgesetzt.
Eigene Fachwörter oder Aufnahmegrenzen deshalb vorher aus der `config.json`
sichern und anschließend über das Einstellungsfenster wieder eintragen.

Vor einer Aktualisierung empfiehlt es sich, WhisperFlow über das Tray-Menü zu
beenden. Unter Linux kann der Benutzer-Dienst zusätzlich so gestoppt werden:

```bash
systemctl --user stop local-dictation.service
```

## Deinstallation

Die folgenden Befehle löschen nur die ausdrücklich genannten
WhisperFlow-Pfade. Externe, selbst ausgewählte GGML-Modelle bleiben erhalten.

### Windows

WhisperFlow zunächst über das Tray-Menü beenden. Dann in PowerShell die
expliziten App-Pfade entfernen:

```powershell
Remove-Item -LiteralPath "$env:APPDATA\WhisperFlow" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "$env:LOCALAPPDATA\WhisperFlow" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\WhisperFlow.cmd" -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\WhisperFlow.cmd" -Force -ErrorAction SilentlyContinue
```

Damit werden Konfiguration, verwaltete Modelle, lokale Diagnosedaten, die
isolierte Python-Umgebung und die Engine entfernt.

### Linux

```bash
systemctl --user disable --now local-dictation.service
~/.local/share/whisperflow/venv/bin/python -m local_dictation --purge --yes
rm -rf -- "$HOME/.local/share/whisperflow"
rm -f -- "$HOME/.local/bin/whisperflow"
rm -f -- "$HOME/.local/share/applications/whisperflow.desktop"
rm -f -- "$HOME/.config/systemd/user/local-dictation.service"
systemctl --user daemon-reload
```

Bei angepasstem `XDG_DATA_HOME` oder `XDG_CONFIG_HOME` zeigt `--purge` die
tatsächlich betroffenen Pfade vor dem Löschen an. Die Mitgliedschaft in der
Gruppe `input` wird nicht automatisch zurückgenommen, da sie auch von anderen
Programmen verwendet werden kann.
