from __future__ import annotations

import sys

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface


class SessionMonitor(QObject):
    locked_changed = pyqtSignal(bool)
    prepare_for_sleep = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if sys.platform == "win32":
            self._last_windows_lock = self.currently_locked()
            self._windows_timer = QTimer(self)
            self._windows_timer.setInterval(1000)
            self._windows_timer.timeout.connect(self._poll_windows_lock)
            self._windows_timer.start()
            return
        session = QDBusConnection.sessionBus()
        session.connect(
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            "ActiveChanged",
            self._on_locked_changed,
        )
        session.connect(
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.kde.screensaver",
            "AboutToLock",
            self._on_about_to_lock,
        )
        system = QDBusConnection.systemBus()
        system.connect(
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            self._on_prepare_for_sleep,
        )

    def currently_locked(self) -> bool:
        if sys.platform == "win32":
            try:
                import ctypes

                desktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
                if not desktop:
                    return True
                ctypes.windll.user32.CloseDesktop(desktop)
                return False
            except Exception:
                return False
        interface = QDBusInterface(
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            QDBusConnection.sessionBus(),
        )
        reply = interface.call("GetActive")
        args = reply.arguments()
        return bool(args[0]) if args else False

    def _poll_windows_lock(self) -> None:
        locked = self.currently_locked()
        if locked != self._last_windows_lock:
            self._last_windows_lock = locked
            self.locked_changed.emit(locked)

    @pyqtSlot(bool, name="on_locked_changed")
    def _on_locked_changed(self, locked: bool) -> None:
        self.locked_changed.emit(locked)

    @pyqtSlot(name="on_about_to_lock")
    def _on_about_to_lock(self) -> None:
        self.locked_changed.emit(True)

    @pyqtSlot(bool, name="on_prepare_for_sleep")
    def _on_prepare_for_sleep(self, sleeping: bool) -> None:
        self.prepare_for_sleep.emit(sleeping)
