from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import local_dictation.diagnostics as diagnostics_module
from local_dictation.diagnostics import (
    AUDIO_FILENAME,
    DIAGNOSTICS_SCHEMA_VERSION,
    INSERTION_FILENAME,
    METADATA_FILENAME,
    TRANSCRIPT_FILENAME,
    DiagnosticsArchive,
    DiagnosticsError,
    UnsafeDiagnosticsDirectory,
    get_diagnostics_dir,
)


def mode(path: Path) -> int:
    if os.name == "nt":
        return 0o700 if path.is_dir() else 0o600
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def make_audio(path: Path, marker: bytes = b"audio payload") -> Path:
    path.write_bytes(b"RIFF" + marker)
    return path


def test_default_directory_uses_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert get_diagnostics_dir() == tmp_path / "data" / "local-dictation" / "diagnostics"
    assert DiagnosticsArchive().directory == get_diagnostics_dir()


def test_ensure_directory_creates_private_validated_root(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics")

    assert archive.ensure_directory() == archive.directory
    assert archive.directory.is_dir()
    assert mode(archive.directory) == 0o700


def test_success_bundle_is_private_atomic_and_keeps_text_out_of_metadata(tmp_path, caplog):
    source = make_audio(tmp_path / "recording.wav", b"original")
    archive = DiagnosticsArchive(tmp_path / "diagnostics", max_entries=10)

    session = archive.archive_audio(source)
    source.write_bytes(b"changed after copy")
    transcript = "Geheimes Diktat mit Grüßen und Umlauten."
    result = archive.finish_success(
        session,
        transcript,
        audio_seconds=1.25,
        inference_seconds=0.4,
    )

    assert result == session.directory
    assert (result / AUDIO_FILENAME).read_bytes() == b"RIFForiginal"
    assert (result / TRANSCRIPT_FILENAME).read_text(encoding="utf-8") == transcript
    metadata_text = (result / METADATA_FILENAME).read_text(encoding="utf-8")
    metadata = json.loads(metadata_text)
    assert metadata["status"] == "success"
    assert metadata["audio"] == {"filename": AUDIO_FILENAME, "bytes": 12}
    assert metadata["transcript"] == {
        "filename": TRANSCRIPT_FILENAME,
        "bytes": len(transcript.encode("utf-8")),
    }
    assert metadata["timings"] == {"audio_seconds": 1.25, "inference_seconds": 0.4}
    assert transcript not in metadata_text
    assert transcript not in caplog.text

    assert mode(archive.directory) == 0o700
    assert mode(result) == 0o700
    assert mode(result / AUDIO_FILENAME) == 0o600
    assert mode(result / METADATA_FILENAME) == 0o600
    assert mode(result / TRANSCRIPT_FILENAME) == 0o600
    assert not list(result.glob(".*.tmp"))


def test_error_bundle_has_empty_transcript_and_structured_error(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics")
    session = archive.archive_audio(make_audio(tmp_path / "recording.wav"))

    archive.finish_error(
        session,
        RuntimeError("Server\nnicht erreichbar"),
        stage="transcription",
        audio_seconds=0.75,
    )

    assert (session.directory / TRANSCRIPT_FILENAME).read_bytes() == b""
    metadata = json.loads((session.directory / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert metadata["status"] == "error"
    assert metadata["error"] == {
        "type": "RuntimeError",
        "message": "Server nicht erreichbar",
        "stage": "transcription",
    }
    assert metadata["timings"] == {"audio_seconds": 0.75}


def test_insertion_outcome_is_private_and_separate_from_transcript(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics")
    session = archive.archive_audio(make_audio(tmp_path / "recording.wav"))
    archive.finish_success(session, "Vertraulicher erkannter Text")

    archive.record_insertion(
        session,
        copied=False,
        shortcut_sent=False,
        error="Text konnte nicht kopiert werden",
    )

    insertion_path = session.directory / INSERTION_FILENAME
    insertion = json.loads(insertion_path.read_text(encoding="utf-8"))
    assert DIAGNOSTICS_SCHEMA_VERSION == 2
    assert insertion["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
    assert insertion["status"] == "error"
    assert insertion["copied"] is False
    assert insertion["shortcut_sent"] is False
    assert insertion["error"]["message"] == "Text konnte nicht kopiert werden"
    assert "Vertraulicher erkannter Text" not in insertion_path.read_text(encoding="utf-8")
    assert mode(insertion_path) == 0o600


def test_failed_finish_releases_session_for_retention(monkeypatch, tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics", max_entries=1)
    first = archive.archive_audio(make_audio(tmp_path / "first.wav"))
    real_atomic_write = diagnostics_module._atomic_write

    def fail_write(_path: Path, _payload: bytes) -> None:
        raise OSError("simulated disk error")

    monkeypatch.setattr(diagnostics_module, "_atomic_write", fail_write)
    with pytest.raises(OSError, match="simulated disk error"):
        archive.finish_success(first, "Text")
    monkeypatch.setattr(diagnostics_module, "_atomic_write", real_atomic_write)

    second = archive.archive_audio(make_audio(tmp_path / "second.wav"))
    archive.finish_success(second, "Neuer Text")

    assert not first.directory.exists()
    assert second.directory.is_dir()


def test_invalid_transcript_encoding_still_releases_session_for_retention(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics", max_entries=1)
    first = archive.archive_audio(make_audio(tmp_path / "invalid-encoding.wav"))

    with pytest.raises(UnicodeEncodeError):
        archive.finish_success(first, "bad\ud800")

    second = archive.archive_audio(make_audio(tmp_path / "valid-encoding.wav"))
    archive.finish_success(second, "Gültiger Text")
    assert not first.directory.exists()
    assert second.directory.is_dir()


def test_retention_prunes_oldest_and_can_be_reconfigured(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics", max_entries=3)
    sessions = []
    for index in range(4):
        source = make_audio(tmp_path / f"recording-{index}.wav", str(index).encode())
        session = archive.archive_audio(source)
        archive.finish_success(session, f"Diktat {index}")
        sessions.append(session)

    assert not sessions[0].directory.exists()
    assert all(session.directory.is_dir() for session in sessions[1:])

    assert archive.set_max_entries(1) == 1
    assert not sessions[1].directory.exists()
    assert not sessions[2].directory.exists()
    assert sessions[3].directory.is_dir()


def test_new_archive_instance_prunes_existing_excess_on_request(tmp_path):
    root = tmp_path / "diagnostics"
    archive = DiagnosticsArchive(root, max_entries=3)
    sessions = []
    for index in range(3):
        session = archive.archive_audio(make_audio(tmp_path / f"restart-{index}.wav"))
        archive.finish_success(session, f"Text {index}")
        sessions.append(session)

    restarted = DiagnosticsArchive(root, max_entries=1)

    assert restarted.prune() == 2
    assert not sessions[0].directory.exists()
    assert not sessions[1].directory.exists()
    assert sessions[2].directory.is_dir()


@pytest.mark.parametrize("value", [True, 0, 501, 1.5])
def test_retention_bounds_are_enforced(tmp_path, value):
    with pytest.raises((TypeError, ValueError)):
        DiagnosticsArchive(tmp_path / "diagnostics", max_entries=value)


def test_clear_removes_bundles_but_retains_private_root(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics")
    for index in range(2):
        session = archive.archive_audio(make_audio(tmp_path / f"source-{index}.wav"))
        archive.finish_success(session, "Text")

    assert archive.clear() == 2
    assert archive.directory.is_dir()
    assert mode(archive.directory) == 0o700
    assert list(archive.directory.iterdir()) == []
    assert archive.clear() == 0


def test_clear_rejects_symlink_root_without_touching_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("do not delete", encoding="utf-8")
    diagnostics = tmp_path / "diagnostics"
    diagnostics.symlink_to(target, target_is_directory=True)

    with pytest.raises(UnsafeDiagnosticsDirectory):
        DiagnosticsArchive(diagnostics).clear()

    assert sentinel.read_text(encoding="utf-8") == "do not delete"


def test_finish_rejects_archive_root_replaced_by_symlink(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics")
    session = archive.archive_audio(make_audio(tmp_path / "source.wav"))
    original_root = tmp_path / "original-diagnostics"
    archive.directory.rename(original_root)
    archive.directory.symlink_to(original_root, target_is_directory=True)

    with pytest.raises(UnsafeDiagnosticsDirectory):
        archive.finish_success(session, "darf nicht geschrieben werden")

    assert (original_root / session.session_id / TRANSCRIPT_FILENAME).read_bytes() == b""


def test_clear_preflights_nested_symlinks_before_deleting_anything(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics")
    session = archive.archive_audio(make_audio(tmp_path / "source.wav"))
    archive.finish_success(session, "bleibt erhalten")
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    (archive.directory / "unsafe-link").symlink_to(target)

    with pytest.raises(UnsafeDiagnosticsDirectory):
        archive.clear()

    assert session.directory.is_dir()
    assert target.read_text(encoding="utf-8") == "outside"


def test_archive_rejects_symlink_audio_source(tmp_path):
    real_source = make_audio(tmp_path / "real.wav")
    linked_source = tmp_path / "linked.wav"
    linked_source.symlink_to(real_source)

    with pytest.raises(DiagnosticsError):
        DiagnosticsArchive(tmp_path / "diagnostics").archive_audio(linked_source)


def test_concurrent_sessions_are_serialized_and_retained_safely(tmp_path):
    archive = DiagnosticsArchive(tmp_path / "diagnostics", max_entries=5)
    sources = [make_audio(tmp_path / f"audio-{index}.wav") for index in range(8)]

    def complete(source: Path):
        session = archive.archive_audio(source)
        archive.finish_success(session, "privater Text")
        return session

    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(complete, sources))

    remaining = [session for session in sessions if session.directory.exists()]
    assert len(remaining) == 5
    for session in remaining:
        metadata = json.loads(
            (session.directory / METADATA_FILENAME).read_text(encoding="utf-8")
        )
        assert metadata["status"] == "success"
