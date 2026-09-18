from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtWidgets import QApplication

from local_dictation.control import ControlServer


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication(["local-dictation-test"])
    assert isinstance(app, QApplication)
    return app


def spin_until(predicate: object, timeout: float = 1.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.001)
    raise AssertionError("Qt condition timed out")


def connect(path: Path) -> QLocalSocket:
    socket = QLocalSocket()
    socket.connectToServer(str(path))
    spin_until(lambda: socket.state() == QLocalSocket.LocalSocketState.ConnectedState)
    return socket


def read_reply(socket: QLocalSocket) -> dict[str, Any]:
    spin_until(lambda: socket.canReadLine())
    return json.loads(bytes(socket.readLine()).decode("utf-8"))


def test_control_socket_is_owner_only_and_accepts_fragmented_json(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: dict[str, Any], reply: object) -> None:
        requests.append(request)
        reply({"ok": True, "echo": request["command"]})  # type: ignore[operator]

    path = tmp_path / "runtime" / "control.sock"
    server = ControlServer(path, handler)
    server.listen()
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        socket = connect(path)
        socket.write(b'{"command":"sta')
        socket.flush()
        for _ in range(5):
            QCoreApplication.processEvents()
        assert requests == []
        socket.write(b'tus"}\n')
        socket.flush()
        assert read_reply(socket) == {"ok": True, "echo": "status"}
        assert requests == [{"command": "status"}]
    finally:
        server.close()
    assert not path.exists()


def test_control_socket_supports_a_delayed_worker_reply(tmp_path: Path) -> None:
    callbacks: list[object] = []

    def handler(_request: dict[str, Any], reply: object) -> None:
        callbacks.append(reply)

    path = tmp_path / "control.sock"
    server = ControlServer(path, handler)
    server.listen()
    try:
        socket = connect(path)
        socket.write(b'{"command":"benchmark"}\n')
        socket.flush()
        spin_until(lambda: len(callbacks) == 1)
        assert not socket.canReadLine()
        callbacks[0]({"ok": True, "inference_seconds": 1.25})  # type: ignore[operator]
        assert read_reply(socket)["inference_seconds"] == 1.25
    finally:
        server.close()


def test_delayed_reply_is_safe_after_client_disconnect(tmp_path: Path) -> None:
    callbacks: list[object] = []
    path = tmp_path / "control.sock"
    server = ControlServer(path, lambda _request, reply: callbacks.append(reply))
    server.listen()
    try:
        socket = connect(path)
        socket.write(b'{"command":"benchmark"}\n')
        socket.flush()
        spin_until(lambda: len(callbacks) == 1)
        socket.abort()
        spin_until(lambda: socket.state() == QLocalSocket.LocalSocketState.UnconnectedState)
        QCoreApplication.sendPostedEvents()

        callbacks[0]({"ok": True})  # type: ignore[operator]
    finally:
        server.close()


@pytest.mark.parametrize("payload", [b"not-json\n", b"[]\n", b"\xff\n"])
def test_control_socket_rejects_malformed_requests(tmp_path: Path, payload: bytes) -> None:
    called: list[object] = []
    path = tmp_path / f"control-{len(payload)}.sock"
    server = ControlServer(path, lambda *args: called.append(args))
    server.listen()
    try:
        socket = connect(path)
        socket.write(payload)
        socket.flush()
        assert read_reply(socket) == {"ok": False, "error": "Ungültige Anfrage"}
        assert called == []
    finally:
        server.close()


def test_control_socket_rejects_oversized_request(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    server = ControlServer(path, lambda *_args: pytest.fail("handler must not run"))
    server.listen()
    try:
        socket = connect(path)
        socket.write(b"x" * 65_537)
        socket.flush()
        assert read_reply(socket) == {"ok": False, "error": "Anfrage zu groß"}
    finally:
        server.close()
