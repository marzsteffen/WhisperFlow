from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CLIPBOARD_SETTLE_SECONDS = 0.05
PASTE_DELIVERY_SECONDS = 0.10
KWIN_QUERY_TIMEOUT_SECONDS = 0.5
KEY_BACKSPACE = 14
KEY_LEFTSHIFT = 42
KEY_INSERT = 110
REVISION_KEY_DELAY_MS = 1
MAX_REVISION_GRAPHEMES = 64
_KWIN_WINDOW_ID = re.compile(
    r"^\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}$"
)


@dataclass(frozen=True, slots=True)
class InsertionResult:
    copied: bool
    inserted: bool
    message: str


@dataclass(frozen=True, slots=True)
class RevisionPlan:
    """Minimal suffix edit needed to turn one rendered draft into another."""

    delete_graphemes: int
    insert_text: str
    common_prefix: str


@dataclass(frozen=True, slots=True)
class RevisionResult:
    """Outcome of a best-effort edit of text already present in a target."""

    copied: bool
    revised: bool
    state_known: bool
    message: str
    cancelled: bool = False


def _is_variation_selector(character: str) -> bool:
    value = ord(character)
    return 0xFE00 <= value <= 0xFE0F or 0xE0100 <= value <= 0xE01EF


def _is_emoji_modifier(character: str) -> bool:
    return 0x1F3FB <= ord(character) <= 0x1F3FF


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def grapheme_clusters(text: str) -> tuple[str, ...]:
    """Split the transcript into practical editor-backspace units.

    Whisper's German output is normally NFC text.  Handling combining marks,
    variation selectors, emoji modifiers, ZWJ sequences, and paired regional
    indicators also keeps rollback counts correct for pasted user vocabulary
    without adding a Unicode-regex runtime dependency.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    clusters: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        extend = (
            category.startswith("M")
            or _is_variation_selector(character)
            or _is_emoji_modifier(character)
        )
        if not clusters:
            clusters.append(character)
        elif extend or character == "\u200d" or clusters[-1].endswith("\u200d"):
            clusters[-1] += character
        elif _is_regional_indicator(character) and all(
            _is_regional_indicator(item) for item in clusters[-1]
        ) and len(clusters[-1]) == 1:
            clusters[-1] += character
        else:
            clusters.append(character)
    return tuple(clusters)


def plan_revision(previous: str, current: str) -> RevisionPlan:
    """Return a grapheme-aligned longest-common-prefix suffix replacement."""

    previous_units = grapheme_clusters(previous)
    current_units = grapheme_clusters(current)
    common = 0
    limit = min(len(previous_units), len(current_units))
    while common < limit and previous_units[common] == current_units[common]:
        common += 1
    return RevisionPlan(
        delete_graphemes=len(previous_units) - common,
        insert_text="".join(current_units[common:]),
        common_prefix="".join(previous_units[:common]),
    )


def _backspace_suffix_is_portable(previous: str, delete_graphemes: int) -> bool:
    """Conservatively reject units editors may erase inconsistently.

    Qt text controls, terminals, and browser editors do not agree whether one
    Backspace removes an extended multi-codepoint grapheme or only one scalar.
    A direct revision must never guess, because a wrong count invalidates the
    session ledger and can make a later correction delete unrelated text.
    """

    if delete_graphemes <= 0:
        return True
    suffix = grapheme_clusters(previous)[-delete_graphemes:]
    return all(
        len(unit) == 1
        and not unicodedata.category(unit).startswith(("C", "M"))
        and not _is_variation_selector(unit)
        and not _is_emoji_modifier(unit)
        and not _is_regional_indicator(unit)
        for unit in suffix
    )


class InsertionBackend:
    def __init__(
        self,
        runtime_dir: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        environ: Mapping[str, str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.runtime_dir = runtime_dir
        self._runner = runner
        self._environ = dict(os.environ if environ is None else environ)
        self._sleep = sleeper
        self._operation_lock = threading.RLock()

    def _socket_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if value := self._environ.get("YDOTOOL_SOCKET"):
            candidates.append(Path(value))
        candidates.append(self.runtime_dir.parent / ".ydotool_socket")
        candidates.append(Path("/tmp/.ydotool_socket"))
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _socket_is_safe(path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return False
        # ydotool 1.0.x uses an owner-only AF_UNIX datagram socket. Connecting
        # without sending verifies that this is a live endpoint rather than a
        # stale socket inode.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(path))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def find_socket(self) -> Path | None:
        return next((path for path in self._socket_candidates() if self._socket_is_safe(path)), None)

    def ensure_daemon(self) -> Path | None:
        if path := self.find_socket():
            return path
        for unit in ("ydotoold.service", "ydotool.service"):
            try:
                shown = self._runner(
                    ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if shown.returncode != 0 or shown.stdout.strip() != "loaded":
                continue
            self._runner(
                ["systemctl", "--user", "start", unit],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for _ in range(20):
                if path := self.find_socket():
                    return path
                time.sleep(0.05)
            break
        return None

    def preflight(self) -> str | None:
        try:
            result = self._runner(
                ["wl-copy", "--help"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "wl-copy ist nicht installiert"
        help_text = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0 or "--sensitive" not in help_text:
            return "wl-copy 2.3 oder neuer mit --sensitive wird benötigt"
        if self.ensure_daemon() is None:
            return "Der ydotool-Benutzerdienst ist nicht erreichbar"
        return None

    def copy(self, text: str) -> InsertionResult:
        with self._operation_lock:
            return self._copy_unlocked(text)

    def _copy_unlocked(self, text: str) -> InsertionResult:
        env = dict(self._environ)
        # wl-copy 2.3 may spool stdin before its clipboard-owning fork. Keep
        # that transient plaintext in our private XDG runtime directory.
        env["TMPDIR"] = str(self.runtime_dir)
        try:
            result = self._runner(
                ["wl-copy", "--sensitive", "--type", "text/plain;charset=utf-8"],
                input=text.encode("utf-8"),
                # wl-copy forks a clipboard-owning child. Captured pipes stay
                # open in that child and make subprocess.run wait until our
                # timeout even though the launcher exited successfully.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return InsertionResult(False, False, "Text konnte nicht kopiert werden")
        if result.returncode != 0:
            return InsertionResult(False, False, "Text konnte nicht kopiert werden")
        return InsertionResult(True, False, "Text wurde kopiert")

    @staticmethod
    def _cancelled(cancel: Any | None) -> bool:
        if cancel is None:
            return False
        is_set = getattr(cancel, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        return bool(cancel()) if callable(cancel) else bool(cancel)

    def insert(self, text: str, *, cancel: Any | None = None) -> InsertionResult:
        with self._operation_lock:
            return self._insert_unlocked(text, cancel=cancel)

    def _insert_unlocked(
        self, text: str, *, cancel: Any | None = None
    ) -> InsertionResult:
        if self._cancelled(cancel):
            return InsertionResult(False, False, "Einfügen wurde abgebrochen")
        copied = self._copy_unlocked(text)
        if not copied.copied:
            return copied
        # A lock/suspend notification can arrive while wl-copy establishes
        # clipboard ownership. Re-check immediately before synthesizing keys.
        if self._cancelled(cancel):
            return InsertionResult(True, False, "Kopiert, Einfügen wurde abgebrochen")
        ydotool_socket = self.ensure_daemon()
        if ydotool_socket is None:
            return InsertionResult(True, False, "Kopiert, aber ydotool ist nicht erreichbar")
        if self._cancelled(cancel):
            return InsertionResult(True, False, "Kopiert, Einfügen wurde abgebrochen")
        # The status overlay is hidden immediately before this worker starts.
        # Give KWin enough time to restore the previously active window and
        # clipboard consumers enough time to observe the new selection.
        self._sleep(CLIPBOARD_SETTLE_SECONDS)
        if self._cancelled(cancel):
            return InsertionResult(True, False, "Kopiert, Einfügen wurde abgebrochen")
        env = dict(self._environ)
        env["YDOTOOL_SOCKET"] = str(ydotool_socket)
        try:
            result = self._runner(
                ["ydotool", "key", "42:1", "110:1", "110:0", "42:0"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            self._release_insert_keys(env)
            return InsertionResult(True, False, "Kopiert, aber Einfügen ist fehlgeschlagen")
        if result.returncode != 0:
            # Best effort releases in case a daemon accepted only part of the sequence.
            self._release_insert_keys(env)
            return InsertionResult(True, False, "Kopiert, aber Einfügen ist fehlgeschlagen")
        return InsertionResult(True, True, "Einfügen gesendet")

    def capture_active_window(self) -> str | None:
        """Return KWin's active-window UUID, or ``None`` without a safe token."""

        with self._operation_lock:
            return self._capture_active_window_unlocked()

    def _capture_active_window_unlocked(self) -> str | None:
        try:
            result = self._runner(
                ["kdotool", "getactivewindow"],
                capture_output=True,
                text=True,
                timeout=KWIN_QUERY_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        token = result.stdout.strip() if result.returncode == 0 else ""
        return token if _KWIN_WINDOW_ID.fullmatch(token) else None

    def revise(
        self,
        previous: str,
        current: str,
        *,
        expected_window: str,
        cancel: Any | None = None,
    ) -> RevisionResult:
        """Replace only this session's changed suffix in the active target.

        The replacement text is copied before any destructive key is emitted.
        A failed ydotool call is conservatively reported as an unknown field
        state because the daemon may have accepted a prefix of the sequence.
        """

        with self._operation_lock:
            if self._cancelled(cancel):
                return RevisionResult(
                    False,
                    False,
                    True,
                    "Live-Einfügung wurde abgebrochen",
                    cancelled=True,
                )
            # The controller has just hidden its overlay. Give KWin time to
            # restore the prior active surface before validating the token.
            self._sleep(CLIPBOARD_SETTLE_SECONDS)
            if self._cancelled(cancel):
                return RevisionResult(
                    False,
                    False,
                    True,
                    "Live-Einfügung wurde abgebrochen",
                    cancelled=True,
                )
            if self._capture_active_window_unlocked() != expected_window:
                return RevisionResult(
                    False,
                    False,
                    True,
                    "Das aktive Fenster hat sich geändert",
                )
            plan = plan_revision(previous, current)
            if not plan.delete_graphemes and not plan.insert_text:
                return RevisionResult(False, True, True, "Live-Text ist unverändert")
            if plan.delete_graphemes > MAX_REVISION_GRAPHEMES:
                recovery = self._copy_unlocked(current) if current else None
                return RevisionResult(
                    bool(recovery and recovery.copied),
                    False,
                    True,
                    "Die Live-Korrektur ist zu groß",
                )
            if not _backspace_suffix_is_portable(previous, plan.delete_graphemes):
                recovery = self._copy_unlocked(current) if current else None
                return RevisionResult(
                    bool(recovery and recovery.copied),
                    False,
                    True,
                    "Die Live-Korrektur enthält nicht sicher löschbare Unicode-Zeichen",
                )
            copied = False
            if plan.insert_text:
                copy_result = self._copy_unlocked(plan.insert_text)
                if not copy_result.copied:
                    return RevisionResult(False, False, True, copy_result.message)
                copied = True
            elif plan.delete_graphemes and current:
                # Pure deletion needs no paste, but keeping the complete new
                # hypothesis on the clipboard provides a recovery path if a
                # target consumes only part of the synthetic key sequence.
                copy_result = self._copy_unlocked(current)
                if not copy_result.copied:
                    return RevisionResult(False, False, True, copy_result.message)
                copied = True
            ydotool_socket = self.ensure_daemon()
            if ydotool_socket is None:
                return RevisionResult(
                    copied,
                    False,
                    True,
                    "ydotool ist für die Live-Korrektur nicht erreichbar",
                )
            self._sleep(CLIPBOARD_SETTLE_SECONDS)
            if self._cancelled(cancel):
                return RevisionResult(
                    copied,
                    False,
                    True,
                    "Live-Einfügung wurde abgebrochen",
                    cancelled=True,
                )
            if self._capture_active_window_unlocked() != expected_window:
                return RevisionResult(
                    copied,
                    False,
                    True,
                    "Das aktive Fenster hat sich geändert",
                )
            # Activity, lock, or suspend may have arrived while the external
            # KWin query was running. Keep this check adjacent to the first
            # potentially destructive key event.
            if self._cancelled(cancel):
                return RevisionResult(
                    copied,
                    False,
                    True,
                    "Live-Einfügung wurde abgebrochen",
                    cancelled=True,
                )
            keys = [
                event
                for _ in range(plan.delete_graphemes)
                for event in (f"{KEY_BACKSPACE}:1", f"{KEY_BACKSPACE}:0")
            ]
            if plan.insert_text:
                keys.extend(
                    [
                        f"{KEY_LEFTSHIFT}:1",
                        f"{KEY_INSERT}:1",
                        f"{KEY_INSERT}:0",
                        f"{KEY_LEFTSHIFT}:0",
                    ]
                )
            env = dict(self._environ)
            env["YDOTOOL_SOCKET"] = str(ydotool_socket)
            command = [
                "ydotool",
                "key",
                f"--key-delay={REVISION_KEY_DELAY_MS}",
                *keys,
            ]
            try:
                result = self._runner(
                    command,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                self._release_revision_keys(env)
                return RevisionResult(
                    copied,
                    False,
                    False,
                    "Live-Korrektur ist fehlgeschlagen",
                )
            if result.returncode != 0:
                self._release_revision_keys(env)
                return RevisionResult(
                    copied,
                    False,
                    False,
                    "Live-Korrektur ist fehlgeschlagen",
                )
            if plan.insert_text:
                # ydotool confirms that it handed the events to its daemon,
                # not that the target has already requested the clipboard.
                # Keep the operation lock and the suffix clipboard owner for
                # a short delivery barrier before a final full-text copy can
                # replace it.
                self._sleep(PASTE_DELIVERY_SECONDS)
            return RevisionResult(copied, True, True, "Live-Text aktualisiert")

    def _release_insert_keys(self, env: Mapping[str, str]) -> None:
        try:
            self._runner(
                ["ydotool", "key", "110:0", "42:0"],
                env=dict(env),
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _release_revision_keys(self, env: Mapping[str, str]) -> None:
        try:
            self._runner(
                [
                    "ydotool",
                    "key",
                    f"{KEY_BACKSPACE}:0",
                    f"{KEY_INSERT}:0",
                    f"{KEY_LEFTSHIFT}:0",
                ],
                env=dict(env),
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


__all__ = [
    "InsertionBackend",
    "InsertionResult",
    "RevisionPlan",
    "RevisionResult",
    "grapheme_clusters",
    "plan_revision",
]
