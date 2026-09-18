"""Cross-platform process entry point for WhisperFlow."""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from .audio import get_runtime_dir
from .config import ConfigError, get_config_path, load_config, save_config
from .controller import DictationController
from .ui import apply_theme


def _acquire_instance_lock(runtime_dir: Path) -> int:
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_dir.chmod(0o700)
    path = runtime_dir / "instance.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise RuntimeError("local-dictation läuft in dieser Sitzung bereits") from None
    return descriptor


def run_daemon() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="local-dictation: %(levelname)s: %(message)s",
    )
    try:
        os.umask(0o077)
        runtime_dir = get_runtime_dir()
        lock_descriptor = _acquire_instance_lock(runtime_dir)
        config = load_config()
        if not get_config_path().exists():
            save_config(config)
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f"local-dictation: {exc}", file=sys.stderr)
        return 1

    app = QApplication(["local-dictation"])
    app.setApplicationName("WhisperFlow")
    app.setApplicationDisplayName("WhisperFlow")
    app.setDesktopFileName("local-dictation")
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)
    controller = DictationController(app, config, runtime_dir)
    app.aboutToQuit.connect(controller.shutdown)

    # Python signal handlers are serviced promptly even when the Qt queue is idle.
    signal.signal(signal.SIGTERM, lambda *_args: app.quit())
    signal.signal(signal.SIGINT, lambda *_args: app.quit())
    signal_timer = QTimer()
    signal_timer.setInterval(500)
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start()

    try:
        controller.start()
        if os.environ.pop("WHISPERFLOW_SHOW_SETTINGS", "") == "1":
            QTimer.singleShot(1200, controller.show_settings)
        return app.exec()
    except Exception as exc:
        logging.getLogger(__name__).exception("Start fehlgeschlagen: %s", exc)
        controller.shutdown()
        return 1
    finally:
        os.close(lock_descriptor)


__all__ = ["run_daemon"]
