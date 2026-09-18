"""Windows global right-Control hook with the same pipe API as the Linux proxy."""

from __future__ import annotations

import multiprocessing
import time
from dataclasses import dataclass
from typing import Any


def _worker(control: Any, events: Any) -> None:
    try:
        import keyboard
    except ImportError as exc:
        events.send({"type": "error", "code": "backend-unavailable", "message": str(exc), "fatal": True})
        return

    enabled = False
    down = False
    last_heartbeat = time.monotonic()

    def emit(payload: dict[str, Any]) -> None:
        try:
            events.send(payload)
        except (BrokenPipeError, EOFError, OSError):
            pass

    def on_key(event: Any) -> None:
        nonlocal down
        if not enabled:
            return
        pressed = str(getattr(event, "event_type", "")) == "down"
        if pressed == down:
            return
        down = pressed
        emit({"type": "trigger", "state": "pressed" if pressed else "released", "timestamp": time.monotonic()})

    try:
        keyboard.hook_key("right ctrl", on_key, suppress=True)
        emit({"type": "status", "state": "started"})
        stopping = False
        while not stopping:
            if control.poll(0.1):
                message = control.recv()
                command = message.get("type") if isinstance(message, dict) else ""
                if command == "heartbeat":
                    last_heartbeat = time.monotonic()
                elif command == "enable":
                    enabled = True
                    emit({"type": "status", "state": "input-ready", "all_grabbed": True})
                elif command == "disable":
                    enabled = False
                    if down:
                        down = False
                        emit({"type": "trigger", "state": "released", "timestamp": time.monotonic()})
                    emit({"type": "status", "state": "input-disabled", "all_grabbed": False})
                elif command == "shutdown":
                    stopping = True
            if enabled and time.monotonic() - last_heartbeat > 2.5:
                enabled = False
                down = False
                emit({"type": "status", "state": "input-disabled", "all_grabbed": False})
        emit({"type": "status", "state": "stopped", "all_grabbed": False})
    except Exception as exc:
        emit({"type": "error", "code": "windows-hook-failed", "message": str(exc), "fatal": True})
    finally:
        try:
            keyboard.unhook_all()
        except Exception:
            pass


@dataclass(slots=True)
class WindowsInputHandle:
    process: multiprocessing.Process
    control: Any
    events: Any

    def send(self, command: str, **fields: Any) -> None:
        self.control.send({"type": command, **fields})

    def heartbeat(self) -> None:
        self.send("heartbeat")

    def enable(self, *, reason: str = "ready") -> None:
        self.send("enable", reason=reason)

    def disable(self, *, reason: str = "parent") -> None:
        self.send("disable", reason=reason)

    def stop(self, timeout: float = 2.0) -> None:
        if self.process.is_alive():
            try:
                self.send("shutdown")
            except (BrokenPipeError, EOFError, OSError):
                pass
            self.process.join(timeout)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout)


def spawn_windows_input() -> WindowsInputHandle:
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=True)
    parent_events, child_events = context.Pipe(duplex=False)
    process = context.Process(
        target=_worker,
        args=(child_control, child_events),
        name="whisperflow-windows-input",
        daemon=True,
    )
    process.start()
    child_control.close()
    child_events.close()
    return WindowsInputHandle(process, parent_control, parent_events)


__all__ = ["WindowsInputHandle", "spawn_windows_input"]
