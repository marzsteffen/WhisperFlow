from __future__ import annotations

import os
import signal
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

from .audio import EXPECTED_CHANNELS, EXPECTED_SAMPLE_RATE, EXPECTED_SAMPLE_WIDTH

PCM_BYTES_PER_FRAME = EXPECTED_CHANNELS * EXPECTED_SAMPLE_WIDTH
PCM_FRAMES_PER_MILLISECOND = EXPECTED_SAMPLE_RATE // 1000
MAX_RECORDING_SECONDS = 300


@dataclass(frozen=True, slots=True)
class PcmSnapshot:
    """An immutable, sample-aligned range copied from the active recording."""

    pcm: bytes
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class WavSnapshot:
    """A private WAV containing one immutable range of the active recording."""

    path: Path
    start_frame: int
    end_frame: int
    start_ms: int
    end_ms: int

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def window_start_ms(self) -> int:
        return self.start_ms

    @property
    def window_end_ms(self) -> int:
        return self.end_ms


def _frame_to_ms(frame: int) -> float:
    return frame * 1000.0 / EXPECTED_SAMPLE_RATE


def _millisecond_cursor(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} muss eine ganze Millisekundenzahl sein")
    if value < 0:
        raise ValueError(f"{name} darf nicht negativ sein")
    return value


