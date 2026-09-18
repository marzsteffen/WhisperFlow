from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


class ControlServer(QObject):
    """Owner-only, newline-delimited JSON control socket."""

    def __init__(
        self,
        path: Path,
        handler: Callable[[dict[str, Any], Callable[[dict[str, Any]], None]], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.path = path
        self.handler = handler
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self._buffers: dict[QLocalSocket, bytearray] = {}

    def listen(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if self.path.is_socket() or self.path.is_file():
                self.path.unlink()
        except OSError:
            pass
        listen_name = self.path.name if sys.platform == "win32" else str(self.path)
        if not self.server.listen(listen_name):
            raise RuntimeError(f"Control-Socket konnte nicht geöffnet werden: {self.server.errorString()}")
        if sys.platform != "win32":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                self.server.close()
                raise

    def _accept(self) -> None:
        while socket := self.server.nextPendingConnection():
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda current=socket: self._read(current))
            socket.disconnected.connect(lambda current=socket: self._drop(current))

    def _read(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 65_536:
            self._reply(socket, {"ok": False, "error": "Anfrage zu groß"})
            return
        while b"\n" in buffer:
            raw, _, rest = buffer.partition(b"\n")
            self._buffers[socket] = buffer = bytearray(rest)
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._reply(socket, {"ok": False, "error": "Ungültige Anfrage"})
                return
            self.handler(request, lambda response, current=socket: self._reply(current, response))
            # The protocol intentionally permits exactly one request per
            # connection. In particular, an asynchronous benchmark reply must
            # not race a second request that disconnects the same socket.
            return

    def _reply(self, socket: QLocalSocket, response: dict[str, Any]) -> None:
        try:
            connected = socket.state() != QLocalSocket.LocalSocketState.UnconnectedState
        except RuntimeError:
            # A delayed worker can finish after the peer disconnected and Qt
            # deleted the underlying C++ object.
            self._buffers.pop(socket, None)
            return
        if not connected:
            return
        try:
            socket.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            socket.flush()
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
        except RuntimeError:
            self._buffers.pop(socket, None)

    def _drop(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        socket.deleteLater()

    def close(self) -> None:
        self.server.close()
        for socket in tuple(self._buffers):
            socket.abort()
        self._buffers.clear()
        try:
            if self.path.is_socket():
                self.path.unlink()
        except OSError:
            pass
