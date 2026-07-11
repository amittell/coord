from __future__ import annotations

import json
import os
import signal
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from coordination import cli, cli_ops


# This module owns the CLI's POSIX/Windows process dispatch, signal behavior,
# and OS-facing status paths. Keep it in the small per-PR platform smoke set.
pytestmark = pytest.mark.platform


def _make_repo(root: Path) -> Path:
    repo = root / "app"
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    return repo


class _MockServer:
    """Tiny HTTP server + state helpers used by several status/claims/release tests."""

    def __init__(self, handler_class: type[BaseHTTPRequestHandler]) -> None:
        self.server = socketserver.TCPServer(("127.0.0.1", 0), handler_class)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockServer":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# coord stop
# ---------------------------------------------------------------------------


def test_pid_file_round_trip(tmp_path: Path) -> None:
    """Writing a PID record and reading it back yields the same fields."""
    from coordination.cli_ops import _read_pid_record, _write_pid_record

    path = tmp_path / "coord.pid"
    _write_pid_record(path, 43210, "2026-04-17T11:15:00.123Z", "coordination.cli _serve")

    pid, record = _read_pid_record(path)
    assert pid == 43210
    assert record is not None
    assert record.pid == 43210
    assert record.start_time == "2026-04-17T11:15:00.123Z"
    assert record.marker == "coordination.cli _serve"


class _FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


@pytest.fixture()
def _force_posix_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the cli_ops platform dispatcher to take the POSIX branch.

    The six tests below are pinning POSIX-specific behavior (argv shape,
    error handling) regardless of the host running them. Without this,
    they would take the Windows branch on a Windows CI runner and fail
    for the wrong reason (mismatched command vector). The dedicated
    Windows-branch tests sit in `test_pid_belongs_to_coord_windows_*`.
    """
    monkeypatch.setattr("coordination.cli_ops.sys.platform", "linux")


def test_pid_belongs_to_coord_calls_ps(
    monkeypatch: pytest.MonkeyPatch, _force_posix_platform: None
) -> None:
    """Verifies the helper shells out to `ps` with the expected argv and
    returns True only when the marker is present in ps output."""
    from coordination.cli_ops import _pid_belongs_to_coord

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = list(cmd)
        captured["timeout"] = kwargs.get("timeout")
        captured["capture_output"] = kwargs.get("capture_output")
        captured["text"] = kwargs.get("text")
        return _FakeCompleted(
            stdout="/usr/bin/python3 -m coordination.cli _serve\n",
            returncode=0,
        )

    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is True
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:3] == ["ps", "-p", "12345"]
    assert captured["timeout"] == 1.0
    assert captured["capture_output"] is True
    assert captured["text"] is True


def test_pid_belongs_to_coord_returns_false_when_marker_absent(
    monkeypatch: pytest.MonkeyPatch, _force_posix_platform: None
) -> None:
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(stdout="/usr/sbin/sshd -D\n", returncode=0)

    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


def test_pid_belongs_to_coord_returns_false_when_ps_errors(
    monkeypatch: pytest.MonkeyPatch, _force_posix_platform: None
) -> None:
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(stdout="", returncode=1)

    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


def test_pid_belongs_to_coord_returns_false_on_empty_stdout(
    monkeypatch: pytest.MonkeyPatch, _force_posix_platform: None
) -> None:
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(stdout="", returncode=0)

    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


def test_pid_belongs_to_coord_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch, _force_posix_platform: None
) -> None:
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd="ps", timeout=1.0)

    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


def test_pid_belongs_to_coord_returns_false_on_oserror(
    monkeypatch: pytest.MonkeyPatch, _force_posix_platform: None
) -> None:
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("ps not found")

    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


def test_pid_belongs_to_coord_posix_dispatches_to_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On POSIX (linux/darwin), the dispatcher must invoke `ps -p <pid> ...`."""
    from coordination.cli_ops import _pid_belongs_to_coord

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = list(cmd)
        return _FakeCompleted(
            stdout="/usr/bin/python3 -m coordination.cli _serve\n",
            returncode=0,
        )

    monkeypatch.setattr("coordination.cli_ops.sys.platform", "linux")
    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is True
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:3] == ["ps", "-p", "12345"]


