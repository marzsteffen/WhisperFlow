from __future__ import annotations

import hashlib
import json
import wave
from importlib.resources import as_file, files

import pytest

from local_dictation.audio import validate_wav


def _resources():
    return files("local_dictation").joinpath("resources")


def _metadata():
    return json.loads(_resources().joinpath("fixtures.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture_name", ["warmup", "benchmark"])
def test_audio_fixtures_are_packaged_validated_pcm(fixture_name):
    metadata = _metadata()["fixtures"][fixture_name]
    resource = _resources().joinpath(metadata["resource"])

    with as_file(resource) as path:
        payload = path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == metadata["sha256"]
        with wave.open(str(path), "rb") as audio:
            assert audio.getnchannels() == 1
            assert audio.getframerate() == 16_000
            assert audio.getsampwidth() == 2
            assert audio.getcomptype() == "NONE"
            assert audio.getnframes() == metadata["frames"]
        metrics = validate_wav(path)

    assert metrics.duration_ms / 1000 == pytest.approx(metadata["duration_seconds"])


def test_fixture_durations_match_warmup_and_benchmark_roles():
    fixtures = _metadata()["fixtures"]
    assert 1.0 <= fixtures["warmup"]["duration_seconds"] <= 5.0
    assert 10.0 <= fixtures["benchmark"]["duration_seconds"] <= 15.0


def test_benchmark_source_covers_german_and_technical_terms():
    benchmark = _metadata()["fixtures"]["benchmark"]
    text = benchmark["text"]
    assert all(character in text.casefold() for character in "äöü")
    assert all(
        term in text
        for term in ("Cachy", "K D E", "Wayland", "PipeWire", "Vulkan", "Codex")
    )
    assert benchmark["expected_terms"] == ["Wayland", "Vulkan", "Codex"]
