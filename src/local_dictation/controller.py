"""Qt orchestration for recording, local transcription, and safe insertion.

Transcripts stay in memory unless the user explicitly enables the bounded,
private diagnostic archive. Recognized text is never written to application
logs, even when that archive is enabled.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QSocketNotifier, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QMessageBox

from .audio import cleanup_audio, validate_wav
from .config import AppConfig, get_models_dir, parse_config, save_config
from .control import ControlServer
from .diagnostics import DiagnosticsArchive, DiagnosticSession
from .engine import (
    BackendUnavailableError,
    EngineCancelled,
    EngineConfig,
    WhisperEngine,
)
from .input_proxy import InputProxyHandle, spawn_input_proxy
from .insertion import InsertionBackend, InsertionResult, RevisionResult
from .microphones import Microphone, list_microphones
from .model_store import (
    MAIN_MODEL,
    VAD_MODEL,
    DownloadCancelled,
    ModelStore,
    validate_required_models,
)
from .recorder import Recorder
from .session import SessionMonitor
from .state import DictationState, DictationStateMachine, EngineState
from .text import normalize_transcript
from .ui import DownloadDialog, Overlay, SettingsDialog, Tray
from .workers import FunctionWorker

LOG = logging.getLogger(__name__)
LIVE_PREVIEW_WINDOW_MS = 12_000
LIVE_PREVIEW_TIMEOUT_S = 3.0
LIVE_DIRECT_MAX_AUDIO_MS = 60_000


@dataclass(slots=True)
class EngineLaunchResult:
    status: str
    engine: WhisperEngine | None = None
    config: AppConfig | None = None
    error: str = ""
    insertion_warning: str = ""


@dataclass(slots=True)
class TranscriptionResult:
    ok: bool
    text: str = ""
    error: str = ""
    audio_seconds: float = 0.0
    inference_seconds: float = 0.0
    diagnostic_session: DiagnosticSession | None = None
    diagnostic_warning: str = ""


class OperationBridge(QObject):
    download_started = pyqtSignal()
    download_progress = pyqtSignal(str, int, int)
    phase = pyqtSignal(str)


class DictationController(QObject):
    """Own all long-lived helpers and serialize user-visible operations."""

    def __init__(
        self,
        app: QApplication,
        config: AppConfig,
        runtime_dir: Path,
        *,
        input_factory: Callable[..., InputProxyHandle] = spawn_input_proxy,
        engine_factory: Callable[[EngineConfig], WhisperEngine] = WhisperEngine,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__()
        self.app = app
        self.config = config
        self.runtime_dir = runtime_dir
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._input_factory = input_factory
        self._engine_factory = engine_factory
        self.pool = thread_pool or QThreadPool.globalInstance()
        self.machine = DictationStateMachine()
        self.engine_state = EngineState.MISSING_MODELS
        self.engine: WhisperEngine | None = None
        self.recorder = Recorder(runtime_dir, self)
        self.insertion = InsertionBackend(runtime_dir)
        self.diagnostics = DiagnosticsArchive(
            max_entries=config.diagnostics.retention_entries
        )
        self.tray = Tray(self)
        self.overlay = Overlay()
        self.session = SessionMonitor(self)
        self.control = ControlServer(runtime_dir / "control.sock", self._handle_control, self)

        self.input: InputProxyHandle | None = None
        self._input_notifier: QSocketNotifier | None = None
        self._input_requested = False
        self._input_ready = False
        self._input_failure = ""
        self._physical_trigger_down = False
        self._locked = False
        self._sleeping = False
        self._shutting_down = False

        self._engine_busy = False
        self._engine_generation = 0
        self._engine_cancel = threading.Event()
        self._engine_restart_pending = False
        self._engine_force_pending = False
        self._operation_bridge: OperationBridge | None = None
        self._download_dialog: DownloadDialog | None = None

        self._dictation_generation = 0
        self._dictation_cancel = threading.Event()
        self._automatic_limit = False
        self._diagnostics_enabled_for_cycle = False
        self._diagnostic_session_for_cycle: DiagnosticSession | None = None
        self._diagnostics_warning_for_cycle = ""
        self._pending_insert_text: str | None = None
        self._last_result: str | None = None
        self._transcription_paths: set[Path] = set()
        self._transcription_paths_lock = threading.Lock()

        # Live inference is deliberately orthogonal to the public dictation
        # state machine. The safe mode only updates the overlay; the explicit
        # experimental mode maintains a guarded draft in the target field.
        # The authoritative full recording is always transcribed after release.
        # Every field is reset per trigger generation so late results are harmless.
        self._live_enabled_for_cycle = False
        self._live_direct_for_cycle = False
        self._live_request_inflight = False
        self._live_polling_disabled = False
        self._live_cancel = threading.Event()
        self._live_field_cancel = threading.Event()
        self._live_target_window: str | None = None
        self._live_assumed_text = ""
        self._live_edit_inflight = False
        self._live_edit_attached = False
        self._live_field_state_known = True
        self._live_final_pending: tuple[str, int] | None = None

        self._benchmark_generation = 0
        self._benchmark_active = False
        self._benchmark_waiting = False
        self._benchmark_reply: Callable[[dict[str, Any]], None] | None = None
        self._health_pending = False
        self._microphones_pending = False
        self._microphones_loaded = False
        self._microphones: list[Microphone] = []
        self._settings: SettingsDialog | None = None
        self._workers: set[FunctionWorker] = set()

        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(1000)
        self._heartbeat_timer.timeout.connect(self._heartbeat)
        self._microphone_timer = QTimer(self)
        self._microphone_timer.setInterval(15_000)
        self._microphone_timer.timeout.connect(self.refresh_microphones)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(30_000)
        self._health_timer.timeout.connect(self._check_health)
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)

        self.recorder.started.connect(self._recording_started)
        self.recorder.finished.connect(self._recording_finished)
        self.recorder.failed.connect(self._recording_failed)
        self.recorder.discarded.connect(self._recording_discarded)
        self.session.locked_changed.connect(self._session_locked)
        self.session.prepare_for_sleep.connect(self._prepare_for_sleep)
        self.tray.show_settings.connect(self.show_settings)
        self.tray.enabled_changed.connect(self._set_enabled)
        self.tray.microphone_selected.connect(self._select_microphone)
        self.tray.copy_last.connect(self.copy_last_result)
        self.tray.benchmark.connect(lambda: self.start_benchmark(None))
        self.tray.restart_engine.connect(lambda: self.request_engine_start())
        self.tray.quit_requested.connect(self.app.quit)

    def start(self) -> None:
        """Expose the control surface, then initialize helpers asynchronously."""

        self.control.listen()
        self.tray.set_enabled(self.config.enabled)
        self.tray.show()
        self._refresh_status()
        try:
            self._locked = self.session.currently_locked()
        except Exception:
            self._locked = False
        try:
            self.input = self._input_factory()
            self._input_notifier = QSocketNotifier(
                self.input.events.fileno(), QSocketNotifier.Type.Read, self
            )
            self._input_notifier.activated.connect(self._drain_input_events)
            self._heartbeat_timer.start()
        except Exception as exc:
            self._input_failure = f"Input-Proxy konnte nicht gestartet werden: {exc}"
            LOG.error("Input-Proxy konnte nicht gestartet werden: %s", exc)
        self._microphone_timer.start()
        self._health_timer.start()
        self.refresh_microphones()
        self._submit(
            self.diagnostics.prune,
            lambda removed: LOG.info(
                "Diagnose-Aufbewahrung beim Start angewendet; entfernt=%d", removed
            )
            if isinstance(removed, int) and removed
            else None,
            on_error=lambda message: LOG.error(
                "Diagnose-Aufbewahrung konnte beim Start nicht angewendet werden: %s",
                message,
            ),
        )
        self.request_engine_start()

    # ---- generic worker ownership -------------------------------------------------

    def _submit(
        self,
        function: Callable[[], Any],
        on_result: Callable[[Any], None],
        *,
        on_error: Callable[[str], None] | None = None,
        on_finished: Callable[[], None] | None = None,
    ) -> FunctionWorker:
        worker = FunctionWorker(function)
        self._workers.add(worker)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(on_error or self._background_error)

        def finished() -> None:
            self._workers.discard(worker)
            if on_finished is not None:
                on_finished()

        worker.signals.finished.connect(finished)
        self.pool.start(worker)
        return worker

    def _background_error(self, message: str) -> None:
        if self._shutting_down:
            return
        LOG.error("Hintergrundvorgang fehlgeschlagen: %s", message)
        self.overlay.show_message(message, kind="error", timeout_ms=5000)

    # ---- microphone discovery -----------------------------------------------------

    def refresh_microphones(self) -> None:
        if self._microphones_pending or self._shutting_down:
            return
        self._microphones_pending = True
        self._submit(
            list_microphones,
            self._microphones_refreshed,
            on_error=self._microphones_failed,
            on_finished=lambda: setattr(self, "_microphones_pending", False),
        )

    def _microphones_refreshed(self, microphones: object) -> None:
        self._microphones = list(microphones) if isinstance(microphones, list) else []
        self._microphones_loaded = True
        self.tray.set_microphones(self._microphones, self.config.microphone_id)
        if self._settings is not None:
            self._settings.set_microphones(self._microphones, self.config.microphone_id)
        self._sync_input_state()

    def _microphones_failed(self, message: str) -> None:
        self._microphones_loaded = True
        self._microphones = []
        LOG.error("Mikrofone konnten nicht ermittelt werden: %s", message)
        self._sync_input_state()

    def _selected_microphone(self) -> Microphone | None:
        return next(
            (mic for mic in self._microphones if mic.node_name == self.config.microphone_id),
            None,
        )

    # ---- input helper and state ----------------------------------------------------

    def _heartbeat(self) -> None:
        handle = self.input
        if handle is None:
            return
        try:
            if not handle.process.is_alive():
                raise RuntimeError("Helferprozess wurde beendet")
            handle.heartbeat()
        except (BrokenPipeError, EOFError, OSError, RuntimeError) as exc:
            self._input_failure = f"Input-Proxy nicht erreichbar: {exc}"
            self._input_ready = False
            self._input_requested = False
            self._heartbeat_timer.stop()
            if self._input_notifier is not None:
                self._input_notifier.setEnabled(False)
            self._settle_ready_state()

    def _drain_input_events(self, *_args: object) -> None:
        handle = self.input
        if handle is None:
            return
        try:
            while handle.events.poll():
                event = handle.events.recv()
                if isinstance(event, dict):
                    self._handle_input_event(event)
        except (BrokenPipeError, EOFError, OSError):
            self._input_failure = "Input-Proxy wurde unerwartet beendet"
            self._input_ready = False
            if self._input_notifier is not None:
                self._input_notifier.setEnabled(False)
            self._settle_ready_state()

    def _handle_input_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "trigger":
            state = event.get("state")
            if state == "pressed":
                self._trigger_pressed()
            elif state == "released":
                self._trigger_released()
            return
        if event_type == "activity":
            if (
                self._live_direct_for_cycle
                and self._live_edit_attached
            ):
                self._detach_live_edit(
                    "Tastatureingabe während des Live-Diktats erkannt",
                    state_known=False,
                )
            return
        if event_type == "status":
            state = str(event.get("state", ""))
            if state == "input-ready" and bool(event.get("all_grabbed")):
                self._input_ready = True
                self._input_failure = ""
            elif state in {"input-partial", "input-unavailable", "input-disabled", "stopped"}:
                self._input_ready = False
                if state in {"input-disabled", "stopped"}:
                    # The helper watchdog may disable itself while the Qt loop
                    # is temporarily stalled. Its actual state then overrides
                    # the last command we sent, allowing an immediate retry.
                    self._input_requested = False
                if state == "input-partial":
                    self._input_failure = "Nicht alle Tastaturen konnten reserviert werden"
                elif state == "input-unavailable":
                    self._input_failure = "Keine geeignete Tastatur gefunden"
            self._settle_ready_state()
            if state == "input-disabled":
                self._sync_input_state()
            return
        if event_type == "error":
            message = str(event.get("message") or "Unbekannter Input-Fehler")
            code = str(event.get("code") or "input-error")
            self._input_failure = f"{code}: {message}"
            if bool(event.get("fatal")):
                self._input_ready = False
            LOG.error("Input-Proxy: %s: %s", code, message)
            self._settle_ready_state()

    def _can_request_input(self) -> bool:
        return bool(
            self.config.enabled
            and self.engine is not None
            and self.engine.ready
            and self.engine_state in {EngineState.READY_VULKAN, EngineState.READY_CPU}
            and not self._locked
            and not self._sleeping
            and not self._benchmark_active
            and not self._benchmark_waiting
            and not self._shutting_down
            and self._microphones_loaded
            and self._selected_microphone() is not None
        )

    def _proxy_enabled(self, enabled: bool, reason: str) -> None:
        handle = self.input
        if handle is None or enabled == self._input_requested:
            return
        self._input_requested = enabled
        if not enabled:
            self._input_ready = False
        try:
            if enabled:
                handle.enable(reason=reason)
            else:
                handle.disable(reason=reason)
        except (BrokenPipeError, EOFError, OSError):
            self._input_requested = False
            self._input_ready = False
            self._input_failure = "Input-Proxy ist nicht erreichbar"

    def _sync_input_state(self) -> None:
        desired = self._can_request_input()
        self._proxy_enabled(desired, "ready" if desired else "not-ready")
        self._settle_ready_state()

    def _settle_ready_state(self) -> None:
        operational = self._can_request_input() and self._input_ready
        if not operational and self.machine.state in {
            DictationState.STARTING_RECORDING,
            DictationState.RECORDING,
        }:
            # Without a complete proxy set the corresponding key release is no
            # longer guaranteed. Discard instead of recording indefinitely.
            self._cancel_cycle(suspended=self._locked or self._sleeping)
            self._refresh_status()
            return
        if operational:
            if self.machine.state in {
                DictationState.DISABLED,
                DictationState.ERROR,
                DictationState.SUSPENDED,
            }:
                self.machine.set_ready()
        elif self.machine.state in {DictationState.READY, DictationState.ERROR}:
            if self._locked or self._sleeping:
                self.machine.suspend()
            else:
                self.machine.disable()
        self._refresh_status()

    def _reset_live_cycle(self, enabled: bool) -> None:
        """Reset all preview state for the next trigger generation."""

        self._live_cancel.set()
        self._live_cancel = threading.Event()
        self._live_field_cancel.set()
        self._live_field_cancel = threading.Event()
        self._live_timer.stop()
        self._live_enabled_for_cycle = enabled
        self._live_direct_for_cycle = enabled and self.config.live.direct_insert
        self._live_request_inflight = False
        self._live_polling_disabled = False
        self._live_target_window = None
        self._live_assumed_text = ""
        self._live_edit_inflight = False
        self._live_edit_attached = self._live_direct_for_cycle
        self._live_field_state_known = True
        self._live_final_pending = None

    def _trigger_pressed(self) -> None:
        self._physical_trigger_down = True
        if not self.machine.trigger_down():
            return
        if self.engine is None or not self.engine.ready:
            self._cycle_error("Die Whisper-Engine ist nicht bereit")
            self.request_engine_start()
            return
        self._dictation_generation += 1
        self._dictation_cancel.set()
        self._dictation_cancel = threading.Event()
        self._automatic_limit = False
        self._diagnostics_enabled_for_cycle = self.config.diagnostics.enabled
        self._diagnostic_session_for_cycle = None
        self._diagnostics_warning_for_cycle = ""
        self._pending_insert_text = None
        self._reset_live_cycle(self.config.live.enabled)
        try:
            self.recorder.start(
                self.config.microphone_id,
                self.config.recording.max_duration_s,
            )
        except Exception as exc:
            self._cycle_error(f"Aufnahme konnte nicht gestartet werden: {exc}")
            return
        if self._live_direct_for_cycle:
            # Start capturing first: even a wedged optional KWin query must not
            # discard the opening words of the dictation.
            self._live_target_window = self.insertion.capture_active_window()
            if self._live_target_window is None:
                self._live_direct_for_cycle = False
                self._live_edit_attached = False
                LOG.warning(
                    "Direkte Live-Einfügung ist ohne KWin-Zielfenster nicht verfügbar"
                )
        self.overlay.show_message(
            (
                "Aufnahme mit direktem Live-Text …"
                if self._live_direct_for_cycle
                else "Aufnahme mit Live-Vorschau …"
            )
            if self._live_enabled_for_cycle
            else "Aufnahme …",
            kind="recording",
        )
        self._refresh_status()

    def _trigger_released(self) -> None:
        self._physical_trigger_down = False
        if self.machine.trigger_up():
            self._live_timer.stop()
            self._live_cancel.set()
            self.recorder.stop()
            if self._live_direct_for_cycle:
                self.overlay.hide()
            else:
                self.overlay.show_message("Aufnahme wird abgeschlossen …", kind="working")
            self._refresh_status()
            return
        if self.machine.all_triggers_released():
            text, self._pending_insert_text = self._pending_insert_text, None
            if text:
                self._begin_insertion(text, self._dictation_generation)

    def _recording_started(self) -> None:
        # A release can arrive while QProcess is still in Starting state.
        if self.machine.state is DictationState.STARTING_RECORDING:
            self.machine.recording_started()
        if (
            self._live_enabled_for_cycle
            and self.machine.state is DictationState.RECORDING
        ):
            self._live_timer.setInterval(self.config.live.interval_ms)
            self._live_timer.start()
        self._refresh_status()

    def _live_tick(self) -> None:
        """Submit a bounded recent recording window, without creating backlog."""

        if (
            not self._live_enabled_for_cycle
            or self._live_polling_disabled
            or self._live_request_inflight
            or self._live_edit_inflight
            or self._shutting_down
            or self.machine.state is not DictationState.RECORDING
        ):
            return
        try:
            # A growing full-recording preview becomes progressively slower
            # and can occupy whisper-server when the key is released.  The
            # recent tail is enough for a useful draft and bounds that delay.
            duration_ms = int(self.recorder.recorded_duration_ms)
            if self._live_direct_for_cycle and duration_ms > LIVE_DIRECT_MAX_AUDIO_MS:
                self._live_polling_disabled = True
                LOG.info("Direkter Live-Text wurde nach 60 Sekunden eingefroren")
                return
            start_ms = (
                0
                if self._live_direct_for_cycle
                else max(0, duration_ms - LIVE_PREVIEW_WINDOW_MS)
            )
            snapshot = self.recorder.snapshot_wav(start_ms)
        except Exception as exc:
            # A single snapshot failure must not abort the authoritative final
            # transcription. Stop polling and finish normally on key release.
            self._live_polling_disabled = True
            LOG.error("Live-Audiosnapshot konnte nicht erstellt werden: %s", exc)
            return
        if snapshot is None:
            return
        if snapshot.duration_ms < 1000:
            self.recorder.delete_snapshot(snapshot)
            return
        self._submit_live_preview(
            snapshot.path,
            window_truncated=snapshot.start_ms > 0,
        )

    def _submit_live_preview(self, path: Path, *, window_truncated: bool) -> None:
        """Decode one bounded snapshot solely for the visible preview."""

        generation = self._dictation_generation
        cancel = self._live_cancel
        engine = self.engine
        prompt = self.config.initial_prompt
        self._live_request_inflight = True
        with self._transcription_paths_lock:
            self._transcription_paths.add(path)

        def transcribe_preview() -> str:
            try:
                if cancel.is_set():
                    raise EngineCancelled("Live-Vorschau wurde abgebrochen")
                if engine is None or not engine.ready:
                    raise RuntimeError("Die Whisper-Engine ist nicht bereit")
                return normalize_transcript(
                    engine.transcribe(
                        path,
                        initial_prompt=prompt,
                        timeout=LIVE_PREVIEW_TIMEOUT_S,
                        cancel_event=cancel,
                    )
                )
            finally:
                self.recorder.delete_snapshot(path)
                with self._transcription_paths_lock:
                    self._transcription_paths.discard(path)

        self._submit(
            transcribe_preview,
            lambda text: self._live_preview_completed(
                generation,
                text,
                window_truncated=window_truncated,
            ),
            on_error=lambda message: self._live_preview_failed(generation, message),
        )

    def _live_preview_completed(
        self,
        generation: int,
        value: object,
        *,
        window_truncated: bool,
    ) -> None:
        if generation != self._dictation_generation or self._shutting_down:
            return
        self._live_request_inflight = False
        if self.machine.state is not DictationState.RECORDING:
            return
        text = value if isinstance(value, str) else ""
        if not text:
            return
        if self._live_direct_for_cycle:
            self._submit_live_edit(generation, text)
            return
        tail = text[-220:]
        if window_truncated or len(text) > len(tail):
            tail = f"…{tail.lstrip()}"
        self.overlay.show_message(tail, kind="recording")

    def _submit_live_edit(self, generation: int, text: str) -> None:
        """Apply one complete hypothesis to the assumed end-of-caret draft."""

        if (
            not self._live_edit_attached
            or not self._live_field_state_known
            or self._live_target_window is None
            or self._live_edit_inflight
            or self.machine.state is not DictationState.RECORDING
        ):
            return
        previous = self._live_assumed_text
        target = self._live_target_window
        cancel = self._live_cancel
        self._live_edit_inflight = True
        # Keep the overlay hidden from the first direct edit until the final
        # reconciliation; KWin can otherwise make even a no-focus Tool window
        # the active surface for synthetic key input.
        self.overlay.hide()
        self._submit(
            lambda: self.insertion.revise(
                previous,
                text,
                expected_window=target,
                cancel=cancel,
            ),
            lambda result: self._live_edit_completed(
                generation,
                previous,
                text,
                result,
            ),
            on_error=lambda message: self._live_edit_failed(generation, message),
        )

    def _live_edit_completed(
        self,
        generation: int,
        previous: str,
        current: str,
        value: object,
    ) -> None:
        if generation != self._dictation_generation or self._shutting_down:
            return
        self._live_edit_inflight = False
        result = value if isinstance(value, RevisionResult) else RevisionResult(
            False,
            False,
            False,
            "Live-Korrektur lieferte kein gültiges Ergebnis",
        )
        # Only advance the ledger when the worker confirms it edited exactly
        # the draft version it was given and no input activity detached us in
        # the meantime.
        if (
            result.revised
            and self._live_edit_attached
            and self._live_field_state_known
            and self._live_assumed_text == previous
        ):
            self._live_assumed_text = current
        elif (
            not result.revised
            and not (
                result.cancelled
                and result.state_known
                and self.machine.state is not DictationState.RECORDING
            )
        ):
            self._detach_live_edit(result.message, state_known=result.state_known)
        self._continue_pending_final_insertion()

    def _live_edit_failed(self, generation: int, message: str) -> None:
        if generation != self._dictation_generation or self._shutting_down:
            return
        self._live_edit_inflight = False
        self._detach_live_edit(message, state_known=False)
        self._continue_pending_final_insertion()

    def _detach_live_edit(self, reason: str, *, state_known: bool) -> None:
        """Fail closed: retain the field and never emit more live backspaces."""

        if not self._live_direct_for_cycle:
            return
        self._live_edit_attached = False
        self._live_field_state_known = self._live_field_state_known and state_known
        self._live_polling_disabled = True
        self._live_timer.stop()
        self._live_cancel.set()
        self._live_field_cancel.set()
        LOG.warning("Direkter Live-Text wurde sicher angehalten: %s", reason)

    def _live_preview_failed(self, generation: int, message: str) -> None:
        if generation != self._dictation_generation or self._shutting_down:
            return
        self._live_request_inflight = False
        if self._live_cancel.is_set():
            return
        self._live_polling_disabled = True
        LOG.warning("Live-Vorschau wurde für dieses Diktat beendet: %s", message)
        if (
            self.machine.state is DictationState.RECORDING
            and not self._live_direct_for_cycle
        ):
            self.overlay.show_message(
                "Aufnahme … (Live-Vorschau pausiert)",
                kind="recording",
            )

    def _recording_finished(self, path_text: str, _duration: float, automatic: bool) -> None:
        path = Path(path_text)
        self._live_timer.stop()
        self._live_cancel.set()
        self.recorder.take_path()
        if self._shutting_down:
            cleanup_audio(path)
            return
        if self.machine.state in {
            DictationState.STARTING_RECORDING,
            DictationState.RECORDING,
        }:
            self.machine.trigger_up()
        if self.machine.state is not DictationState.FINALIZING:
            cleanup_audio(path)
            self._cycle_error("Unerwarteter Aufnahmezustand")
            return
        self.machine.recording_finished()
        self._automatic_limit = automatic
        generation = self._dictation_generation
        cancel = self._dictation_cancel
        engine = self.engine
        prompt = self.config.initial_prompt
        minimum = self.config.recording.min_duration_ms
        silence = self.config.recording.silence_threshold_dbfs
        diagnostic_session: DiagnosticSession | None = None
        diagnostic_warning = ""
        if self._diagnostics_enabled_for_cycle:
            try:
                # Copy before returning to the Qt event loop. A lock, suspend,
                # or engine restart is then free to unlink the transient WAV
                # without racing the explicitly requested persistent archive.
                diagnostic_session = self.diagnostics.archive_audio(path)
            except Exception as exc:
                # Diagnostics must never prevent the actual dictation. Do not
                # include audio or recognized text in the journal message.
                LOG.error("Diagnoseaufnahme konnte nicht gespeichert werden: %s", exc)
                diagnostic_warning = "Diagnosedaten konnten nicht gespeichert werden"
        if self._live_direct_for_cycle:
            self.overlay.hide()
        else:
            self.overlay.show_message("Transkription …", kind="working")
        self._refresh_status()
        with self._transcription_paths_lock:
            self._transcription_paths.add(path)

        def transcribe() -> TranscriptionResult:
            cycle_diagnostic_warning = diagnostic_warning
            audio_seconds: float | None = None
            inference_seconds: float | None = None
            inference_started: float | None = None
            stage = "audio-validation"
            try:
                metrics = validate_wav(
                    path,
                    min_duration_ms=minimum,
                    silence_threshold_dbfs=silence,
                )
                audio_seconds = metrics.duration_ms / 1000.0
                if cancel.is_set():
                    raise EngineCancelled("Transkription wurde abgebrochen")
                if engine is None or not engine.ready:
                    raise RuntimeError("Die Whisper-Engine ist nicht bereit")
                stage = "transcription"
                inference_started = time.monotonic()
                raw = engine.transcribe(path, initial_prompt=prompt, cancel_event=cancel)
                inference_seconds = time.monotonic() - inference_started
                stage = "text-normalization"
                text = normalize_transcript(raw)
                if not text:
                    raise RuntimeError("Es wurde kein Text erkannt")
                result = TranscriptionResult(
                    ok=True,
                    text=text,
                    audio_seconds=audio_seconds,
                    inference_seconds=inference_seconds,
                    diagnostic_session=diagnostic_session,
                    diagnostic_warning=cycle_diagnostic_warning,
                )
                if diagnostic_session is not None:
                    try:
                        self.diagnostics.finish_success(
                            diagnostic_session,
                            text,
                            audio_seconds=audio_seconds,
                            inference_seconds=inference_seconds,
                        )
                    except Exception as exc:
                        LOG.error("Diagnosepaket konnte nicht abgeschlossen werden: %s", exc)
                        result.diagnostic_warning = (
                            "Diagnosedaten konnten nicht vollständig gespeichert werden"
                        )
                return result
            except Exception as exc:
                if inference_started is not None and inference_seconds is None:
                    inference_seconds = time.monotonic() - inference_started
                if diagnostic_session is not None:
                    try:
                        self.diagnostics.finish_error(
                            diagnostic_session,
                            exc,
                            stage=stage,
                            audio_seconds=audio_seconds,
                            inference_seconds=inference_seconds,
                        )
                    except Exception as diagnostic_exc:
                        LOG.error(
                            "Fehlerhaftes Diagnosepaket konnte nicht abgeschlossen werden: %s",
                            diagnostic_exc,
                        )
                        cycle_diagnostic_warning = (
                            "Diagnosedaten konnten nicht vollständig gespeichert werden"
                        )
                return TranscriptionResult(
                    ok=False,
                    error=str(exc) or type(exc).__name__,
                    diagnostic_warning=cycle_diagnostic_warning,
                )
            finally:
                cleanup_audio(path)
                with self._transcription_paths_lock:
                    self._transcription_paths.discard(path)

        self._submit(
            transcribe,
            lambda result: self._transcription_completed(generation, result),
            on_error=lambda message: self._transcription_worker_failed(generation, message),
        )

    def _recording_failed(self, message: str) -> None:
        self._cycle_error(message)

    def _recording_discarded(self) -> None:
        self._refresh_status()

    def _transcription_completed(self, generation: int, value: object) -> None:
        if generation != self._dictation_generation or self._shutting_down:
            return
        if not isinstance(value, TranscriptionResult) or not value.ok:
            error = value.error if isinstance(value, TranscriptionResult) else "Transkription fehlgeschlagen"
            if isinstance(value, TranscriptionResult) and value.diagnostic_warning:
                error = f"{error} – {value.diagnostic_warning}"
            self._cycle_error(error)
            return
        self._diagnostic_session_for_cycle = value.diagnostic_session
        self._diagnostics_warning_for_cycle = value.diagnostic_warning
        self._last_result = value.text
        self.tray.set_last_available(True)
        wait_for_release = self._automatic_limit and self._physical_trigger_down
        self.machine.transcript_ready(wait_for_release)
        if wait_for_release:
            self._pending_insert_text = value.text
            if self._live_direct_for_cycle:
                self.overlay.hide()
            else:
                self.overlay.show_message(
                    "Maximaldauer erreicht – rechte Strg-Taste loslassen",
                    kind="neutral",
                )
            self._refresh_status()
            return
        self._begin_insertion(value.text, generation)

    def _transcription_worker_failed(self, generation: int, message: str) -> None:
        if generation == self._dictation_generation:
            self._cycle_error(message)

    def _begin_insertion(self, text: str, generation: int) -> None:
        if self.machine.state is not DictationState.INSERTING:
            self._cycle_error("Unerwarteter Einfügezustand")
            return
        # A Qt Tool window can become KWin's active surface on Wayland even
        # with WindowDoesNotAcceptFocus/WA_ShowWithoutActivating. Hide it so
        # KWin restores the text field that was active before dictation.
        self.overlay.hide()
        self._refresh_status()
        if self._live_direct_for_cycle and self._live_edit_inflight:
            self._live_final_pending = (text, generation)
            return
        self._dispatch_final_insertion(text, generation)

    def _continue_pending_final_insertion(self) -> None:
        if self._live_edit_inflight or self._live_final_pending is None:
            return
        text, generation = self._live_final_pending
        self._live_final_pending = None
        if (
            generation == self._dictation_generation
            and self.machine.state is DictationState.INSERTING
            and not self._shutting_down
        ):
            self._dispatch_final_insertion(text, generation)

    def _dispatch_final_insertion(self, text: str, generation: int) -> None:
        cancel = self._dictation_cancel
        diagnostic_session = self._diagnostic_session_for_cycle
        if self._live_direct_for_cycle:
            previous = self._live_assumed_text
            target = self._live_target_window
            field_cancel = self._live_field_cancel
            can_revise = bool(
                self._live_edit_attached
                and self._live_field_state_known
                and target is not None
            )

            def finalize_live_text() -> InsertionResult:
                if cancel.is_set():
                    return InsertionResult(False, False, "Einfügen wurde abgebrochen")
                revision_cancelled = lambda: cancel.is_set() or field_cancel.is_set()
                revision: RevisionResult | None = None
                if can_revise and target is not None:
                    revision = self.insertion.revise(
                        previous,
                        text,
                        expected_window=target,
                        cancel=revision_cancelled,
                    )
                if revision is not None and revision.revised:
                    if cancel.is_set():
                        return InsertionResult(
                            revision.copied,
                            False,
                            "Live-Text finalisiert, Abschluss wurde abgebrochen",
                        )
                    if field_cancel.is_set():
                        copied = self.insertion.copy(text)
                        return InsertionResult(
                            copied.copied,
                            False,
                            "Endtext nur kopiert – Eingabe im Zielfeld erkannt",
                        )
                    copied = self.insertion.copy(text)
                    return InsertionResult(
                        copied.copied,
                        True,
                        "Live-Text finalisiert"
                        if copied.copied
                        else "Live-Text finalisiert; Zwischenablage nicht aktualisiert",
                    )
                if cancel.is_set():
                    return InsertionResult(
                        bool(revision and revision.copied),
                        False,
                        "Einfügen wurde abgebrochen",
                    )
                copied = self.insertion.copy(text)
                if copied.copied:
                    detail = revision.message if revision is not None else "Live-Ziel wurde verändert"
                    return InsertionResult(
                        True,
                        False,
                        f"Endtext nur kopiert – {detail}",
                    )
                return InsertionResult(
                    False,
                    False,
                    "Live-Ziel ist unsicher und der Endtext konnte nicht kopiert werden",
                )

            self._submit(
                finalize_live_text,
                lambda result: self._insertion_completed(
                    generation, result, diagnostic_session
                ),
                on_error=lambda message: self._insertion_completed(
                    generation,
                    InsertionResult(False, False, message),
                    diagnostic_session,
                ),
            )
            return
        self._submit(
            lambda: self.insertion.insert(text, cancel=cancel),
            lambda result: self._insertion_completed(
                generation, result, diagnostic_session
            ),
            on_error=lambda message: self._insertion_completed(
                generation,
                InsertionResult(False, False, message),
                diagnostic_session,
            ),
        )

    def _insertion_completed(
        self,
        generation: int,
        value: object,
        diagnostic_session: DiagnosticSession | None = None,
    ) -> None:
        result = value if isinstance(value, InsertionResult) else InsertionResult(
            False, False, "Einfügen ist fehlgeschlagen"
        )
        insertion_diagnostic_warning = ""
        if diagnostic_session is not None:
            try:
                self.diagnostics.record_insertion(
                    diagnostic_session,
                    copied=result.copied,
                    shortcut_sent=result.inserted,
                    error=None if result.inserted else result.message,
                )
            except Exception as exc:
                LOG.error("Einfüge-Diagnose konnte nicht gespeichert werden: %s", exc)
                insertion_diagnostic_warning = (
                    "Diagnosedaten konnten nicht vollständig gespeichert werden"
                )
        if generation != self._dictation_generation or self._shutting_down:
            return
        self._live_final_pending = None
        self._live_assumed_text = ""
        self._live_edit_attached = False
        if insertion_diagnostic_warning:
            self._diagnostics_warning_for_cycle = insertion_diagnostic_warning
        if self._diagnostic_session_for_cycle is diagnostic_session:
            self._diagnostic_session_for_cycle = None
        if self.machine.state is DictationState.INSERTING:
            self.machine.insertion_finished()
        diagnostics_warning, self._diagnostics_warning_for_cycle = (
            self._diagnostics_warning_for_cycle,
            "",
        )
        if result.inserted and diagnostics_warning:
            self.overlay.show_message(
                f"Shortcut gesendet, aber {diagnostics_warning.lower()}",
                kind="error",
                timeout_ms=5000,
            )
        elif result.inserted:
            self.overlay.show_message(
                result.message or "Einfügen gesendet",
                kind="success",
                timeout_ms=1800,
            )
        else:
            self.overlay.show_message(result.message, kind="error", timeout_ms=5000)
        self._settle_ready_state()

    def _cycle_error(self, message: str) -> None:
        if self._shutting_down:
            return
        if self._live_direct_for_cycle:
            if self._live_assumed_text:
                message = f"{message} – Live-Entwurf bleibt im Feld"
            elif self._live_edit_inflight:
                message = f"{message} – Live-Entwurf könnte im Feld bleiben"
        self._live_timer.stop()
        self._live_cancel.set()
        self._live_field_cancel.set()
        self._live_final_pending = None
        self._live_edit_attached = False
        self._live_assumed_text = ""
        self._pending_insert_text = None
        self._diagnostic_session_for_cycle = None
        self._diagnostics_warning_for_cycle = ""
        self.machine.fail()
        self.overlay.show_message(message or "Diktat fehlgeschlagen", kind="error", timeout_ms=5000)
        self._settle_ready_state()

    def _cancel_cycle(self, *, suspended: bool = False) -> None:
        self._dictation_generation += 1
        self._dictation_cancel.set()
        self._live_timer.stop()
        self._live_cancel.set()
        self._live_field_cancel.set()
        self._live_enabled_for_cycle = False
        self._live_direct_for_cycle = False
        self._live_edit_attached = False
        self._live_field_state_known = False
        self._live_assumed_text = ""
        self._live_final_pending = None
        self._pending_insert_text = None
        self._diagnostic_session_for_cycle = None
        self._diagnostics_warning_for_cycle = ""
        self._cleanup_transcription_audio()
        if self.recorder.active:
            self.recorder.stop(discard=True)
        if suspended:
            self.machine.suspend()
        else:
            self.machine.disable()
        self.overlay.hide()

    def _cleanup_transcription_audio(self) -> None:
        """Immediately unlink all WAVs owned by active transcription workers."""

        with self._transcription_paths_lock:
            paths = tuple(self._transcription_paths)
            self._transcription_paths.clear()
        for path in paths:
            cleanup_audio(path)

    # ---- model and engine lifecycle ------------------------------------------------

    def request_engine_start(self, *, force_download: bool = False) -> None:
        if self._shutting_down:
            return
        if self._engine_busy:
            self._engine_restart_pending = True
            self._engine_force_pending = self._engine_force_pending or force_download
            self._engine_cancel.set()
            if self.engine is not None:
                self.engine.cancel_pending()
            return
        self._begin_engine_start(force_download=force_download)

    def _begin_engine_start(self, *, force_download: bool) -> None:
        self._engine_busy = True
        self._engine_generation += 1
        generation = self._engine_generation
        self._engine_cancel = threading.Event()
        cancel = self._engine_cancel
        snapshot = self.config
        old_engine, self.engine = self.engine, None
        self.engine_state = EngineState.VERIFYING
        self._proxy_enabled(False, "engine-restart")
        self._cancel_cycle()
        self._refresh_status()

        bridge = OperationBridge(self)
        self._operation_bridge = bridge
        bridge.download_started.connect(lambda: self._show_download(generation))
        bridge.download_progress.connect(
            lambda name, done, total: self._download_progress(generation, name, done, total)
        )
        bridge.phase.connect(lambda phase: self._engine_phase(generation, phase))

        def launch() -> EngineLaunchResult:
            new_engine: WhisperEngine | None = None
            try:
                if old_engine is not None:
                    old_engine.cancel_pending()
                    old_engine.close()
                if cancel.is_set():
                    raise DownloadCancelled("Engine-Neustart wurde abgebrochen")

                effective = snapshot
                store = ModelStore(get_models_dir())
                try:
                    if force_download:
                        raise FileNotFoundError("erneuter Download angefordert")
                    validate_required_models(snapshot.model_path, snapshot.vad_model_path)
                except Exception:
                    bridge.download_started.emit()
                    main_path = store.download(
                        MAIN_MODEL,
                        progress=lambda done, total: bridge.download_progress.emit(
                            MAIN_MODEL.filename, done, total
                        ),
                        cancel=cancel,
                        force=force_download,
                    )
                    vad_path = store.download(
                        VAD_MODEL,
                        progress=lambda done, total: bridge.download_progress.emit(
                            VAD_MODEL.filename, done, total
                        ),
                        cancel=cancel,
                        force=force_download,
                    )
                    effective = replace(
                        snapshot,
                        model_path=str(main_path),
                        vad_model_path=str(vad_path),
                    )
                if cancel.is_set():
                    raise DownloadCancelled("Engine-Neustart wurde abgebrochen")

                bridge.phase.emit("Engine und Modelle werden geladen …")
                engine_config = EngineConfig.from_app_config(effective, self.runtime_dir)
                new_engine = self._engine_factory(engine_config)
                warmup = self._resource_path("warmup-de.wav")
                new_engine.start(
                    warmup_wav=warmup if warmup is not None and warmup.is_file() else None,
                    cancel_event=cancel,
                )
                if cancel.is_set():
                    raise EngineCancelled("Engine-Neustart wurde abgebrochen")
                warning = self.insertion.preflight()
                if cancel.is_set():
                    raise EngineCancelled("Engine-Neustart wurde abgebrochen")
                return EngineLaunchResult(
                    status="ok",
                    engine=new_engine,
                    config=effective,
                    insertion_warning=warning or "",
                )
            except BackendUnavailableError as exc:
                if new_engine is not None:
                    new_engine.close()
                return EngineLaunchResult(status="backend-unavailable", error=str(exc))
            except (DownloadCancelled, EngineCancelled) as exc:
                if new_engine is not None:
                    new_engine.close()
                return EngineLaunchResult(status="cancelled", error=str(exc))
            except Exception as exc:
                if new_engine is not None:
                    new_engine.close()
                return EngineLaunchResult(status="error", error=str(exc) or type(exc).__name__)

        self._submit(
            launch,
            lambda result: self._engine_started(generation, result),
            on_error=lambda message: self._engine_started(
                generation, EngineLaunchResult(status="error", error=message)
            ),
            on_finished=self._engine_operation_finished,
        )

    @staticmethod
    def _resource_path(name: str) -> Path | None:
        try:
            return Path(resources.files("local_dictation").joinpath("resources", name))
        except (AttributeError, TypeError):
            return None

    def _show_download(self, generation: int) -> None:
        if generation != self._engine_generation or self._shutting_down:
            return
        if self._download_dialog is None:
            dialog = DownloadDialog()
            dialog.cancelled.connect(self._cancel_engine_operation)
            dialog.finished.connect(lambda _result: self._clear_download_dialog(dialog))
            self._download_dialog = dialog
        self._download_dialog.show()
        self._download_dialog.raise_()

    def _clear_download_dialog(self, dialog: DownloadDialog) -> None:
        if self._download_dialog is dialog:
            self._download_dialog = None

    def _download_progress(self, generation: int, name: str, done: int, total: int) -> None:
        if generation != self._engine_generation or self._download_dialog is None:
            return
        self._download_dialog.update_progress(name, done, total)

    def _engine_phase(self, generation: int, phase: str) -> None:
        if generation != self._engine_generation:
            return
        self.engine_state = EngineState.STARTING
        if self._download_dialog is not None:
            self._download_dialog.label.setText(phase)
            self._download_dialog.progress.setRange(0, 0)
            self._download_dialog.details.setText("Das Modell bleibt anschließend im Speicher.")
        self._refresh_status()

    def _cancel_engine_operation(self) -> None:
        self._engine_cancel.set()
        if self.engine is not None:
            self.engine.cancel_pending()
        if self._download_dialog is not None:
            self._download_dialog.label.setText("Vorgang wird abgebrochen …")
            self._download_dialog.cancel_button.setEnabled(False)

    def _engine_started(self, generation: int, value: object) -> None:
        outcome = value if isinstance(value, EngineLaunchResult) else EngineLaunchResult(
            status="error", error="Engine lieferte kein gültiges Ergebnis"
        )
        if generation != self._engine_generation or self._shutting_down:
            if outcome.engine is not None:
                outcome.engine.close()
            return
        if outcome.status == "ok" and outcome.engine is not None and outcome.config is not None:
            if self._engine_restart_pending:
                # Settings changed while this engine was starting. Never let
                # its stale snapshot overwrite the newer desired config; the
                # pending launch will install the matching engine next.
                outcome.engine.close()
                return
            self.engine = outcome.engine
            self.config = replace(
                self.config,
                model_path=outcome.config.model_path,
                vad_model_path=outcome.config.vad_model_path,
                backend=outcome.config.backend,
                language=outcome.config.language,
            )
            try:
                save_config(self.config)
            except OSError as exc:
                LOG.error("Konfiguration konnte nicht gespeichert werden: %s", exc)
            self.engine_state = (
                EngineState.READY_VULKAN
                if self.config.backend == "vulkan"
                else EngineState.READY_CPU
            )
            if self._download_dialog is not None:
                self._download_dialog.accept()
            LOG.info("Whisper-Engine bereit; Backend=%s", self.config.backend)
            if outcome.insertion_warning:
                self.overlay.show_message(
                    outcome.insertion_warning, kind="error", timeout_ms=7000
                )
            if not self._benchmark_waiting:
                self._sync_input_state()
            return

        self.engine_state = EngineState.ERROR
        self._proxy_enabled(False, "engine-error")
        if self._download_dialog is not None:
            self._download_dialog.reject()
        if outcome.status == "backend-unavailable" and self.config.backend == "vulkan":
            answer = QMessageBox.question(
                None,
                "Vulkan nicht verfügbar",
                f"{outcome.error}\n\nSoll die Engine ausdrücklich im CPU-Modus gestartet werden?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._apply_config(replace(self.config, backend="cpu"), restart_engine=True)
                return
        elif outcome.status != "cancelled":
            self.overlay.show_message(
                outcome.error or "Whisper-Engine konnte nicht gestartet werden",
                kind="error",
                timeout_ms=7000,
            )
            LOG.error("Whisper-Engine konnte nicht gestartet werden: %s", outcome.error)
        if outcome.status != "cancelled" or not self._engine_restart_pending:
            self._fail_waiting_benchmark(
                outcome.error or "Whisper-Engine konnte nicht gestartet werden"
            )
        self._settle_ready_state()

    def _engine_operation_finished(self) -> None:
        self._engine_busy = False
        self._operation_bridge = None
        if self._engine_restart_pending and not self._shutting_down:
            force = self._engine_force_pending
            self._engine_restart_pending = False
            self._engine_force_pending = False
            self._begin_engine_start(force_download=force)
            return
        if self._benchmark_waiting and not self._shutting_down:
            reply, self._benchmark_reply = self._benchmark_reply, None
            self._benchmark_waiting = False
            self.start_benchmark(reply)

    # ---- benchmark and control socket ---------------------------------------------

    def _handle_control(
        self,
        request: dict[str, Any],
        reply: Callable[[dict[str, Any]], None],
    ) -> None:
        command = request.get("command")
        if command == "show-settings":
            self.show_settings()
            reply({"ok": True})
        elif command == "benchmark":
            self.start_benchmark(reply)
        elif command == "status":
            reply(
                {
                    "ok": True,
                    "engine": self.engine_state.value,
                    "dictation": self.machine.state.value,
                    "backend": self.config.backend,
                }
            )
        else:
            reply({"ok": False, "error": "Unbekannter Befehl"})

    def start_benchmark(
        self, reply: Callable[[dict[str, Any]], None] | None
    ) -> None:
        if self._benchmark_active or self._benchmark_waiting:
            if reply:
                reply({"ok": False, "error": "Ein Benchmark läuft bereits"})
            return
        if self.machine.state not in {
            DictationState.READY,
            DictationState.DISABLED,
            DictationState.ERROR,
        }:
            if reply:
                reply({"ok": False, "error": "Während eines Diktats ist kein Benchmark möglich"})
            return
        fixture = self._resource_path("benchmark-de.wav")
        if fixture is None or not fixture.is_file():
            if reply:
                reply({"ok": False, "error": "Benchmark-Audio ist nicht verfügbar"})
            else:
                QMessageBox.warning(None, "Benchmark", "Benchmark-Audio ist nicht verfügbar.")
            return
        engine = self.engine
        if engine is None or not engine.ready:
            self._benchmark_waiting = True
            self._benchmark_reply = reply
            self._proxy_enabled(False, "benchmark-waiting")
            self.machine.disable()
            self.overlay.show_message("Benchmark wartet auf die Whisper-Engine …", kind="working")
            self._refresh_status()
            if not self._engine_busy:
                self.request_engine_start()
            return
        self._benchmark_active = True
        self._benchmark_generation += 1
        generation = self._benchmark_generation
        self._benchmark_reply = reply
        self._proxy_enabled(False, "benchmark")
        self.machine.disable()
        self.overlay.show_message("Benchmark läuft …", kind="working")
        self._refresh_status()

        def benchmark() -> dict[str, Any]:
            try:
                metrics = validate_wav(fixture, min_duration_ms=1, silence_threshold_dbfs=-100.0)
                started = time.monotonic()
                raw = engine.transcribe(fixture, initial_prompt=self.config.initial_prompt)
                elapsed = time.monotonic() - started
                normalized = normalize_transcript(raw)
                quality_ok = self._benchmark_quality(normalized)
                return {
                    "ok": True,
                    "backend": engine.backend.value,
                    "audio_seconds": metrics.duration_ms / 1000.0,
                    "inference_seconds": elapsed,
                    "realtime_factor": elapsed / (metrics.duration_ms / 1000.0),
                    "target_met": elapsed <= 5.0,
                    "quality_ok": quality_ok,
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc) or type(exc).__name__}

        self._submit(
            benchmark,
            lambda result: self._benchmark_completed(generation, result),
            on_error=lambda message: self._benchmark_completed(
                generation, {"ok": False, "error": message}
            ),
        )

    def _fail_waiting_benchmark(self, message: str) -> None:
        if not self._benchmark_waiting:
            return
        reply, self._benchmark_reply = self._benchmark_reply, None
        self._benchmark_waiting = False
        result = {"ok": False, "error": message or "Benchmark fehlgeschlagen"}
        if reply is not None:
            reply(result)
        else:
            QMessageBox.warning(None, "Benchmark", result["error"])

    @staticmethod
    def _benchmark_quality(text: str) -> bool:
        folded = text.casefold()
        try:
            resource = resources.files("local_dictation").joinpath(
                "resources", "fixtures.json"
            )
            metadata = json.loads(resource.read_text(encoding="utf-8"))
            terms = metadata.get("fixtures", {}).get("benchmark", {}).get(
                "expected_terms", []
            )
            if isinstance(terms, list) and terms:
                return all(str(term).casefold() in folded for term in terms)
        except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return bool(text)

    def _benchmark_completed(self, generation: int, value: object) -> None:
        if generation != self._benchmark_generation:
            return
        result = value if isinstance(value, dict) else {
            "ok": False,
            "error": "Benchmark lieferte kein gültiges Ergebnis",
        }
        reply, self._benchmark_reply = self._benchmark_reply, None
        self._benchmark_active = False
        if reply is not None:
            reply(result)
        if result.get("ok"):
            seconds = float(result.get("inference_seconds", 0.0))
            target = "erreicht" if result.get("target_met") else "nicht erreicht"
            self.overlay.show_message(
                f"Benchmark: {seconds:.2f} s, 5-s-Ziel {target}",
                kind="success" if result.get("target_met") else "error",
                timeout_ms=5000,
            )
            if reply is None:
                QMessageBox.information(
                    None,
                    "Lokaler Diktat-Benchmark",
                    f"Backend: {result.get('backend')}\n"
                    f"Audio: {float(result.get('audio_seconds', 0.0)):.2f} s\n"
                    f"Inferenz: {seconds:.2f} s\n"
                    f"5-Sekunden-Ziel: {target}\n"
                    f"Begriffsprüfung: {'bestanden' if result.get('quality_ok') else 'nicht bestanden'}",
                )
        else:
            message = str(result.get("error") or "Benchmark fehlgeschlagen")
            self.overlay.show_message(message, kind="error", timeout_ms=5000)
            if reply is None:
                QMessageBox.warning(None, "Benchmark", message)
        self._sync_input_state()

    # ---- settings and tray ---------------------------------------------------------

    def show_settings(self) -> None:
        if self._settings is not None:
            self._settings.show()
            self._settings.raise_()
            self._settings.activateWindow()
            return
        dialog = SettingsDialog(self.config)
        self._settings = dialog
        dialog.set_microphones(self._microphones, self.config.microphone_id)
        dialog.set_model_status(self._model_status_text())
        dialog.saved.connect(lambda value: self._apply_config(value, restart_engine=None))
        dialog.download_requested.connect(lambda: self.request_engine_start(force_download=True))
        dialog.diagnostics_open_requested.connect(self.open_diagnostics)
        dialog.diagnostics_clear_requested.connect(self.clear_diagnostics)
        dialog.finished.connect(lambda _result: self._clear_settings(dialog))
        dialog.show()

    def _clear_settings(self, dialog: SettingsDialog) -> None:
        if self._settings is dialog:
            self._settings = None

    def _apply_config(
        self,
        value: object,
        *,
        restart_engine: bool | None,
    ) -> None:
        if not isinstance(value, AppConfig):
            QMessageBox.warning(None, "Einstellungen", "Ungültige Konfiguration")
            return
        try:
            validated = parse_config(value.to_dict())
            if validated.diagnostics.enabled and not self.config.diagnostics.enabled:
                # Refuse to claim diagnostics are enabled if the private root
                # cannot be created safely. No recording is involved here.
                self.diagnostics.ensure_directory()
            save_config(validated)
        except Exception as exc:
            QMessageBox.warning(None, "Einstellungen", str(exc))
            return
        previous = self.config
        self.config = validated
        if (
            previous.diagnostics.retention_entries
            != validated.diagnostics.retention_entries
        ):
            try:
                self.diagnostics.set_max_entries(
                    validated.diagnostics.retention_entries
                )
            except Exception as exc:
                LOG.error("Diagnose-Aufbewahrung konnte nicht angewendet werden: %s", exc)
                QMessageBox.warning(
                    None,
                    "Diagnosedaten",
                    "Die neue Aufbewahrungsgrenze ist gespeichert, konnte aber derzeit "
                    f"nicht auf das Archiv angewendet werden:\n{exc}",
                )
        self.tray.set_enabled(validated.enabled)
        engine_changed = (
            previous.model_path,
            previous.vad_model_path,
            previous.backend,
            previous.language,
        ) != (
            validated.model_path,
            validated.vad_model_path,
            validated.backend,
            validated.language,
        )
        if not validated.enabled:
            self._proxy_enabled(False, "disabled")
            self._cancel_cycle()
        if restart_engine is True or (restart_engine is None and engine_changed):
            self.request_engine_start()
        else:
            self._sync_input_state()
        self.refresh_microphones()

    def open_diagnostics(self) -> None:
        """Open the private diagnostic archive in the desktop file manager."""

        try:
            path = self.diagnostics.ensure_directory()
        except Exception as exc:
            QMessageBox.warning(None, "Diagnosedaten", str(exc))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(
                None,
                "Diagnosedaten",
                f"Der Ordner konnte nicht geöffnet werden:\n{path}",
            )

    def clear_diagnostics(self) -> None:
        """Clear all retained diagnostic bundles without blocking the GUI."""

        if self._shutting_down:
            return
        self._submit(
            self.diagnostics.clear,
            self._diagnostics_cleared,
            on_error=lambda message: QMessageBox.warning(
                None,
                "Diagnosedaten",
                f"Diagnosedaten konnten nicht gelöscht werden:\n{message}",
            ),
        )

    def _diagnostics_cleared(self, value: object) -> None:
        count = value if isinstance(value, int) and not isinstance(value, bool) else 0
        self.overlay.show_message(
            f"Diagnosedaten gelöscht ({count} Einträge)",
            kind="success",
            timeout_ms=2500,
        )

    def _set_enabled(self, enabled: bool) -> None:
        if enabled == self.config.enabled:
            return
        self._apply_config(replace(self.config, enabled=enabled), restart_engine=False)

    def _select_microphone(self, node_name: str) -> None:
        if node_name == self.config.microphone_id:
            return
        self._apply_config(
            replace(self.config, microphone_id=node_name), restart_engine=False
        )

    def copy_last_result(self) -> None:
        text = self._last_result
        if not text:
            return
        self._submit(
            lambda: self.insertion.copy(text),
            lambda result: self.overlay.show_message(
                result.message if isinstance(result, InsertionResult) else "Kopieren fehlgeschlagen",
                kind="success"
                if isinstance(result, InsertionResult) and result.copied
                else "error",
                timeout_ms=2500,
            ),
        )

    # ---- lock, suspend, health, status, shutdown ----------------------------------

    def _session_locked(self, locked: bool) -> None:
        self._locked = locked
        if locked:
            self._proxy_enabled(False, "session-locked")
            self._cancel_cycle(suspended=True)
        else:
            self._sync_input_state()

    def _prepare_for_sleep(self, sleeping: bool) -> None:
        self._sleeping = sleeping
        if sleeping:
            self._proxy_enabled(False, "suspend")
            self._cancel_cycle(suspended=True)
        else:
            self.request_engine_start()

    def _check_health(self) -> None:
        if (
            self._health_pending
            or self._shutting_down
            or self._sleeping
            or self._engine_busy
            or self._benchmark_active
            or self._benchmark_waiting
            or self.machine.state
            not in {DictationState.READY, DictationState.DISABLED, DictationState.ERROR}
        ):
            return
        engine = self.engine
        if engine is None:
            return
        self._health_pending = True
        self._submit(
            engine.health,
            lambda healthy: self._health_result(engine, bool(healthy)),
            on_error=lambda _message: self._health_result(engine, False),
            on_finished=lambda: setattr(self, "_health_pending", False),
        )

    def _health_result(self, engine: WhisperEngine, healthy: bool) -> None:
        if self._shutting_down or engine is not self.engine:
            return
        if not healthy:
            self.engine_state = EngineState.ERROR
            self._proxy_enabled(False, "engine-health")
            self.request_engine_start()

    def _model_status_text(self) -> str:
        labels = {
            EngineState.MISSING_MODELS: "fehlt",
            EngineState.VERIFYING: "wird geprüft",
            EngineState.STARTING: "wird geladen",
            EngineState.WARMING: "wird aufgewärmt",
            EngineState.READY_VULKAN: "large-v3-turbo / Vulkan",
            EngineState.READY_CPU: "large-v3-turbo / CPU",
            EngineState.ERROR: "Fehler",
            EngineState.STOPPED: "gestoppt",
        }
        return labels[self.engine_state]

    def _refresh_status(self) -> None:
        state = self.machine.state
        color = "#3daee9"
        labels = {
            DictationState.READY: "bereit – rechte Strg halten",
            DictationState.STARTING_RECORDING: "Aufnahme startet",
            DictationState.RECORDING: "Aufnahme",
            DictationState.FINALIZING: "Aufnahme wird abgeschlossen",
            DictationState.TRANSCRIBING: "Transkription",
            DictationState.WAITING_FOR_RELEASE: "rechte Strg loslassen",
            DictationState.INSERTING: "Einfügen",
            DictationState.SUSPENDED: "Sitzung gesperrt / Suspend",
            DictationState.ERROR: "Fehler",
            DictationState.SHUTTING_DOWN: "wird beendet",
            DictationState.DISABLED: "nicht bereit",
        }
        if state is DictationState.RECORDING:
            color = "#da4453"
        elif state in {
            DictationState.FINALIZING,
            DictationState.TRANSCRIBING,
            DictationState.INSERTING,
        }:
            color = "#fdbc4b"
        elif state is DictationState.READY:
            color = "#27ae60"
        elif self.engine_state is EngineState.ERROR or self._input_failure:
            color = "#ed1515"
        ready = labels[state]
        if state is DictationState.DISABLED:
            if not self.config.enabled:
                ready = "deaktiviert"
            elif not self._microphones_loaded:
                ready = "Mikrofone werden gesucht"
            elif self._selected_microphone() is None:
                ready = "gewähltes Mikrofon fehlt"
            elif self._input_failure:
                ready = self._input_failure
            elif self._engine_busy:
                ready = "Engine wird vorbereitet"
        self.tray.set_status(ready, self._model_status_text(), color=color)
        if self._settings is not None:
            self._settings.set_model_status(self._model_status_text())

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.machine.shutdown()
        self._heartbeat_timer.stop()
        self._microphone_timer.stop()
        self._health_timer.stop()
        self._live_timer.stop()
        self._live_cancel.set()
        self._live_field_cancel.set()
        self._live_final_pending = None
        self._live_edit_attached = False
        self._live_assumed_text = ""
        benchmark_reply, self._benchmark_reply = self._benchmark_reply, None
        self._benchmark_waiting = False
        self._benchmark_active = False
        if benchmark_reply is not None:
            benchmark_reply({"ok": False, "error": "Dienst wird beendet"})
        self.control.close()
        self._engine_cancel.set()
        self._dictation_cancel.set()
        self._cleanup_transcription_audio()
        self._benchmark_generation += 1
        self._proxy_enabled(False, "shutdown")
        if self.input is not None:
            try:
                self.input.stop(timeout=1.0)
            except Exception:
                pass
        if self._input_notifier is not None:
            self._input_notifier.setEnabled(False)
        self.recorder.shutdown()
        if self.engine is not None:
            self.engine.cancel_pending()
            self.engine.close()
            self.engine = None
        self.engine_state = EngineState.STOPPED
        self.overlay.close()
        self.tray.icon.hide()
        # Give cleanup workers a bounded opportunity to delete transient audio.
        self.pool.waitForDone(2500)


__all__ = [
    "DictationController",
    "EngineLaunchResult",
    "OperationBridge",
    "TranscriptionResult",
]