def _write_private_wav(path: Path, pcm: bytes) -> None:
    """Create a new 0600 mono PCM WAV without ever following an existing path."""

    if len(pcm) % PCM_BYTES_PER_FRAME:
        raise ValueError("PCM-Daten enden in einem unvollständigen Sample")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            with wave.open(output, "wb") as recording:
                recording.setnchannels(EXPECTED_CHANNELS)
                recording.setsampwidth(EXPECTED_SAMPLE_WIDTH)
                recording.setframerate(EXPECTED_SAMPLE_RATE)
                recording.writeframes(pcm)
    except Exception:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class Recorder(QObject):
    started = pyqtSignal()
    finished = pyqtSignal(str, float, bool)
    discarded = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, runtime_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.runtime_dir = runtime_dir
        self.audio_dir = runtime_dir / "audio"
        self.audio_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Do not rely on an existing directory's previous permissions. The
        # runtime location contains microphone audio and must remain private.
        try:
            self.audio_dir.chmod(0o700)
        except OSError:
            pass
        self._process: QProcess | None = None
        self._path: Path | None = None
        self._started_at = 0.0
        self._stop_requested = False
        self._discard = False
        self._auto_limit = False
        self._done = False
        self._pcm_lock = threading.RLock()
        self._pcm = bytearray()
        self._pending_pcm_byte = b""
        self._max_pcm_bytes = 0
        self._snapshot_lock = threading.RLock()
        self._snapshot_paths: set[Path] = set()
        self._max_timer = QTimer(self)
        self._max_timer.setSingleShot(True)
        self._max_timer.timeout.connect(self._on_limit)
        self._escalation_timer = QTimer(self)
        self._escalation_timer.setSingleShot(True)
        self._escalation_timer.timeout.connect(self._terminate)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill)

    @property
    def active(self) -> bool:
        return self._process is not None and not self._done

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def recorded_frames(self) -> int:
        with self._pcm_lock:
            return len(self._pcm) // PCM_BYTES_PER_FRAME

    @property
    def recorded_duration_ms(self) -> float:
        return _frame_to_ms(self.recorded_frames)

    def start(self, microphone_id: str, max_duration_s: int) -> None:
        if self.active:
            raise RuntimeError("Eine Aufnahme läuft bereits")
        if (
            isinstance(max_duration_s, bool)
            or not isinstance(max_duration_s, int)
            or not 1 <= max_duration_s <= MAX_RECORDING_SECONDS
        ):
            raise ValueError(
                f"max_duration_s muss zwischen 1 und {MAX_RECORDING_SECONDS} liegen"
            )
        self.cleanup_stale()
        self._done = False
        self._stop_requested = False
        self._discard = False
        self._auto_limit = False
        self._started_at = time.monotonic()
        self._path = self.audio_dir / f"recording-{uuid.uuid4().hex}.wav"
        with self._pcm_lock:
            self._pcm.clear()
            self._pending_pcm_byte = b""
            self._max_pcm_bytes = max_duration_s * EXPECTED_SAMPLE_RATE * PCM_BYTES_PER_FRAME

        process = QProcess(self)
        self._process = process
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._drain_stdout)
        process.readyReadStandardError.connect(lambda: bytes(process.readAllStandardError()))
        process.started.connect(self._on_started)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        process.setProgram("pw-record")
        process.setArguments(
            [
                "--media-category",
                "Capture",
                "--media-role",
                "Communication",
                "--target",
                microphone_id,
                "--rate",
                str(EXPECTED_SAMPLE_RATE),
                "--channels",
                str(EXPECTED_CHANNELS),
                "--channel-map",
                "MONO",
                "--format",
                "s16",
                "--raw",
                "--sample-count",
                str(max_duration_s * EXPECTED_SAMPLE_RATE),
                "-",
            ]
        )
        process.start()
        self._max_timer.start(max_duration_s * 1000 + 250)

    def stop(self, *, discard: bool = False, automatic_limit: bool = False) -> None:
        if not self.active:
            if discard:
                self._clear_pcm()
                self._delete_audio()
                self._delete_all_snapshots()
            return
        self._stop_requested = True
        self._discard = self._discard or discard
        self._auto_limit = self._auto_limit or automatic_limit
        process = self._process
        if process is None:
            return
        if process.state() == QProcess.ProcessState.Starting:
            return
        self._send_signal(signal.SIGINT)
        self._escalation_timer.start(1000)

    def snapshot_pcm(
        self,
        start_ms: int = 0,
        *,
        end_ms: int | None = None,
    ) -> PcmSnapshot:
        """Copy a sample-aligned range of the currently buffered raw audio.

        The returned bytes cannot change when recording continues. ``start_ms``
        and ``end_ms`` in the result describe the actual included frame
        boundaries and are therefore safe to use as the next cursor.
        """

        requested_start = _millisecond_cursor(start_ms, "start_ms")
        requested_end = (
            None if end_ms is None else _millisecond_cursor(end_ms, "end_ms")
        )
        if requested_end is not None and requested_end < requested_start:
            raise ValueError("end_ms darf nicht vor start_ms liegen")

        with self._pcm_lock:
            available_frames = len(self._pcm) // PCM_BYTES_PER_FRAME
            # Live timestamps are integral milliseconds. Restrict snapshots to
            # the corresponding 16-frame boundaries instead of rounding a
            # partial millisecond into a cursor the ledger cannot represent.
            available_ms = available_frames // PCM_FRAMES_PER_MILLISECOND
            actual_start_ms = min(requested_start, available_ms)
            actual_end_ms = (
                available_ms
                if requested_end is None
                else min(max(requested_end, actual_start_ms), available_ms)
            )
            start_frame = actual_start_ms * PCM_FRAMES_PER_MILLISECOND
            end_frame = actual_end_ms * PCM_FRAMES_PER_MILLISECOND
            begin = start_frame * PCM_BYTES_PER_FRAME
            end = end_frame * PCM_BYTES_PER_FRAME
            pcm = bytes(self._pcm[begin:end])
        return PcmSnapshot(
            pcm=pcm,
            start_frame=start_frame,
            end_frame=end_frame,
            start_ms=actual_start_ms,
            end_ms=actual_end_ms,
        )

    def snapshot_wav(
        self,
        start_ms: int = 0,
        *,
        end_ms: int | None = None,
    ) -> WavSnapshot | None:
        """Write one immutable buffered range to a private runtime WAV.

        No file is created for an empty range. Call :meth:`delete_snapshot`
        when the consumer is done; shutdown and all abort paths are a second
        cleanup boundary.
        """

        # Keep creation and registration under the same lock as lifecycle
        # cleanup. A worker cannot otherwise finish creating a snapshot just
        # after shutdown or a discard has swept the directory.
        with self._snapshot_lock:
            pcm_snapshot = self.snapshot_pcm(start_ms, end_ms=end_ms)
            if not pcm_snapshot.pcm:
                return None
            path = self.audio_dir / f"snapshot-{uuid.uuid4().hex}.wav"
            _write_private_wav(path, pcm_snapshot.pcm)
            self._snapshot_paths.add(path)
        return WavSnapshot(
            path=path,
            start_frame=pcm_snapshot.start_frame,
            end_frame=pcm_snapshot.end_frame,
            start_ms=pcm_snapshot.start_ms,
            end_ms=pcm_snapshot.end_ms,
        )

    def delete_snapshot(self, snapshot: WavSnapshot | str | os.PathLike[str]) -> None:
        """Remove a snapshot created by this recorder, never an arbitrary path."""

        candidate = snapshot.path if isinstance(snapshot, WavSnapshot) else Path(snapshot)
        with self._snapshot_lock:
            if candidate not in self._snapshot_paths:
                return
            self._snapshot_paths.discard(candidate)
            self._unlink_private_file(candidate)

    def _drain_stdout(self) -> None:
        process = self._process
        if process is None:
            return
        chunk = bytes(process.readAllStandardOutput())
        if not chunk:
            return
        with self._pcm_lock:
            data = self._pending_pcm_byte + chunk
            complete_length = len(data) - (len(data) % PCM_BYTES_PER_FRAME)
            remaining = max(0, self._max_pcm_bytes - len(self._pcm))
            accepted = min(complete_length, remaining - (remaining % PCM_BYTES_PER_FRAME))
            if accepted:
                self._pcm.extend(data[:accepted])
            if accepted == complete_length and complete_length < len(data) and remaining > accepted:
                self._pending_pcm_byte = data[complete_length:]
            else:
                # Drop incomplete or over-limit data. Never retain half a
                # sample once the configured recording capacity is full.
                self._pending_pcm_byte = b""

    def _on_started(self) -> None:
        self.started.emit()
        if self._stop_requested:
            self._send_signal(signal.SIGINT)
            self._escalation_timer.start(1000)

    def _on_limit(self) -> None:
        self.stop(automatic_limit=True)

    def _send_signal(self, sig: signal.Signals) -> None:
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return
        try:
            os.kill(int(process.processId()), sig)
        except (OSError, ValueError):
            pass

    def _terminate(self) -> None:
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return
        process.terminate()
        self._kill_timer.start(1000)

    def _kill(self) -> None:
        process = self._process
        if process is not None and process.state() != QProcess.ProcessState.NotRunning:
            process.kill()

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if self._done or error == QProcess.ProcessError.Crashed:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self._complete_failure("pw-record konnte nicht gestartet werden")

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._done:
            return
        # QProcess may announce completion before the last readyRead signal is
        # dispatched, so consume the pipe once more before freezing the WAV.
        self._drain_stdout()
        self._done = True
        self._stop_timers()
        duration = max(0.0, time.monotonic() - self._started_at)
        path = self._path
        # With --sample-count, reaching the configured maximum can make
        # pw-record exit with either 0 or 1 (the latter is observable with
        # PipeWire 1.4.9) before the slightly delayed Qt guard timer fires.
        # A completely filled, capped buffer is authoritative evidence that
        # the limit was reached; a shorter non-zero exit remains an error.
        with self._pcm_lock:
            reached_sample_limit = (
                self._max_pcm_bytes > 0 and len(self._pcm) >= self._max_pcm_bytes
            )
        if not self._stop_requested and (exit_code == 0 or reached_sample_limit):
            self._auto_limit = True
        requested = self._stop_requested or self._auto_limit
        self._dispose_process()
        if self._discard:
            self._clear_pcm()
            self._delete_audio()
            self._delete_all_snapshots()
            self.discarded.emit()
            return
        if exit_code != 0 and not requested:
            self._clear_pcm()
            self._delete_audio()
            self._delete_all_snapshots()
            self.failed.emit("pw-record wurde unerwartet beendet")
            return
        if path is None:
            self._clear_pcm()
            self._delete_audio()
            self._delete_all_snapshots()
            self.failed.emit("Die Aufnahme hat keinen Zieldateinamen erzeugt")
            return
        with self._pcm_lock:
            pcm = bytes(self._pcm)
            self._pcm.clear()
            self._pending_pcm_byte = b""
        try:
            _write_private_wav(path, pcm)
        except Exception as exc:
            self._delete_audio()
            self._delete_all_snapshots()
            self.failed.emit(f"Die Aufnahme konnte nicht gespeichert werden: {exc}")
            return
        self.finished.emit(str(path), duration, self._auto_limit)

    def _complete_failure(self, message: str) -> None:
        if self._done:
            return
        self._done = True
        self._stop_timers()
        self._dispose_process()
        self._clear_pcm()
        self._delete_audio()
        self._delete_all_snapshots()
        self.failed.emit(message)

    def _stop_timers(self) -> None:
        self._max_timer.stop()
        self._escalation_timer.stop()
        self._kill_timer.stop()

    def _dispose_process(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.deleteLater()

    def _clear_pcm(self) -> None:
        with self._pcm_lock:
            self._pcm.clear()
            self._pending_pcm_byte = b""
            self._max_pcm_bytes = 0

    @staticmethod
    def _unlink_private_file(path: Path) -> None:
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass

    def _delete_audio(self) -> None:
        path, self._path = self._path, None
        if path is not None:
            self._unlink_private_file(path)

    def _delete_all_snapshots(self) -> None:
        with self._snapshot_lock:
            paths = tuple(self._snapshot_paths)
            self._snapshot_paths.clear()
            for path in paths:
                self._unlink_private_file(path)

    def take_path(self) -> Path | None:
        path, self._path = self._path, None
        return path

    def cleanup_stale(self) -> None:
        self._delete_all_snapshots()
        try:
            for pattern in ("recording-*.wav", "snapshot-*.wav"):
                for path in self.audio_dir.glob(pattern):
                    self._unlink_private_file(path)
        except OSError:
            pass

    def shutdown(self) -> None:
        if self.active:
            self.stop(discard=True)
            process = self._process
            if process is not None and not process.waitForFinished(2500):
                process.kill()
                process.waitForFinished(1000)
        self._clear_pcm()
        self._delete_audio()
        self._delete_all_snapshots()
        self.cleanup_stale()


__all__ = [
    "MAX_RECORDING_SECONDS",
    "PCM_BYTES_PER_FRAME",
    "PCM_FRAMES_PER_MILLISECOND",
    "PcmSnapshot",
    "Recorder",
    "WavSnapshot",
]
