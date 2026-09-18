from __future__ import annotations

import os
import signal
import stat
import struct
import threading
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import QApplication

import local_dictation.recorder as recorder_module
from local_dictation.recorder import Recorder


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication(["local-dictation-test"])
    assert isinstance(app, QApplication)
    return app


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in tuple(self.callbacks):
            callback(*args)  # type: ignore[operator]


class FakeProcess:
    ProcessChannelMode = QProcess.ProcessChannelMode
    ProcessState = QProcess.ProcessState
    ProcessError = QProcess.ProcessError
    ExitStatus = QProcess.ExitStatus

    instances: list[FakeProcess] = []

    def __init__(self, _parent: object = None) -> None:
        self.started = FakeSignal()
        self.finished = FakeSignal()
        self.errorOccurred = FakeSignal()
        self.readyReadStandardOutput = FakeSignal()
        self.readyReadStandardError = FakeSignal()
        self._state = QProcess.ProcessState.NotRunning
        self.program = ""
        self.arguments: list[str] = []
        self.terminated = False
        self.killed = False
        self.deleted = False
        self.wait_results: list[bool] = []
        self.stdout = bytearray()
        type(self).instances.append(self)

    def setProcessChannelMode(self, _mode: object) -> None:
        pass

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = arguments

    def start(self) -> None:
        self._state = QProcess.ProcessState.Starting

    def state(self) -> QProcess.ProcessState:
        return self._state

    def processId(self) -> int:
        return 4242

    def readAllStandardOutput(self) -> bytes:
        data = bytes(self.stdout)
        self.stdout.clear()
        return data

    def readAllStandardError(self) -> bytes:
        return b""

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self._state = QProcess.ProcessState.NotRunning

    def waitForFinished(self, _timeout_ms: int) -> bool:
        return self.wait_results.pop(0) if self.wait_results else True

    def deleteLater(self) -> None:
        self.deleted = True

    def emit_started(self) -> None:
        self._state = QProcess.ProcessState.Running
        self.started.emit()

    def feed_stdout(self, data: bytes, *, notify: bool = True) -> None:
        self.stdout.extend(data)
        if notify:
            self.readyReadStandardOutput.emit()

    def emit_finished(
        self,
        exit_code: int = 0,
        status: QProcess.ExitStatus = QProcess.ExitStatus.NormalExit,
    ) -> None:
        self._state = QProcess.ProcessState.NotRunning
        self.finished.emit(exit_code, status)


@pytest.fixture
def fake_process(monkeypatch: pytest.MonkeyPatch) -> type[FakeProcess]:
    FakeProcess.instances.clear()
    monkeypatch.setattr(recorder_module, "QProcess", FakeProcess)
    return FakeProcess


