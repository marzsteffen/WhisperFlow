from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import APP_DIRECTORY, get_config_dir, get_data_dir


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise RuntimeError("XDG_RUNTIME_DIR ist nicht gesetzt")
    return Path(base) / APP_DIRECTORY


def control_path() -> Path:
    return runtime_dir() / "control.sock"


def _start_service() -> None:
    result = subprocess.run(
        ["systemctl", "--user", "start", "local-dictation.service"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or "local-dictation.service konnte nicht gestartet werden")


def _request(payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + min(timeout, 30.0)
    path = control_path()
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(max(0.2, min(2.0, deadline - time.monotonic())))
        try:
            client.connect(str(path))
            break
        except OSError as exc:
            last_error = exc
            client.close()
            time.sleep(0.1)
    else:
        raise RuntimeError(f"Dienst antwortet nicht: {last_error or path}")

    client.settimeout(timeout)
    try:
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        received = bytearray()
        while b"\n" not in received:
            block = client.recv(65_536)
            if not block:
                break
            received.extend(block)
            if len(received) > 1_048_576:
                raise RuntimeError("Antwort des Dienstes ist zu groß")
    finally:
        client.close()
    if not received:
        raise RuntimeError("Dienst hat keine Antwort gesendet")
    try:
        response = json.loads(received.partition(b"\n")[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Dienst hat eine ungültige Antwort gesendet") from exc
    if not isinstance(response, dict):
        raise RuntimeError("Dienst hat eine ungültige Antwort gesendet")
    return response


def _validated_purge_target(path: Path) -> Path:
    if path.name != APP_DIRECTORY or path.is_symlink():
        raise RuntimeError(f"Unsicherer Purge-Pfad wird nicht gelöscht: {path}")
    resolved = path.resolve(strict=False)
    if resolved in {Path("/"), Path.home(), Path.home().parent} or len(resolved.parts) < 4:
        raise RuntimeError(f"Unsicherer Purge-Pfad wird nicht gelöscht: {resolved}")
    return path


def purge(*, assume_yes: bool) -> int:
    targets = [_validated_purge_target(get_config_dir()), _validated_purge_target(get_data_dir())]
    existing = [path for path in targets if path.exists()]
    if not existing:
        print("Keine verwalteten Benutzerdateien vorhanden.")
        return 0
    print("Folgende verwaltete Pfade werden unwiderruflich gelöscht:")
    for path in existing:
        print(f"  {path}")
    if not assume_yes:
        if not sys.stdin.isatty():
            print("Abbruch: Für nichtinteraktiven Purge ist --yes erforderlich.", file=sys.stderr)
            return 2
        answer = input("Wirklich löschen? [j/N] ").strip().casefold()
        if answer not in {"j", "ja", "y", "yes"}:
            print("Abgebrochen.")
            return 1
    subprocess.run(
        ["systemctl", "--user", "stop", "local-dictation.service"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    for path in existing:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_file() and not path.is_symlink():
            path.unlink()
    print("Konfiguration und verwaltete Modelle wurden gelöscht.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-dictation")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--benchmark", action="store_true", help="lokalen Whisper-Benchmark ausführen")
    parser.add_argument("--json", action="store_true", help="Benchmark als JSON ausgeben")
    parser.add_argument("--purge", action="store_true", help="Benutzerkonfiguration und Standardmodelle löschen")
    parser.add_argument("--yes", action="store_true", help="Purge-Rückfrage bestätigen")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.daemon:
        from .app import run_daemon

        return run_daemon()
    if args.purge:
        return purge(assume_yes=args.yes)
    try:
        _start_service()
        if args.benchmark:
            response = _request({"command": "benchmark"}, timeout=600.0)
            if args.json:
                print(json.dumps(response, ensure_ascii=False, sort_keys=True))
            elif response.get("ok"):
                print("Lokaler Diktat-Benchmark")
                print(f"  Backend: {response.get('backend', 'unbekannt')}")
                print(f"  Audio: {response.get('audio_seconds', 0):.2f} s")
                print(f"  Inferenz: {response.get('inference_seconds', 0):.2f} s")
                print(f"  Echtzeitfaktor: {response.get('realtime_factor', 0):.3f}")
                print(f"  Ziel <= 5 s: {'ja' if response.get('target_met') else 'nein'}")
            else:
                print(response.get("error", "Benchmark fehlgeschlagen"), file=sys.stderr)
            return 0 if response.get("ok") else 1
        response = _request({"command": "show-settings"})
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "Einstellungen konnten nicht geöffnet werden")))
        return 0
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f"local-dictation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

