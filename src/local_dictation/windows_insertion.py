"""Native Windows clipboard and keyboard insertion backend."""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any

from .insertion import (
    MAX_REVISION_GRAPHEMES,
    InsertionResult,
    RevisionResult,
    _backspace_suffix_is_portable,
    plan_revision,
)


class WindowsInsertionBackend:
    def __init__(self, _runtime_dir) -> None:
        self._operation_lock = threading.RLock()

    @staticmethod
    def _cancelled(cancel: Any | None) -> bool:
        if cancel is None:
            return False
        is_set = getattr(cancel, "is_set", None)
        return bool(is_set()) if callable(is_set) else bool(cancel() if callable(cancel) else cancel)

    def preflight(self) -> str | None:
        try:
            import keyboard  # noqa: F401
            import pyperclip  # noqa: F401
        except ImportError as exc:
            return f"Windows-Eingabemodul fehlt: {exc}"
        return None

    def copy(self, text: str) -> InsertionResult:
        try:
            import pyperclip

            pyperclip.copy(text)
            return InsertionResult(True, False, "Text wurde kopiert")
        except Exception:
            return InsertionResult(False, False, "Text konnte nicht kopiert werden")

    def insert(self, text: str, *, cancel: Any | None = None) -> InsertionResult:
        with self._operation_lock:
            if self._cancelled(cancel):
                return InsertionResult(False, False, "Einfügen wurde abgebrochen")
            copied = self.copy(text)
            if not copied.copied or self._cancelled(cancel):
                return copied
            try:
                import keyboard

                time.sleep(0.05)
                keyboard.send("shift+insert")
                return InsertionResult(True, True, "Einfügen gesendet")
            except Exception:
                return InsertionResult(True, False, "Kopiert, aber Einfügen ist fehlgeschlagen")

    def capture_active_window(self) -> str | None:
        try:
            handle = int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            return None
        return str(handle) if handle else None

    def revise(self, previous: str, current: str, *, expected_window: str, cancel: Any | None = None) -> RevisionResult:
        with self._operation_lock:
            if self._cancelled(cancel):
                return RevisionResult(False, False, True, "Live-Einfügung wurde abgebrochen", cancelled=True)
            if self.capture_active_window() != expected_window:
                return RevisionResult(False, False, True, "Das aktive Fenster hat sich geändert")
            plan = plan_revision(previous, current)
            if not plan.delete_graphemes and not plan.insert_text:
                return RevisionResult(False, True, True, "Live-Text ist unverändert")
            if plan.delete_graphemes > MAX_REVISION_GRAPHEMES or not _backspace_suffix_is_portable(previous, plan.delete_graphemes):
                copied = self.copy(current)
                return RevisionResult(copied.copied, False, True, "Die Live-Korrektur ist nicht sicher ausführbar")
            copied = self.copy(plan.insert_text) if plan.insert_text else InsertionResult(False, False, "")
            try:
                import keyboard

                for _ in range(plan.delete_graphemes):
                    keyboard.send("backspace")
                if plan.insert_text:
                    keyboard.send("shift+insert")
                return RevisionResult(copied.copied, True, True, "Live-Text aktualisiert")
            except Exception:
                return RevisionResult(copied.copied, False, False, "Live-Korrektur ist fehlgeschlagen")


__all__ = ["WindowsInsertionBackend"]
