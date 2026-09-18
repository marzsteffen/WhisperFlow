"""WAV validation and guaranteed cleanup for transient recordings."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import wave
from array import array
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

EXPECTED_SAMPLE_RATE = 16_000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2
DEFAULT_MIN_DURATION_MS = 350
DEFAULT_SILENCE_THRESHOLD_DBFS = -50.0


class AudioValidationError(ValueError):
    """A recording is missing, malformed, or has an unsupported format."""


class AudioTooShortError(AudioValidationError):
    """A valid WAV contains too little audio to transcribe."""


class SilentAudioError(AudioValidationError):
    """A valid WAV is below the configured RMS threshold."""


class RuntimeDirectoryError(RuntimeError):
    """No safe per-user runtime directory is available."""


@dataclass(frozen=True, slots=True)
class WavMetrics:
    frames: int
    duration_ms: float
    rms_dbfs: float
    sample_rate: int = EXPECTED_SAMPLE_RATE
    channels: int = EXPECTED_CHANNELS
    sample_width: int = EXPECTED_SAMPLE_WIDTH


# A descriptive alias retained for callers that regard the result as metadata.
AudioInfo = WavMetrics


def _pcm16_rms_dbfs(data: bytes) -> float:
    if not data:
        return float("-inf")
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return float("-inf")
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    if mean_square <= 0:
        return float("-inf")
    # A signed PCM16 signal is conventionally measured against 32768 full
    # scale, including the representable -32768 endpoint.
    return 20.0 * math.log10(math.sqrt(mean_square) / 32768.0)


def validate_wav(
    path: str | os.PathLike[str],
    *,
    min_duration_ms: int = DEFAULT_MIN_DURATION_MS,
    silence_threshold_dbfs: float = DEFAULT_SILENCE_THRESHOLD_DBFS,
) -> WavMetrics:
    """Validate a mono, 16 kHz, signed-16-bit PCM WAV and measure its RMS.

    :raises AudioTooShortError: for recordings shorter than the configured
        minimum.
    :raises SilentAudioError: when RMS is lower than the configured threshold.
    :raises AudioValidationError: for unreadable, truncated, compressed, or
        incorrectly formatted files.
    """

    if isinstance(min_duration_ms, bool) or not isinstance(min_duration_ms, int) or min_duration_ms < 0:
        raise ValueError("min_duration_ms must be a non-negative integer")
    if isinstance(silence_threshold_dbfs, bool) or not isinstance(
        silence_threshold_dbfs, (int, float)
    ):
        raise TypeError("silence_threshold_dbfs must be numeric")
    if not math.isfinite(float(silence_threshold_dbfs)) or not (
        -200.0 <= float(silence_threshold_dbfs) <= 0.0
    ):
        raise ValueError("silence_threshold_dbfs must be between -200 and 0")

    wav_path = Path(path)
    try:
        with wave.open(str(wav_path), "rb") as recording:
            channels = recording.getnchannels()
            sample_width = recording.getsampwidth()
            sample_rate = recording.getframerate()
            compression = recording.getcomptype()
            frames = recording.getnframes()
            if channels != EXPECTED_CHANNELS:
                raise AudioValidationError(
                    f"Aufnahme muss mono sein (gefunden: {channels} Kanäle)"
                )
            if sample_rate != EXPECTED_SAMPLE_RATE:
                raise AudioValidationError(
                    f"Aufnahme muss {EXPECTED_SAMPLE_RATE} Hz haben (gefunden: {sample_rate} Hz)"
                )
            if sample_width != EXPECTED_SAMPLE_WIDTH:
                raise AudioValidationError(
                    "Aufnahme muss 16-Bit-PCM verwenden "
                    f"(gefunden: {sample_width * 8} Bit)"
                )
            if compression != "NONE":
                raise AudioValidationError("Aufnahme muss unkomprimiertes PCM enthalten")
            pcm = recording.readframes(frames)
    except AudioValidationError:
        raise
    except (FileNotFoundError, PermissionError, OSError, EOFError, wave.Error) as exc:
        raise AudioValidationError(f"WAV-Datei kann nicht gelesen werden: {exc}") from exc

    expected_bytes = frames * EXPECTED_CHANNELS * EXPECTED_SAMPLE_WIDTH
    if len(pcm) != expected_bytes:
        raise AudioValidationError(
            f"WAV-Datei ist unvollständig ({len(pcm)} statt {expected_bytes} Audiodatenbytes)"
        )

    duration_ms = frames * 1000.0 / EXPECTED_SAMPLE_RATE
    if duration_ms < min_duration_ms:
        raise AudioTooShortError(
            f"Aufnahme ist zu kurz ({duration_ms:.0f} ms; mindestens {min_duration_ms} ms)"
        )

    rms_dbfs = _pcm16_rms_dbfs(pcm)
    if rms_dbfs < float(silence_threshold_dbfs):
        rendered = "-inf" if math.isinf(rms_dbfs) else f"{rms_dbfs:.1f}"
        raise SilentAudioError(
            f"Aufnahme ist zu leise ({rendered} dBFS; Schwelle {float(silence_threshold_dbfs):.1f} dBFS)"
        )
    return WavMetrics(frames=frames, duration_ms=duration_ms, rms_dbfs=rms_dbfs)


def get_runtime_dir() -> Path:
    """Return the app's private per-user runtime directory."""

    systemd_runtime = os.environ.get("RUNTIME_DIRECTORY")
    if systemd_runtime:
        # systemd may export multiple colon-separated RuntimeDirectory values.
        candidate = systemd_runtime.split(":", 1)[0]
        if os.path.isabs(candidate):
            return Path(candidate)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime and os.path.isabs(xdg_runtime):
        return Path(xdg_runtime) / "local-dictation"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path(tempfile.gettempdir())
        return root / "WhisperFlow" / "runtime"
    raise RuntimeDirectoryError(
        "Weder RUNTIME_DIRECTORY noch ein absolutes XDG_RUNTIME_DIR ist gesetzt"
    )


def cleanup_audio(path: str | os.PathLike[str]) -> None:
    """Best-effort removal used by every recording completion path."""

    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        # Callers can invoke this safely from shutdown/finally paths.  A private
        # systemd RuntimeDirectory provides the final cleanup boundary.
        pass


@contextmanager
def temporary_wav_path(
    runtime_dir: str | os.PathLike[str] | None = None,
) -> Iterator[Path]:
    """Yield a private runtime WAV path and always remove it afterwards."""

    directory = Path(runtime_dir) if runtime_dir is not None else get_runtime_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="recording-", suffix=".wav", dir=directory)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    path = Path(name)
    try:
        yield path
    finally:
        cleanup_audio(path)


__all__ = [
    "DEFAULT_MIN_DURATION_MS",
    "DEFAULT_SILENCE_THRESHOLD_DBFS",
    "EXPECTED_CHANNELS",
    "EXPECTED_SAMPLE_RATE",
    "EXPECTED_SAMPLE_WIDTH",
    "AudioInfo",
    "AudioTooShortError",
    "AudioValidationError",
    "RuntimeDirectoryError",
    "SilentAudioError",
    "WavMetrics",
    "cleanup_audio",
    "get_runtime_dir",
    "temporary_wav_path",
    "validate_wav",
]