def test_pid_belongs_to_coord_windows_dispatches_to_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows, the dispatcher must invoke PowerShell to query Win32_Process."""
    from coordination.cli_ops import _pid_belongs_to_coord

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = list(cmd)
        captured["timeout"] = kwargs.get("timeout")
        captured["capture_output"] = kwargs.get("capture_output")
        captured["text"] = kwargs.get("text")
        return _FakeCompleted(
            stdout="C:\\Python312\\python.exe -m coordination.cli _serve\r\n",
            returncode=0,
        )

    monkeypatch.setattr("coordination.cli_ops.sys.platform", "win32")
    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is True
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:3] == ["powershell", "-NoProfile", "-Command"]
    # Marker check: the joined command must reference ProcessId=<pid>.
    joined = " ".join(str(part) for part in cmd)
    assert "12345" in joined
    assert captured["timeout"] == 2.0
    assert captured["capture_output"] is True
    assert captured["text"] is True


def test_pid_belongs_to_coord_windows_marker_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows path: when PowerShell stdout lacks the marker, return False."""
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(
            stdout="C:\\Windows\\System32\\notepad.exe\r\n",
            returncode=0,
        )

    monkeypatch.setattr("coordination.cli_ops.sys.platform", "win32")
    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


def test_pid_belongs_to_coord_windows_powershell_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows path: timeout from PowerShell -> False (do not signal)."""
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=2.0)

    monkeypatch.setattr("coordination.cli_ops.sys.platform", "win32")
    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


def test_pid_belongs_to_coord_windows_powershell_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows path: non-zero exit from PowerShell -> False."""
    from coordination.cli_ops import _pid_belongs_to_coord

    def fake_run(_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeCompleted(stdout="", returncode=1)

    monkeypatch.setattr("coordination.cli_ops.sys.platform", "win32")
    monkeypatch.setattr("coordination.cli_ops.subprocess.run", fake_run)
    assert _pid_belongs_to_coord(12345, "coordination.cli _serve") is False


# ---------------------------------------------------------------------------
# coord start: readiness timeout default + env override
# ---------------------------------------------------------------------------


def test_wait_for_http_ready_default_timeout_is_at_least_15s() -> None:
    """The default timeout must tolerate cold-start on loaded machines.

    Asserted via signature inspection so the test does not have to wait
    15+ real seconds. 15s is the minimum acceptable value.
    """
    import inspect

    from coordination.cli_start import _wait_for_http_ready

    sig = inspect.signature(_wait_for_http_ready)
    default = sig.parameters["timeout_sec"].default
    assert isinstance(default, float)
    assert default >= 15.0


def test_wait_for_http_ready_respects_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COORD_START_READY_TIMEOUT_SEC must override the default and cap waits.

    With the env var set to 0.5s and an httpx mock that always raises
    (service never becomes ready), the helper must time out and raise
    RuntimeError quickly - not block for the 15s default.
    """
    import time as _time

    import httpx as _httpx

    from coordination import cli_start

    monkeypatch.setenv("COORD_START_READY_TIMEOUT_SEC", "0.5")

    class _StubProc:
        def poll(self):  # type: ignore[no-untyped-def]
            return None

    def fake_get(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise _httpx.ConnectError("refused")

    monkeypatch.setattr(cli_start.httpx, "get", fake_get)
    monkeypatch.setattr(cli_start.time, "sleep", lambda _s: None)

    started = _time.monotonic()
    with pytest.raises(RuntimeError):
        cli_start._wait_for_http_ready("http://127.0.0.1:1", _StubProc())  # type: ignore[arg-type]
    elapsed = _time.monotonic() - started
    # Must respect the 0.5s override, not the 15s default.
    assert elapsed < 5.0


def _write_record(home: Path, pid: int, marker: str = "coordination.cli _serve") -> None:
    """Helper: write a new-format JSON PID record for tests."""
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "start_time": "2026-04-17T11:15:00.000Z",
        "marker": marker,
    }
    (home / "coord.pid").write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_legacy_pid_file_is_rejected_politely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare-integer PID file (pre-JSON format) cannot be verified, so
    `coord stop` must refuse to signal, print a clear message, and leave
    the file for the user to remove. Exit code must be 0 (non-fatal)."""
    home = tmp_path / ".coord-home"
    home.mkdir()
    (home / "coord.pid").write_text("12345\n", encoding="utf-8")
    monkeypatch.setenv("COORD_HOME", str(home))

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    monkeypatch.setattr("coordination.cli_ops.os.kill", fake_kill)

    exit_code = cli.main(["stop"])

    assert exit_code == 0
    assert kill_calls == []
    out = capsys.readouterr().out
    assert "Legacy PID file format" in out
    # File must remain so the user sees the warning and can act.
    assert (home / "coord.pid").exists()


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="SIGKILL is POSIX-only; coord stop has no SIGKILL escalation on Windows",
)
def test_stop_refuses_when_marker_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the recorded PID is alive but ps output does not contain the
    marker, the process is NOT ours - some unrelated process reused the
    PID. Refuse to signal; exit non-zero; keep the PID file."""
    home = tmp_path / ".coord-home"
    # Use the test's own PID - guaranteed to be alive.
    my_pid = os.getpid()
    _write_record(home, my_pid)
    monkeypatch.setenv("COORD_HOME", str(home))

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    monkeypatch.setattr("coordination.cli_ops.os.kill", fake_kill)
    # Force the verification step to report "not ours".
    monkeypatch.setattr(
        "coordination.cli_ops._pid_belongs_to_coord",
        lambda _pid, _marker: False,
    )

    exit_code = cli.main(["stop"])

    # No SIGTERM/SIGKILL should have been sent (the liveness probe uses
    # sig 0 which some implementations may run; we specifically assert
    # neither TERM nor KILL was sent).
    terminal_signals = [sig for (_, sig) in kill_calls if sig in (signal.SIGTERM, signal.SIGKILL)]
    assert terminal_signals == []
    assert exit_code != 0
    out = capsys.readouterr().out
    assert "not a coord service" in out.lower()
    assert str(my_pid) in out
    # PID file must remain so subsequent invocations still see the mismatch
    # (the operator must investigate / clean up manually).
    assert (home / "coord.pid").exists()


def test_stop_succeeds_when_marker_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ps output confirms the PID is a coord service, send SIGTERM
    and clean up the PID file after the process exits."""
    home = tmp_path / ".coord-home"
    _write_record(home, 7777)
    monkeypatch.setenv("COORD_HOME", str(home))

    state = {"terminated": False}

    def fake_kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            state["terminated"] = True
            return
        if sig == 0:
            if state["terminated"]:
                raise ProcessLookupError
            return
        raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("coordination.cli_ops.os.kill", fake_kill)
    monkeypatch.setattr("coordination.cli_ops.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "coordination.cli_ops._pid_belongs_to_coord",
        lambda _pid, _marker: True,
    )

    exit_code = cli.main(["stop"])

    assert exit_code == 0
    assert state["terminated"] is True
    assert not (home / "coord.pid").exists()
    assert "Stopped" in capsys.readouterr().out


