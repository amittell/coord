from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
import webbrowser

import httpx

from coordination.cli_ops import _write_pid_record
from coordination.cli_shared import (
    COORD_SERVE_MARKER,
    ensure_parent,
    ensure_token_file,
    state_paths,
)


@dataclass
class StartState:
    api_url: str
    dashboard_url: str
    database_path: str
    token_file: str
    auth_token: str
    pid: int | None = None


# Default readiness wait. Cold-start of the spawned uvicorn process
# (Python import + DB init) routinely exceeds 5s on loaded laptops and CI
# runners, so we give it generous headroom by default. Operators who need
# more (or less) can override via the COORD_START_READY_TIMEOUT_SEC env var.
_DEFAULT_READY_TIMEOUT_SEC = 15.0
_READY_TIMEOUT_ENV = "COORD_START_READY_TIMEOUT_SEC"


def _resolve_ready_timeout(explicit: float) -> float:
    """Return the effective readiness timeout.

    Precedence: COORD_START_READY_TIMEOUT_SEC (if set + parseable + > 0)
    overrides the caller-supplied default. This lets ops bump the wait
    without code changes when running on heavily loaded hardware.
    """
    raw = os.environ.get(_READY_TIMEOUT_ENV)
    if raw is None:
        return explicit
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return explicit
    if value <= 0:
        return explicit
    return value


def _wait_for_http_ready(
    api_url: str,
    process: subprocess.Popen[bytes],
    timeout_sec: float = _DEFAULT_READY_TIMEOUT_SEC,
) -> None:
    effective_timeout = _resolve_ready_timeout(timeout_sec)
    deadline = time.monotonic() + effective_timeout
    last_error = "service did not become ready"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("coord background process exited before becoming ready")
        try:
            response = httpx.get(f"{api_url}/readyz", timeout=0.5)
            if response.status_code == 200:
                return
            last_error = f"unexpected /readyz status: {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(last_error)


def _build_env(host: str, port: int) -> tuple[dict[str, str], StartState]:
    paths = state_paths()
    ensure_parent(paths["database_path"])
    token = ensure_token_file(paths["token_file"])
    env = os.environ.copy()
    env.update(
        {
            "COORD_AUTH_TOKEN": token,
            "COORD_DATABASE_PATH": str(paths["database_path"]),
            "COORD_HOST": host,
            "COORD_PORT": str(port),
        }
    )
    api_url = f"http://{host}:{port}".replace("0.0.0.0", "127.0.0.1")
    state = StartState(
        api_url=api_url,
        dashboard_url=f"{api_url}/dashboard",
        database_path=str(paths["database_path"]),
        token_file=str(paths["token_file"]),
        auth_token=token,
    )
    return env, state


def _print_state(state: StartState, json_output: bool) -> None:
    if json_output:
        print(json.dumps(asdict(state)))
        return
    print(f"API URL:       {state.api_url}")
    print(f"Dashboard URL: {state.dashboard_url}")
    print(f"Database path: {state.database_path}")
    print(f"Token file:    {state.token_file}")
    if state.pid is not None:
        print(f"Background PID: {state.pid}")
    print("")
    print("Auth token (copy this into your shell to run curl/coord commands):")
    print(f"  export COORD_AUTH_TOKEN={state.auth_token}")
    print("")
    print("Next step:     coord init    (in the application repo you want to coordinate)")


def _service_already_running(api_url: str) -> bool:
    try:
        response = httpx.get(f"{api_url}/readyz", timeout=1.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _utc_iso_now() -> str:
    """ISO8601 UTC timestamp with millisecond precision and trailing 'Z'."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write_pid_file(pid: int) -> None:
    """Persist the background PID along with identity metadata.

    The JSON record lets `coord stop` verify the recorded PID still
    belongs to a coord service (by matching `marker` against `ps`
    output) before sending any signals - guarding against PID reuse
    after the original service exited.
    """
    pid_file = state_paths()["home"] / "coord.pid"
    _write_pid_record(pid_file, pid, _utc_iso_now(), COORD_SERVE_MARKER)


def run_start(args) -> int:
    env, state = _build_env(args.host, args.port)
    if args.background:
        if _service_already_running(state.api_url):
            if not args.json:
                print(
                    f"A coordination service is already responding at {state.api_url}. "
                    "Nothing to do."
                )
            _print_state(state, args.json)
            return 0
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordination.cli", "_serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_http_ready(state.api_url, proc)
        except RuntimeError as exc:
            print(str(exc))
            pid_file = state_paths()["home"] / "coord.pid"
            if pid_file.exists():
                try:
                    pid_file.unlink()
                except OSError:
                    pass
            return 1
        state.pid = proc.pid
        _write_pid_file(proc.pid)
        _print_state(state, args.json)
        if args.open_dashboard:
            webbrowser.open(state.dashboard_url)
        return 0

    os.environ.update(env)
    _print_state(state, args.json)
    if args.open_dashboard:
        webbrowser.open(state.dashboard_url)
    from coordination.main import run

    run()
    return 0

