"""End-to-end integration tests for `coord start --background` and `coord stop`.

These tests spawn the real service subprocess (no mocks), probe `/readyz`
over HTTP, then issue `coord stop` and verify the service terminates and
the PID file is removed. They validate the live path that unit tests
short-circuit with mocks.

Marked `integration` so CI loops that want the fast suite can deselect
with `pytest -m "not integration"`. Enabled by default.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

from coordination import cli
from coordination.cli_ops import _read_pid_record


# `_wait_for_http_ready` now defaults to 15s in `coordination.cli_start`,
# which tolerates cold-start on loaded machines. The previous
# `patient_readiness` monkeypatch fixture was removed once the production
# default became safe; ops who need more headroom can bump it via the
# COORD_START_READY_TIMEOUT_SEC environment variable.
#
# These tests use POSIX signal semantics (SIGTERM/SIGKILL in teardown,
# `coord stop`'s signal path). Windows uses the PowerShell-backed dispatch
# in cli_ops (covered by unit tests with mocks) but os.kill's signal
# semantics differ; live integration is POSIX-only by design.


pytestmark = [
    pytest.mark.integration,
    pytest.mark.platform,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="Integration tests use POSIX signals; Windows path is mock-tested in test_cli_ops.py.",
    ),
]


def _pick_free_port() -> int:
    """Bind to port 0 on loopback, read the assigned port, then release it.

    There is an unavoidable TOCTOU window between releasing the port here
    and the service binding to it. The `_start_with_port_retry` helper
    below retries on this specific class of collision.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", 0))
        except OSError as exc:
            pytest.skip(f"cannot bind 127.0.0.1: {exc}")
        return int(s.getsockname()[1])


_PORT_COLLISION_MARKERS = (
    "address already in use",
    "errno 48",
    "errno 98",
    "winerror 10048",
)


def _looks_like_port_collision(text: str) -> bool:
    """Heuristic: did `coord start` fail because the port was taken?"""
    lowered = text.lower()
    return any(marker in lowered for marker in _PORT_COLLISION_MARKERS)


def _start_with_port_retry(
    cli_args_without_port: list[str],
    capsys: pytest.CaptureFixture[str],
    *,
    max_attempts: int = 3,
    runner=None,
) -> tuple[int, int, str]:
    """Run `coord start ...` with a fresh port, retrying on collisions.

    Returns (exit_code, port_used, captured_stdout). Raises RuntimeError
    after `max_attempts` consecutive port collisions, naming the failure.

    `runner` defaults to `cli.main`. Tests can pass a stub to exercise
    retry logic without spawning real processes.
    """
    run = runner if runner is not None else cli.main
    last_output = ""
    last_port = -1
    for attempt in range(1, max_attempts + 1):
        port = _pick_free_port()
        last_port = port
        args = list(cli_args_without_port) + ["--port", str(port)]
        exit_code = run(args)
        captured = capsys.readouterr()
        last_output = (captured.out or "") + (captured.err or "")
        if exit_code == 0:
            return exit_code, port, last_output
        if _looks_like_port_collision(last_output):
            continue
        return exit_code, port, last_output
    raise RuntimeError(
        f"coord start failed with port collisions after {max_attempts} attempts; "
        f"last port {last_port}; last output: {last_output.strip()[:400]}"
    )


def test_port_race_retry_succeeds_after_collision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """First attempt simulates a port collision; second attempt succeeds.
    The retry helper must transparently recover."""
    state = {"calls": 0}

    def stub_runner(args: list[str]) -> int:
        state["calls"] += 1
        if state["calls"] == 1:
            print("[Errno 48] Address already in use")
            return 1
        print("started ok")
        return 0

    exit_code, _port, output = _start_with_port_retry(
        ["start", "--background", "--host", "127.0.0.1"],
        capsys,
        runner=stub_runner,
    )
    assert exit_code == 0
    assert state["calls"] == 2
    assert "started ok" in output