def test_stop_with_no_pid_file_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    exit_code = cli.main(["stop"])
    assert exit_code == 0
    assert "No background coord service recorded." in capsys.readouterr().out


def test_stop_with_stale_pid_file_is_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".coord-home"
    _write_record(home, 99999)
    monkeypatch.setenv("COORD_HOME", str(home))

    def fake_kill(pid: int, sig: int) -> None:
        # PID is dead at the OS level: every probe / signal fails.
        raise ProcessLookupError

    monkeypatch.setattr("coordination.cli_ops.os.kill", fake_kill)
    # If we ever got as far as calling ps, refuse - but we expect the
    # liveness probe to fail first and short-circuit before this runs.
    monkeypatch.setattr(
        "coordination.cli_ops._pid_belongs_to_coord",
        lambda _pid, _marker: False,
    )

    exit_code = cli.main(["stop"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Stale PID file removed." in out
    assert not (home / "coord.pid").exists()


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="SIGKILL is POSIX-only; coord stop has no SIGKILL escalation on Windows",
)
def test_stop_sends_sigterm_then_sigkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".coord-home"
    _write_record(home, 4242)
    monkeypatch.setenv("COORD_HOME", str(home))

    calls: list[tuple[int, int]] = []

    def fake_sleep(_: float) -> None:
        return None

    # Simulate "still alive" right up until SIGKILL fires by making kill(pid, 0)
    # raise only after SIGKILL.
    killed = {"done": False}

    def kill_with_state(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == signal.SIGKILL:
            killed["done"] = True
            return
        if sig == 0 and killed["done"]:
            raise ProcessLookupError

    monkeypatch.setattr("coordination.cli_ops.os.kill", kill_with_state)
    monkeypatch.setattr("coordination.cli_ops.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "coordination.cli_ops._pid_belongs_to_coord",
        lambda _pid, _marker: True,
    )

    exit_code = cli.main(["stop"])
    assert exit_code == 0
    signals_sent = [sig for (_, sig) in calls if sig in (signal.SIGTERM, signal.SIGKILL)]
    assert signal.SIGTERM in signals_sent
    assert signal.SIGKILL in signals_sent
    assert not (home / "coord.pid").exists()
    assert "Stopped" in capsys.readouterr().out


def test_stop_when_process_exits_after_sigterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".coord-home"
    _write_record(home, 5252)
    monkeypatch.setenv("COORD_HOME", str(home))

    state = {"terminated": False}

    def fake_kill(pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            state["terminated"] = True
            return
        if sig == 0:
            if state["terminated"]:
                raise ProcessLookupError
            return
        raise AssertionError(f"unexpected signal {sig}")

    monkeypatch.setattr("coordination.cli_ops.os.kill", fake_kill)
    monkeypatch.setattr("coordination.cli_ops.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "coordination.cli_ops._pid_belongs_to_coord",
        lambda _pid, _marker: True,
    )

    exit_code = cli.main(["stop"])
    assert exit_code == 0
    assert not (home / "coord.pid").exists()
    assert "Stopped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# coord status
# ---------------------------------------------------------------------------


def _status_handler_factory(
    ready_ok: bool = True,
    meta_ok: bool = True,
    claims_count: int = 3,
    token_warning: str | None = None,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            auth = self.headers.get("Authorization", "")
            if self.path == "/readyz":
                if ready_ok:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        b'{"status":"ready","version":"0.1.0","auth_mode":"bearer","database_path":"x"}'
                    )
                else:
                    self.send_response(500)
                    self.end_headers()
                return
            if self.path == "/meta":
                if meta_ok:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        b'{"name":"multi-agent-coordination","version":"0.1.0","auth_mode":"bearer","repo_root_configured":false}'
                    )
                else:
                    self.send_response(500)
                    self.end_headers()
                return
            if self.path.startswith("/claims"):
                if not auth.startswith("Bearer "):
                    self.send_response(401)
                    self.end_headers()
                    return
                claims = [
                    {"id": f"c{i}", "engineer": "alice", "pattern": "src/**",
                     "expires_at": "2030-01-01T00:00:00Z"}
                    for i in range(claims_count)
                ]
                body = json.dumps({"claims": claims, "count": len(claims)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if token_warning is not None:
                    self.send_header("X-Coord-Token-Warning", token_warning)
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    return Handler


def _init_repo_with_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int) -> Path:
    import httpx as _httpx

    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    # bypass init probe temporarily
    saved = _httpx.get

    def fake_get(*_: object, **__: object):
        return type("R", (), {"status_code": 200})()

    _httpx.get = fake_get  # type: ignore[assignment]
    try:
        cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"])
    finally:
        _httpx.get = saved  # type: ignore[assignment]

    config_path = repo / ".coordination" / "config.toml"
    text = config_path.read_text(encoding="utf-8")
    text = text.replace("http://127.0.0.1:8080", f"http://127.0.0.1:{port}")
    config_path.write_text(text, encoding="utf-8")
    return repo


def test_status_prints_compact_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _MockServer(_status_handler_factory(claims_count=3)) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Service" in out
    assert "ready" in out
    assert "Version: 0.1.0" in out
    assert "Active claims: 3" in out


def test_status_without_repo_or_env_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COORD_API_URL", raising=False)
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    exit_code = cli.main(["status"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "not configured" in out.lower() or "no coordination" in out.lower()


# ---------------------------------------------------------------------------
# coord claims
# ---------------------------------------------------------------------------


def test_claims_empty_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _MockServer(_status_handler_factory(claims_count=0)) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["claims"])
    assert exit_code == 0
    assert "No active claims." in capsys.readouterr().out


def test_claims_default_lists_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _MockServer(_status_handler_factory(claims_count=2)) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["claims"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "c0" in out
    assert "c1" in out
    assert "alice" in out
    assert "src/**" in out


def test_claims_json_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _MockServer(_status_handler_factory(claims_count=1)) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["claims", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["claims"][0]["id"] == "c0"


def test_claims_engineer_filter_is_forwarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    received: list[str] = []
    received_engineers: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            auth = self.headers.get("Authorization", "")
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
                return
            if self.path.startswith("/claims"):
                received.append(self.path)
                received_engineers.append(self.headers.get("X-Coord-Engineer"))
                if not auth.startswith("Bearer "):
                    self.send_response(401)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"claims":[],"count":0}')
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    with _MockServer(Handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["claims", "--engineer", "bob", "--all"])
    assert exit_code == 0
    assert any("engineer=bob" in p for p in received)
    assert any("active_only=false" in p for p in received)
    assert received_engineers == ["bob"]


def _claims_path_recorder() -> "tuple[type[BaseHTTPRequestHandler], list[str]]":
    """A handler that records every /claims request path and returns empty."""
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            auth = self.headers.get("Authorization", "")
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
                return
            if self.path.startswith("/claims"):
                received.append(self.path)
                if not auth.startswith("Bearer "):
                    self.send_response(401)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"claims":[],"count":0}')
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    return Handler, received


def test_claims_scopes_to_local_repo_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # _init_repo_with_service detects the repo id as the directory basename
    # ("app") because the fake repo has no git origin. Issue #30: a repo-local
    # client scopes its claim view to that repo by default.
    handler, received = _claims_path_recorder()
    with _MockServer(handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["claims"])
    assert exit_code == 0
    assert any("repo=app" in p for p in received)


def test_claims_all_repos_flag_sends_explicit_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # v0.42: --all-repos sends an explicit ``all_repos=true`` (rather than
    # merely omitting ``repo``) so a repo-scoped token is rejected with a
    # 403 instead of being silently narrowed to its own repo. The local
    # repo scope must not be applied.
    handler, received = _claims_path_recorder()
    with _MockServer(handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["claims", "--all-repos"])
    assert exit_code == 0
    assert received
    assert any("all_repos=true" in p for p in received)
    assert all("repo=app" not in p for p in received)


def test_claims_repo_flag_overrides_local_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler, received = _claims_path_recorder()
    with _MockServer(handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["claims", "--repo", "otherorg/svc"])
    assert exit_code == 0
    # httpx URL-encodes the slash in the query string.
    assert any("repo=otherorg%2Fsvc" in p for p in received)
    assert all("repo=app" not in p for p in received)


def test_claims_falls_back_to_local_env_repo_id_when_config_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler, received = _claims_path_recorder()
    with _MockServer(handler) as mock:
        repo = _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        config_path = repo / ".coordination" / "config.toml"
        config_text = "\n".join(
            line
            for line in config_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith("repo_id =")
        )
        config_path.write_text(config_text + "\n", encoding="utf-8")
        local_env = repo / ".coordination" / "local.env"
        with local_env.open("a", encoding="utf-8") as fh:
            fh.write("COORD_REPO_ID='local-env-app'\n")
        capsys.readouterr()
        exit_code = cli.main(["claims"])
    assert exit_code == 0
    assert any("repo=local-env-app" in p for p in received)


def test_scope_repo_id_ignores_placeholder_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = cli_ops._ServiceContext(
        service_url="http://svc",
        auth_token="",
        repo_id=None,
        repo_root=None,
        config=None,
    )
    monkeypatch.setenv("COORD_REPO_ID", "example-org/example-repo")

    assert cli_ops._scope_repo_id(ctx) is None


def test_auth_headers_strip_engineer_name() -> None:
    ctx = cli_ops._ServiceContext(
        service_url="http://svc",
        auth_token="tok",
        repo_id=None,
        repo_root=None,
        config=None,
    )

    assert cli_ops._auth_headers(ctx, " alice ")["X-Coord-Engineer"] == "alice"
    assert "X-Coord-Engineer" not in cli_ops._auth_headers(ctx, "   ")


def test_status_shows_repo_scope_and_symbol_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _MockServer(_status_handler_factory(claims_count=1)) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    # Issue #30: the old single "Repo-aware:" line is split into an honest
    # client-side repo scope and a server-side symbol-validation signal.
    assert "Repo scope: app" in out
    assert "Symbol validation:" in out


def test_status_active_claims_count_is_scoped_to_local_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Review follow-up: the "Active claims" count must be scoped to the same
    # repo the "Repo scope" line reports, not a cross-repo total.
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
                return
            if self.path == "/meta":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"version":"0.1.0","repo_root_configured":false}'
                )
                return
            if self.path.startswith("/claims"):
                seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"claims":[],"count":0}')
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    with _MockServer(Handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["status"])
    assert exit_code == 0
    assert any("repo=app" in p for p in seen)


def test_claims_repo_and_all_repos_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review follow-up: --repo and --all-repos contradict each other; argparse
    # should reject the combination (exit 2) rather than silently pick one.
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["claims", "--repo", "otherorg/svc", "--all-repos"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# coord release
# ---------------------------------------------------------------------------


def test_release_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self) -> None:  # noqa: N802
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"released": 1}')

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    with _MockServer(Handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["release", "abc123", "--engineer", "alice"])
    assert exit_code == 0
    assert "Released: abc123" in capsys.readouterr().out


