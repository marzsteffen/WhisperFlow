import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

import local_dictation.insertion as insertion_module
from local_dictation.insertion import (
    InsertionBackend,
    grapheme_clusters,
    plan_revision,
)

WINDOW = "{01234567-89ab-cdef-0123-456789abcdef}"
requires_unix_sockets = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="Linux Unix-domain socket test"
)


@requires_unix_sockets
def test_find_socket_accepts_live_owned_unix_datagram(tmp_path: Path) -> None:
    socket_path = tmp_path / "ydotool-dgram.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as daemon:
        daemon.bind(str(socket_path))
        socket_path.chmod(0o600)
        backend = InsertionBackend(
            tmp_path / "runtime",
            environ={"YDOTOOL_SOCKET": str(socket_path)},
        )

        assert backend.find_socket() == socket_path


@requires_unix_sockets
def test_live_owned_unix_stream_is_rejected(tmp_path: Path) -> None:
    socket_path = tmp_path / "ydotool-stream.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as daemon:
        daemon.bind(str(socket_path))
        socket_path.chmod(0o600)
        daemon.listen(1)

        assert not InsertionBackend._socket_is_safe(socket_path)


@requires_unix_sockets
def test_symlink_to_live_owned_unix_datagram_is_rejected(tmp_path: Path) -> None:
    socket_path = tmp_path / "real-ydotool.sock"
    link_path = tmp_path / "linked-ydotool.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as daemon:
        daemon.bind(str(socket_path))
        socket_path.chmod(0o600)
        link_path.symlink_to(socket_path)

        assert link_path.is_symlink()
        assert not InsertionBackend._socket_is_safe(link_path)


@requires_unix_sockets
def test_live_owned_unix_datagram_with_group_bits_is_rejected(tmp_path: Path) -> None:
    socket_path = tmp_path / "permissive-ydotool.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as daemon:
        daemon.bind(str(socket_path))
        socket_path.chmod(0o660)

        assert not InsertionBackend._socket_is_safe(socket_path)


@requires_unix_sockets
def test_stale_unix_datagram_socket_is_rejected(tmp_path: Path) -> None:
    socket_path = tmp_path / "stale-ydotool.sock"
    daemon = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    daemon.bind(str(socket_path))
    socket_path.chmod(0o600)
    daemon.close()

    assert socket_path.is_socket()
    assert not InsertionBackend._socket_is_safe(socket_path)


@requires_unix_sockets
def test_live_unix_datagram_owned_by_another_uid_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    socket_path = tmp_path / "foreign-ydotool.sock"
    owner_uid = os.getuid()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as daemon:
        daemon.bind(str(socket_path))
        socket_path.chmod(0o600)
        monkeypatch.setattr(insertion_module.os, "getuid", lambda: owner_uid + 1)

        assert not InsertionBackend._socket_is_safe(socket_path)


def test_sensitive_clipboard_precedes_shift_insert(tmp_path: Path) -> None:
    calls = []
    sleeps = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(
        tmp_path,
        runner=runner,
        environ={},
        sleeper=sleeps.append,
    )
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]
    result = backend.insert("Grüße")
    assert result.inserted
    assert calls[0][0] == [
        "wl-copy",
        "--sensitive",
        "--type",
        "text/plain;charset=utf-8",
    ]
    assert calls[0][1]["input"] == "Grüße".encode()
    assert calls[0][1]["stdout"] == subprocess.DEVNULL
    assert calls[0][1]["stderr"] == subprocess.DEVNULL
    assert "capture_output" not in calls[0][1]
    assert calls[0][1]["env"]["TMPDIR"] == str(tmp_path)
    assert sleeps == [0.05]
    assert calls[1][0] == ["ydotool", "key", "42:1", "110:1", "110:0", "42:0"]


def test_failed_insert_keeps_successful_copy(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1 if command[0] == "ydotool" else 0, "", "")

    backend = InsertionBackend(
        tmp_path,
        runner=runner,
        environ={},
        sleeper=lambda _delay: None,
    )
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]
    result = backend.insert("Text")
    assert result.copied
    assert not result.inserted


