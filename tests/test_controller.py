from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from PyQt6.QtCore import QObject

from local_dictation.config import DiagnosticsConfig, LiveConfig, default_config
from local_dictation.controller import (
    DictationController,
    EngineLaunchResult,
    TranscriptionResult,
)
from local_dictation.diagnostics import (
    AUDIO_FILENAME,
    INSERTION_FILENAME,
    METADATA_FILENAME,
    TRANSCRIPT_FILENAME,
    DiagnosticsArchive,
    DiagnosticSession,
)
from local_dictation.engine import Backend
from local_dictation.insertion import InsertionResult, RevisionResult
from local_dictation.microphones import Microphone
from local_dictation.recorder import WavSnapshot
from local_dictation.state import DictationState, DictationStateMachine, EngineState


class FakeRecorder:
    def __init__(self) -> None:
        self.active = False
        self.path: Path | None = None
        self.start_calls: list[tuple[str, int]] = []
        self.stop_calls: list[dict[str, bool]] = []
        self.shutdown_calls = 0
        self.snapshots: list[object] = []
        self.recorded_duration_ms = 0.0
        self.snapshot_starts: list[int] = []

    def start(self, microphone_id: str, maximum: int) -> None:
        self.start_calls.append((microphone_id, maximum))
        self.active = True

    def stop(self, *, discard: bool = False, automatic_limit: bool = False) -> None:
        self.stop_calls.append(
            {"discard": discard, "automatic_limit": automatic_limit}
        )

    def take_path(self) -> Path | None:
        path, self.path = self.path, None
        self.active = False
        return path

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.active = False
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None

    def snapshot_wav(self, start_ms: int):
        self.snapshot_starts.append(start_ms)
        return self.snapshots.pop(0) if self.snapshots else None

    def delete_snapshot(self, snapshot: object) -> None:
        path = getattr(snapshot, "path", snapshot)
        if isinstance(path, Path):
            path.unlink(missing_ok=True)


class FakeEngine:
    def __init__(self, result: str = "  Grüße\nCachyOS.  ") -> None:
        self.ready = True
        self.backend = Backend.VULKAN
        self.result = result
        self.error: Exception | None = None
        self.transcribe_calls: list[tuple[Path, str, object, object]] = []
        self.cancel_calls = 0
        self.close_calls = 0

    def transcribe(
        self,
        path: Path,
        *,
        initial_prompt: str,
        timeout: object | None = None,
        cancel_event: object | None = None,
    ) -> str:
        self.transcribe_calls.append((Path(path), initial_prompt, cancel_event, timeout))
        if self.error is not None:
            raise self.error
        return self.result

    def cancel_pending(self) -> None:
        self.cancel_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        self.ready = False

    def health(self) -> bool:
        return self.ready


class FakeInsertion:
    def __init__(self) -> None:
        self.insert_calls: list[str] = []
        self.insert_cancellations: list[object | None] = []
        self.copy_calls: list[str] = []
        self.result = InsertionResult(True, True, "Einfügen gesendet")
        self.target_window = "{01234567-89ab-cdef-0123-456789abcdef}"
        self.capture_calls = 0
        self.revise_calls: list[tuple[str, str, str, object | None]] = []
        self.revision_result = RevisionResult(True, True, True, "Live-Text aktualisiert")

    def insert(
        self, text: str, *, cancel: object | None = None
    ) -> InsertionResult:
        self.insert_cancellations.append(cancel)
        if cancel is not None and cancel.is_set():
            return InsertionResult(False, False, "Einfügen wurde abgebrochen")
        self.insert_calls.append(text)
        return self.result

    def copy(self, text: str) -> InsertionResult:
        self.copy_calls.append(text)
        return InsertionResult(True, False, "Text wurde kopiert")

    def capture_active_window(self) -> str | None:
        self.capture_calls += 1
        return self.target_window

    def revise(
        self,
        previous: str,
        current: str,
        *,
        expected_window: str,
        cancel: object | None = None,
    ) -> RevisionResult:
        self.revise_calls.append((previous, current, expected_window, cancel))
        return self.revision_result

    def preflight(self) -> None:
        return None


class FakeOverlay:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.hidden = 0
        self.closed = 0

    def show_message(self, message: str, **kwargs: object) -> None:
        self.messages.append((message, kwargs))

    def hide(self) -> None:
        self.hidden += 1

    def close(self) -> None:
        self.closed += 1


class FakeIcon:
    def __init__(self) -> None:
        self.hide_calls = 0

    def hide(self) -> None:
        self.hide_calls += 1


class FakeTray:
    def __init__(self) -> None:
        self.last_available: list[bool] = []
        self.statuses: list[tuple[str, str, str]] = []
        self.microphone_updates: list[tuple[list[Microphone], str]] = []
        self.icon = FakeIcon()

    def set_last_available(self, available: bool) -> None:
        self.last_available.append(available)

    def set_status(self, ready: str, model: str, *, color: str) -> None:
        self.statuses.append((ready, model, color))

    def set_microphones(self, microphones: list[Microphone], selected: str) -> None:
        self.microphone_updates.append((list(microphones), selected))


class FakeInput:
    def __init__(self) -> None:
        self.enable_calls: list[str] = []
        self.disable_calls: list[str] = []
        self.stop_calls: list[float] = []

    def enable(self, *, reason: str) -> None:
        self.enable_calls.append(reason)

    def disable(self, *, reason: str) -> None:
        self.disable_calls.append(reason)

    def stop(self, timeout: float) -> None:
        self.stop_calls.append(timeout)


class FakeTimer:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.start_calls = 0
        self.interval = 0

    def stop(self) -> None:
        self.stop_calls += 1

    def start(self) -> None:
        self.start_calls += 1

    def setInterval(self, value: int) -> None:
        self.interval = value


class FakeControl:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakePool:
    def __init__(self) -> None:
        self.wait_calls: list[int] = []

    def waitForDone(self, milliseconds: int) -> bool:
        self.wait_calls.append(milliseconds)
        return True


class PendingJob:
    def __init__(self, function, on_result, on_error=None, on_finished=None) -> None:
        self.function = function
        self.on_result = on_result
        self.on_error = on_error
        self.on_finished = on_finished

    def run(self):
        try:
            result = self.function()
        except Exception as exc:  # mirrors FunctionWorker's boundary
            if self.on_error is not None:
                self.on_error(str(exc) or type(exc).__name__)
            else:
                raise
            return None
        else:
            self.on_result(result)
            return result
        finally:
            if self.on_finished is not None:
                self.on_finished()