def test_release_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"released": 0}')

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    with _MockServer(Handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["release", "nope", "--engineer", "alice"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert "not released" in out.lower()


def test_release_auth_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
                return
            self.send_response(404)
            self.end_headers()

        def do_DELETE(self) -> None:  # noqa: N802
            self.send_response(401)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    with _MockServer(Handler) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["release", "abc123", "--engineer", "alice"])
    assert exit_code != 0
    out = capsys.readouterr().out.lower()
    assert "401" in out or "auth" in out or "unauth" in out


def test_release_requires_engineer_flag_when_no_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COORD_API_URL", raising=False)
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    exit_code = cli.main(["release", "abc"])
    assert exit_code == 1


def test_status_prints_token_warning_when_unscoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # v0.43: when the server flags an unscoped per-engineer token via the
    # X-Coord-Token-Warning header, `coord status` surfaces it to the human.
    warning = "Your coord token is not bound to a repo. Switch to a scoped token."
    with _MockServer(
        _status_handler_factory(claims_count=1, token_warning=warning)
    ) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Token warning:" in out
    assert warning in out


def test_status_no_token_warning_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _MockServer(_status_handler_factory(claims_count=1)) as mock:
        _init_repo_with_service(tmp_path, monkeypatch, mock.port)
        capsys.readouterr()
        exit_code = cli.main(["status"])
    assert exit_code == 0
    assert "Token warning:" not in capsys.readouterr().out
