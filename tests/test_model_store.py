from __future__ import annotations

import hashlib

import pytest

from local_dictation.model_store import (
    MAIN_MODEL,
    VAD_MODEL,
    WHISPER_MODELS,
    DownloadCancelled,
    ModelIntegrityError,
    ModelMissingError,
    ModelSpec,
    ModelStore,
    validate_model,
)


class FakeResponse:
    def __init__(self, payload: bytes, *, declared_size: int | None = None):
        self.payload = payload
        self.headers = {
            "content-length": str(len(payload) if declared_size is None else declared_size)
        }
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    def get(self, url, *, stream, timeout):
        self.calls.append((url, stream, timeout))
        return self.response


def make_spec(payload: bytes) -> ModelSpec:
    return ModelSpec(
        key="test",
        filename="test-model.bin",
        url="https://models.invalid/immutable-commit/test-model.bin",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_official_specs_are_immutable_and_pinned():
    assert MAIN_MODEL is WHISPER_MODELS["small"]
    assert set(WHISPER_MODELS) == {"tiny", "base", "small", "medium", "large-v3-turbo"}
    assert all("98aa99a0a9db05ae2342309f5096248665f7cba3" in spec.url for spec in WHISPER_MODELS.values())
    assert WHISPER_MODELS["large-v3-turbo"].size == 1_624_555_275
    assert WHISPER_MODELS["large-v3-turbo"].sha256 == "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
    assert "9ffd54a1e1ee413ddf265af9913beaf518d1639b" in VAD_MODEL.url
    assert VAD_MODEL.size == 885_098
    assert VAD_MODEL.sha256 == "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"


def test_streams_valid_download_then_atomically_installs(tmp_path):
    payload = b"0123456789" * 7
    spec = make_spec(payload)
    response = FakeResponse(payload)
    progress = []
    store = ModelStore(tmp_path, http_client=FakeClient(response), chunk_size=9)

    destination = store.download(spec, progress=lambda done, total: progress.append((done, total)))

    assert destination.read_bytes() == payload
    assert validate_model(destination, spec) == destination
    assert progress[0] == (0, len(payload))
    assert progress[-1] == (len(payload), len(payload))
    assert not (tmp_path / "test-model.bin.part").exists()
    assert response.closed


def test_bad_download_cleans_partial_and_preserves_existing_final(tmp_path):
    payload = b"correct bytes"
    spec = make_spec(payload)
    destination = tmp_path / spec.filename
    destination.write_bytes(b"old corrupt model")
    response = FakeResponse(b"x" * len(payload))
    store = ModelStore(tmp_path, http_client=FakeClient(response), chunk_size=3)

    with pytest.raises(ModelIntegrityError):
        store.download(spec)

    assert destination.read_bytes() == b"old corrupt model"
    assert not (tmp_path / "test-model.bin.part").exists()
    assert response.closed


def test_cancel_cleans_partial(tmp_path):
    payload = b"abcdefghij"
    spec = make_spec(payload)
    response = FakeResponse(payload)
    cancelled = False

    def progress(done, total):
        nonlocal cancelled
        if done >= 4:
            cancelled = True

    store = ModelStore(tmp_path, http_client=FakeClient(response), chunk_size=4)
    with pytest.raises(DownloadCancelled):
        store.download(spec, progress=progress, cancel=lambda: cancelled)

    assert not (tmp_path / spec.filename).exists()
    assert not (tmp_path / f"{spec.filename}.part").exists()


def test_validate_rejects_missing_size_and_hash(tmp_path):
    payload = b"expected"
    spec = make_spec(payload)
    path = tmp_path / spec.filename
    with pytest.raises(ModelMissingError):
        validate_model(path, spec)
    path.write_bytes(b"short")
    with pytest.raises(ModelIntegrityError, match="Modellgröße"):
        validate_model(path, spec)
    path.write_bytes(b"X" * len(payload))
    with pytest.raises(ModelIntegrityError, match="SHA-256"):
        validate_model(path, spec)
