#!/usr/bin/env python3
"""One-window installer for WhisperFlow on Windows and Linux.

The UI is served by the Python standard library, so the same interface is
available before PyQt and the application dependencies have been installed.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import venv
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)
WHISPER_TAG = "b5130"
MODEL_REVISION = "98aa99a0a9db05ae2342309f5096248665f7cba3"
VAD_REVISION = "9ffd54a1e1ee413ddf265af9913beaf518d1639b"
MODELS = {
    "tiny": ("ggml-tiny.bin", 77_691_713, "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21"),
    "base": ("ggml-base.bin", 147_951_465, "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"),
    "small": ("ggml-small.bin", 487_601_967, "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"),
    "medium": ("ggml-medium.bin", 1_533_763_059, "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208"),
    "large-v3-turbo": ("ggml-large-v3-turbo.bin", 1_624_555_275, "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"),
}
VAD = ("ggml-silero-v6.2.0.bin", 885_098, "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987")
ENGINE = {
    "Windows": (
        "whisper-bin-x64.zip",
        8_573_270,
        "f9ec6c52a2e949b62ab51fa21d0d497958f9e41c3010c157c4e42932d5316f3c",
    ),
    "Linux": (
        "whisper-bin-ubuntu-x64.tar.gz",
        9_793_438,
        "53e7fd8b5764edad916b8848dd0af6abb1ff1d3b86c899e79c78652412536c32",
    ),
}


STATE: dict[str, Any] = {
    "phase": "ready",
    "step": 0,
    "progress": 0,
    "title": "Bereit zur Installation",
    "detail": "Wähle dein Sprachmodell und starte die Installation.",
    "log": [],
    "done": False,
    "error": "",
    "needs_relogin": False,
}
STATE_LOCK = threading.Lock()
INSTALL_THREAD: threading.Thread | None = None


def update(**values: Any) -> None:
    with STATE_LOCK:
        STATE.update(values)
        detail = values.get("detail")
        if detail and (not STATE["log"] or STATE["log"][-1] != detail):
            STATE["log"] = [*STATE["log"][-9:], detail]


def paths() -> dict[str, Path]:
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        data = local / "WhisperFlow"
        config = roaming / "WhisperFlow"
        install_root = data
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "local-dictation"
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "local-dictation"
        install_root = Path.home() / ".local" / "share" / "whisperflow"
    return {
        "data": data,
        "config": config,
        "install": install_root,
        "venv": install_root / "venv",
        "models": data / "models",
        "bin": data / "bin",
    }


def run(command: list[str], *, timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess[str]:
    update(detail=f"Ausgeführt: {Path(command[0]).name} {' '.join(command[1:3])}")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=creationflags,
    )
    if check and result.returncode:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(message[-1] if message else f"Befehl fehlgeschlagen ({result.returncode})")
    return result


def download(url: str, destination: Path, expected_size: int, expected_hash: str, label: str, start: int, span: int) -> Path:
    if destination.is_file() and destination.stat().st_size == expected_size:
        digest = hashlib.sha256(destination.read_bytes()).hexdigest() if expected_size < 20_000_000 else file_hash(destination)
        if digest == expected_hash:
            update(progress=start + span, detail=f"{label} ist bereits geprüft.")
            return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "WhisperFlow-Installer/1"})
    digest = hashlib.sha256()
    received = 0
    update(detail=f"{label} wird heruntergeladen …", progress=start)
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
                digest.update(block)
                received += len(block)
                update(
                    progress=start + int(span * min(received, expected_size) / expected_size),
                    detail=f"{label}: {received / 1024**2:.0f} von {expected_size / 1024**2:.0f} MB",
                )
        if received != expected_size or digest.hexdigest() != expected_hash:
            raise RuntimeError(f"Integritätsprüfung für {label} fehlgeschlagen")
        os.replace(partial, destination)
        return destination
    finally:
        partial.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def install_engine(targets: dict[str, Path]) -> None:
    system = platform.system()
    machine = platform.machine().lower()
    if system not in ENGINE or machine not in {"amd64", "x86_64"}:
        raise RuntimeError(f"Noch kein geprüftes Engine-Paket für {system}/{machine}")
    name, size, sha256 = ENGINE[system]
    archive = targets["install"] / "downloads" / name
    url = f"https://github.com/ggml-org/whisper.cpp/releases/download/{WHISPER_TAG}/{name}"
    download(url, archive, size, sha256, "whisper.cpp-Engine", 24, 8)
    targets["bin"].mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="whisperflow-engine-") as temporary:
        temporary_path = Path(temporary)
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as package:
                package.extractall(temporary_path)
        else:
            with tarfile.open(archive, "r:gz") as package:
                root = temporary_path.resolve()
                for member in package.getmembers():
                    destination = (temporary_path / member.name).resolve()
                    if not destination.is_relative_to(root) or member.isdev():
                        raise RuntimeError("Unsicherer Pfad im Engine-Paket")
                    if member.issym():
                        link_target = (temporary_path / member.name).parent / member.linkname
                        if not link_target.resolve().is_relative_to(root):
                            raise RuntimeError("Unsicherer Link im Engine-Paket")
                    elif member.islnk():
                        link_target = temporary_path / member.linkname
                        if not link_target.resolve().is_relative_to(root):
                            raise RuntimeError("Unsicherer Link im Engine-Paket")
                package.extractall(temporary_path)
        source = next(temporary_path.rglob("whisper-server.exe" if os.name == "nt" else "whisper-server"), None)
        if source is None:
            raise RuntimeError("whisper-server fehlt im geprüften Engine-Paket")
        for item in source.parent.iterdir():
            if item.is_file():
                resolved = item.resolve()
                if not resolved.is_relative_to(temporary_path.resolve()):
                    raise RuntimeError("Engine-Paket verweist auf eine externe Datei")
                shutil.copy2(resolved, targets["bin"] / item.name)
    executable = targets["bin"] / ("whisper-server.exe" if os.name == "nt" else "whisper-server")
    if os.name != "nt":
        executable.chmod(0o755)
    if not executable.is_file():
        raise RuntimeError("Engine wurde nicht installiert")


def install_linux_prerequisites() -> None:
    required = ["pw-record", "pw-dump", "wl-copy", "ydotool"]
    missing = [name for name in required if shutil.which(name) is None]
    try:
        import ensurepip  # noqa: F401

        venv_missing = False
    except ImportError:
        venv_missing = True
    try:
        import ctypes

        ctypes.CDLL("libgomp.so.1")
        openmp_missing = False
    except OSError:
        openmp_missing = True
    if not missing and not venv_missing and not openmp_missing:
        update(detail="Linux-Systemkomponenten sind vorhanden.")
        return
    package_manager = next((name for name in ("apt-get", "dnf", "pacman") if shutil.which(name)), None)
    pkexec = shutil.which("pkexec")
    if not package_manager or not pkexec:
        names = [
            *missing,
            *(["python3-venv"] if venv_missing else []),
            *(["OpenMP-Laufzeit"] if openmp_missing else []),
        ]
        raise RuntimeError(
            "Fehlende Linux-Pakete: "
            + ", ".join(names)
            + ". Bitte PipeWire, wl-clipboard, ydotool und python3-venv installieren."
        )
    packages = {
        "apt-get": [
            "python3-venv",
            "libgomp1",
            "pipewire-bin",
            "wireplumber",
            "wl-clipboard",
            "ydotool",
        ],
        "dnf": ["libgomp", "pipewire-utils", "wireplumber", "wl-clipboard", "ydotool"],
        "pacman": ["gcc-libs", "pipewire", "wireplumber", "wl-clipboard", "ydotool"],
    }[package_manager]
    if package_manager == "pacman":
        command = [pkexec, package_manager, "-S", "--needed", "--noconfirm", *packages]
    else:
        command = [pkexec, package_manager, "install", "-y", *packages]
    update(detail="Linux-Komponenten werden installiert; die Systemabfrage bitte bestätigen …")
    run(command, timeout=1200)
    username = os.environ.get("USER")
    if username and shutil.which("usermod"):
        groups = run(["id", "-nG", username], check=False).stdout.split()
        if "input" not in groups:
            run([pkexec, "usermod", "-aG", "input", username])
            update(needs_relogin=True, detail="Die Eingaberechte gelten nach einmaligem Ab- und Anmelden.")


def write_config(targets: dict[str, Path], model_filename: str) -> None:
    targets["config"].mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 5,
        "enabled": True,
        "microphone_id": "",
        "model_path": str(targets["models"] / model_filename),
        "vad_model_path": str(targets["models"] / VAD[0]),
        "language": "de",
        "trigger": "KEY_RIGHTCTRL",
        "backend": "cpu",
        "initial_prompt": "CachyOS, KDE Plasma, Windows, Linux, PyQt6, whisper.cpp, Claude, ChatGPT und Codex.",
        "recording": {"min_duration_ms": 350, "max_duration_s": 300, "silence_threshold_dbfs": -50.0},
        "live": {"enabled": False, "direct_insert": False, "interval_ms": 1500},
        "diagnostics": {"enabled": False, "retention_entries": 20},
    }
    destination = targets["config"] / "config.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def create_launchers(targets: dict[str, Path], autostart: bool) -> None:
    python = targets["venv"] / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
    if os.name == "nt":
        start_menu = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs"
        start_menu.mkdir(parents=True, exist_ok=True)
        launcher = start_menu / "WhisperFlow.cmd"
        launcher.write_text(f'@echo off\r\nstart "" "{python}" -m local_dictation\r\n', encoding="utf-8")
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/WhisperFlow.cmd"
        if autostart:
            startup.write_text(f'@echo off\r\nstart "" "{python}" -m local_dictation --daemon\r\n', encoding="utf-8")
        else:
            startup.unlink(missing_ok=True)
    else:
        local_bin = Path.home() / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        launcher = local_bin / "whisperflow"
        launcher.write_text(f'#!/bin/sh\nexec "{python}" -m local_dictation "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        applications = Path.home() / ".local" / "share" / "applications"
        applications.mkdir(parents=True, exist_ok=True)
        desktop = applications / "whisperflow.desktop"
        desktop.write_text(
            "[Desktop Entry]\nType=Application\nName=WhisperFlow\nComment=Lokales Push-to-talk-Diktat\n"
            f"Exec={launcher}\nIcon=audio-input-microphone\nTerminal=false\nCategories=Utility;Audio;\n",
            encoding="utf-8",
        )
        service_dir = Path.home() / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True, exist_ok=True)
        service = service_dir / "local-dictation.service"
        service.write_text(
            "[Unit]\nDescription=WhisperFlow local dictation\nAfter=graphical-session.target pipewire.service\n\n"
            f"[Service]\nType=simple\nExecStart={python} -m local_dictation --daemon\nRestart=on-failure\nRestartSec=2\n\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        if shutil.which("systemctl"):
            run(["systemctl", "--user", "daemon-reload"], check=False)
            if autostart:
                run(["systemctl", "--user", "enable", "local-dictation.service"], check=False)
            else:
                run(["systemctl", "--user", "disable", "local-dictation.service"], check=False)


def install(options: dict[str, Any]) -> None:
    model_key = str(options.get("model", "small"))
    autostart = bool(options.get("autostart", True))
    if model_key not in MODELS:
        update(phase="error", error="Unbekanntes Sprachmodell", detail="Installation abgebrochen.")
        return
    targets = paths()
    try:
        update(phase="running", step=1, progress=2, title="System wird vorbereitet", detail=f"Erkannt: {platform.system()} {platform.machine()}")
        if platform.system() not in {"Windows", "Linux"}:
            raise RuntimeError("WhisperFlow unterstützt derzeit Windows und Linux")
        if sys.version_info < (3, 11):  # noqa: UP036 - installer runs before package metadata
            raise RuntimeError("Python 3.11 oder neuer wird benötigt")
        if platform.system() == "Linux":
            install_linux_prerequisites()

        update(step=2, progress=10, title="App wird eingerichtet", detail="Private Python-Umgebung wird erstellt …")
        targets["install"].mkdir(parents=True, exist_ok=True)
        if not targets["venv"].exists():
            venv.EnvBuilder(with_pip=True).create(targets["venv"])
        python = targets["venv"] / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", str(ROOT)], timeout=1200)

        update(step=3, progress=24, title="Lokale Engine wird installiert", detail="Offizielles whisper.cpp-Paket wird geprüft …")
        install_engine(targets)

        update(step=4, progress=34, title="Sprachmodell wird geladen", detail="Der größte Installationsschritt beginnt …")
        filename, size, sha256 = MODELS[model_key]
        model_url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{MODEL_REVISION}/{filename}"
        download(model_url, targets["models"] / filename, size, sha256, f"Sprachmodell {model_key}", 34, 48)
        vad_url = f"https://huggingface.co/ggml-org/whisper-vad/resolve/{VAD_REVISION}/{VAD[0]}"
        download(vad_url, targets["models"] / VAD[0], VAD[1], VAD[2], "Spracherkennung", 82, 3)

        update(step=5, progress=87, title="Integration wird abgeschlossen", detail="Einstellungen und Autostart werden angelegt …")
        write_config(targets, filename)
        create_launchers(targets, autostart)

        update(step=6, progress=94, title="Installation wird geprüft", detail="Python-Paket und Engine werden getestet …")
        run([str(python), "-c", "import local_dictation; print('ok')"])
        engine = targets["bin"] / ("whisper-server.exe" if os.name == "nt" else "whisper-server")
        if not engine.is_file():
            raise RuntimeError("Engine-Prüfung fehlgeschlagen")

        if not STATE.get("needs_relogin"):
            env = dict(os.environ)
            env["WHISPERFLOW_SHOW_SETTINGS"] = "1"
            executable = targets["venv"] / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
            subprocess.Popen([str(executable), "-m", "local_dictation", "--daemon"], env=env, close_fds=True)
        update(
            phase="done",
            step=6,
            progress=100,
            title="WhisperFlow ist fertig installiert",
            detail="Wähle im geöffneten Fenster dein Mikrofon. Danach: rechte Strg-Taste halten, sprechen, loslassen.",
            done=True,
        )
    except Exception as exc:
        update(phase="error", error=str(exc) or type(exc).__name__, title="Installation nicht abgeschlossen", detail="Der Fehler ist unten beschrieben.")


HTML = r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhisperFlow installieren</title><style>
:root{color-scheme:dark;--bg:#090b12;--card:#121722;--line:#273045;--muted:#96a2b8;--text:#eef2ff;--brand:#7d8dff;--accent:#52ddb2;--danger:#ff7285}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#17203b 0,transparent 36%),radial-gradient(circle at 90% 90%,#14342f 0,transparent 32%),var(--bg);color:var(--text);font:15px/1.5 Inter,Segoe UI,system-ui,sans-serif;min-height:100vh}.shell{max-width:980px;margin:auto;padding:48px 24px}.brand{display:flex;gap:14px;align-items:center;margin-bottom:34px}.logo{width:48px;height:48px;border-radius:15px;background:linear-gradient(145deg,var(--brand),#5965d8);display:grid;place-items:center;box-shadow:0 12px 38px #6577f340}.logo svg{width:28px}.brand h1{font-size:22px;margin:0}.brand p{margin:2px 0 0;color:var(--muted)}.panel{background:#111620e8;border:1px solid var(--line);border-radius:24px;padding:30px;box-shadow:0 28px 90px #0008;backdrop-filter:blur(20px)}h2{font-size:30px;line-height:1.2;margin:0 0 10px}.lead{color:var(--muted);margin:0 0 28px}.models{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:18px 0 25px}.model input{position:absolute;opacity:0}.model label{display:block;height:100%;padding:16px 12px;background:#181e2a;border:1px solid #2e374b;border-radius:14px;cursor:pointer;transition:.2s transform,.2s border,.2s background}.model label:hover{transform:translateY(-2px);border-color:#6172d8}.model input:checked+label{background:#20294a;border-color:#8795ff;box-shadow:inset 0 0 0 1px #8795ff}.model b,.model small{display:block}.model small{color:var(--muted);margin-top:5px}.choice{display:flex;align-items:center;gap:10px;color:var(--muted);margin-bottom:25px}.choice input{width:18px;height:18px;accent-color:var(--brand)}button{border:0;border-radius:12px;padding:12px 19px;background:linear-gradient(135deg,#7d8dff,#6170e5);color:#fff;font-weight:700;font-size:15px;cursor:pointer;box-shadow:0 8px 25px #6577f338;transition:.2s transform,.2s opacity}button:hover{transform:translateY(-1px)}button:disabled{opacity:.4;cursor:default;transform:none}.steps{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin:28px 0 22px}.step{height:5px;border-radius:4px;background:#242b3b;overflow:hidden}.step.on{background:var(--brand);box-shadow:0 0 16px #7788ff77}.status{display:none}.status.show{display:block;animation:rise .35s ease}.status-head{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}.badge{color:#aeb8cd;background:#202735;border:1px solid #303a50;border-radius:99px;padding:5px 10px;font-size:12px}.progress{height:10px;background:#202633;border-radius:99px;overflow:hidden;margin:20px 0}.bar{height:100%;width:0;background:linear-gradient(90deg,var(--brand),var(--accent));border-radius:inherit;transition:width .45s ease}.detail{color:var(--muted);min-height:24px}.log{margin-top:18px;padding:14px 16px;background:#0c1017;border:1px solid #222a38;border-radius:12px;color:#8793a9;font:12px/1.7 ui-monospace,monospace;max-height:150px;overflow:auto}.error{color:var(--danger);font-weight:650}.done{display:none;grid-template-columns:1fr 1.15fr;gap:22px;align-items:center}.done.show{display:grid;animation:rise .45s ease}.done img{width:100%;border-radius:17px;border:1px solid var(--line);background:#0c1018}.hint{padding:13px 15px;background:#1d2f2d;border:1px solid #2c5a50;border-radius:12px;color:#bcebdd}@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}@media(max-width:760px){.models{grid-template-columns:1fr 1fr}.done.show{grid-template-columns:1fr}.shell{padding:24px 14px}.panel{padding:22px}}
</style></head><body><main class="shell"><div class="brand"><div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><rect x="8" y="3" width="8" height="12" rx="4"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/></svg></div><div><h1>WhisperFlow</h1><p>Privates Diktat · vollständig lokal</p></div></div><section class="panel" id="setup"><h2>Einmal einrichten. Einfach lossprechen.</h2><p class="lead">Der Installer richtet App, lokale Engine, Sprachmodell und Autostart ein. Audio verlässt dieses Gerät nicht.</p><b>Sprachmodell wählen</b><div class="models">
<div class="model"><input id="tiny" name="model" type="radio" value="tiny"><label for="tiny"><b>Tiny</b><small>75 MB<br>maximal schnell</small></label></div><div class="model"><input id="base" name="model" type="radio" value="base"><label for="base"><b>Base</b><small>142 MB<br>schnell</small></label></div><div class="model"><input id="small" name="model" type="radio" value="small" checked><label for="small"><b>Small</b><small>465 MB<br>empfohlen</small></label></div><div class="model"><input id="medium" name="model" type="radio" value="medium"><label for="medium"><b>Medium</b><small>1,5 GB<br>genauer</small></label></div><div class="model"><input id="large" name="model" type="radio" value="large-v3-turbo"><label for="large"><b>Large Turbo</b><small>1,6 GB<br>beste Qualität</small></label></div></div><label class="choice"><input id="autostart" type="checkbox" checked> WhisperFlow bei der Anmeldung starten</label><button id="start">Installation starten</button></section>
<section class="panel status" id="status"><div class="status-head"><div><h2 id="title">Installation läuft</h2><p class="lead" id="detail"></p></div><span class="badge" id="badge">Schritt 1 von 6</span></div><div class="steps" id="steps"></div><div class="progress"><div class="bar" id="bar"></div></div><div class="log" id="log"></div><p class="error" id="error"></p></section>
<section class="panel done" id="done"><div><h2>Fertig installiert</h2><p class="lead">Wähle noch dein Mikrofon im Einstellungsfenster. Das Sprachmodell kannst du dort jederzeit wechseln.</p><p class="hint" id="relogin" hidden>Bitte einmal vollständig ab- und wieder anmelden, damit Linux die neuen Eingaberechte übernimmt.</p><button id="close">Installer schließen</button></div><img src="/quickstart.svg" alt="Kurzanleitung: rechte Strg-Taste halten, sprechen, loslassen"></section></main>
<script>const token='__TOKEN__';const setup=document.querySelector('#setup'),status=document.querySelector('#status'),done=document.querySelector('#done');document.querySelector('#steps').innerHTML='<i class="step"></i>'.repeat(6);document.querySelector('#start').onclick=async()=>{setup.style.display='none';status.classList.add('show');const model=document.querySelector('input[name=model]:checked').value;await fetch('/start?token='+token,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model,autostart:document.querySelector('#autostart').checked})});poll()};document.querySelector('#close').onclick=async()=>{await fetch('/close?token='+token,{method:'POST'});window.close()};async function poll(){let s=await(await fetch('/status?token='+token)).json();document.querySelector('#title').textContent=s.title;document.querySelector('#detail').textContent=s.detail;document.querySelector('#badge').textContent='Schritt '+s.step+' von 6';document.querySelector('#bar').style.width=s.progress+'%';[...document.querySelectorAll('.step')].forEach((x,i)=>x.classList.toggle('on',i<s.step));document.querySelector('#log').innerHTML=s.log.map(x=>'<div>'+esc(x)+'</div>').join('');document.querySelector('#error').textContent=s.error||'';if(s.done){status.classList.remove('show');done.classList.add('show');document.querySelector('#relogin').hidden=!s.needs_relogin;return}if(s.phase!=='error')setTimeout(poll,700)}function esc(x){let d=document.createElement('div');d.textContent=x;return d.innerHTML}</script></body></html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        pass

    def _authorized(self) -> bool:
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(self.path).query).get("token", [""])[0] == TOKEN

    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/":
            body = HTML.replace("__TOKEN__", TOKEN).encode()
            self._send(200, "text/html; charset=utf-8", body)
        elif route == "/quickstart.svg":
            self._send(200, "image/svg+xml", (ROOT / "assets" / "quickstart.svg").read_bytes())
        elif route == "/status" and self._authorized():
            with STATE_LOCK:
                body = json.dumps(STATE, ensure_ascii=False).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self) -> None:
        global INSTALL_THREAD
        route = self.path.split("?", 1)[0]
        if route == "/close" and self._authorized():
            self._send(200, "application/json", b'{"ok":true}')
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if route != "/start" or not self._authorized():
            self._send(403, "text/plain", b"Forbidden")
            return
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        try:
            options = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send(400, "text/plain", b"Invalid JSON")
            return
        if INSTALL_THREAD is None or not INSTALL_THREAD.is_alive():
            INSTALL_THREAD = threading.Thread(target=install, args=(options,), daemon=True)
            INSTALL_THREAD.start()
        self._send(202, "application/json", b'{"ok":true}')

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"WhisperFlow-Installer: {url}")
    webbrowser.open(url, new=1)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
