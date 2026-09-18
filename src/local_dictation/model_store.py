"""Pinned Whisper model metadata, download, and integrity validation."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MAIN_MODEL_FILENAME, VAD_MODEL_FILENAME, get_models_dir

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool] | Any


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    filename: str
    url: str
    size: int
    sha256: str


MAIN_MODEL = ModelSpec(
    key="main",
    filename=MAIN_MODEL_FILENAME,
    url=(
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/"
        "98aa99a0a9db05ae2342309f5096248665f7cba3/ggml-large-v3-turbo.bin"
    ),
    size=1_624_555_275,
    sha256="1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
)

VAD_MODEL = ModelSpec(
    key="vad",
    filename=VAD_MODEL_FILENAME,
    url=(
        "https://huggingface.co/ggml-org/whisper-vad/resolve/"
        "9ffd54a1e1ee413ddf265af9913beaf518d1639b/ggml-silero-v6.2.0.bin"
    ),
    size=885_098,
    sha256="2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
)

# Explicit aliases make call sites self-documenting while keeping concise
# names for menus and tests.
MAIN_MODEL_SPEC = MAIN_MODEL
VAD_MODEL_SPEC = VAD_MODEL
MODEL_SPECS: Mapping[str, ModelSpec] = {
    MAIN_MODEL.key: MAIN_MODEL,
    VAD_MODEL.key: VAD_MODEL,
}


class ModelStoreError(RuntimeError):
    """Base class for model storage failures."""


class ModelMissingError(ModelStoreError):
    """A required model does not exist."""


class ModelIntegrityError(ModelStoreError):
    """A model's size or SHA-256 does not match its pinned metadata."""


class ModelDownloadError(ModelStoreError):
    """A model could not be downloaded."""


class DownloadCancelled(ModelDownloadError):
    """The user cancelled a model download."""


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


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    """Return a file's lowercase SHA-256 digest without loading it in memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_model(path: str | os.PathLike[str], spec: ModelSpec) -> Path:
    """Validate a model's exact byte size and SHA-256, returning its path."""

    model_path = Path(path)
    try:
        actual_size = model_path.stat().st_size
    except FileNotFoundError as exc:
        raise ModelMissingError(f"Modell fehlt: {model_path}") from exc
    except OSError as exc:
        raise ModelStoreError(f"Modell kann nicht geprüft werden: {model_path}: {exc}") from exc
    if not model_path.is_file():
        raise ModelMissingError(f"Modell ist keine reguläre Datei: {model_path}")
    if actual_size != spec.size:
        raise ModelIntegrityError(
            f"Falsche Modellgröße für {model_path.name}: {actual_size} statt {spec.size} Bytes"
        )
    try:
        actual_hash = sha256_file(model_path)
    except OSError as exc:
        raise ModelStoreError(f"Modell kann nicht gelesen werden: {model_path}: {exc}") from exc
    if actual_hash.lower() != spec.sha256.lower():
        raise ModelIntegrityError(
            f"SHA-256 stimmt für {model_path.name} nicht überein "
            f"({actual_hash} statt {spec.sha256})"
        )
    return model_path


def validate_required_models(
    model_path: str | os.PathLike[str],
    vad_model_path: str | os.PathLike[str],
) -> tuple[Path, Path]:
    """Validate both pinned models; call this before every engine launch."""

    return (
        validate_model(model_path, MAIN_MODEL),
        validate_model(vad_model_path, VAD_MODEL),
    )


