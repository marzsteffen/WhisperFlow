"""Tests for git-based update discovery and the headless updater."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from local_dictation import updater


def _result(args_tail: str, *, code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git", "-C", "repo", *args_tail.split()], code, stdout, stderr)


class FakeGit:
    """Maps git subcommands to canned responses."""

    def __init__(self, responses: dict[str, subprocess.CompletedProcess]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        for tail, response in self.responses.items():
            tail_args = tail.split()
            if args[-len(tail_args):] == tail_args if tail_args else True:
                return response
        return _result("", code=1, stderr=f"unexpected call {args}")


def test_check_for_update_counts_behind_commits() -> None:
    fake = FakeGit(
        {
            "rev-parse HEAD": _result("rev-parse HEAD", stdout="abc123\n"),
            "fetch origin main": _result("fetch origin main"),
            "rev-parse origin/main": _result("rev-parse origin/main", stdout="def456\n"),
            "rev-list --count HEAD..origin/main": _result("rev-list --count HEAD..origin/main", stdout="3\n"),
        }
    )
    info = updater.check_for_update("C:/repo", "main", runner=fake)
    assert info.behind == 3
    assert info.current == "abc123"
    assert info.remote == "def456"
    assert info.error == ""


def test_check_for_update_reports_fetch_failure() -> None:
    fake = FakeGit(
        {
            "rev-parse HEAD": _result("rev-parse HEAD", stdout="abc123\n"),
            "fetch origin main": _result("fetch origin main", code=128, stderr="fatal: could not read from remote\n"),
        }
    )
    info = updater.check_for_update("C:/repo", "main", runner=fake)
    assert info.behind == 0
    assert "could not read" in info.error


def test_check_for_update_survives_missing_git() -> None:
    def broken(_args: list[str]) -> subprocess.CompletedProcess:
        raise FileNotFoundError("git nicht gefunden")

    info = updater.check_for_update("C:/repo", "main", runner=broken)
    assert info.behind == 0
    assert info.error != ""


def test_marker_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(updater, "marker_path", lambda: tmp_path / "update.json")
    updater.save_marker("C:/repo", "feature", installed_commit="abc123")
    assert updater.load_marker() == {
        "repo": "C:/repo",
        "branch": "feature",
        "installed_commit": "abc123",
    }


def test_check_for_update_detects_checkout_newer_than_installed_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        updater,
        "load_marker",
        lambda: {
            "repo": "C:/repo",
            "branch": "main",
            "installed_commit": "old123",
        },
    )
    fake = FakeGit(
        {
            "rev-parse HEAD": _result("rev-parse HEAD", stdout="new456\n"),
            "fetch origin main": _result("fetch origin main"),
            "rev-parse origin/main": _result("rev-parse origin/main", stdout="new456\n"),
            "rev-list --count old123..origin/main": _result(
                "rev-list --count old123..origin/main", stdout="1\n"
            ),
            "rev-list --count old123..HEAD": _result(
                "rev-list --count old123..HEAD", stdout="1\n"
            ),
        }
    )

    info = updater.check_for_update("C:/repo", "main", runner=fake)

    assert info.current == "new456"
    assert info.remote == "new456"
    assert info.behind == 1


def test_load_marker_tolerates_missing_or_invalid_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(updater, "marker_path", lambda: tmp_path / "update.json")
    assert updater.load_marker() == {}
    (tmp_path / "update.json").write_text("not json", encoding="utf-8")
    assert updater.load_marker() == {}


def test_apply_update_pulls_installs_and_relaunches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, list[str]]] = []
    spawned: list[list[str]] = []
    saved: list[tuple[Path, str, str]] = []

    def fake_git(_repo: Path, *args: str, timeout: float = 0) -> subprocess.CompletedProcess:
        if args == ("rev-parse", "HEAD"):
            return _result("rev-parse HEAD", stdout="installed789\n")
        return _result("pull")

    monkeypatch.setattr(updater, "_git", fake_git)
    monkeypatch.setattr(
        updater,
        "save_marker",
        lambda repo, branch, *, installed_commit="": saved.append(
            (Path(repo), branch, installed_commit)
        ),
    )
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda args, **_kw: calls.append(("pip", args)) or subprocess.CompletedProcess(args, 0),
    )
    monkeypatch.setattr(
        updater.subprocess,
        "Popen",
        lambda args, **_kw: spawned.append(args) or None,
    )
    monkeypatch.setattr(updater, "_process_alive", lambda _pid: False)
    monkeypatch.setattr(updater, "_log", lambda _message: None)

    updater.run_apply_update(tmp_path, "main", parent_pid=1234)

    assert any("pip" in args for _, args in calls)
    assert saved == [(tmp_path, "main", "installed789")]
    assert spawned and "local_dictation" in spawned[0][2] and "--daemon" in spawned[0]


@pytest.mark.parametrize("alive,expected", [(True, False), (False, True)])
def test_wait_for_exit_polls_process_liveness(monkeypatch: pytest.MonkeyPatch, alive: bool, expected: bool) -> None:
    monkeypatch.setattr(updater, "_process_alive", lambda _pid: alive)
    assert updater.wait_for_exit(4321, timeout=0.4) is expected