def test_ydotool_exception_sends_best_effort_key_releases(tmp_path: Path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["ydotool", "key", "42:1", "110:1", "110:0", "42:0"]:
            raise subprocess.TimeoutExpired(command, 5)
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(
        tmp_path,
        runner=runner,
        environ={"DISPLAY": "unused"},
        sleeper=lambda _delay: None,
    )
    socket_path = tmp_path / ".ydotool_socket"
    backend.ensure_daemon = lambda: socket_path  # type: ignore[method-assign]

    result = backend.insert("Text")

    assert result.copied
    assert not result.inserted
    assert [call[0] for call in calls] == [
        ["wl-copy", "--sensitive", "--type", "text/plain;charset=utf-8"],
        ["ydotool", "key", "42:1", "110:1", "110:0", "42:0"],
        ["ydotool", "key", "110:0", "42:0"],
    ]
    assert calls[1][1]["env"]["YDOTOOL_SOCKET"] == str(socket_path)
    assert calls[2][1]["env"]["YDOTOOL_SOCKET"] == str(socket_path)


def test_cancelled_insert_sends_no_external_command(tmp_path: Path) -> None:
    calls = []
    cancelled = threading.Event()
    cancelled.set()
    backend = InsertionBackend(
        tmp_path,
        runner=lambda command, **kwargs: calls.append((command, kwargs)),
        environ={},
    )

    result = backend.insert("nicht einfügen", cancel=cancelled)

    assert not result.copied
    assert not result.inserted
    assert calls == []


def test_lock_after_clipboard_copy_prevents_ydotool(tmp_path: Path) -> None:
    calls = []
    cancelled = threading.Event()

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "wl-copy":
            cancelled.set()
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={})
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.insert("nur kopieren", cancel=cancelled)

    assert result.copied
    assert not result.inserted
    assert [command[0][0] for command in calls] == ["wl-copy"]


def test_lock_during_settle_delay_prevents_ydotool(tmp_path: Path) -> None:
    calls = []
    cancelled = threading.Event()

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    def cancel_during_sleep(_delay: float) -> None:
        cancelled.set()

    backend = InsertionBackend(
        tmp_path,
        runner=runner,
        environ={},
        sleeper=cancel_during_sleep,
    )
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.insert("nur kopieren", cancel=cancelled)

    assert result.copied
    assert not result.inserted
    assert [command[0][0] for command in calls] == ["wl-copy"]


def test_revision_plan_replaces_only_changed_grapheme_suffix() -> None:
    assert plan_revision("", "Hallo") == insertion_module.RevisionPlan(0, "Hallo", "")
    assert plan_revision("Hallo", "Hallo Welt") == insertion_module.RevisionPlan(
        0, " Welt", "Hallo"
    )
    assert plan_revision("Hallo Wekt", "Hallo Welt") == insertion_module.RevisionPlan(
        2, "lt", "Hallo We"
    )
    assert plan_revision("Hallo Welt", "Hallo") == insertion_module.RevisionPlan(
        5, "", "Hallo"
    )


def test_grapheme_clusters_cover_dictation_unicode_boundaries() -> None:
    assert grapheme_clusters("äöüß") == ("ä", "ö", "ü", "ß")
    assert grapheme_clusters("u\u0308") == ("u\u0308",)
    assert grapheme_clusters("👩🏽\u200d💻") == ("👩🏽\u200d💻",)
    assert grapheme_clusters("🇩🇪🇦🇹") == ("🇩🇪", "🇦🇹")
    assert plan_revision("Grüße 👩🏽\u200d💻", "Grüße!").delete_graphemes == 2


def test_capture_active_window_accepts_only_kwin_uuid(tmp_path: Path) -> None:
    outputs = iter((WINDOW + "\n", "not a window\n"))

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, next(outputs), "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={})

    assert backend.capture_active_window() == WINDOW
    assert backend.capture_active_window() is None


def test_revision_copies_before_exact_backspaces_and_never_sends_enter(
    tmp_path: Path,
) -> None:
    calls = []
    events = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        events.append(("command", command[0]))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, WINDOW + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def sleeper(delay: float) -> None:
        events.append(("sleep", delay))

    backend = InsertionBackend(tmp_path, runner=runner, environ={}, sleeper=sleeper)
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.revise("Hallo Wekt", "Hallo Welt", expected_window=WINDOW)

    assert result.revised
    assert result.state_known
    commands = [call[0] for call in calls]
    assert [command[0] for command in commands] == [
        "kdotool",
        "wl-copy",
        "kdotool",
        "ydotool",
    ]
    assert calls[1][1]["input"] == b"lt"
    key_command = commands[-1]
    assert key_command[:3] == ["ydotool", "key", "--key-delay=1"]
    assert key_command[3:7] == ["14:1", "14:0", "14:1", "14:0"]
    assert key_command[-4:] == ["42:1", "110:1", "110:0", "42:0"]
    assert all(not item.startswith("28:") for item in key_command)
    assert events[-2:] == [("command", "ydotool"), ("sleep", 0.1)]


def test_revision_can_delete_without_pasting_and_keeps_recovery_clipboard(tmp_path: Path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, WINDOW, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={}, sleeper=lambda _delay: None)
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.revise("Hallo Welt", "Hallo", expected_window=WINDOW)

    assert result.revised
    assert result.copied
    clipboard = next(kwargs for command, kwargs in calls if command[0] == "wl-copy")
    assert clipboard["input"] == b"Hallo"
    key_command = next(command for command, _ in calls if command[0] == "ydotool")
    assert key_command.count("14:1") == 5
    assert key_command.count("14:0") == 5
    assert "110:1" not in key_command


