from __future__ import annotations

import math
import os
import wave
from array import array
from pathlib import Path

import pytest

from local_dictation.audio import (
    AudioTooShortError,
    AudioValidationError,
    RuntimeDirectoryError,
    SilentAudioError,
    get_runtime_dir,
    temporary_wav_path,
    validate_wav,
)


def write_wav(path: Path, *, frames: int, amplitude: int, channels: int = 1, rate: int = 16_000):
    samples = array("h", [amplitude] * frames * channels)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


def test_validates_exact_minimum_and_reports_rms(tmp_path):
    path = tmp_path / "valid.wav"
    write_wav(path, frames=5_600, amplitude=2_000)

    metrics = validate_wav(path)

    assert metrics.duration_ms == 350.0
    assert metrics.frames == 5_600
    assert metrics.rms_dbfs == pytest.approx(20 * math.log10(2_000 / 32_768))


def test_rejects_short_and_silent_recordings(tmp_path):
    short = tmp_path / "short.wav"
    write_wav(short, frames=5_599, amplitude=2_000)
    with pytest.raises(AudioTooShortError):
        validate_wav(short)

    silent = tmp_path / "silent.wav"
    write_wav(silent, frames=5_600, amplitude=0)
    with pytest.raises(SilentAudioError):
        validate_wav(silent)


@pytest.mark.parametrize(
    ("channels", "rate"),
    [(2, 16_000), (1, 48_000)],
)
def test_rejects_wrong_capture_format(tmp_path, channels, rate):
    path = tmp_path / "wrong.wav"
    write_wav(path, frames=16_000, amplitude=2_000, channels=channels, rate=rate)
    with pytest.raises(AudioValidationError):
        validate_wav(path)


def test_detects_truncated_pcm(tmp_path):
    path = tmp_path / "truncated.wav"
    write_wav(path, frames=5_600, amplitude=2_000)
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(AudioValidationError, match="unvollständig"):
        validate_wav(path)


def test_temporary_audio_is_private_and_removed_on_error(tmp_path):
    captured = None
    with pytest.raises(RuntimeError), temporary_wav_path(tmp_path) as path:
        captured = path
        assert path.exists()
        assert path.parent == tmp_path
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
        raise RuntimeError("transcription failed")
    assert captured is not None
    assert not captured.exists()


def test_runtime_directory_uses_platform_private_location(monkeypatch, tmp_path):
    monkeypatch.delenv("RUNTIME_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert get_runtime_dir() == tmp_path / "local-dictation"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert get_runtime_dir() == tmp_path / "WhisperFlow" / "runtime"
    else:
        with pytest.raises(RuntimeDirectoryError):
            get_runtime_dir()