def test_port_race_retry_gives_up_after_3_attempts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All three attempts collide; helper must raise RuntimeError naming the cause."""
    state = {"calls": 0}

    def stub_runner(args: list[str]) -> int:
        state["calls"] += 1
        print("OSError: [Errno 48] Address already in use")
        return 1

    with pytest.raises(RuntimeError) as excinfo:
        _start_with_port_retry(
            ["start", "--background", "--host", "127.0.0.1"],
            capsys,
            runner=stub_runner,
        )
    assert state["calls"] == 3
    message = str(excinfo.value).lower()
    assert "port collision" in message or "address already in use" in message
    assert "3 attempts" in message


def _force_kill_pid_file(home: Path) -> None:
    """Best-effort teardown: if a PID file remains, SIGKILL the recorded PID.

    Used in `finally` blocks so a mid-test exception never leaks a
    background service. Safe to call when nothing is running.
    """
    pid_file = home / "coord.pid"
    if not pid_file.exists():
        return
    try:
        pid, _record = _read_pid_record(pid_file)
    except OSError:
        pid = None
    if pid is not None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
    try:
        pid_file.unlink()
    except OSError:
        pass


def _wait_ready(url: str, timeout: float = 10.0) -> httpx.Response:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/readyz", timeout=1.0)
            if response.status_code == 200:
                return response
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.1)
    raise AssertionError(
        f"/readyz did not return 200 within {timeout}s; last error: {last_exc}"
    )


def _wait_down(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/readyz", timeout=0.5)
        except httpx.HTTPError:
            return
        time.sleep(0.1)
    raise AssertionError(f"service at {url} still responding after {timeout}s")


def test_coord_start_background_and_stop_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Full round-trip: start background service, hit /readyz, stop, verify dead."""
    home = tmp_path / ".coord-home"
    monkeypatch.setenv("COORD_HOME", str(home))
    # Spawned service also needs an auth token; ensure_token_file will
    # generate one, but we also set it explicitly to simulate normal use.
    monkeypatch.setenv("COORD_AUTH_TOKEN", "integration-test-token")

    try:
        exit_code, port, stdout = _start_with_port_retry(
            ["start", "--background", "--host", "127.0.0.1", "--json"],
            capsys,
        )
        api_url = f"http://127.0.0.1:{port}"
        assert exit_code == 0, "coord start --background should succeed"

        state = json.loads(stdout)
        assert state["api_url"] == api_url
        assert state["pid"] is not None

        # Confirm /readyz is actually serving.
        response = _wait_ready(api_url, timeout=5.0)
        body = response.json()
        assert body.get("status") == "ready"

        # PID file must exist in the new JSON format.
        pid, record = _read_pid_record(home / "coord.pid")
        assert pid == state["pid"]
        assert record is not None
        assert record.marker == "coordination.cli _serve"

        # Now stop the service.
        stop_code = cli.main(["stop"])
        assert stop_code == 0
        stop_out = capsys.readouterr().out
        assert "Stopped" in stop_out

        # Verify /readyz no longer responds.
        _wait_down(api_url, timeout=5.0)
        # PID file was removed on success.
        assert not (home / "coord.pid").exists()
    finally:
        _force_kill_pid_file(home)


def test_coord_start_twice_detects_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Starting twice on the same port should detect the running service
    and return 0 without spawning a second process."""
    home = tmp_path / ".coord-home"
    monkeypatch.setenv("COORD_HOME", str(home))
    monkeypatch.setenv("COORD_AUTH_TOKEN", "integration-test-token")

    try:
        # First start.
        first_exit, port, first_out = _start_with_port_retry(
            ["start", "--background", "--host", "127.0.0.1", "--json"],
            capsys,
        )
        api_url = f"http://127.0.0.1:{port}"
        assert first_exit == 0
        first_state = json.loads(first_out)
        first_pid = first_state["pid"]
        _wait_ready(api_url, timeout=5.0)

        # Second start on the SAME port must detect the running service.
        # We deliberately reuse the bound port, so we do not retry here.
        assert cli.main([
            "start", "--background",
            "--host", "127.0.0.1", "--port", str(port),
            "--json",
        ]) == 0
        second_state = json.loads(capsys.readouterr().out)
        # Second invocation printed state but did not spawn a new process:
        # it reports no pid (no process was started by this call).
        assert second_state.get("pid") is None

        # PID file still references the first process.
        pid, _record = _read_pid_record(home / "coord.pid")
        assert pid == first_pid

        # Clean up via stop.
        assert cli.main(["stop"]) == 0
        _wait_down(api_url, timeout=5.0)
    finally:
        _force_kill_pid_file(home)
