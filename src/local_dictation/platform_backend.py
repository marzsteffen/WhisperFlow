"""Small platform switchboard; UI and controller stay shared on every OS."""

from __future__ import annotations

import sys

from .input_proxy import spawn_input_proxy
from .insertion import InsertionBackend
from .recorder import Recorder


def create_input_proxy():
    if sys.platform == "win32":
        from .windows_input import spawn_windows_input

        return spawn_windows_input()
    return spawn_input_proxy()


def create_recorder(runtime_dir, parent=None):
    if sys.platform == "win32":
        from .windows_recorder import WindowsRecorder

        return WindowsRecorder(runtime_dir, parent)
    return Recorder(runtime_dir, parent)


def create_insertion_backend(runtime_dir):
    if sys.platform == "win32":
        from .windows_insertion import WindowsInsertionBackend

        return WindowsInsertionBackend(runtime_dir)
    return InsertionBackend(runtime_dir)


__all__ = ["create_input_proxy", "create_insertion_backend", "create_recorder"]