def test_unchanged_revision_only_checks_target(tmp_path: Path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, WINDOW, "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={})

    result = backend.revise("gleich", "gleich", expected_window=WINDOW)

    assert result.revised
    assert [command[0][0] for command in calls] == ["kdotool"]


def test_focus_change_prevents_clipboard_and_destructive_keys(tmp_path: Path) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            "{11111111-2222-3333-4444-555555555555}\n",
            "",
        )

    backend = InsertionBackend(tmp_path, runner=runner, environ={})

    result = backend.revise("Entwurf", "Endtext", expected_window=WINDOW)

    assert not result.revised
    assert result.state_known
    assert [command[0][0] for command in calls] == ["kdotool"]


def test_focus_change_after_copy_still_prevents_destructive_keys(tmp_path: Path) -> None:
    calls = []
    windows = iter((WINDOW, "{11111111-2222-3333-4444-555555555555}"))

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, next(windows), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={}, sleeper=lambda _delay: None)
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.revise("Entwurf", "Endtext", expected_window=WINDOW)

    assert not result.revised
    assert result.state_known
    assert result.copied
    assert [command[0][0] for command in calls] == [
        "kdotool",
        "wl-copy",
        "kdotool",
    ]


def test_revision_cancel_after_copy_emits_no_destructive_keys(tmp_path: Path) -> None:
    calls = []
    cancelled = threading.Event()
    sleeps = 0

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, WINDOW, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def sleeper(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            cancelled.set()

    backend = InsertionBackend(tmp_path, runner=runner, environ={}, sleeper=sleeper)
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.revise("alt", "neu", expected_window=WINDOW, cancel=cancelled)

    assert not result.revised
    assert result.state_known
    assert result.cancelled
    assert result.copied
    assert [command[0][0] for command in calls] == ["kdotool", "wl-copy"]


def test_revision_cancel_during_second_focus_query_emits_no_destructive_keys(
    tmp_path: Path,
) -> None:
    calls = []
    cancelled = threading.Event()
    focus_queries = 0

    def runner(command, **kwargs):
        nonlocal focus_queries
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            focus_queries += 1
            if focus_queries == 2:
                cancelled.set()
            return subprocess.CompletedProcess(command, 0, WINDOW, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(
        tmp_path,
        runner=runner,
        environ={},
        sleeper=lambda _delay: None,
    )
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.revise("alt", "neu", expected_window=WINDOW, cancel=cancelled)

    assert not result.revised
    assert result.state_known
    assert result.cancelled
    assert result.copied
    assert [command[0][0] for command in calls] == [
        "kdotool",
        "wl-copy",
        "kdotool",
    ]


def test_revision_command_failure_marks_field_state_unknown_and_releases_keys(
    tmp_path: Path,
) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, WINDOW, "")
        if command[:2] == ["ydotool", "key"] and "--key-delay=1" in command:
            return subprocess.CompletedProcess(command, 1, "", "failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={}, sleeper=lambda _delay: None)
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.revise("alt", "neu", expected_window=WINDOW)

    assert not result.revised
    assert not result.state_known
    assert calls[-1][0] == ["ydotool", "key", "14:0", "110:0", "42:0"]


def test_revision_rollback_limit_fails_closed_and_copies_recovery_text(
    tmp_path: Path,
) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, WINDOW, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={})

    result = backend.revise("a" * 65, "sicher", expected_window=WINDOW)

    assert not result.revised
    assert result.state_known
    assert result.copied
    assert [command[0][0] for command in calls] == ["kdotool", "wl-copy"]
    assert calls[-1][1]["input"] == b"sicher"


def test_revision_refuses_nonportable_multicodepoint_backspace_suffix(
    tmp_path: Path,
) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, WINDOW, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(tmp_path, runner=runner, environ={})

    result = backend.revise("Hallo 🇩🇪", "Hallo!", expected_window=WINDOW)

    assert not result.revised
    assert result.state_known
    assert result.copied
    assert "Unicode" in result.message
    assert [command[0][0] for command in calls] == ["kdotool", "wl-copy"]
    assert calls[-1][1]["input"] == b"Hallo!"


def test_revision_allows_precomposed_german_letters_as_single_backspaces(
    tmp_path: Path,
) -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "kdotool":
            return subprocess.CompletedProcess(command, 0, WINDOW, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = InsertionBackend(
        tmp_path,
        runner=runner,
        environ={},
        sleeper=lambda _delay: None,
    )
    backend.ensure_daemon = lambda: tmp_path / ".ydotool_socket"  # type: ignore[method-assign]

    result = backend.revise("Grü", "Gru", expected_window=WINDOW)

    assert result.revised
    key_command = next(command for command, _ in calls if command[0] == "ydotool")
    assert key_command.count("14:1") == 1
    assert key_command.count("14:0") == 1
