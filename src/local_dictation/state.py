from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EngineState(str, Enum):
    MISSING_MODELS = "missing_models"
    VERIFYING = "verifying"
    STARTING = "starting"
    WARMING = "warming"
    READY_VULKAN = "ready_vulkan"
    READY_CPU = "ready_cpu"
    ERROR = "error"
    STOPPED = "stopped"


class DictationState(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    STARTING_RECORDING = "starting_recording"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    TRANSCRIBING = "transcribing"
    WAITING_FOR_RELEASE = "waiting_for_release"
    INSERTING = "inserting"
    ERROR = "error"
    SUSPENDED = "suspended"
    SHUTTING_DOWN = "shutting_down"


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    engine: EngineState
    dictation: DictationState
    backend: str
    message: str = ""


class StateError(RuntimeError):
    pass


class DictationStateMachine:
    """Small deterministic state machine used by the Qt controller."""

    def __init__(self) -> None:
        self.state = DictationState.DISABLED

    def set_ready(self) -> None:
        if self.state in {
            DictationState.DISABLED,
            DictationState.ERROR,
            DictationState.SUSPENDED,
        }:
            self.state = DictationState.READY

    def trigger_down(self) -> bool:
        if self.state != DictationState.READY:
            return False
        self.state = DictationState.STARTING_RECORDING
        return True

    def recording_started(self) -> None:
        if self.state != DictationState.STARTING_RECORDING:
            raise StateError(f"recording_started in {self.state.value}")
        self.state = DictationState.RECORDING

    def trigger_up(self) -> bool:
        if self.state in {
            DictationState.STARTING_RECORDING,
            DictationState.RECORDING,
        }:
            self.state = DictationState.FINALIZING
            return True
        return False

    def recording_finished(self) -> None:
        if self.state != DictationState.FINALIZING:
            raise StateError(f"recording_finished in {self.state.value}")
        self.state = DictationState.TRANSCRIBING

    def transcript_ready(self, trigger_still_down: bool) -> None:
        if self.state != DictationState.TRANSCRIBING:
            raise StateError(f"transcript_ready in {self.state.value}")
        self.state = (
            DictationState.WAITING_FOR_RELEASE
            if trigger_still_down
            else DictationState.INSERTING
        )

    def all_triggers_released(self) -> bool:
        if self.state != DictationState.WAITING_FOR_RELEASE:
            return False
        self.state = DictationState.INSERTING
        return True

    def insertion_finished(self) -> None:
        if self.state != DictationState.INSERTING:
            raise StateError(f"insertion_finished in {self.state.value}")
        self.state = DictationState.READY

    def fail(self) -> None:
        self.state = DictationState.ERROR

    def disable(self) -> None:
        self.state = DictationState.DISABLED

    def suspend(self) -> None:
        self.state = DictationState.SUSPENDED

    def shutdown(self) -> None:
        self.state = DictationState.SHUTTING_DOWN

