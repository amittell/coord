"""Exercise the Makefile guard with real process matching and isolated runners."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.platform, pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("make") or not shutil.which("pgrep"),
    reason="POSIX make and pgrep are required",
)]


def _fixture(root: Path, special: bool = False) -> tuple[Path, Path]:
    root.mkdir()
    venv = root / ("venv [qa]+(one)" if special else ".venv")
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    (root / "Makefile").write_text(
        (Path(__file__).resolve().parents[1] / "Makefile").read_text()
    )
    (bindir / "activate").write_text(f"PATH={shlex.quote(str(bindir))}:$PATH; export PATH\n")
    runner = bindir / "pytest"
    runner.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        "if '--hold' in sys.argv:\n"
        "    pathlib.Path(sys.argv[-1]).write_text('ready')\n"
        "    time.sleep(30)\n"
        "else:\n"
        "    print('ISOLATED_PYTEST_ENTERED')\n"
    )
    runner.chmod(0o755)
    return venv, runner


def _start(runner: Path, ready: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([str(runner), "--hold", str(ready)])
    deadline = time.monotonic() + 5
    while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("Owned competitor did not start")
    return proc


@pytest.mark.parametrize("layout", ["plain", "special", "symlink"])
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("competitor", ["none", "same", "other"])
def test_make_guard_matches_only_real_target_runner(
    tmp_path: Path, layout: str, absolute: bool, competitor: str
) -> None:
    project = tmp_path / ("project [x]+(one).dot" if layout == "special" else "project")
    venv, runner = _fixture(project, special=layout == "special")
    if layout == "symlink":
        link = project / "linked-venv"
        link.symlink_to(venv, target_is_directory=True)
        venv = link
        if absolute:
            runner = link / "bin" / "pytest"
    proc = None
    if competitor != "none":
        if competitor == "other":
            _, runner = _fixture(tmp_path / "other-project", special=layout == "special")
        proc = _start(runner, tmp_path / "ready")
    try:
        argument = str(venv) if absolute else os.path.relpath(venv, project)
        result = subprocess.run(
            ["make", f"VENV={argument}", "test"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if competitor == "same":
            assert result.returncode != 0
            assert "another coord pytest run" in result.stdout
            assert "ISOLATED_PYTEST_ENTERED" not in result.stdout
            assert "[p]ytest" in result.stdout
            assert proc is not None and proc.poll() is None
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            assert "ISOLATED_PYTEST_ENTERED" in result.stdout
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)
