import pytest

from local_dictation.state import DictationState, DictationStateMachine, StateError


def test_happy_path() -> None:
    machine = DictationStateMachine()
    machine.set_ready()
    assert machine.trigger_down()
    machine.recording_started()
    assert machine.trigger_up()
    machine.recording_finished()
    machine.transcript_ready(False)
    machine.insertion_finished()
    assert machine.state is DictationState.READY


def test_busy_trigger_is_ignored_and_maximum_waits_for_release() -> None:
    machine = DictationStateMachine()
    machine.set_ready()
    machine.trigger_down()
    machine.recording_started()
    assert not machine.trigger_down()
    machine.trigger_up()
    machine.recording_finished()
    machine.transcript_ready(True)
    assert machine.state is DictationState.WAITING_FOR_RELEASE
    assert machine.all_triggers_released()
    assert machine.state is DictationState.INSERTING


def test_illegal_transition_is_reported() -> None:
    machine = DictationStateMachine()
    with pytest.raises(StateError):
        machine.recording_started()

