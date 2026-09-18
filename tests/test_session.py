from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

import local_dictation.session as session_module
from local_dictation.session import SessionMonitor


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication(["local-dictation-test"])
    assert isinstance(app, QApplication)
    return app


class FakeBus:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connections: list[tuple[object, ...]] = []

    def connect(self, *args: object) -> bool:
        self.connections.append(args)
        return True


class FakeConnectionFactory:
    session = FakeBus("session")
    system = FakeBus("system")

    @classmethod
    def sessionBus(cls) -> FakeBus:
        return cls.session

    @classmethod
    def systemBus(cls) -> FakeBus:
        return cls.system


class FakeReply:
    def __init__(self, arguments: list[object]) -> None:
        self._arguments = arguments

    def arguments(self) -> list[object]:
        return self._arguments


class FakeInterface:
    next_arguments: list[object] = [True]
    created: list[tuple[object, ...]] = []

    def __init__(self, *args: object) -> None:
        type(self).created.append(args)

    def call(self, method: str) -> FakeReply:
        assert method == "GetActive"
        return FakeReply(type(self).next_arguments)


@pytest.fixture
def fake_dbus(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeConnectionFactory.session = FakeBus("session")
    FakeConnectionFactory.system = FakeBus("system")
    FakeInterface.created.clear()
    FakeInterface.next_arguments = [True]
    monkeypatch.setattr(session_module, "QDBusConnection", FakeConnectionFactory)
    monkeypatch.setattr(session_module, "QDBusInterface", FakeInterface)


def test_monitor_subscribes_to_lock_and_suspend_signals(fake_dbus: None) -> None:
    monitor = SessionMonitor()

    assert [
        (c[0], c[1], c[2], c[3], c[4].__name__)
        for c in FakeConnectionFactory.session.connections
    ] == [
        (
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            "ActiveChanged",
            "_on_locked_changed",
        ),
        (
            "org.freedesktop.ScreenSaver",
            "/ScreenSaver",
            "org.kde.screensaver",
            "AboutToLock",
            "_on_about_to_lock",
        ),
    ]
    assert [
        (c[0], c[1], c[2], c[3], c[4].__name__)
        for c in FakeConnectionFactory.system.connections
    ] == [
        (
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            "_on_prepare_for_sleep",
        )
    ]
    assert monitor.currently_locked() is True


def test_monitor_forwards_all_session_transitions(fake_dbus: None) -> None:
    monitor = SessionMonitor()
    locked: list[bool] = []
    sleeping: list[bool] = []
    monitor.locked_changed.connect(locked.append)
    monitor.prepare_for_sleep.connect(sleeping.append)

    monitor._on_locked_changed(False)
    monitor._on_about_to_lock()
    monitor._on_prepare_for_sleep(True)
    monitor._on_prepare_for_sleep(False)

    assert locked == [False, True]
    assert sleeping == [True, False]


def test_missing_get_active_value_is_treated_as_unlocked(fake_dbus: None) -> None:
    FakeInterface.next_arguments = []
    assert SessionMonitor().currently_locked() is False
