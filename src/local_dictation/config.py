"""Versioned configuration and XDG path handling for local-dictation.

The module intentionally has no GUI or third-party dependencies so the CLI,
service, and setup UI can all use it during early startup.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5
APP_DIRECTORY = "local-dictation"
CONFIG_FILENAME = "config.json"
MAIN_MODEL_FILENAME = "ggml-large-v3-turbo.bin"
VAD_MODEL_FILENAME = "ggml-silero-v6.2.0.bin"

DEFAULT_MICROPHONE_ID = "alsa_input.pci-0000_c4_00.6.HiFi__Mic1__source"
LEGACY_DEFAULT_INITIAL_PROMPT = (
    "Dies ist ein deutsches Diktat. Bevorzugte Schreibweisen: CachyOS, "
    "KDE Plasma, Wayland, PipeWire, PyQt6, whisper.cpp, Vulkan, Radeon 860M, "
    "Claude, ChatGPT und Codex."
)
PREVIOUS_DEFAULT_INITIAL_PROMPT = (
    "CachyOS, KDE Plasma, Wayland, PipeWire, PyQt6, whisper.cpp, Vulkan, Radeon 860M, "
    "Claude, ChatGPT und Codex."
)
SCHEMA4_DEFAULT_INITIAL_PROMPT = (
    "CachyOS, KDE Plasma, Wayland, PipeWire, PyQt6, whisper.cpp, Vulkan, Radeon 860M, "
    "Claude, ChatGPT, Codex, Dicio, Eigenpfad, GrapheneOS und Pixel 8 Pro."
)
DEFAULT_INITIAL_PROMPT = (
    "CachyOS, KDE Plasma, Wayland, PipeWire, PyQt6, whisper.cpp, Vulkan, Radeon 860M, "
    "Claude, ChatGPT, Codex, Dicio, Eigenpfad, GrapheneOS, Pixel 8 Pro und "
    "Innerkofler Straße."
)


class ConfigError(ValueError):
    """The configuration cannot be safely interpreted."""


class UnsupportedConfigVersion(ConfigError):
    """The configuration was written by a newer application version."""


@dataclass(slots=True)
class RecordingConfig:
    min_duration_ms: int = 350
    max_duration_s: int = 300
    silence_threshold_dbfs: float = -50.0


@dataclass(slots=True)
class LiveConfig:
    enabled: bool = False
    direct_insert: bool = False
    interval_ms: int = 1500


@dataclass(slots=True)
class DiagnosticsConfig:
    enabled: bool = False
    retention_entries: int = 20


@dataclass(slots=True)
class AppConfig:
    schema_version: int
    enabled: bool
    microphone_id: str
    model_path: str
    vad_model_path: str
    language: str
    trigger: str
    backend: str
    initial_prompt: str
    recording: RecordingConfig
    live: LiveConfig
    diagnostics: DiagnosticsConfig

    def to_dict(self) -> dict[str, Any]:
        """Return the exact on-disk schema representation."""

        return asdict(self)


def _xdg_home(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


def get_config_dir() -> Path:
    return _xdg_home("XDG_CONFIG_HOME", Path.home() / ".config") / APP_DIRECTORY


def get_data_dir() -> Path:
    return _xdg_home("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_DIRECTORY


def get_config_path() -> Path:
    return get_config_dir() / CONFIG_FILENAME


def get_models_dir() -> Path:
    return get_data_dir() / "models"


def default_config() -> AppConfig:
    """Build defaults using the current XDG environment.

    This is a function rather than a module constant so tests, portable homes,
    and first-start setup all honor environment changes made after import.
    """

    models = get_models_dir()
    return AppConfig(
        schema_version=SCHEMA_VERSION,
        enabled=True,
        microphone_id=DEFAULT_MICROPHONE_ID,
        model_path=str(models / MAIN_MODEL_FILENAME),
        vad_model_path=str(models / VAD_MODEL_FILENAME),
        language="de",
        trigger="KEY_RIGHTCTRL",
        backend="vulkan",
        initial_prompt=DEFAULT_INITIAL_PROMPT,
        recording=RecordingConfig(),
        live=LiveConfig(),
        diagnostics=DiagnosticsConfig(),
    )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} muss ein JSON-Objekt sein")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} muss true oder false sein")
    return value


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ConfigError(f"{label} muss eine nichtleere Zeichenkette sein")
    return value


def _integer(value: Any, label: str, *, minimum: int, maximum: int | None = None) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        range_text = f"zwischen {minimum} und {maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"{label} muss eine ganze Zahl {range_text} sein")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} muss eine Zahl sein")
    result = float(value)
    if not math.isfinite(result) or not (-200.0 <= result <= 0.0):
        raise ConfigError(f"{label} muss zwischen -200 und 0 dBFS liegen")
    return result


def migrate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Migrate supported configuration data to the current schema.

    The unversioned legacy format stored recording limits at the top level.
    Common early-development aliases and schema version 1 are accepted so an
    existing local installation is upgraded without losing custom values.
    """

    source = dict(_require_mapping(raw, "Konfiguration"))
    version = source.get("schema_version")
    if isinstance(version, bool):
        raise ConfigError("schema_version muss eine ganze Zahl sein")
    if version is not None and not isinstance(version, int):
        raise ConfigError("schema_version muss eine ganze Zahl sein")
    if version is not None and version > SCHEMA_VERSION:
        raise UnsupportedConfigVersion(
            f"Konfigurationsversion {version} ist neuer als unterstützt ({SCHEMA_VERSION})"
        )
    if version not in (None, 0, 1, 2, 3, 4, SCHEMA_VERSION):
        raise ConfigError(f"Konfigurationsversion {version} wird nicht unterstützt")
    if version == SCHEMA_VERSION:
        return source

    if version in {1, 2, 3, 4}:
        # Version 1 briefly shipped an explanatory sentence as the Whisper
        # context. Whisper treats this field as preceding transcript text,
        # not as an instruction. Version 3 shipped the shorter context list.
        # Migrate only exact old defaults while preserving every user-authored
        # prompt byte-for-byte.
        prompt = source.get("initial_prompt")
        if (version == 1 and prompt == LEGACY_DEFAULT_INITIAL_PROMPT) or prompt in {
            PREVIOUS_DEFAULT_INITIAL_PROMPT,
            SCHEMA4_DEFAULT_INITIAL_PROMPT,
        }:
            source["initial_prompt"] = DEFAULT_INITIAL_PROMPT
        source.setdefault("diagnostics", asdict(DiagnosticsConfig()))
        source.setdefault("live", asdict(LiveConfig()))
        source["schema_version"] = SCHEMA_VERSION
        return source

    migrated = default_config().to_dict()
    direct = (
        "enabled",
        "microphone_id",
        "model_path",
        "vad_model_path",
        "language",
        "trigger",
        "backend",
        "initial_prompt",
        "live",
        "diagnostics",
    )
    for key in direct:
        if key in source:
            migrated[key] = source[key]

    aliases = {
        "microphone": "microphone_id",
        "model": "model_path",
        "vad_model": "vad_model_path",
        "prompt": "initial_prompt",
    }
    for old, new in aliases.items():
        if old in source and new not in source:
            migrated[new] = source[old]
    if migrated.get("initial_prompt") in {
        LEGACY_DEFAULT_INITIAL_PROMPT,
        PREVIOUS_DEFAULT_INITIAL_PROMPT,
    }:
        migrated["initial_prompt"] = DEFAULT_INITIAL_PROMPT

    recording = dict(migrated["recording"])
    nested = source.get("recording")
    if nested is not None:
        nested_mapping = _require_mapping(nested, "recording")
        for key in recording:
            if key in nested_mapping:
                recording[key] = nested_mapping[key]
    recording_aliases = {
        "min_duration_ms": "min_duration_ms",
        "recording_min_duration_ms": "min_duration_ms",
        "max_duration_s": "max_duration_s",
        "max_recording_seconds": "max_duration_s",
        "silence_threshold_dbfs": "silence_threshold_dbfs",
        "silence_threshold_db": "silence_threshold_dbfs",
    }
    for old, new in recording_aliases.items():
        if old in source:
            recording[new] = source[old]
    migrated["recording"] = recording
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def parse_config(raw: Mapping[str, Any]) -> AppConfig:
    """Migrate and validate a mapping as :class:`AppConfig`."""

    data = migrate_config(raw)
    defaults = default_config().to_dict()
    # Missing fields receive their documented defaults, allowing upgrades
    # from short-lived development builds without weakening type validation.
    merged = {**defaults, **data}
    recording_raw = {**defaults["recording"], **dict(_require_mapping(merged["recording"], "recording"))}
    diagnostics_raw = {
        **defaults["diagnostics"],
        **dict(_require_mapping(merged["diagnostics"], "diagnostics")),
    }
    live_raw = {
        **defaults["live"],
        **dict(_require_mapping(merged["live"], "live")),
    }

    schema_version = _integer(merged["schema_version"], "schema_version", minimum=1)
    if schema_version > SCHEMA_VERSION:
        raise UnsupportedConfigVersion(
            f"Konfigurationsversion {schema_version} ist neuer als unterstützt ({SCHEMA_VERSION})"
        )
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(f"Konfigurationsversion {schema_version} wird nicht unterstützt")

    recording = RecordingConfig(
        min_duration_ms=_integer(
            recording_raw["min_duration_ms"], "recording.min_duration_ms", minimum=1
        ),
        max_duration_s=_integer(
            recording_raw["max_duration_s"],
            "recording.max_duration_s",
            minimum=1,
            maximum=300,
        ),
        silence_threshold_dbfs=_number(
            recording_raw["silence_threshold_dbfs"], "recording.silence_threshold_dbfs"
        ),
    )
    if recording.max_duration_s * 1000 < recording.min_duration_ms:
        raise ConfigError("recording.max_duration_s muss mindestens min_duration_ms abdecken")

    diagnostics = DiagnosticsConfig(
        enabled=_bool(diagnostics_raw["enabled"], "diagnostics.enabled"),
        retention_entries=_integer(
            diagnostics_raw["retention_entries"],
            "diagnostics.retention_entries",
            minimum=1,
            maximum=100,
        ),
    )

    live = LiveConfig(
        enabled=_bool(live_raw["enabled"], "live.enabled"),
        direct_insert=_bool(
            live_raw["direct_insert"], "live.direct_insert"
        ),
        interval_ms=_integer(
            live_raw["interval_ms"],
            "live.interval_ms",
            minimum=1000,
            maximum=5000,
        ),
    )

    backend = _string(merged["backend"], "backend")
    if backend not in {"vulkan", "cpu"}:
        raise ConfigError("backend muss 'vulkan' oder 'cpu' sein")

    language = _string(merged["language"], "language")
    if language != "de":
        raise ConfigError("language muss für diese Version 'de' sein")
    trigger = _string(merged["trigger"], "trigger")
    if trigger != "KEY_RIGHTCTRL":
        raise ConfigError("trigger muss für diese Version 'KEY_RIGHTCTRL' sein")

    return AppConfig(
        schema_version=schema_version,
        enabled=_bool(merged["enabled"], "enabled"),
        microphone_id=_string(merged["microphone_id"], "microphone_id", allow_empty=True),
        model_path=_string(merged["model_path"], "model_path"),
        vad_model_path=_string(merged["vad_model_path"], "vad_model_path"),
        language=language,
        trigger=trigger,
        backend=backend,
        initial_prompt=_string(merged["initial_prompt"], "initial_prompt", allow_empty=True),
        recording=recording,
        live=live,
        diagnostics=diagnostics,
    )


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Read configuration, returning defaults when no file exists."""

    config_path = Path(path) if path is not None else get_config_path()
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return default_config()
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Konfiguration kann nicht gelesen werden: {exc}") from exc
    return parse_config(_require_mapping(raw, "Konfiguration"))


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_config(
    config: AppConfig | Mapping[str, Any],
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Validate and atomically save configuration with mode ``0600``."""

    validated = config if isinstance(config, AppConfig) else parse_config(config)
    # Re-parse dataclasses too, preventing callers from persisting mutated and
    # invalid values after construction.
    validated = parse_config(validated.to_dict())
    config_path = Path(path) if path is not None else get_config_path()
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(validated.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
        os.chmod(config_path, 0o600)
        _fsync_directory(config_path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return config_path


__all__ = [
    "APP_DIRECTORY",
    "CONFIG_FILENAME",
    "DEFAULT_INITIAL_PROMPT",
    "DEFAULT_MICROPHONE_ID",
    "DiagnosticsConfig",
    "LiveConfig",
    "MAIN_MODEL_FILENAME",
    "PREVIOUS_DEFAULT_INITIAL_PROMPT",
    "SCHEMA4_DEFAULT_INITIAL_PROMPT",
    "SCHEMA_VERSION",
    "VAD_MODEL_FILENAME",
    "AppConfig",
    "ConfigError",
    "RecordingConfig",
    "UnsupportedConfigVersion",
    "default_config",
    "get_config_dir",
    "get_config_path",
    "get_data_dir",
    "get_models_dir",
    "load_config",
    "migrate_config",
    "parse_config",
    "save_config",
]