def test_release_while_qprocess_is_starting_stops_as_soon_as_started(
    tmp_path: Path,
    fake_process: type[FakeProcess],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(recorder_module.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    recorder = Recorder(tmp_path)
    completed: list[tuple[str, float, bool]] = []
    recorder.finished.connect(lambda *args: completed.append(args))

    recorder.start("stable.pipewire.node", 300)
    process = fake_process.instances[-1]
    path = recorder.path
    assert path is not None
    assert process.program == "pw-record"
    assert process.arguments[-1] == "-"
    assert "--raw" in process.arguments
    assert "--container" not in process.arguments

    recorder.stop()
    assert sent == []
    process.emit_started()
    assert sent == [(4242, signal.SIGINT)]

    process.feed_stdout(struct.pack("<4h", 1, -2, 3, -4))
    process.emit_finished(-signal.SIGINT, QProcess.ExitStatus.CrashExit)
    assert completed and completed[0][0] == str(path)
    assert completed[0][2] is False
    assert recorder.active is False
    assert process.deleted is True
    with wave.open(str(path), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getframerate() == 16_000
        assert recording.readframes(4) == struct.pack("<4h", 1, -2, 3, -4)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_to_start_reports_once_and_removes_partial_audio(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    failures: list[str] = []
    recorder.failed.connect(failures.append)
    recorder.start("missing", 10)
    process = fake_process.instances[-1]
    path = recorder.path
    assert path is not None
    process.feed_stdout(b"partial")

    process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)
    process.emit_finished(1, QProcess.ExitStatus.CrashExit)

    assert failures == ["pw-record konnte nicht gestartet werden"]
    assert not path.exists()
    assert recorder.path is None
    assert recorder.active is False


def test_automatic_limit_is_reported_and_sigint_exit_is_accepted(
    tmp_path: Path,
    fake_process: type[FakeProcess],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[signal.Signals] = []
    monkeypatch.setattr(recorder_module.os, "kill", lambda _pid, sig: sent.append(sig))
    recorder = Recorder(tmp_path)
    completed: list[tuple[str, float, bool]] = []
    recorder.finished.connect(lambda *args: completed.append(args))
    recorder.start("microphone", 5)
    process = fake_process.instances[-1]
    process.emit_started()
    path = recorder.path
    assert path is not None
    process.feed_stdout(struct.pack("<2h", 1, -1))

    recorder._on_limit()
    process.emit_finished(-signal.SIGINT, QProcess.ExitStatus.CrashExit)

    assert sent == [signal.SIGINT]
    assert completed and completed[0][2] is True


def test_clean_unrequested_exit_is_treated_as_automatic_sample_limit(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    completed: list[tuple[str, float, bool]] = []
    failures: list[str] = []
    recorder.finished.connect(lambda *args: completed.append(args))
    recorder.failed.connect(failures.append)
    recorder.start("microphone", 5)
    process = fake_process.instances[-1]
    process.emit_started()
    path = recorder.path
    assert path is not None
    process.feed_stdout(struct.pack("<2h", 1, -1))

    # pw-record exits cleanly by itself when --sample-count is exhausted. The
    # Qt timer can legitimately be delivered after this process notification.
    process.emit_finished(0, QProcess.ExitStatus.NormalExit)

    assert failures == []
    assert completed and completed[0][0] == str(path)
    assert completed[0][2] is True


def test_nonzero_pw_record_exit_after_full_sample_count_is_accepted(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    completed: list[tuple[str, float, bool]] = []
    failures: list[str] = []
    recorder.finished.connect(lambda *args: completed.append(args))
    recorder.failed.connect(failures.append)
    recorder.start("microphone", 1)
    process = fake_process.instances[-1]
    process.emit_started()
    process.feed_stdout(bytes(32_000))

    # PipeWire 1.4.9's pw-record returns 1 after producing the complete raw
    # stream requested with --sample-count. The full capped buffer proves this
    # was a normal automatic-limit completion rather than a capture failure.
    process.emit_finished(1, QProcess.ExitStatus.NormalExit)

    assert failures == []
    assert completed and completed[0][2] is True
    path = Path(completed[0][0])
    with wave.open(str(path), "rb") as recording:
        assert recording.getnframes() == 16_000


def test_unrequested_process_error_removes_audio(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    failures: list[str] = []
    recorder.failed.connect(failures.append)
    recorder.start("microphone", 10)
    process = fake_process.instances[-1]
    process.emit_started()
    path = recorder.path
    assert path is not None
    path.write_bytes(b"partial")

    process.emit_finished(17, QProcess.ExitStatus.CrashExit)

    assert failures == ["pw-record wurde unerwartet beendet"]
    assert not path.exists()
    assert recorder.path is None


def test_discard_and_stale_cleanup_never_follow_symlinks(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    target = tmp_path / "keep.wav"
    target.write_bytes(b"keep")
    stale = recorder.audio_dir / "recording-stale.wav"
    stale.write_bytes(b"stale")
    symlink = recorder.audio_dir / "recording-link.wav"
    symlink.symlink_to(target)

    recorder.cleanup_stale()

    assert not stale.exists()
    assert symlink.is_symlink()
    assert target.read_bytes() == b"keep"


def test_odd_process_chunks_never_expose_or_write_half_a_sample(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)
    process = fake_process.instances[-1]
    process.emit_started()

    process.feed_stdout(b"\x34")
    assert recorder.recorded_frames == 0
    assert recorder.snapshot_pcm().pcm == b""

    process.feed_stdout(b"\x12\x78")
    assert recorder.recorded_frames == 1
    assert recorder.snapshot_pcm().pcm == b""

    process.feed_stdout(bytes(range(29)))
    first = recorder.snapshot_pcm()
    expected = b"\x34\x12\x78" + bytes(range(29))
    assert first.pcm == expected
    assert first.frames == 16
    assert first.end_ms == 1

    # The final orphan byte is deliberately discarded when the process exits.
    process.feed_stdout(b"\x99")
    recorder.stop()
    process.emit_finished(-signal.SIGINT, QProcess.ExitStatus.CrashExit)
    path = recorder.take_path()
    assert path is not None
    with wave.open(str(path), "rb") as recording:
        assert recording.getnframes() == 16
        assert recording.readframes(16) == expected


def test_pcm_snapshot_uses_exact_sample_boundaries_and_is_immutable(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)
    process = fake_process.instances[-1]
    samples = tuple(range(80))
    process.feed_stdout(struct.pack("<80h", *samples))

    snapshot = recorder.snapshot_pcm(1, end_ms=3)

    assert snapshot.start_frame == 16
    assert snapshot.end_frame == 48
    assert snapshot.start_ms == 1
    assert snapshot.end_ms == 3
    assert snapshot.frames == 32
    assert snapshot.duration_ms == 2
    assert struct.unpack("<32h", snapshot.pcm) == samples[16:48]

    process.feed_stdout(struct.pack("<16h", *range(80, 96)))
    assert struct.unpack("<32h", snapshot.pcm) == samples[16:48]
    assert recorder.recorded_duration_ms == 6.0


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "error"),
    [
        (-1, None, ValueError),
        (float("nan"), None, TypeError),
        (False, None, TypeError),
        (2, 1, ValueError),
    ],
)
def test_snapshot_rejects_invalid_cursors(
    tmp_path: Path,
    fake_process: type[FakeProcess],
    start_ms: object,
    end_ms: object,
    error: type[Exception],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)

    with pytest.raises(error):
        recorder.snapshot_pcm(start_ms, end_ms=end_ms)  # type: ignore[arg-type]


def test_wav_snapshot_is_private_exact_and_explicitly_deletable(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)
    process = fake_process.instances[-1]
    samples = tuple(range(64))
    process.feed_stdout(struct.pack("<64h", *samples))

    snapshot = recorder.snapshot_wav(1, end_ms=3)

    assert snapshot is not None
    assert snapshot.start_frame == 16
    assert snapshot.end_frame == 48
    assert snapshot.start_ms == 1
    assert snapshot.end_ms == 3
    assert snapshot.window_start_ms == 1
    assert snapshot.window_end_ms == 3
    assert snapshot.duration_ms == 2
    if os.name != "nt":
        assert stat.S_IMODE(snapshot.path.stat().st_mode) == 0o600
    with wave.open(str(snapshot.path), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getframerate() == 16_000
        assert recording.getnframes() == 32
        assert recording.readframes(32) == struct.pack("<32h", *samples[16:48])

    process.feed_stdout(struct.pack("<16h", *range(64, 80)))
    with wave.open(str(snapshot.path), "rb") as recording:
        assert recording.getnframes() == 32

    recorder.delete_snapshot(snapshot)
    assert not snapshot.path.exists()


def test_empty_snapshot_creates_no_file(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)

    assert recorder.snapshot_wav() is None
    assert list(recorder.audio_dir.glob("snapshot-*.wav")) == []


def test_snapshot_delete_never_removes_unowned_path(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)
    unrelated = tmp_path / "unrelated.wav"
    unrelated.write_bytes(b"keep")

    recorder.delete_snapshot(unrelated)

    assert unrelated.read_bytes() == b"keep"


def test_raw_buffer_is_capped_at_configured_duration(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 1)
    process = fake_process.instances[-1]
    expected = bytes(index % 251 for index in range(32_000))
    process.feed_stdout(expected + b"overflow" * 100)

    assert recorder.recorded_frames == 16_000
    assert recorder.recorded_duration_ms == 1000.0
    assert recorder.snapshot_pcm().pcm == expected

    process.emit_finished(0, QProcess.ExitStatus.NormalExit)
    path = recorder.take_path()
    assert path is not None
    with wave.open(str(path), "rb") as recording:
        assert recording.getnframes() == 16_000
        assert recording.readframes(16_000) == expected


@pytest.mark.parametrize("value", [0, 301, True, 1.5])
def test_recording_duration_is_bounded_to_five_minutes(
    tmp_path: Path,
    fake_process: type[FakeProcess],
    value: object,
) -> None:
    recorder = Recorder(tmp_path)

    with pytest.raises(ValueError):
        recorder.start("microphone", value)  # type: ignore[arg-type]


def test_failure_and_discard_remove_all_owned_snapshots(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    failed_recorder = Recorder(tmp_path / "failed")
    failed_recorder.start("microphone", 5)
    failed_process = fake_process.instances[-1]
    failed_process.feed_stdout(struct.pack("<32h", *range(32)))
    failed_snapshot = failed_recorder.snapshot_wav()
    assert failed_snapshot is not None

    failed_process.emit_finished(9, QProcess.ExitStatus.CrashExit)
    assert not failed_snapshot.path.exists()

    discarded_recorder = Recorder(tmp_path / "discarded")
    discarded_recorder.start("microphone", 5)
    discarded_process = fake_process.instances[-1]
    discarded_process.emit_started()
    discarded_process.feed_stdout(struct.pack("<32h", *range(32)))
    discarded_snapshot = discarded_recorder.snapshot_wav()
    assert discarded_snapshot is not None

    discarded_recorder.stop(discard=True)
    discarded_process.emit_finished(-signal.SIGINT, QProcess.ExitStatus.CrashExit)
    assert not discarded_snapshot.path.exists()
    assert list(discarded_recorder.audio_dir.iterdir()) == []


def test_shutdown_removes_final_target_and_snapshots(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)
    process = fake_process.instances[-1]
    process.emit_started()
    process.feed_stdout(struct.pack("<32h", *range(32)))
    snapshot = recorder.snapshot_wav()
    final_path = recorder.path
    assert snapshot is not None
    assert final_path is not None
    # A foreign partial file at the reserved final path is cleaned as well.
    final_path.write_bytes(b"partial")

    recorder.shutdown()

    assert not snapshot.path.exists()
    assert not final_path.exists()
    assert recorder.recorded_frames == 0


def test_snapshot_reads_remain_sample_aligned_during_concurrent_appends(
    tmp_path: Path,
    fake_process: type[FakeProcess],
) -> None:
    recorder = Recorder(tmp_path)
    recorder.start("microphone", 5)
    process = fake_process.instances[-1]
    failures: list[str] = []

    def append_chunks() -> None:
        try:
            for index in range(200):
                process.feed_stdout(bytes((index % 256, (index + 1) % 256)))
        except Exception as exc:  # pragma: no cover - assertion transport
            failures.append(str(exc))

    writer = threading.Thread(target=append_chunks)
    writer.start()
    snapshots = [recorder.snapshot_pcm() for _ in range(200)]
    writer.join()

    assert failures == []
    assert all(len(snapshot.pcm) % 2 == 0 for snapshot in snapshots)
    assert all(snapshot.end_frame >= snapshot.start_frame for snapshot in snapshots)
    assert recorder.recorded_frames == 200
