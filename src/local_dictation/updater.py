"""Git-based update discovery and headless update application.

The installer records the source checkout it installed from in
``update.json``. At runtime the app can check that checkout against its
upstream branch and, with user consent, apply the update in a detached
helper process that waits for the daemon to exit, reinstalls the package,
and relaunches the daemon.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import get_data_dir

MARKER_FILENAME = "update.json"
UPDATE_LOG_FILENAME = "update.log"


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    repo: str
    branch: str
    current: str
    remote: str
    behind: int
    error: str = ""


def marker_path() -> Path:
    return get_data_dir() / MARKER_FILENAME


def save_marker(
    repo: str | Path,
    branch: str,
    *,
    installed_commit: str = "",
) -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {"repo": str(repo), "branch": branch}
    if installed_commit:
        values["installed_commit"] = installed_commit
    path.write_text(json.dumps(values) + "\n", encoding="utf-8")


def load_marker() -> dict[str, str]:
    try:
        values = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    repo = str(values.get("repo", ""))
    branch = str(values.get("branch", ""))
    if not repo or not branch:
        return {}
    marker = {"repo": repo, "branch": branch}
    installed_commit = str(values.get("installed_commit", "")).strip()
    if installed_commit:
        marker["installed_commit"] = installed_commit
    return marker


def _same_checkout(first: str | Path, second: str | Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
            os.path.abspath(second)
        )
    except (OSError, TypeError, ValueError):
        return False


def _git(repo: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def check_for_update(
    repo: str | Path,
    branch: str,
    *,
    fetch: bool = True,
    runner: Any = None,
) -> UpdateInfo:
    """Compare the local checkout with its upstream; never raises."""

    def run(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if runner is not None:
            return runner(["git", "-C", str(repo), *args])
        return _git(repo, *args)

    try:
        current = run("rev-parse", "HEAD")
        if current.returncode != 0:
            return UpdateInfo(str(repo), branch, "", "", 0, error=current.stderr.strip() or "git rev-parse fehlgeschlagen")
        if fetch:
            fetched = run("fetch", "origin", branch, timeout=30)
            if fetched.returncode != 0:
                detail = (fetched.stderr or fetched.stdout).strip().splitlines()
                message = detail[-1] if detail else "git fetch fehlgeschlagen"
                return UpdateInfo(str(repo), branch, current.stdout.strip(), "", 0, error=message)
        remote = run("rev-parse", f"origin/{branch}")
        if remote.returncode != 0:
            return UpdateInfo(str(repo), branch, current.stdout.strip(), "", 0, error=f"origin/{branch} ist unbekannt")

        current_revision = current.stdout.strip()
        remote_revision = remote.stdout.strip()
        marker = load_marker()
        installed_revision = current_revision
        installed_ref = "HEAD"
        if (
            marker.get("branch") == branch
            and _same_checkout(marker.get("repo", ""), repo)
            and marker.get("installed_commit")
        ):
            installed_revision = marker["installed_commit"]
            installed_ref = installed_revision

        targets = [f"{installed_ref}..origin/{branch}"]
        if current_revision != installed_revision:
            targets.append(f"{installed_ref}..HEAD")

        counts: list[int] = []
        for target in targets:
            counting = run("rev-list", "--count", target)
            if counting.returncode != 0:
                continue
            try:
                counts.append(int(counting.stdout.strip() or "0"))
            except ValueError:
                continue
        behind = max(counts, default=0)
        if installed_revision not in {current_revision, remote_revision} and not counts:
            # The recorded revision may have been pruned or may belong to a
            # rebased branch.  A differing installed revision still requires
            # one reinstall even when Git can no longer count the commits.
            behind = 1
        return UpdateInfo(
            repo=str(repo),
            branch=branch,
            current=current_revision,
            remote=remote_revision,
            behind=behind,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateInfo(str(repo), branch, "", "", 0, error=str(exc))


def _log(message: str) -> None:
    try:
        path = get_data_dir() / UPDATE_LOG_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            try:
                WAIT_TIMEOUT = 0x00000102
                return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return False
    return Path(f"/proc/{pid}").exists()


def wait_for_exit(pid: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.5)
    return not _process_alive(pid)


def run_apply_update(repo: str | Path, branch: str, *, parent_pid: int | None = None) -> int:
    """Headless updater: wait for the daemon, pull, reinstall, relaunch."""

    repo_path = Path(repo)
    _log(f"Update gestartet: {repo_path} ({branch})")
    if parent_pid and not wait_for_exit(parent_pid):
        _log(f"Warten auf Daemon (PID {parent_pid}) fehlgeschlagen; fahre trotzdem fort")
    pulled = _git(repo_path, "pull", "--ff-only", "origin", branch, timeout=600)
    if pulled.returncode != 0:
        detail = (pulled.stderr or pulled.stdout).strip().splitlines()
        _log(f"git pull fehlgeschlagen: {detail[-1] if detail else pulled.returncode}")
    else:
        _log("git pull abgeschlossen")
    pip = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", "--upgrade", str(repo_path)],
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if pip.returncode != 0:
        _log(f"pip install fehlgeschlagen: {(pip.stderr or pip.stdout).strip().splitlines()[-1:]}")
    else:
        _log("App-Paket aktualisiert")
        revision = _git(repo_path, "rev-parse", "HEAD")
        installed_commit = revision.stdout.strip() if revision.returncode == 0 else ""
        if installed_commit:
            try:
                save_marker(repo_path, branch, installed_commit=installed_commit)
                _log(f"Installierten Stand gespeichert: {installed_commit[:12]}")
            except OSError as exc:
                _log(f"Installierter Stand konnte nicht gespeichert werden: {exc}")
    executable = Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else sys.executable
    if not executable.is_file():
        executable = Path(sys.executable)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [str(executable), "-m", "local_dictation", "--daemon"],
        creationflags=flags,
        start_new_session=os.name != "nt",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    _log("Daemon neu gestartet")
    return 0


def spawn_apply_update(repo: str | Path, branch: str, *, parent_pid: int | None = None) -> subprocess.Popen[Any]:
    """Start the detached headless updater; returns immediately."""

    executable = sys.executable
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [executable, "-m", "local_dictation.updater", str(repo), branch, str(parent_pid or 0)],
        creationflags=flags,
        start_new_session=os.name != "nt",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) < 2:
        print("Verwendung: local_dictation.updater REPO BRANCH [PARENT_PID]", file=sys.stderr)
        return 2
    repo, branch = values[0], values[1]
    parent_pid = int(values[2]) if len(values) > 2 and values[2].isdigit() else None
    if parent_pid == 0:
        parent_pid = None
    return run_apply_update(repo, branch, parent_pid=parent_pid)


if __name__ == "__main__":
    raise SystemExit(main())
