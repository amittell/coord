from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordination import db as db_mod


# fcntl.flock is POSIX-only. On Windows, acquire_instance_lock is a
# documented no-op that returns a sentinel. The tests below actively
# exercise the flock semantics (real fd, cross-process contention) so
# they only make sense on POSIX. The Windows no-op path is covered
# separately by test_acquire_lock_windows_skip at the bottom of this file.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl.flock is POSIX-only; Windows takes the documented no-op path",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the bypass env var does not leak from other tests."""
    monkeypatch.delenv("COORD_DISABLE_INSTANCE_LOCK", raising=False)


@_POSIX_ONLY
def test_acquire_lock_succeeds_when_uncontested(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    fd = db_mod.acquire_instance_lock(db_path)
    try:
        assert fd is not None
        assert isinstance(fd, int)
        assert fd >= 0
    finally:
        os.close(fd)


@_POSIX_ONLY
def test_acquire_lock_second_call_raises_with_pid(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    fd = db_mod.acquire_instance_lock(db_path)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            db_mod.acquire_instance_lock(db_path)
        msg = str(exc_info.value)
        assert "Another coord service" in msg
        assert str(os.getpid()) in msg
    finally:
        os.close(fd)


@_POSIX_ONLY
def test_acquire_lock_released_on_fd_close(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    fd = db_mod.acquire_instance_lock(db_path)
    os.close(fd)
    fd2 = db_mod.acquire_instance_lock(db_path)
    try:
        assert fd2 >= 0
    finally:
        os.close(fd2)


@_POSIX_ONLY
def test_acquire_lock_subprocess_contention(tmp_path: Path) -> None:
    db_path = tmp_path / "db.sqlite"
    ready_marker = tmp_path / "child.ready"
    release_marker = tmp_path / "child.release"

    child_src = textwrap.dedent(
        f"""
        import os, sys, time
        from pathlib import Path
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from coordination.db import acquire_instance_lock
        fd = acquire_instance_lock(Path({str(db_path)!r}))
        Path({str(ready_marker)!r}).write_text(str(os.getpid()))
        # Wait up to 5s for the parent to signal release.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if Path({str(release_marker)!r}).exists():
                break
            time.sleep(0.02)
        os.close(fd)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", child_src])
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and not ready_marker.exists():
            time.sleep(0.02)
        assert ready_marker.exists(), "child never reported lock acquisition"
        child_pid = int(ready_marker.read_text())

        with pytest.raises(RuntimeError) as exc_info:
            db_mod.acquire_instance_lock(db_path)
        msg = str(exc_info.value)
        assert "Another coord service" in msg
        assert str(child_pid) in msg
    finally:
        release_marker.write_text("go")
        proc.wait(timeout=5)


@_POSIX_ONLY
def test_acquire_lock_bypass_via_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "db.sqlite"
    fd = db_mod.acquire_instance_lock(db_path)
    try:
        monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "true")
        # Must not raise even though the lock is held.
        fd2 = db_mod.acquire_instance_lock(db_path)
        assert fd2 is not None  # sentinel or a real fd
    finally:
        os.close(fd)


def test_acquire_lock_windows_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setattr(sys, "platform", "win32")
    a = db_mod.acquire_instance_lock(db_path)
    b = db_mod.acquire_instance_lock(db_path)
    # Both calls must succeed on Windows; the function returns a sentinel
    # (non-raising) so the caller can hold it for the process lifetime.
    assert a is not None
    assert b is not None
