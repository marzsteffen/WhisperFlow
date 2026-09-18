from __future__ import annotations

import json
import os
import shutil
import time
from importlib.resources import as_file, files
from pathlib import Path

import pytest

from local_dictation.config import default_config
from local_dictation.engine import EngineConfig, WhisperEngine
from local_dictation.model_store import validate_required_models
from local_dictation.text import normalize_transcript

pytestmark = pytest.mark.integration


def _integration_enabled() -> bool:
    return os.environ.get("LOCAL_DICTATION_RUN_INTEGRATION") == "1"


def _model_paths() -> tuple[Path, Path]:
    defaults = default_config()
    return (
        Path(os.environ.get("LOCAL_DICTATION_MODEL_PATH", defaults.model_path)),
        Path(os.environ.get("LOCAL_DICTATION_VAD_MODEL_PATH", defaults.vad_model_path)),
    )


@pytest.mark.skipif(not _integration_enabled(), reason="set LOCAL_DICTATION_RUN_INTEGRATION=1")
def test_real_server_transcribes_german_fixture_with_vulkan(tmp_path):
    if shutil.which("whisper-server") is None:
        pytest.fail("whisper-server is not installed")
    model_path, vad_model_path = _model_paths()
    validate_required_models(model_path, vad_model_path)

    resources = files("local_dictation").joinpath("resources")
    metadata = json.loads(resources.joinpath("fixtures.json").read_text(encoding="utf-8"))
    warmup = resources.joinpath(metadata["fixtures"]["warmup"]["resource"])
    benchmark = resources.joinpath(metadata["fixtures"]["benchmark"]["resource"])
    config = EngineConfig(
        model_path=model_path,
        vad_model_path=vad_model_path,
        runtime_dir=tmp_path,
        backend="vulkan",
    )

    with (
        as_file(warmup) as warmup_path,
        as_file(benchmark) as benchmark_path,
        WhisperEngine(config) as engine,
    ):
        engine.start(warmup_wav=warmup_path)
        started = time.monotonic()
        transcript = normalize_transcript(
            engine.transcribe(
                benchmark_path,
                initial_prompt=default_config().initial_prompt,
            )
        )
        elapsed = time.monotonic() - started

    folded = transcript.casefold()
    assert transcript
    assert any(character in transcript for character in "äöüÄÖÜ")
    assert "." in transcript
    assert all(term in folded for term in ("wayland", "vulkan", "codex"))
    assert elapsed <= 5.0, f"warmer Vulkan-Lauf dauerte {elapsed:.2f} s"
    assert not list(tmp_path.glob("*.wav")), "whisper-server left transient WAV files behind"