def _is_cancelled(cancel: CancelCheck | None) -> bool:
    if cancel is None:
        return False
    is_set = getattr(cancel, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    if callable(cancel):
        return bool(cancel())
    return bool(cancel)


class ModelStore:
    """Manage pinned model files in a private XDG data directory."""

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        http_client: Any | None = None,
        chunk_size: int = 1024 * 1024,
        timeout: tuple[float, float] = (10.0, 60.0),
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.directory = Path(directory) if directory is not None else get_models_dir()
        self.http_client = http_client
        self.chunk_size = chunk_size
        self.timeout = timeout

    def path_for(self, spec: ModelSpec) -> Path:
        return self.directory / spec.filename

    def validate(self, spec: ModelSpec, path: str | os.PathLike[str] | None = None) -> Path:
        return validate_model(path if path is not None else self.path_for(spec), spec)

    def validate_all(self) -> dict[str, Path]:
        """Validate every required model at its managed location."""

        return {key: self.validate(spec) for key, spec in MODEL_SPECS.items()}

    def _client(self) -> Any:
        if self.http_client is not None:
            return self.http_client
        try:
            import requests  # Imported lazily; offline engine startup needs no requests.
        except ImportError as exc:  # pragma: no cover - depends on host packaging
            raise ModelDownloadError(
                "Für den einmaligen Modelldownload fehlt das Python-Paket 'requests'"
            ) from exc
        return requests

    def download(
        self,
        spec: ModelSpec,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
        force: bool = False,
    ) -> Path:
        """Stream, validate, and atomically install one pinned model.

        The final path is untouched until a complete ``.part`` file matches
        both the pinned byte length and digest.  Every failure and cancellation
        removes that partial file.
        """

        destination = self.path_for(spec)
        if destination.exists() and not force:
            try:
                return validate_model(destination, spec)
            except ModelIntegrityError:
                # Preserve the corrupt final file until a verified replacement
                # is ready; os.replace below makes the switch atomic.
                pass

        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        partial.unlink(missing_ok=True)
        response: Any | None = None
        try:
            if _is_cancelled(cancel):
                raise DownloadCancelled(f"Download von {spec.filename} wurde abgebrochen")
            if progress is not None:
                progress(0, spec.size)

            response = self._client().get(spec.url, stream=True, timeout=self.timeout)
            response.raise_for_status()
            content_length = response.headers.get("content-length") if response.headers else None
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise ModelDownloadError("Server lieferte eine ungültige Content-Length") from exc
                if declared_size != spec.size:
                    raise ModelIntegrityError(
                        f"Server meldet {declared_size} statt {spec.size} Bytes für {spec.filename}"
                    )

            digest = hashlib.sha256()
            downloaded = 0
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    for chunk in response.iter_content(chunk_size=self.chunk_size):
                        if _is_cancelled(cancel):
                            raise DownloadCancelled(
                                f"Download von {spec.filename} wurde abgebrochen"
                            )
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > spec.size:
                            raise ModelIntegrityError(
                                f"Download von {spec.filename} ist größer als erwartet"
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        if progress is not None:
                            progress(downloaded, spec.size)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

            if downloaded != spec.size:
                raise ModelIntegrityError(
                    f"Download von {spec.filename} hat {downloaded} statt {spec.size} Bytes"
                )
            actual_hash = digest.hexdigest()
            if actual_hash.lower() != spec.sha256.lower():
                raise ModelIntegrityError(
                    f"SHA-256 des Downloads stimmt nicht überein "
                    f"({actual_hash} statt {spec.sha256})"
                )
            if _is_cancelled(cancel):
                raise DownloadCancelled(f"Download von {spec.filename} wurde abgebrochen")

            os.replace(partial, destination)
            os.chmod(destination, 0o600)
            _fsync_directory(self.directory)
            return destination
        except (DownloadCancelled, ModelIntegrityError, ModelDownloadError):
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise ModelDownloadError(f"Download von {spec.filename} fehlgeschlagen: {exc}") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()


def download_model(
    spec: ModelSpec,
    *,
    directory: str | os.PathLike[str] | None = None,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
    http_client: Any | None = None,
) -> Path:
    """Convenience wrapper for one-off setup code."""

    return ModelStore(directory, http_client=http_client).download(
        spec, progress=progress, cancel=cancel
    )


__all__ = [
    "MAIN_MODEL",
    "MAIN_MODEL_SPEC",
    "MODEL_SPECS",
    "VAD_MODEL",
    "VAD_MODEL_SPEC",
    "DownloadCancelled",
    "ModelDownloadError",
    "ModelIntegrityError",
    "ModelMissingError",
    "ModelSpec",
    "ModelStore",
    "ModelStoreError",
    "ProgressCallback",
    "download_model",
    "sha256_file",
    "validate_model",
    "validate_required_models",
]
