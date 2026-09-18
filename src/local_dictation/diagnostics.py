"""Private, bounded diagnostic bundles for explicitly retained dictations.

The archive deliberately keeps transcript contents in ``transcript.txt`` only.
It never logs transcript text and never duplicates it in JSON metadata.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import get_data_dir

DIAGNOSTICS_SCHEMA_VERSION = 2
DEFAULT_MAX_ENTRIES = 20
MIN_MAX_ENTRIES = 1
MAX_MAX_ENTRIES = 500

AUDIO_FILENAME = "audio.wav"
METADATA_FILENAME = "metadata.json"
TRANSCRIPT_FILENAME = "transcript.txt"
INSERTION_FILENAME = "insertion.json"

_SESSION_ID_RE = re.compile(
    r"^dictation-(?P<timestamp>\d{8}T\d{6}\.\d{6}Z)-(?P<nonce>[0-9a-f]{12})$"
)


class DiagnosticsError(RuntimeError):
    """A diagnostic bundle could not be created, completed, or removed."""


class UnsafeDiagnosticsDirectory(DiagnosticsError):
    """A diagnostics path failed ownership, type, or permission checks."""


class UnknownDiagnosticSession(DiagnosticsError):
    """A session handle does not belong to this archive or no longer exists."""


@dataclass(frozen=True, slots=True)
class DiagnosticSession:
    """Opaque handle returned by :meth:`DiagnosticsArchive.archive_audio`."""

    session_id: str
    directory: Path
    created_at: str
    audio_bytes: int

    @property
    def path(self) -> Path:
        """Alias useful to callers presenting the bundle location."""

        return self.directory


def get_diagnostics_dir() -> Path:
    """Return the persistent private archive below the current XDG data home."""

    return get_data_dir() / "diagnostics"


def _validate_max_entries(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_MAX_ENTRIES <= value <= MAX_MAX_ENTRIES
    ):
        raise ValueError(
            f"max_entries must be an integer between {MIN_MAX_ENTRIES} and {MAX_MAX_ENTRIES}"
        )
    return value


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # The data is already atomically named. Some filesystems do not allow
        # fsync on directories, so this is a durability enhancement only.
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_copy(source: Path, destination: Path) -> int:
    source_descriptor = -1
    destination_descriptor = -1
    temporary: Path | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, flags)
        source_info = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_info.st_mode):
            raise DiagnosticsError("Audio source must be a regular file")

        destination_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(temporary_name)
        os.fchmod(destination_descriptor, 0o600)
        with os.fdopen(source_descriptor, "rb") as source_handle:
            source_descriptor = -1
            with os.fdopen(destination_descriptor, "wb") as destination_handle:
                destination_descriptor = -1
                shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())

        copied_bytes = temporary.stat(follow_symlinks=False).st_size
        os.replace(temporary, destination)
        temporary = None
        os.chmod(destination, 0o600, follow_symlinks=False)
        _fsync_directory(destination.parent)
        return copied_bytes
    except OSError as exc:
        raise DiagnosticsError(f"Audio could not be archived: {exc}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _single_line(value: str, *, limit: int = 2000) -> str:
    rendered = "".join(
        " " if unicodedata.category(character) in {"Cc", "Zl", "Zp"} else character
        for character in value
    )
    return rendered.strip()[:limit]


def _duration(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


class DiagnosticsArchive:
    """Create private per-recording bundles with bounded retention.

    ``directory`` is the archive directory itself, not its parent. It must be
    named ``diagnostics`` so :meth:`clear` can never be redirected at a broad
    data directory by a caller mistake.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        selected = Path(directory) if directory is not None else get_diagnostics_dir()
        # abspath normalizes relative components without following symlinks.
        self.directory = Path(os.path.abspath(os.fspath(selected)))
        if self.directory.name != "diagnostics":
            raise ValueError("diagnostics archive directory must be named 'diagnostics'")
        self._max_entries = _validate_max_entries(max_entries)
        self._lock = threading.RLock()
        self._active: set[str] = set()

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def ensure_directory(self) -> Path:
        """Create and validate the private archive root for presentation."""

        with self._lock:
            return self._ensure_root()

    def set_max_entries(self, value: int) -> int:
        """Set retention and immediately prune completed excess bundles."""

        validated = _validate_max_entries(value)
        with self._lock:
            self._max_entries = validated
            self._prune_locked()
        return validated

    @staticmethod
    def _validate_private_directory(path: Path, *, label: str) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as exc:
            raise UnsafeDiagnosticsDirectory(f"{label} is not accessible: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDiagnosticsDirectory(f"{label} is not a real directory")
        if info.st_uid != os.getuid():
            raise UnsafeDiagnosticsDirectory(f"{label} is not owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise UnsafeDiagnosticsDirectory(f"{label} is accessible by other users")
        return info

    def _ensure_root(self) -> Path:
        self.directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            info = self.directory.lstat()
        except OSError as exc:
            raise UnsafeDiagnosticsDirectory(
                f"diagnostics directory is not accessible: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDiagnosticsDirectory("diagnostics path is not a real directory")
        if info.st_uid != os.getuid():
            raise UnsafeDiagnosticsDirectory(
                "diagnostics directory is not owned by the current user"
            )
        try:
            os.chmod(self.directory, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise UnsafeDiagnosticsDirectory(
                f"diagnostics directory cannot be made private: {exc}"
            ) from exc
        self._validate_private_directory(self.directory, label="diagnostics directory")
        return self.directory

    def _existing_root(self) -> Path | None:
        try:
            self.directory.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UnsafeDiagnosticsDirectory(
                f"diagnostics directory is not accessible: {exc}"
            ) from exc
        self._validate_private_directory(self.directory, label="diagnostics directory")
        return self.directory

    @staticmethod
    def _new_session_id() -> tuple[str, str]:
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        identifier_time = now.strftime("%Y%m%dT%H%M%S.%fZ")
        return f"dictation-{identifier_time}-{secrets.token_hex(6)}", created_at

    def archive_audio(self, audio_path: str | os.PathLike[str]) -> DiagnosticSession:
        """Copy one completed recording into a new private session bundle."""

        source = Path(audio_path)
        with self._lock:
            root = self._ensure_root()
            while True:
                session_id, created_at = self._new_session_id()
                session_dir = root / session_id
                try:
                    session_dir.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                break
            os.chmod(session_dir, 0o700, follow_symlinks=False)
            self._active.add(session_id)
            try:
                audio_bytes = _atomic_copy(source, session_dir / AUDIO_FILENAME)
                _atomic_write(session_dir / TRANSCRIPT_FILENAME, b"")
                session = DiagnosticSession(
                    session_id=session_id,
                    directory=session_dir,
                    created_at=created_at,
                    audio_bytes=audio_bytes,
                )
                self._write_metadata(session, status="pending")
                self._prune_locked()
                return session
            except BaseException:
                self._active.discard(session_id)
                try:
                    self._remove_tree(session_dir)
                except (OSError, DiagnosticsError):
                    pass
                raise

    def _session_directory(self, session: DiagnosticSession) -> Path:
        if not isinstance(session, DiagnosticSession):
            raise TypeError("session must be a DiagnosticSession")
        if _SESSION_ID_RE.fullmatch(session.session_id) is None:
            raise UnknownDiagnosticSession("invalid diagnostic session identifier")
        if self._existing_root() is None:
            raise UnknownDiagnosticSession("diagnostics archive no longer exists")
        expected = self.directory / session.session_id
        if session.directory != expected:
            raise UnknownDiagnosticSession("diagnostic session belongs to another archive")
        self._validate_private_directory(expected, label="diagnostic session")
        return expected

    def _metadata(
        self,
        session: DiagnosticSession,
        *,
        status: str,
        transcript_bytes: int = 0,
        audio_seconds: float | None = None,
        inference_seconds: float | None = None,
        error: str | BaseException | None = None,
        stage: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
            "session_id": session.session_id,
            "status": status,
            "created_at": session.created_at,
            "audio": {
                "filename": AUDIO_FILENAME,
                "bytes": session.audio_bytes,
            },
            "transcript": {
                "filename": TRANSCRIPT_FILENAME,
                "bytes": transcript_bytes,
            },
        }
        if status != "pending":
            metadata["completed_at"] = datetime.now(UTC).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
        timings = {
            key: value
            for key, value in (
                ("audio_seconds", audio_seconds),
                ("inference_seconds", inference_seconds),
            )
            if value is not None
        }
        if timings:
            metadata["timings"] = timings
        if error is not None:
            error_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
            metadata["error"] = {
                "type": error_type,
                "message": _single_line(str(error)),
            }
            if stage:
                metadata["error"]["stage"] = _single_line(stage, limit=100)
        return metadata

    def _write_metadata(self, session: DiagnosticSession, **values: Any) -> None:
        payload = json.dumps(
            self._metadata(session, **values),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _atomic_write(session.directory / METADATA_FILENAME, payload)

    def finish_success(
        self,
        session: DiagnosticSession,
        transcript: str,
        *,
        audio_seconds: float | None = None,
        inference_seconds: float | None = None,
    ) -> Path:
        """Atomically store a successful transcript and finalize its metadata."""

        with self._lock:
            try:
                if not isinstance(transcript, str):
                    raise TypeError("transcript must be a string")
                audio_duration = _duration(audio_seconds, "audio_seconds")
                inference_duration = _duration(inference_seconds, "inference_seconds")
                encoded = transcript.encode("utf-8")
                directory = self._session_directory(session)
                _atomic_write(directory / TRANSCRIPT_FILENAME, encoded)
                self._write_metadata(
                    session,
                    status="success",
                    transcript_bytes=len(encoded),
                    audio_seconds=audio_duration,
                    inference_seconds=inference_duration,
                )
            except BaseException:
                if isinstance(session, DiagnosticSession):
                    self._active.discard(session.session_id)
                try:
                    self._prune_locked()
                except (OSError, DiagnosticsError):
                    pass
                raise
            self._active.discard(session.session_id)
            self._prune_locked()
            return directory

    def finish_error(
        self,
        session: DiagnosticSession,
        error: str | BaseException,
        *,
        stage: str | None = None,
        audio_seconds: float | None = None,
        inference_seconds: float | None = None,
    ) -> Path:
        """Finalize a failed session without writing transcript content."""

        with self._lock:
            try:
                if not isinstance(error, (str, BaseException)):
                    raise TypeError("error must be a string or exception")
                audio_duration = _duration(audio_seconds, "audio_seconds")
                inference_duration = _duration(inference_seconds, "inference_seconds")
                directory = self._session_directory(session)
                # A failed retry must not leave transcript contents from an earlier
                # partial completion attempt in the bundle.
                _atomic_write(directory / TRANSCRIPT_FILENAME, b"")
                self._write_metadata(
                    session,
                    status="error",
                    error=error,
                    stage=stage,
                    audio_seconds=audio_duration,
                    inference_seconds=inference_duration,
                )
            except BaseException:
                if isinstance(session, DiagnosticSession):
                    self._active.discard(session.session_id)
                try:
                    self._prune_locked()
                except (OSError, DiagnosticsError):
                    pass
                raise
            self._active.discard(session.session_id)
            self._prune_locked()
            return directory

    def record_insertion(
        self,
        session: DiagnosticSession,
        *,
        copied: bool,
        shortcut_sent: bool,
        error: str | None = None,
    ) -> Path:
        """Store the clipboard/insertion outcome without duplicating transcript text."""

        if not isinstance(copied, bool) or not isinstance(shortcut_sent, bool):
            raise TypeError("copied and shortcut_sent must be booleans")
        if shortcut_sent and not copied:
            raise ValueError("shortcut_sent cannot be true when copied is false")
        if error is not None and not isinstance(error, str):
            raise TypeError("error must be a string or None")
        payload: dict[str, Any] = {
            "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
            "recorded_at": datetime.now(UTC).isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "status": "shortcut-sent" if shortcut_sent else "error",
            "copied": copied,
            # ydotool can confirm delivery to its daemon, but Wayland offers no
            # acknowledgement that the focused client consumed the shortcut.
            "shortcut_sent": shortcut_sent,
        }
        if error and not shortcut_sent:
            payload["error"] = {"message": _single_line(error)}
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        with self._lock:
            directory = self._session_directory(session)
            _atomic_write(directory / INSERTION_FILENAME, encoded)
            self._prune_locked()
            return directory

    def _bundle_directories(self) -> list[Path]:
        root = self._existing_root()
        if root is None:
            return []
        bundles: list[Path] = []
        try:
            entries = list(root.iterdir())
        except OSError as exc:
            raise DiagnosticsError(f"diagnostics directory cannot be listed: {exc}") from exc
        for entry in entries:
            if _SESSION_ID_RE.fullmatch(entry.name) is None:
                continue
            try:
                self._validate_private_directory(entry, label="diagnostic session")
            except UnsafeDiagnosticsDirectory:
                # Retention must never follow or remove an untrusted entry.
                continue
            bundles.append(entry)
        return sorted(bundles, key=lambda path: path.name)

    def _prune_locked(self) -> int:
        bundles = self._bundle_directories()
        excess = max(0, len(bundles) - self._max_entries)
        removed = 0
        for bundle in bundles:
            if excess <= 0:
                break
            if bundle.name in self._active:
                continue
            self._validate_tree(bundle)
            self._remove_tree(bundle)
            excess -= 1
            removed += 1
        return removed

    def prune(self) -> int:
        """Remove the oldest completed bundles above the retention limit."""

        with self._lock:
            return self._prune_locked()

    @classmethod
    def _validate_tree(cls, path: Path) -> None:
        cls._validate_private_directory(path, label="diagnostics directory")
        try:
            entries = list(path.iterdir())
        except OSError as exc:
            raise UnsafeDiagnosticsDirectory(f"diagnostics tree cannot be listed: {exc}") from exc
        for entry in entries:
            try:
                info = entry.lstat()
            except OSError as exc:
                raise UnsafeDiagnosticsDirectory(
                    f"diagnostics entry is not accessible: {exc}"
                ) from exc
            if info.st_uid != os.getuid():
                raise UnsafeDiagnosticsDirectory("diagnostics entry has a foreign owner")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise UnsafeDiagnosticsDirectory("diagnostics entry is accessible by other users")
            if stat.S_ISDIR(info.st_mode):
                cls._validate_tree(entry)
            elif not stat.S_ISREG(info.st_mode):
                # This explicitly rejects symlinks and all device/socket types.
                raise UnsafeDiagnosticsDirectory("diagnostics tree contains a non-regular entry")

    @classmethod
    def _remove_tree(cls, path: Path) -> None:
        cls._validate_private_directory(path, label="diagnostics directory")
        for entry in list(path.iterdir()):
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode):
                cls._remove_tree(entry)
            elif stat.S_ISREG(info.st_mode):
                entry.unlink()
            else:
                raise UnsafeDiagnosticsDirectory("refusing to remove a non-regular entry")
        path.rmdir()

    def clear(self) -> int:
        """Clear a securely validated archive while retaining its root.

        The complete tree is checked before the first deletion. A symlink at
        the archive root or anywhere below it therefore aborts the operation.
        """

        with self._lock:
            root = self._existing_root()
            if root is None:
                self._active.clear()
                return 0
            self._validate_tree(root)
            entries = list(root.iterdir())
            for entry in entries:
                info = entry.lstat()
                if stat.S_ISDIR(info.st_mode):
                    self._remove_tree(entry)
                else:
                    entry.unlink()
            self._active.clear()
            _fsync_directory(root)
            return len(entries)


__all__ = [
    "AUDIO_FILENAME",
    "DEFAULT_MAX_ENTRIES",
    "DIAGNOSTICS_SCHEMA_VERSION",
    "INSERTION_FILENAME",
    "MAX_MAX_ENTRIES",
    "METADATA_FILENAME",
    "MIN_MAX_ENTRIES",
    "TRANSCRIPT_FILENAME",
    "DiagnosticSession",
    "DiagnosticsArchive",
    "DiagnosticsError",
    "UnknownDiagnosticSession",
    "UnsafeDiagnosticsDirectory",
    "get_diagnostics_dir",
]