class CapturingSubmitter:
    def __init__(self) -> None:
        self.jobs: list[PendingJob] = []

    def __call__(self, function, on_result, *, on_error=None, on_finished=None):
        job = PendingJob(function, on_result, on_error, on_finished)
        self.jobs.append(job)
        return job


def write_speech_wav(path: Path, *, milliseconds: int = 500) -> None:
    frames = 16_000 * milliseconds // 1000
    # A constant 25%-scale signal is sufficient for the controller's real WAV
    # duration/RMS validation.  Its content never leaves the temporary folder.
    sample = int(32767 * 0.25).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(sample * frames)


def fake_live_snapshot(
    root: Path,
    name: str,
    *,
    start_ms: int,
    end_ms: int,
) -> WavSnapshot:
    path = root / name
    path.write_bytes(b"private live snapshot")
    return WavSnapshot(
        path=path,
        start_frame=start_ms * 16,
        end_frame=end_ms * 16,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def make_controller(root: Path) -> tuple[DictationController, CapturingSubmitter]:
    """Build the orchestration surface without constructing real desktop UI."""

    controller = DictationController.__new__(DictationController)
    QObject.__init__(controller)
    config = replace(
        default_config(),
        microphone_id="mic.test",
        model_path=str(root / "model.bin"),
        vad_model_path=str(root / "vad.bin"),
    )
    controller.config = config
    controller.runtime_dir = root
    controller.machine = DictationStateMachine()
    controller.engine_state = EngineState.READY_VULKAN
    controller.engine = FakeEngine()
    controller.recorder = FakeRecorder()
    controller.insertion = FakeInsertion()
    controller.diagnostics = DiagnosticsArchive(root / "diagnostics")
    controller.tray = FakeTray()
    controller.overlay = FakeOverlay()
    controller.input = FakeInput()
    controller.control = FakeControl()
    controller.pool = FakePool()
    controller._heartbeat_timer = FakeTimer()
    controller._microphone_timer = FakeTimer()
    controller._health_timer = FakeTimer()
    controller._live_timer = FakeTimer()
    controller._input_notifier = None
    controller._settings = None
    controller._download_dialog = None
    controller._operation_bridge = None

    controller._input_requested = True
    controller._input_ready = True
    controller._input_failure = ""
    controller._physical_trigger_down = False
    controller._locked = False
    controller._sleeping = False
    controller._shutting_down = False
    controller._microphones_loaded = True
    controller._microphones_pending = False
    controller._microphones = [Microphone("mic.test", "Test microphone", 1)]

    controller._engine_busy = False
    controller._engine_generation = 0
    controller._engine_cancel = threading.Event()
    controller._engine_restart_pending = False
    controller._engine_force_pending = False
    controller._dictation_generation = 0
    controller._dictation_cancel = threading.Event()
    controller._transcription_paths = set()
    controller._transcription_paths_lock = threading.Lock()
    controller._live_enabled_for_cycle = False
    controller._live_direct_for_cycle = False
    controller._live_request_inflight = False
    controller._live_polling_disabled = False
    controller._live_cancel = threading.Event()
    controller._live_field_cancel = threading.Event()
    controller._live_target_window = None
    controller._live_assumed_text = ""
    controller._live_edit_inflight = False
    controller._live_edit_attached = False
    controller._live_field_state_known = True
    controller._live_final_pending = None
    controller._automatic_limit = False
    controller._diagnostics_enabled_for_cycle = False
    controller._diagnostic_session_for_cycle = None
    controller._pending_insert_text = None
    controller._last_result = None
    controller._benchmark_generation = 0
    controller._benchmark_active = False
    controller._benchmark_waiting = False
    controller._benchmark_reply = None
    controller._health_pending = False
    controller._workers = set()

    submitter = CapturingSubmitter()
    controller._submit = submitter
    return controller, submitter


class ControllerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.controller, self.submitter = make_controller(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def begin_recording(self) -> None:
        self.controller.machine.set_ready()
        self.controller._trigger_pressed()
        self.assertEqual(
            self.controller.machine.state, DictationState.STARTING_RECORDING
        )
        self.controller._recording_started()
        self.assertEqual(self.controller.machine.state, DictationState.RECORDING)

    def finish_recording(self, *, automatic: bool, release_first: bool) -> Path:
        path = self.root / "recording.wav"
        write_speech_wav(path)
        self.controller.recorder.path = path
        if release_first:
            self.controller._trigger_released()
            self.assertEqual(self.controller.machine.state, DictationState.FINALIZING)
        self.controller._recording_finished(str(path), 0.5, automatic)
        self.assertEqual(self.controller.machine.state, DictationState.TRANSCRIBING)
        return path

    def test_complete_cycle_normalizes_cleans_and_inserts_without_enter(self) -> None:
        self.begin_recording()
        path = self.finish_recording(automatic=False, release_first=True)

        transcription = self.submitter.jobs.pop(0)
        result = transcription.run()

        self.assertIsInstance(result, TranscriptionResult)
        self.assertFalse(path.exists(), "transient WAV must be deleted before insertion")
        self.assertEqual(self.controller.machine.state, DictationState.INSERTING)
        self.assertEqual(self.controller._last_result, "Grüße CachyOS.")
        self.assertEqual(self.controller.tray.last_available, [True])
        self.assertEqual(self.controller.overlay.hidden, 1)
        engine = self.controller.engine
        self.assertEqual(len(engine.transcribe_calls), 1)
        self.assertEqual(
            engine.transcribe_calls[0][1], self.controller.config.initial_prompt
        )

        insertion = self.submitter.jobs.pop(0)
        insertion.run()

        self.assertEqual(self.controller.insertion.insert_calls, ["Grüße CachyOS."])
        self.assertEqual(self.controller.machine.state, DictationState.READY)
        self.assertTrue(
            any(
                message == "Einfügen gesendet"
                for message, _ in self.controller.overlay.messages
            )
        )

    def test_live_preview_updates_overlay_but_final_text_is_pasted_once(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, interval_ms=1500),
        )
        snapshot = fake_live_snapshot(
            self.root,
            "preview.wav",
            start_ms=0,
            end_ms=3000,
        )
        self.controller.recorder.snapshots = [snapshot]
        engine = self.controller.engine
        self.assertIsInstance(engine, FakeEngine)
        engine.result = "  Vorläufiger Entwurf  "

        self.begin_recording()
        self.assertEqual(self.controller._live_timer.interval, 1500)
        self.assertEqual(self.controller._live_timer.start_calls, 1)
        self.assertEqual(
            self.controller.overlay.messages[-1][0],
            "Aufnahme mit Live-Vorschau …",
        )

        self.controller._live_tick()
        self.controller._live_tick()
        self.assertEqual(len(self.submitter.jobs), 1, "preview requests must never queue")
        self.submitter.jobs.pop(0).run()

        self.assertFalse(snapshot.path.exists())
        self.assertEqual(engine.transcribe_calls[0][3], 3.0)
        self.assertEqual(self.controller.insertion.insert_calls, [])
        self.assertEqual(
            self.controller.overlay.messages[-1][0],
            "Vorläufiger Entwurf",
        )

        engine.result = "  Endgültiger vollständiger Text.  "
        final_path = self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        self.assertFalse(final_path.exists())
        self.assertEqual(
            self.controller.insertion.insert_calls,
            ["Endgültiger vollständiger Text."],
        )
        self.assertEqual(
            self.controller._last_result,
            "Endgültiger vollständiger Text.",
        )
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_live_preview_uses_only_the_latest_twelve_seconds(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        self.assertIsInstance(recorder, FakeRecorder)
        recorder.recorded_duration_ms = 25_250.0
        snapshot = fake_live_snapshot(
            self.root,
            "bounded-preview.wav",
            start_ms=13_250,
            end_ms=25_250,
        )
        recorder.snapshots = [snapshot]

        self.begin_recording()
        self.controller._live_tick()

        self.assertEqual(recorder.snapshot_starts, [13_250])
        self.assertEqual(len(self.submitter.jobs), 1)
        self.submitter.jobs.pop(0).run()
        self.assertFalse(snapshot.path.exists())
        self.assertTrue(self.controller.overlay.messages[-1][0].startswith("…"))

    def test_live_preview_failure_never_breaks_authoritative_final_transcription(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, interval_ms=1500),
        )
        snapshot = fake_live_snapshot(
            self.root,
            "preview-error.wav",
            start_ms=0,
            end_ms=2000,
        )
        self.controller.recorder.snapshots = [snapshot]
        engine = self.controller.engine
        self.assertIsInstance(engine, FakeEngine)
        engine.error = RuntimeError("Vorschau fehlgeschlagen")

        self.begin_recording()
        self.controller._live_tick()
        with self.assertLogs("local_dictation.controller", level="WARNING"):
            self.submitter.jobs.pop(0).run()

        self.assertTrue(self.controller._live_polling_disabled)
        self.assertEqual(self.controller.machine.state, DictationState.RECORDING)
        self.assertIn(
            "Live-Vorschau pausiert",
            self.controller.overlay.messages[-1][0],
        )

        engine.error = None
        engine.result = "Final bleibt zuverlässig."
        self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        self.assertEqual(
            self.controller.insertion.insert_calls,
            ["Final bleibt zuverlässig."],
        )
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_preview_result_after_release_cannot_replace_working_overlay(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, interval_ms=1500),
        )
        self.controller.recorder.snapshots = [
            fake_live_snapshot(
                self.root,
                "late-preview.wav",
                start_ms=0,
                end_ms=2000,
            )
        ]
        engine = self.controller.engine
        self.assertIsInstance(engine, FakeEngine)
        engine.result = "Dieser Entwurf kommt zu spät."
        self.begin_recording()
        self.controller._live_tick()

        self.finish_recording(automatic=False, release_first=True)
        self.assertTrue(self.controller._live_cancel.is_set())
        self.assertEqual(
            self.controller.overlay.messages[-1][0],
            "Transkription …",
        )
        self.submitter.jobs.pop(0).run()

        self.assertEqual(
            self.controller.overlay.messages[-1][0],
            "Transkription …",
        )
        self.assertEqual(engine.transcribe_calls, [])
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()
        self.assertEqual(len(engine.transcribe_calls), 1)
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_direct_live_text_revises_draft_and_reconciles_authoritative_final(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)
        recorder.recorded_duration_ms = 3000
        recorder.snapshots = [
            fake_live_snapshot(self.root, "direct-1.wav", start_ms=0, end_ms=3000)
        ]
        engine.result = "Ein erster Entwurf"

        self.begin_recording()
        self.assertEqual(insertion.capture_calls, 1)
        self.assertEqual(
            self.controller.overlay.messages[-1][0],
            "Aufnahme mit direktem Live-Text …",
        )
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.assertTrue(self.controller._live_edit_inflight)
        self.submitter.jobs.pop(0).run()

        self.assertEqual(self.controller.machine.state, DictationState.RECORDING)
        self.assertEqual(self.controller._live_assumed_text, "Ein erster Entwurf")
        self.assertEqual(insertion.revise_calls[0][:3], (
            "",
            "Ein erster Entwurf",
            insertion.target_window,
        ))
        self.assertEqual(insertion.insert_calls, [])

        recorder.recorded_duration_ms = 5000
        recorder.snapshots = [
            fake_live_snapshot(self.root, "direct-2.wav", start_ms=0, end_ms=5000)
        ]
        engine.result = "Ein korrigierter Entwurf."
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()
        self.assertEqual(
            self.controller._live_assumed_text,
            "Ein korrigierter Entwurf.",
        )

        engine.result = "Ein vollständig korrigierter Endtext."
        final_path = self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        self.assertFalse(final_path.exists())
        self.assertEqual(
            [call[:2] for call in insertion.revise_calls],
            [
                ("", "Ein erster Entwurf"),
                ("Ein erster Entwurf", "Ein korrigierter Entwurf."),
                (
                    "Ein korrigierter Entwurf.",
                    "Ein vollständig korrigierter Endtext.",
                ),
            ],
        )
        self.assertEqual(
            insertion.copy_calls,
            ["Ein vollständig korrigierter Endtext."],
        )
        self.assertEqual(insertion.insert_calls, [])
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_direct_live_text_always_uses_complete_growing_snapshot(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        self.assertIsInstance(recorder, FakeRecorder)
        recorder.recorded_duration_ms = 25_250
        recorder.snapshots = [
            fake_live_snapshot(
                self.root,
                "direct-full.wav",
                start_ms=0,
                end_ms=25_250,
            )
        ]

        self.begin_recording()
        self.controller._live_tick()

        self.assertEqual(recorder.snapshot_starts, [0])
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

    def test_final_reconcile_waits_for_already_running_live_edit(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)
        recorder.recorded_duration_ms = 2500
        recorder.snapshots = [
            fake_live_snapshot(self.root, "direct-race.wav", start_ms=0, end_ms=2500)
        ]
        engine.result = "Laufender Entwurf"
        self.begin_recording()
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.assertTrue(self.controller._live_edit_inflight)

        engine.result = "Autoritativer Endtext"
        self.finish_recording(automatic=False, release_first=True)
        final_transcription = self.submitter.jobs.pop(1)
        final_transcription.run()

        self.assertEqual(self.controller.machine.state, DictationState.INSERTING)
        self.assertIsNotNone(self.controller._live_final_pending)
        self.assertEqual(len(self.submitter.jobs), 1)

        self.submitter.jobs.pop(0).run()
        self.assertIsNone(self.controller._live_final_pending)
        self.assertEqual(len(self.submitter.jobs), 1)
        self.submitter.jobs.pop(0).run()

        self.assertEqual(
            [call[:2] for call in insertion.revise_calls],
            [
                ("", "Laufender Entwurf"),
                ("Laufender Entwurf", "Autoritativer Endtext"),
            ],
        )
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_release_cancelled_live_edit_stays_attached_for_final_reconcile(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)

        recorder.recorded_duration_ms = 2500
        recorder.snapshots = [
            fake_live_snapshot(
                self.root,
                "direct-release-confirmed.wav",
                start_ms=0,
                end_ms=2500,
            )
        ]
        engine.result = "Bestätigter Entwurf"
        self.begin_recording()
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()
        self.assertEqual(self.controller._live_assumed_text, "Bestätigter Entwurf")

        recorder.recorded_duration_ms = 4000
        recorder.snapshots = [
            fake_live_snapshot(
                self.root,
                "direct-release-cancelled.wav",
                start_ms=0,
                end_ms=4000,
            )
        ]
        engine.result = "Noch nicht bestätigter Zwischenstand"
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.assertTrue(self.controller._live_edit_inflight)

        insertion.revision_result = RevisionResult(
            False,
            False,
            True,
            "Live-Einfügung wurde abgebrochen",
            cancelled=True,
        )
        engine.result = "Autoritativer Endtext"
        self.finish_recording(automatic=False, release_first=True)
        final_transcription = self.submitter.jobs.pop(1)
        final_transcription.run()
        self.assertIsNotNone(self.controller._live_final_pending)

        self.submitter.jobs.pop(0).run()

        self.assertTrue(self.controller._live_edit_attached)
        self.assertTrue(self.controller._live_field_state_known)
        self.assertEqual(self.controller._live_assumed_text, "Bestätigter Entwurf")
        self.assertIsNone(self.controller._live_final_pending)
        self.assertEqual(len(self.submitter.jobs), 1)

        insertion.revision_result = RevisionResult(
            True,
            True,
            True,
            "Live-Text aktualisiert",
        )
        self.submitter.jobs.pop(0).run()

        self.assertEqual(
            [call[:2] for call in insertion.revise_calls],
            [
                ("", "Bestätigter Entwurf"),
                ("Bestätigter Entwurf", "Noch nicht bestätigter Zwischenstand"),
                ("Bestätigter Entwurf", "Autoritativer Endtext"),
            ],
        )
        self.assertEqual(insertion.copy_calls, ["Autoritativer Endtext"])
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_focus_failure_after_release_detaches_instead_of_counting_as_cancel(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)

        recorder.recorded_duration_ms = 2500
        recorder.snapshots = [
            fake_live_snapshot(
                self.root,
                "direct-focus-confirmed.wav",
                start_ms=0,
                end_ms=2500,
            )
        ]
        engine.result = "Bestätigter Entwurf"
        self.begin_recording()
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        recorder.recorded_duration_ms = 4000
        recorder.snapshots = [
            fake_live_snapshot(
                self.root,
                "direct-focus-failed.wav",
                start_ms=0,
                end_ms=4000,
            )
        ]
        engine.result = "Hypothese bei verlorenem Fokus"
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.assertTrue(self.controller._live_edit_inflight)

        insertion.revision_result = RevisionResult(
            True,
            False,
            True,
            "Das aktive Fenster hat sich geändert",
            cancelled=False,
        )
        engine.result = "Autoritativer Endtext"
        self.finish_recording(automatic=False, release_first=True)
        final_transcription = self.submitter.jobs.pop(1)
        final_transcription.run()

        with self.assertLogs("local_dictation.controller", level="WARNING"):
            self.submitter.jobs.pop(0).run()

        self.assertFalse(self.controller._live_edit_attached)
        self.assertTrue(self.controller._live_field_state_known)
        self.assertEqual(self.controller._live_assumed_text, "Bestätigter Entwurf")
        self.assertEqual(len(self.submitter.jobs), 1)
        self.submitter.jobs.pop(0).run()

        self.assertEqual(
            [call[:2] for call in insertion.revise_calls],
            [
                ("", "Bestätigter Entwurf"),
                ("Bestätigter Entwurf", "Hypothese bei verlorenem Fokus"),
            ],
        )
        self.assertEqual(insertion.copy_calls, ["Autoritativer Endtext"])
        self.assertIn("Endtext nur kopiert", self.controller.overlay.messages[-1][0])
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_cancel_during_final_reconcile_never_starts_clipboard_fallback(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)

        recorder.recorded_duration_ms = 2500
        recorder.snapshots = [
            fake_live_snapshot(
                self.root,
                "direct-final-cancel.wav",
                start_ms=0,
                end_ms=2500,
            )
        ]
        engine.result = "Bestätigter Entwurf"
        self.begin_recording()
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        def cancel_during_reconcile(
            previous: str,
            current: str,
            *,
            expected_window: str,
            cancel: object | None = None,
        ) -> RevisionResult:
            insertion.revise_calls.append(
                (previous, current, expected_window, cancel)
            )
            self.controller._session_locked(True)
            return RevisionResult(
                True,
                False,
                True,
                "Live-Einfügung wurde abgebrochen",
                cancelled=True,
            )

        insertion.revise = cancel_during_reconcile  # type: ignore[method-assign]
        engine.result = "Autoritativer Endtext"
        self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()
        self.assertEqual(len(self.submitter.jobs), 1)

        self.submitter.jobs.pop(0).run()

        self.assertEqual(
            [call[:2] for call in insertion.revise_calls],
            [
                ("", "Bestätigter Entwurf"),
                ("Bestätigter Entwurf", "Autoritativer Endtext"),
            ],
        )
        self.assertEqual(insertion.copy_calls, [])
        self.assertEqual(insertion.insert_calls, [])
        self.assertEqual(self.controller.machine.state, DictationState.SUSPENDED)

    def test_physical_keyboard_activity_detaches_and_final_only_copies(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)
        recorder.recorded_duration_ms = 2500
        recorder.snapshots = [
            fake_live_snapshot(self.root, "direct-activity.wav", start_ms=0, end_ms=2500)
        ]
        engine.result = "Live im Feld"
        self.begin_recording()
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        with self.assertLogs("local_dictation.controller", level="WARNING"):
            self.controller._handle_input_event({"type": "activity", "source": "keyboard"})
        self.assertFalse(self.controller._live_edit_attached)
        self.assertFalse(self.controller._live_field_state_known)

        engine.result = "Sicherer vollständiger Endtext"
        self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        self.assertEqual(len(insertion.revise_calls), 1)
        self.assertEqual(insertion.copy_calls, ["Sicherer vollständiger Endtext"])
        self.assertEqual(insertion.insert_calls, [])
        self.assertIn("Endtext nur kopiert", self.controller.overlay.messages[-1][0])
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_missing_kwin_target_falls_back_to_nondestructive_preview(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        insertion = self.controller.insertion
        self.assertIsInstance(insertion, FakeInsertion)
        insertion.target_window = None

        with self.assertLogs("local_dictation.controller", level="WARNING"):
            self.begin_recording()

        self.assertFalse(self.controller._live_direct_for_cycle)
        self.assertEqual(
            self.controller.overlay.messages[-1][0],
            "Aufnahme mit Live-Vorschau …",
        )

    def test_direct_live_edit_failure_detaches_and_final_only_copies(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)
        recorder.recorded_duration_ms = 2500
        recorder.snapshots = [
            fake_live_snapshot(self.root, "direct-fail.wav", start_ms=0, end_ms=2500)
        ]
        insertion.revision_result = RevisionResult(
            True,
            False,
            True,
            "Das aktive Fenster hat sich geändert",
        )
        engine.result = "Nicht eingefügter Entwurf"
        self.begin_recording()
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        with self.assertLogs("local_dictation.controller", level="WARNING"):
            self.submitter.jobs.pop(0).run()

        self.assertFalse(self.controller._live_edit_attached)
        self.assertTrue(self.controller._live_field_state_known)
        engine.result = "Vollständiger Endtext"
        self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        self.assertEqual(len(insertion.revise_calls), 1)
        self.assertEqual(insertion.copy_calls, ["Vollständiger Endtext"])
        self.assertIn("Endtext nur kopiert", self.controller.overlay.messages[-1][0])

    def test_direct_live_transcription_freezes_after_one_minute(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        self.assertIsInstance(recorder, FakeRecorder)
        recorder.recorded_duration_ms = 60_001
        self.begin_recording()

        with self.assertLogs("local_dictation.controller", level="INFO"):
            self.controller._live_tick()

        self.assertTrue(self.controller._live_polling_disabled)
        self.assertEqual(recorder.snapshot_starts, [])
        self.assertEqual(self.submitter.jobs, [])

    def test_final_failure_leaves_visible_live_draft_without_more_keys(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            live=LiveConfig(enabled=True, direct_insert=True, interval_ms=1500),
        )
        recorder = self.controller.recorder
        insertion = self.controller.insertion
        engine = self.controller.engine
        self.assertIsInstance(recorder, FakeRecorder)
        self.assertIsInstance(insertion, FakeInsertion)
        self.assertIsInstance(engine, FakeEngine)
        recorder.recorded_duration_ms = 2500
        recorder.snapshots = [
            fake_live_snapshot(self.root, "direct-final-error.wav", start_ms=0, end_ms=2500)
        ]
        engine.result = "Brauchbarer Live-Entwurf"
        self.begin_recording()
        self.controller._live_tick()
        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        engine.error = RuntimeError("Finale Transkription fehlgeschlagen")
        self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()

        self.assertEqual(len(insertion.revise_calls), 1)
        self.assertIn("Live-Entwurf bleibt im Feld", self.controller.overlay.messages[-1][0])
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_error_warns_when_unconfirmed_live_edit_may_have_reached_field(self) -> None:
        self.controller._live_direct_for_cycle = True
        self.controller._live_edit_inflight = True

        self.controller._cycle_error("Finale Transkription fehlgeschlagen")

        self.assertIn(
            "Live-Entwurf könnte im Feld bleiben",
            self.controller.overlay.messages[-1][0],
        )

    def test_diagnostics_opt_in_keeps_private_audio_transcript_and_timings(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=20),
        )
        self.begin_recording()
        path = self.finish_recording(automatic=False, release_first=True)

        bundles = list(self.controller.diagnostics.directory.iterdir())
        self.assertEqual(len(bundles), 1)
        bundle = bundles[0]
        self.assertTrue((bundle / AUDIO_FILENAME).is_file())
        self.assertTrue(path.exists(), "archive must not replace transient cleanup ownership")

        self.submitter.jobs.pop(0).run()

        self.assertFalse(path.exists())
        self.assertEqual(
            (bundle / TRANSCRIPT_FILENAME).read_text(encoding="utf-8"),
            "Grüße CachyOS.",
        )
        metadata = json.loads((bundle / METADATA_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "success")
        self.assertAlmostEqual(metadata["timings"]["audio_seconds"], 0.5)
        self.assertGreaterEqual(metadata["timings"]["inference_seconds"], 0.0)
        self.assertNotIn("Grüße CachyOS.", json.dumps(metadata, ensure_ascii=False))

        self.submitter.jobs.pop(0).run()
        insertion = json.loads((bundle / INSERTION_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(insertion["status"], "shortcut-sent")
        self.assertTrue(insertion["copied"])
        self.assertTrue(insertion["shortcut_sent"])

    def test_diagnostics_records_transcription_error_without_blocking_cleanup(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=20),
        )
        self.controller.engine.error = RuntimeError("Lokale Inferenz fehlgeschlagen")
        self.begin_recording()
        path = self.finish_recording(automatic=False, release_first=True)

        self.submitter.jobs.pop(0).run()

        bundle = next(self.controller.diagnostics.directory.iterdir())
        metadata = json.loads((bundle / METADATA_FILENAME).read_text(encoding="utf-8"))
        self.assertFalse(path.exists())
        self.assertEqual((bundle / TRANSCRIPT_FILENAME).read_bytes(), b"")
        self.assertEqual(metadata["status"], "error")
        self.assertEqual(metadata["error"]["stage"], "transcription")
        self.assertEqual(metadata["error"]["message"], "Lokale Inferenz fehlgeschlagen")
        self.assertEqual(self.controller.insertion.insert_calls, [])

    def test_diagnostics_archive_failure_never_breaks_dictation(self) -> None:
        class BrokenDiagnostics:
            def archive_audio(self, _path: Path):
                raise OSError("private archive unavailable")

        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=20),
        )
        self.controller.diagnostics = BrokenDiagnostics()
        self.begin_recording()
        with self.assertLogs("local_dictation.controller", level="ERROR") as captured:
            path = self.finish_recording(automatic=False, release_first=True)

        self.submitter.jobs.pop(0).run()

        self.assertFalse(path.exists())
        self.assertEqual(self.controller.machine.state, DictationState.INSERTING)
        self.assertIn("Diagnoseaufnahme", "\n".join(captured.output))
        self.assertNotIn("Grüße CachyOS.", "\n".join(captured.output))
        self.submitter.jobs.pop(0).run()
        self.assertTrue(
            any(
                message.startswith("Shortcut gesendet, aber diagnosedaten")
                for message, _ in self.controller.overlay.messages
            )
        )

    def test_copy_failure_is_saved_with_successful_transcript(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=20),
        )
        self.controller.insertion.result = InsertionResult(
            False,
            False,
            "Text konnte nicht kopiert werden",
        )
        self.begin_recording()
        self.finish_recording(automatic=False, release_first=True)

        self.submitter.jobs.pop(0).run()
        self.submitter.jobs.pop(0).run()

        bundle = next(self.controller.diagnostics.directory.iterdir())
        insertion = json.loads((bundle / INSERTION_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(insertion["status"], "error")
        self.assertFalse(insertion["copied"])
        self.assertFalse(insertion["shortcut_sent"])
        self.assertEqual(
            insertion["error"]["message"],
            "Text konnte nicht kopiert werden",
        )

    def test_stale_insertion_diagnostic_failure_cannot_taint_new_cycle(self) -> None:
        class BrokenInsertionDiagnostics:
            def record_insertion(self, *_args, **_kwargs):
                raise OSError("simulated stale failure")

        stale_session = DiagnosticSession(
            session_id="dictation-20260905T120000.000000Z-012345abcdef",
            directory=self.root / "diagnostics" / "stale",
            created_at="2026-09-05T12:00:00.000000Z",
            audio_bytes=1,
        )
        self.controller.diagnostics = BrokenInsertionDiagnostics()
        self.controller._dictation_generation = 2
        self.controller._diagnostics_warning_for_cycle = "Warnung des neuen Zyklus"

        with self.assertLogs("local_dictation.controller", level="ERROR"):
            self.controller._insertion_completed(
                1,
                InsertionResult(True, True, "Einfügen gesendet"),
                stale_session,
            )

        self.assertEqual(
            self.controller._diagnostics_warning_for_cycle,
            "Warnung des neuen Zyklus",
        )

    def test_too_short_recording_is_archived_only_when_opted_in(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=20),
        )
        self.begin_recording()
        path = self.root / "short.wav"
        write_speech_wav(path, milliseconds=100)
        self.controller.recorder.path = path
        self.controller._trigger_released()
        self.controller._recording_finished(str(path), 0.1, False)

        self.submitter.jobs.pop(0).run()

        bundle = next(self.controller.diagnostics.directory.iterdir())
        metadata = json.loads((bundle / METADATA_FILENAME).read_text(encoding="utf-8"))
        self.assertFalse(path.exists())
        self.assertTrue((bundle / AUDIO_FILENAME).is_file())
        self.assertEqual(metadata["status"], "error")
        self.assertEqual(metadata["error"]["stage"], "audio-validation")

    def test_diagnostics_setting_is_snapshotted_when_recording_starts(self) -> None:
        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=20),
        )
        self.begin_recording()
        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=False, retention_entries=20),
        )

        self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()

        self.assertEqual(len(list(self.controller.diagnostics.directory.iterdir())), 1)

    def test_enabling_diagnostics_mid_recording_does_not_retain_that_cycle(self) -> None:
        self.begin_recording()
        self.controller.config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=20),
        )

        self.finish_recording(automatic=False, release_first=True)
        self.submitter.jobs.pop(0).run()

        self.assertFalse(self.controller.diagnostics.directory.exists())

    def test_release_before_qprocess_started_remains_finalizing(self) -> None:
        self.controller.machine.set_ready()
        self.controller._trigger_pressed()

        self.controller._trigger_released()
        self.controller._recording_started()

        self.assertEqual(self.controller.machine.state, DictationState.FINALIZING)
        self.assertEqual(self.controller.recorder.stop_calls, [
            {"discard": False, "automatic_limit": False}
        ])

    def test_maximum_duration_never_inserts_until_physical_release(self) -> None:
        self.begin_recording()
        path = self.finish_recording(automatic=True, release_first=False)

        self.submitter.jobs.pop(0).run()

        self.assertFalse(path.exists())
        self.assertEqual(
            self.controller.machine.state, DictationState.WAITING_FOR_RELEASE
        )
        self.assertEqual(self.controller.insertion.insert_calls, [])
        self.assertEqual(len(self.submitter.jobs), 0)

        self.controller._trigger_released()
        self.assertEqual(self.controller.machine.state, DictationState.INSERTING)
        self.assertEqual(len(self.submitter.jobs), 1)
        self.submitter.jobs.pop(0).run()
        self.assertEqual(self.controller.machine.state, DictationState.READY)

    def test_press_during_transcription_is_ignored_as_a_new_cycle(self) -> None:
        self.begin_recording()
        self.finish_recording(automatic=False, release_first=True)
        starts = list(self.controller.recorder.start_calls)

        self.controller._trigger_pressed()

        self.assertEqual(self.controller.machine.state, DictationState.TRANSCRIBING)
        self.assertEqual(self.controller.recorder.start_calls, starts)

    def test_cancelled_stale_transcription_deletes_audio_and_cannot_insert(self) -> None:
        self.begin_recording()
        path = self.finish_recording(automatic=False, release_first=True)
        old_generation = self.controller._dictation_generation

        self.controller._cancel_cycle(suspended=True)
        self.submitter.jobs.pop(0).run()

        self.assertGreater(self.controller._dictation_generation, old_generation)
        self.assertFalse(path.exists())
        self.assertEqual(self.controller.machine.state, DictationState.SUSPENDED)
        self.assertEqual(self.controller.insertion.insert_calls, [])
        self.assertEqual(self.submitter.jobs, [])

    def test_cancel_during_blocking_transcription_removes_wav_immediately(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingEngine(FakeEngine):
            def transcribe(
                engine_self,
                path: Path,
                *,
                initial_prompt: str,
                cancel_event: object | None = None,
            ) -> str:
                engine_self.transcribe_calls.append(
                    (Path(path), initial_prompt, cancel_event)
                )
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test worker was not released")
                return engine_self.result

        self.controller.engine = BlockingEngine()
        self.begin_recording()
        path = self.finish_recording(automatic=False, release_first=True)
        transcription = self.submitter.jobs.pop(0)
        worker = threading.Thread(target=transcription.run, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2), "transcription never reached engine")
            self.assertTrue(path.exists())

            self.controller._cancel_cycle(suspended=True)

            self.assertTrue(
                worker.is_alive(), "worker must still be blocked for timing assertion"
            )
            self.assertFalse(
                path.exists(),
                "cancellation must unlink audio before inference returns",
            )
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.controller._transcription_paths, set())
        self.assertEqual(self.controller.machine.state, DictationState.SUSPENDED)

    def test_engine_failure_deletes_audio_and_returns_to_ready(self) -> None:
        self.begin_recording()
        path = self.finish_recording(automatic=False, release_first=True)
        self.controller.engine.error = RuntimeError("Lokale Inferenz fehlgeschlagen")

        self.submitter.jobs.pop(0).run()

        self.assertFalse(path.exists())
        self.assertEqual(self.controller.machine.state, DictationState.READY)
        self.assertEqual(self.controller.insertion.insert_calls, [])
        self.assertTrue(
            any(
                message == "Lokale Inferenz fehlgeschlagen"
                for message, _ in self.controller.overlay.messages
            )
        )

    def test_recording_finished_during_shutdown_deletes_audio_immediately(self) -> None:
        path = self.root / "shutdown-recording.wav"
        write_speech_wav(path)
        self.controller._shutting_down = True
        self.controller.machine.shutdown()

        self.controller._recording_finished(str(path), 0.5, False)

        self.assertFalse(path.exists())
        self.assertEqual(self.controller.machine.state, DictationState.SHUTTING_DOWN)
        self.assertEqual(self.submitter.jobs, [])

    def test_stale_engine_launch_is_closed_instead_of_installed(self) -> None:
        stale = FakeEngine()
        current = self.controller.engine
        self.controller._engine_generation = 8

        self.controller._engine_started(
            7,
            EngineLaunchResult(
                status="ok", engine=stale, config=self.controller.config
            ),
        )

        self.assertEqual(stale.close_calls, 1)
        self.assertIs(self.controller.engine, current)

    def test_engine_start_preserves_newer_diagnostics_and_non_engine_settings(self) -> None:
        launched_config = self.controller.config
        self.controller._engine_generation = 6
        self.controller.config = replace(
            self.controller.config,
            microphone_id="new.mic",
            initial_prompt="Neue Fachwörter",
            diagnostics=DiagnosticsConfig(enabled=True, retention_entries=7),
        )
        launched_engine = FakeEngine()
        outcome = EngineLaunchResult(
            status="ok",
            engine=launched_engine,
            config=replace(
                launched_config,
                model_path="/downloaded/main.bin",
                vad_model_path="/downloaded/vad.bin",
            ),
        )

        with mock.patch("local_dictation.controller.save_config") as saved:
            self.controller._engine_started(6, outcome)

        self.assertIs(self.controller.engine, launched_engine)
        self.assertEqual(self.controller.config.microphone_id, "new.mic")
        self.assertEqual(self.controller.config.initial_prompt, "Neue Fachwörter")
        self.assertEqual(
            self.controller.config.diagnostics,
            DiagnosticsConfig(enabled=True, retention_entries=7),
        )
        self.assertEqual(self.controller.config.model_path, "/downloaded/main.bin")
        saved.assert_called_once_with(self.controller.config)

    def test_pending_engine_restart_rejects_stale_success_snapshot(self) -> None:
        current_config = replace(
            self.controller.config,
            diagnostics=DiagnosticsConfig(enabled=False, retention_entries=3),
        )
        self.controller.config = current_config
        self.controller.engine = None
        self.controller._engine_generation = 4
        self.controller._engine_restart_pending = True
        stale_engine = FakeEngine()

        with mock.patch("local_dictation.controller.save_config") as saved:
            self.controller._engine_started(
                4,
                EngineLaunchResult(
                    status="ok",
                    engine=stale_engine,
                    config=replace(
                        current_config,
                        diagnostics=DiagnosticsConfig(enabled=True, retention_entries=99),
                    ),
                ),
            )

        self.assertEqual(stale_engine.close_calls, 1)
        self.assertIsNone(self.controller.engine)
        self.assertEqual(self.controller.config, current_config)
        saved.assert_not_called()

    def test_cold_start_benchmark_waits_for_engine_then_runs(self) -> None:
        replies: list[dict[str, object]] = []
        self.controller.engine = None
        self.controller.engine_state = EngineState.STARTING
        self.controller._engine_busy = True
        self.controller._engine_generation = 4

        self.controller.start_benchmark(replies.append)

        self.assertTrue(self.controller._benchmark_waiting)
        self.assertEqual(replies, [])
        self.assertEqual(self.submitter.jobs, [])

        engine = FakeEngine("CachyOS Vulkan PyQt6 Radeon 860M")
        outcome = EngineLaunchResult(
            status="ok", engine=engine, config=self.controller.config
        )
        with mock.patch("local_dictation.controller.save_config"):
            self.controller._engine_started(4, outcome)
        self.controller._engine_operation_finished()

        self.assertFalse(self.controller._benchmark_waiting)
        self.assertTrue(self.controller._benchmark_active)
        self.assertEqual(len(self.submitter.jobs), 1)
        self.assertEqual(replies, [])

        self.submitter.jobs.pop(0).run()

        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0]["ok"])
        self.assertFalse(self.controller._benchmark_active)

    def test_cold_start_benchmark_gets_clean_engine_failure_reply(self) -> None:
        replies: list[dict[str, object]] = []
        self.controller.engine = None
        self.controller.engine_state = EngineState.STARTING
        self.controller._engine_busy = True
        self.controller._engine_generation = 9

        self.controller.start_benchmark(replies.append)
        self.assertTrue(self.controller._benchmark_waiting)

        with self.assertLogs("local_dictation.controller", level="ERROR"):
            self.controller._engine_started(
                9,
                EngineLaunchResult(status="error", error="Modellstart fehlgeschlagen"),
            )
        self.controller._engine_operation_finished()

        self.assertFalse(self.controller._benchmark_waiting)
        self.assertFalse(self.controller._benchmark_active)
        self.assertIsNone(self.controller._benchmark_reply)
        self.assertEqual(len(replies), 1)
        self.assertFalse(replies[0]["ok"])
        self.assertIn("Modellstart fehlgeschlagen", str(replies[0]["error"]))
        self.assertEqual(self.submitter.jobs, [])

    def test_engine_restart_cancels_and_discards_active_recording(self) -> None:
        self.begin_recording()
        generation = self.controller._dictation_generation

        self.controller._begin_engine_start(force_download=False)

        self.assertGreater(self.controller._dictation_generation, generation)
        self.assertTrue(self.controller._dictation_cancel.is_set())
        self.assertEqual(
            self.controller.recorder.stop_calls[-1],
            {"discard": True, "automatic_limit": False},
        )
        self.assertEqual(self.controller.machine.state, DictationState.DISABLED)
        self.assertTrue(self.controller._engine_busy)
        self.assertEqual(len(self.submitter.jobs), 1)

    def test_shutdown_is_idempotent_and_closes_owned_components(self) -> None:
        engine = self.controller.engine
        self.controller.recorder.active = True

        self.controller.shutdown()
        self.controller.shutdown()

        self.assertTrue(self.controller._shutting_down)
        self.assertEqual(self.controller.machine.state, DictationState.SHUTTING_DOWN)
        self.assertEqual(self.controller.engine_state, EngineState.STOPPED)
        self.assertIsNone(self.controller.engine)
        self.assertEqual(engine.cancel_calls, 1)
        self.assertEqual(engine.close_calls, 1)
        self.assertEqual(self.controller.recorder.shutdown_calls, 1)
        self.assertEqual(self.controller.input.stop_calls, [1.0])
        self.assertIn("shutdown", self.controller.input.disable_calls)
        self.assertEqual(self.controller.control.close_calls, 1)
        self.assertEqual(self.controller.pool.wait_calls, [2500])
        self.assertEqual(self.controller.overlay.closed, 1)
        self.assertEqual(self.controller.tray.icon.hide_calls, 1)

    def test_late_worker_error_cannot_overwrite_shutdown_state(self) -> None:
        generation = self.controller._dictation_generation
        self.controller.shutdown()

        self.controller._transcription_worker_failed(generation, "late failure")
        self.controller._recording_failed("late recorder failure")

        self.assertEqual(self.controller.machine.state, DictationState.SHUTTING_DOWN)

    def test_input_loss_during_recording_cancels_and_discards_cycle(self) -> None:
        self.begin_recording()
        generation = self.controller._dictation_generation

        self.controller._handle_input_event(
            {"type": "status", "state": "input-unavailable"}
        )

        self.assertGreater(self.controller._dictation_generation, generation)
        self.assertEqual(
            self.controller.recorder.stop_calls[-1],
            {"discard": True, "automatic_limit": False},
        )
        self.assertEqual(self.controller.machine.state, DictationState.DISABLED)

    def test_helper_watchdog_disable_is_automatically_reenabled(self) -> None:
        self.controller.machine.set_ready()
        self.controller._input_requested = True

        self.controller._handle_input_event(
            {"type": "status", "state": "input-disabled", "all_grabbed": False}
        )

        self.assertEqual(self.controller.input.enable_calls, ["ready"])
        self.assertTrue(self.controller._input_requested)
        self.assertEqual(self.controller.machine.state, DictationState.DISABLED)

    def test_fatal_input_error_during_recording_cancels_cycle(self) -> None:
        self.begin_recording()
        generation = self.controller._dictation_generation

        with self.assertLogs("local_dictation.controller", level="ERROR"):
            self.controller._handle_input_event(
                {
                    "type": "error",
                    "code": "backend-failed",
                    "message": "device disappeared",
                    "fatal": True,
                }
            )

        self.assertGreater(self.controller._dictation_generation, generation)
        self.assertEqual(
            self.controller.recorder.stop_calls[-1],
            {"discard": True, "automatic_limit": False},
        )
        self.assertEqual(self.controller.machine.state, DictationState.DISABLED)

    def test_selected_microphone_loss_during_recording_discards_cycle(self) -> None:
        self.begin_recording()
        generation = self.controller._dictation_generation

        self.controller._microphones_refreshed([])

        self.assertGreater(self.controller._dictation_generation, generation)
        self.assertEqual(
            self.controller.recorder.stop_calls[-1],
            {"discard": True, "automatic_limit": False},
        )
        self.assertEqual(self.controller.machine.state, DictationState.DISABLED)

    def test_stale_transcription_completion_is_ignored(self) -> None:
        self.controller.machine.state = DictationState.TRANSCRIBING
        self.controller._dictation_generation = 5

        self.controller._transcription_completed(
            4, TranscriptionResult(ok=True, text="must not be inserted")
        )

        self.assertEqual(self.controller.machine.state, DictationState.TRANSCRIBING)
        self.assertIsNone(self.controller._last_result)
        self.assertEqual(self.submitter.jobs, [])

    def test_queued_insertion_is_cancelled_if_session_locks_before_worker_runs(self) -> None:
        self.controller.machine.state = DictationState.INSERTING
        self.controller._dictation_generation = 3
        self.controller._begin_insertion("must not reach lock screen", 3)
        insertion_job = self.submitter.jobs.pop(0)

        self.controller._session_locked(True)
        insertion_job.run()

        self.assertEqual(self.controller.machine.state, DictationState.SUSPENDED)
        self.assertEqual(self.controller.insertion.insert_calls, [])

    def test_queued_insertion_is_cancelled_on_suspend_before_worker_runs(self) -> None:
        self.controller.machine.state = DictationState.INSERTING
        self.controller._dictation_generation = 9
        self.controller._begin_insertion("must not be sent after suspend", 9)
        insertion_job = self.submitter.jobs.pop(0)

        self.controller._prepare_for_sleep(True)
        insertion_job.run()

        self.assertTrue(self.controller._sleeping)
        self.assertEqual(self.controller.machine.state, DictationState.SUSPENDED)
        self.assertEqual(self.controller.insertion.insert_calls, [])


if __name__ == "__main__":
    unittest.main()
