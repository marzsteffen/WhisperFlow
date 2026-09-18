"""WASAPI recording backend used on Windows."""

from __future__ import annotations

import time
import uuid

from .audio import EXPECTED_CHANNELS, EXPECTED_SAMPLE_RATE
from .recorder import MAX_RECORDING_SECONDS, PCM_BYTES_PER_FRAME, Recorder, _write_private_wav


class WindowsRecorder(Recorder):
    def __init__(self, runtime_dir, parent=None) -> None:
        super().__init__(runtime_dir, parent)
        self._stream = None

    @property
    def active(self) -> bool:
        return self._stream is not None and not self._done

    def start(self, microphone_id: str, max_duration_s: int) -> None:
        if self.active:
            raise RuntimeError("Eine Aufnahme läuft bereits")
        if isinstance(max_duration_s, bool) or not isinstance(max_duration_s, int) or not 1 <= max_duration_s <= MAX_RECORDING_SECONDS:
            raise ValueError(f"max_duration_s muss zwischen 1 und {MAX_RECORDING_SECONDS} liegen")
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Das Windows-Audiomodul sounddevice fehlt") from exc

        self.cleanup_stale()
        self._done = False
        self._discard = False
        self._auto_limit = False
        self._started_at = time.monotonic()
        self._path = self.audio_dir / f"recording-{uuid.uuid4().hex}.wav"
        with self._pcm_lock:
            self._pcm.clear()
            self._pending_pcm_byte = b""
            self._max_pcm_bytes = max_duration_s * EXPECTED_SAMPLE_RATE * PCM_BYTES_PER_FRAME

        def callback(indata, _frames, _time_info, status) -> None:
            if status:
                return
            chunk = bytes(indata)
            with self._pcm_lock:
                remaining = max(0, self._max_pcm_bytes - len(self._pcm))
                accepted = min(len(chunk), remaining)
                accepted -= accepted % PCM_BYTES_PER_FRAME
                if accepted:
                    self._pcm.extend(chunk[:accepted])

        device = int(microphone_id) if str(microphone_id).isdigit() else None
        try:
            self._stream = sd.RawInputStream(
                samplerate=EXPECTED_SAMPLE_RATE,
                channels=EXPECTED_CHANNELS,
                dtype="int16",
                device=device,
                callback=callback,
                blocksize=0,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._delete_audio()
            raise RuntimeError(f"Windows-Mikrofon konnte nicht geöffnet werden: {exc}") from exc
        self.started.emit()
        self._max_timer.start(max_duration_s * 1000)

    def stop(self, *, discard: bool = False, automatic_limit: bool = False) -> None:
        if not self.active:
            if discard:
                self._clear_pcm()
                self._delete_audio()
                self._delete_all_snapshots()
            return
        self._discard = self._discard or discard
        self._auto_limit = self._auto_limit or automatic_limit
        stream, self._stream = self._stream, None
        self._done = True
        self._stop_timers()
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        if self._discard:
            self._clear_pcm()
            self._delete_audio()
            self._delete_all_snapshots()
            self.discarded.emit()
            return
        path = self._path
        with self._pcm_lock:
            pcm = bytes(self._pcm)
            self._pcm.clear()
        if path is None:
            self.failed.emit("Die Aufnahme hat keinen Zieldateinamen erzeugt")
            return
        try:
            _write_private_wav(path, pcm)
        except Exception as exc:
            self._delete_audio()
            self.failed.emit(f"Die Aufnahme konnte nicht gespeichert werden: {exc}")
            return
        self.finished.emit(str(path), max(0.0, time.monotonic() - self._started_at), self._auto_limit)

    def shutdown(self) -> None:
        if self.active:
            self.stop(discard=True)
        self._clear_pcm()
        self._delete_audio()
        self._delete_all_snapshots()
        self.cleanup_stale()


__all__ = ["WindowsRecorder"]
