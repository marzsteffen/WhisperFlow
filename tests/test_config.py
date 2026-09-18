from __future__ import annotations

import json
import stat

import pytest

from local_dictation.config import (
    DEFAULT_INITIAL_PROMPT,
    DEFAULT_MICROPHONE_ID,
    LEGACY_DEFAULT_INITIAL_PROMPT,
    PREVIOUS_DEFAULT_INITIAL_PROMPT,
    SCHEMA4_DEFAULT_INITIAL_PROMPT,
    SCHEMA_VERSION,
    ConfigError,
    UnsupportedConfigVersion,
    default_config,
    get_config_path,
    get_models_dir,
    load_config,
    parse_config,
    save_config,
)


def test_defaults_use_xdg_paths_and_exact_schema(monkeypatch, tmp_path):
    config_home = tmp_path / "cfg"
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    assert get_config_path() == config_home / "local-dictation" / "config.json"
    assert get_models_dir() == data_home / "local-dictation" / "models"
    assert default_config().to_dict() == {
        "schema_version": SCHEMA_VERSION,
        "enabled": True,
        "microphone_id": DEFAULT_MICROPHONE_ID,
        "model_path": str(get_models_dir() / "ggml-large-v3-turbo.bin"),
        "vad_model_path": str(get_models_dir() / "ggml-silero-v6.2.0.bin"),
        "language": "de",
        "trigger": "KEY_RIGHTCTRL",
        "backend": "vulkan",
        "initial_prompt": DEFAULT_INITIAL_PROMPT,
        "recording": {
            "min_duration_ms": 350,
            "max_duration_s": 300,
            "silence_threshold_dbfs": -50.0,
        },
        "live": {
            "enabled": False,
            "direct_insert": False,
            "interval_ms": 1500,
        },
        "diagnostics": {
            "enabled": False,
            "retention_entries": 20,
        },
    }


def test_save_is_atomic_private_and_round_trips(tmp_path):
    path = tmp_path / "nested" / "config.json"
    config = default_config()

    assert save_config(config, path) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".config.json.*.tmp"))
    assert load_config(path) == config
    assert json.loads(path.read_text(encoding="utf-8")) == config.to_dict()


def test_missing_file_returns_defaults(tmp_path):
    assert load_config(tmp_path / "missing.json") == default_config()


def test_migrates_unversioned_flat_recording_settings():
    parsed = parse_config(
        {
            "enabled": False,
            "microphone": "test-source",
            "model": "/models/main.bin",
            "vad_model": "/models/vad.bin",
            "prompt": "Eigennamen: Müller.",
            "min_duration_ms": 420,
            "max_recording_seconds": 42,
            "silence_threshold_db": -44,
        }
    )

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.enabled is False
    assert parsed.microphone_id == "test-source"
    assert parsed.model_path == "/models/main.bin"
    assert parsed.vad_model_path == "/models/vad.bin"
    assert parsed.initial_prompt == "Eigennamen: Müller."
    assert parsed.recording.min_duration_ms == 420
    assert parsed.recording.max_duration_s == 42
    assert parsed.recording.silence_threshold_dbfs == -44.0


def test_default_prompt_is_the_verified_whisper_context_list():
    assert DEFAULT_INITIAL_PROMPT == (
        "CachyOS, KDE Plasma, Wayland, PipeWire, PyQt6, whisper.cpp, Vulkan, "
        "Radeon 860M, Claude, ChatGPT, Codex, Dicio, Eigenpfad, GrapheneOS, "
        "Pixel 8 Pro und Innerkofler Straße."
    )


def test_v1_exact_legacy_default_prompt_is_upgraded():
    raw = default_config().to_dict()
    raw.pop("diagnostics")
    raw.pop("live")
    raw.update(
        schema_version=1,
        initial_prompt=LEGACY_DEFAULT_INITIAL_PROMPT,
    )

    parsed = parse_config(raw)

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.initial_prompt == DEFAULT_INITIAL_PROMPT


def test_v1_custom_prompt_is_preserved_exactly():
    custom = "  Eigennamen: Müller.\nInterne Zeile bleibt erhalten.  "
    raw = default_config().to_dict()
    raw.pop("diagnostics")
    raw.pop("live")
    raw.update(schema_version=1, initial_prompt=custom)

    parsed = parse_config(raw)

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.initial_prompt == custom


def test_v2_adds_disabled_diagnostics_without_changing_existing_values():
    raw = default_config().to_dict()
    raw.pop("diagnostics")
    raw.pop("live")
    raw.update(schema_version=2, initial_prompt="Müller, ACME GmbH.")

    parsed = parse_config(raw)

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.initial_prompt == "Müller, ACME GmbH."
    assert parsed.diagnostics.enabled is False
    assert parsed.diagnostics.retention_entries == 20
    assert parsed.live.enabled is False
    assert parsed.live.direct_insert is False
    assert parsed.live.interval_ms == 1500


def test_v3_adds_disabled_live_mode_and_updates_exact_previous_default():
    raw = default_config().to_dict()
    raw.pop("live")
    raw.update(
        schema_version=3,
        initial_prompt=PREVIOUS_DEFAULT_INITIAL_PROMPT,
    )

    parsed = parse_config(raw)

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.initial_prompt == DEFAULT_INITIAL_PROMPT
    assert parsed.live.enabled is False
    assert parsed.live.direct_insert is False
    assert parsed.live.interval_ms == 1500


def test_v3_custom_prompt_is_preserved_exactly():
    custom = "  Dicio anders geschrieben.\nDiese Leerzeichen bleiben.  "
    raw = default_config().to_dict()
    raw.pop("live")
    raw.update(schema_version=3, initial_prompt=custom)

    parsed = parse_config(raw)

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.initial_prompt == custom
    assert parsed.live.enabled is False
    assert parsed.live.direct_insert is False
    assert parsed.live.interval_ms == 1500


def test_v4_adds_safe_direct_insert_default_and_innerkofler_prompt():
    raw = default_config().to_dict()
    raw["live"].pop("direct_insert")
    raw.update(
        schema_version=4,
        initial_prompt=SCHEMA4_DEFAULT_INITIAL_PROMPT,
    )

    parsed = parse_config(raw)

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.initial_prompt == DEFAULT_INITIAL_PROMPT
    assert parsed.live.enabled is False
    assert parsed.live.direct_insert is False


def test_v4_custom_prompt_is_preserved_exactly():
    custom = "Dicio, Eigenpfad und eigene Innerkofler-Schreibweise."
    raw = default_config().to_dict()
    raw["live"].pop("direct_insert")
    raw.update(schema_version=4, initial_prompt=custom)

    parsed = parse_config(raw)

    assert parsed.initial_prompt == custom
    assert parsed.live.direct_insert is False


def test_future_schema_is_rejected_without_rewriting(tmp_path):
    path = tmp_path / "config.json"
    original = '{"schema_version": 99, "future": true}\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(UnsupportedConfigVersion):
        load_config(path)
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": 1},
        {"backend": "automatic"},
        {"recording": {"min_duration_ms": 0}},
        {"recording": {"min_duration_ms": 2000, "max_duration_s": 1}},
        {"diagnostics": {"enabled": 1}},
        {"diagnostics": {"retention_entries": 0}},
        {"diagnostics": {"retention_entries": 101}},
        {"live": {"enabled": 1}},
        {"live": {"direct_insert": 1}},
        {"live": {"interval_ms": 999}},
        {"live": {"interval_ms": 5001}},
    ],
)
def test_invalid_current_values_are_rejected(change):
    raw = default_config().to_dict()
    raw.update(change)
    with pytest.raises(ConfigError):
        parse_config(raw)
