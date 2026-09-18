from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from local_dictation.app import _acquire_instance_lock


def test_instance_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    descriptor = _acquire_instance_lock(tmp_path / "runtime")
    path = tmp_path / "runtime" / "instance.lock"
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(RuntimeError, match="bereits"):
            _acquire_instance_lock(tmp_path / "runtime")
    finally:
        os.close(descriptor)

    replacement = _acquire_instance_lock(tmp_path / "runtime")
    os.close(replacement)
