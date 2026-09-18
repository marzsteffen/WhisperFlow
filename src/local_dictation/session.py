from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusInterface


class SessionMonitor(QObject):
    locked_changed = pyqtSignal(bool)
    prepare_for_sleep = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
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
        interface = QDBusInterface(
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            QDBusConnection.sessionBus(),
        )
        reply = interface.call("GetActive")
        args = reply.arguments()
        return bool(args[0]) if args else False

    @pyqtSlot(bool, name="on_locked_changed")
    def _on_locked_changed(self, locked: bool) -> None:
        self.locked_changed.emit(locked)

    @pyqtSlot(name="on_about_to_lock")
    def _on_about_to_lock(self) -> None:
        self.locked_changed.emit(True)

    @pyqtSlot(bool, name="on_prepare_for_sleep")
    def _on_prepare_for_sleep(self, sleeping: bool) -> None:
        self.prepare_for_sleep.emit(sleeping)
