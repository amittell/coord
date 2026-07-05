from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from coordination.cli_shared import COORD_SERVE_MARKER, ensure_parent, find_repo_root, state_paths
from coordination.envfile import read_env_file
from coordination.repo_config import RepoConfig


_PLACEHOLDER_REPO_IDS = {"set-me", "example-org/example-repo"}


# ---------------------------------------------------------------------------
# coord stop
# ---------------------------------------------------------------------------


def _pid_file() -> Path:
    return state_paths()["home"] / "coord.pid"


@dataclass
class _PidRecord:
    pid: int
    start_time: str
    marker: str


def _write_pid_record(path: Path, pid: int, start_time: str, marker: str) -> None:
    """Write the PID file as a JSON document with identity metadata.

    The extra fields (start_time, marker) let `coord stop` verify the PID
    still belongs to a coord service before sending signals, guarding
    against PID reuse by an unrelated process.
    """
    ensure_parent(path)
    payload = {"pid": int(pid), "start_time": start_time, "marker": marker}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _read_pid_record(path: Path) -> tuple[int | None, _PidRecord | None]:
    """Parse the PID file.

    Returns (pid, record):
      - (None, None): file missing, empty, or unparseable.
      - (pid, None):  legacy bare-integer format. Caller must NOT signal.
      - (pid, record): JSON record with marker + start_time.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, None
    if not raw:
        return None, None
    # Prefer JSON format.
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict) and "pid" in data:
        try:
            pid = int(data["pid"])
        except (TypeError, ValueError):
            return None, None
        record = _PidRecord(
            pid=pid,
            start_time=str(data.get("start_time", "")),
            marker=str(data.get("marker", "")),
        )
        return pid, record
    # Legacy bare-integer format.
    try:
        pid = int(raw.splitlines()[0])
    except ValueError:
        return None, None
    return pid, None


def _read_pid(path: Path) -> int | None:
    """Back-compat shim returning only the pid (or None)."""
    pid, _ = _read_pid_record(path)
    return pid


def _pid_belongs_to_coord_posix(pid: int, marker: str) -> bool:
    """POSIX implementation: shell out to `ps -p <pid> -o command=`.

    `ps -o command=` returns the full command line on both macOS (BSD)
    and Linux. If the marker string appears anywhere in the output, the
    PID is considered ours. Any error, timeout, empty output, or non-zero
    exit is treated as "not ours" so we never signal a process we cannot
    identify.
    """
    try:
        completed = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if completed.returncode != 0:
        return False
    output = (completed.stdout or "").strip()
    if not output:
        return False
    return marker in output


def _pid_belongs_to_coord_windows(pid: int, marker: str) -> bool:
    """Windows implementation: query Win32_Process via PowerShell.

    `tasklist` without `/V` only shows the image name; with `/V` it
    truncates the command line. PowerShell's `Get-CimInstance
    Win32_Process` is the modern, supported way to read a process's
    full command line on every supported Windows version. The 2-second
    timeout accounts for PowerShell startup being slower than `ps`.
    Any error, timeout, empty output, or non-zero exit -> False.
    """
    pid_int = int(pid)
    powershell_script = (
        f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid_int}\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", powershell_script],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if completed.returncode != 0:
        return False
    output = (completed.stdout or "").strip()
    if not output:
        return False
    return marker in output


def _pid_belongs_to_coord(pid: int, marker: str) -> bool:
    """Verify the process at `pid` is a coord service. Platform dispatch.

    POSIX (linux, darwin, *bsd): `ps -p <pid> -o command=`.
    Windows (`sys.platform == "win32"`): PowerShell + Win32_Process.
    Any other platform falls back to the POSIX path (best effort).
    """
    if sys.platform == "win32":
        return _pid_belongs_to_coord_windows(pid, marker)
    return _pid_belongs_to_coord_posix(pid, marker)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user; treat as alive.
        return True
    return True


def run_stop(_args) -> int:
    path = _pid_file()
    if not path.exists():
        print("No background coord service recorded.")
        return 0

    pid, record = _read_pid_record(path)
    if pid is None:
        try:
            path.unlink()
        except OSError:
            pass
        print("Stale PID file removed.")
        return 0

    # Legacy bare-integer PID file: we have a PID but no identity metadata,
    # so we cannot safely verify the process is ours. Refuse to signal and
    # leave the file in place for the operator to clean up manually.
    if record is None:
        print(
            f"Legacy PID file format at {path} (pid {pid}); cannot verify "
            "process identity. Remove the file manually if stale."
        )
        return 0

    if not _process_alive(pid):
        try:
            path.unlink()
        except OSError:
            pass
        print("Stale PID file removed.")
        return 0

    # PID is alive - verify it actually belongs to a coord service before
    # sending any signals. Protects against PID reuse by an unrelated
    # process after the original coord service exited.
    marker = record.marker or COORD_SERVE_MARKER
    if not _pid_belongs_to_coord(pid, marker):
        print(
            f"PID {pid} is not a coord service; refusing to signal. "
            f"The PID file at {path} may be stale - verify and remove it "
            "manually if appropriate."
        )
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            path.unlink()
        except OSError:
            pass
        print("Stale PID file removed.")
        return 0

    # Wait up to ~5 seconds for graceful exit.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            try:
                path.unlink()
            except OSError:
                pass
            print(f"Stopped coord service (pid {pid}).")
            return 0
        time.sleep(0.1)

    # Force kill. SIGKILL is POSIX-only; on Windows signal.SIGKILL does not
    # exist and Python's os.kill translates SIGTERM into TerminateProcess,
    # which is already forceful, so the POSIX escalation has no Windows
    # counterpart. Report the timeout and leave the PID file in place so the
    # operator can investigate.
    force_kill_signal = getattr(signal, "SIGKILL", None)
    if force_kill_signal is None:
        print(
            f"Process {pid} did not exit within 5s of SIGTERM. Investigate "
            "manually; SIGKILL is not available on this platform."
        )
        return 1
    try:
        os.kill(pid, force_kill_signal)
    except ProcessLookupError:
        pass
    # Give it a moment.
    for _ in range(10):
        if not _process_alive(pid):
            break
        time.sleep(0.05)
    try:
        path.unlink()
    except OSError:
        pass
    print(f"Stopped coord service (pid {pid}) after SIGKILL.")
    return 0


# ---------------------------------------------------------------------------
# shared resolution helpers for status / claims / release
# ---------------------------------------------------------------------------


@dataclass
class _ServiceContext:
    service_url: str
    auth_token: str
    repo_id: str | None
    repo_root: Path | None
    config: RepoConfig | None


def _load_repo_env(repo_root: Path, config: RepoConfig) -> dict[str, str]:
    env_path = repo_root / config.local_env_file
    return read_env_file(env_path)


def _clean_repo_id(value: str | None) -> str | None:
    if value is None:
        return None
    repo_id = value.strip()
    if repo_id and repo_id not in _PLACEHOLDER_REPO_IDS:
        return repo_id
    return None


def _resolve_service() -> _ServiceContext | None:
    repo_root = find_repo_root()
    if repo_root is not None:
        config_path = repo_root / ".coordination" / "config.toml"
        if config_path.exists():
            config = RepoConfig.load(config_path)
            repo_env = _load_repo_env(repo_root, config)
            token = repo_env.get("COORD_AUTH_TOKEN") or os.environ.get(
                "COORD_AUTH_TOKEN", ""
            )
            repo_id = (
                _clean_repo_id(config.repo_id)
                or _clean_repo_id(repo_env.get("COORD_REPO_ID"))
                or _clean_repo_id(os.environ.get("COORD_REPO_ID"))
            )
            return _ServiceContext(
                service_url=config.service_url.rstrip("/"),
                auth_token=token,
                repo_id=repo_id,
                repo_root=repo_root,
                config=config,
            )

    url = os.environ.get("COORD_API_URL")
    token = os.environ.get("COORD_AUTH_TOKEN", "")
    repo_id = _clean_repo_id(os.environ.get("COORD_REPO_ID"))
    if url:
        return _ServiceContext(
            service_url=url.rstrip("/"),
            auth_token=token,
            repo_id=repo_id,
            repo_root=None,
            config=None,
        )
    return None


def _scope_repo_id(ctx: _ServiceContext) -> str | None:
    """The repo id a repo-local client should scope its views to (issue #30).

    Prefers the ``repo_id`` resolved by ``_resolve_service`` from
    ``.coordination/config.toml`` (the canonical per-repo identity ``coord
    init`` writes), then ``.coordination/local.env``, then an exported
    ``COORD_REPO_ID``. Returns None when none is set -- the single-repo
    deployment shape, where scoping is a no-op and every claim is in view.
    Template placeholder values are treated as unset.
    """
    candidates = (ctx.repo_id, os.environ.get("COORD_REPO_ID"))
    for candidate in candidates:
        repo_id = _clean_repo_id(candidate)
        if repo_id:
            return repo_id
    return None


def _auth_headers(
    ctx: _ServiceContext, engineer: str | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if ctx.auth_token:
        headers["Authorization"] = f"Bearer {ctx.auth_token}"
    if engineer:
        stripped = engineer.strip()
        if stripped:
            headers["X-Coord-Engineer"] = stripped
    return headers


# ---------------------------------------------------------------------------
# coord status
# ---------------------------------------------------------------------------


def run_status(_args) -> int:
    ctx = _resolve_service()
    if ctx is None:
        print(
            "No coordination service configured. Run 'coord init' in a repo, or set "
            "COORD_API_URL and COORD_AUTH_TOKEN."
        )
        return 1

    ready_state = "down"
    try:
        r = httpx.get(f"{ctx.service_url}/readyz", timeout=3.0)
        ready_state = "ready" if r.status_code == 200 else f"status {r.status_code}"
    except httpx.HTTPError as exc:
        ready_state = f"unreachable ({exc})"

    version = "?"
    symbol_validation: bool | None = None
    try:
        m = httpx.get(f"{ctx.service_url}/meta", timeout=3.0)
        if m.status_code == 200:
            data = m.json()
            version = str(data.get("version", "?"))
            symbol_validation = bool(data.get("repo_root_configured", False))
    except httpx.HTTPError:
        pass

    # Scope the count to the same repo the "Repo scope" line reports, so a
    # multi-repo hosted service does not show a cross-repo total under a
    # single-repo scope (issue #30).
    scope = _scope_repo_id(ctx)
    count_params: dict[str, str] = {"active_only": "true"}
    if scope:
        count_params["repo"] = scope
    claim_count: int | str = "?"
    token_warning: str | None = None
    try:
        c = httpx.get(
            f"{ctx.service_url}/claims",
            params=count_params,
            headers=_auth_headers(ctx),
            timeout=3.0,
        )
        # v0.43: the server flags an unscoped per-engineer token here so the
        # human running `coord status` sees the same deprecation nudge the
        # agent gets as a coord_notice.
        token_warning = c.headers.get("x-coord-token-warning")
        if c.status_code == 200:
            data = c.json()
            claim_count = int(data.get("count", len(data.get("claims", []))))
        else:
            claim_count = f"status {c.status_code}"
    except httpx.HTTPError as exc:
        claim_count = f"unreachable ({exc})"

    # Issue #30: the old single "Repo-aware" line conflated two unrelated
    # facts -- whether THIS client scopes its views to a repo, and whether the
    # SERVER has a checkout for symbol validation. A hosted shared service is
    # deliberately repo-scoped WITHOUT a global COORD_REPO_ROOT, so the merged
    # signal misreported it as "not repo-aware". Report the two separately.
    # (``scope`` was resolved above to scope the active-claims count.)
    scope_label = scope if scope else "all repos (no COORD_REPO_ID)"
    if symbol_validation is None:
        symbol_label = "unknown"
    else:
        symbol_label = "enabled" if symbol_validation else "disabled"

    print(f"Service: {ctx.service_url}   {ready_state}")
    print(f"Version: {version}")
    print(f"Repo scope: {scope_label}")
    print(f"Symbol validation: {symbol_label} (server COORD_REPO_ROOT)")
    print(f"Active claims: {claim_count}")
    if token_warning:
        print(f"Token warning: {token_warning}")
    return 0 if ready_state == "ready" else 1


# ---------------------------------------------------------------------------
# coord claims
# ---------------------------------------------------------------------------


def run_claims(args) -> int:
    ctx = _resolve_service()
    if ctx is None:
        print(
            "No coordination service configured. Run 'coord init' in a repo, or set "
            "COORD_API_URL and COORD_AUTH_TOKEN."
        )
        return 1

    params: dict[str, str] = {"active_only": "false" if args.all else "true"}
    if args.engineer:
        params["engineer"] = args.engineer
    # Issue #30: scope to the local repo by default so a hosted shared service
    # does not list unrelated repos' claims. --repo <id> targets a specific
    # repo; --all-repos is the operator view across every repo.
    explicit_repo = getattr(args, "repo", None)
    if explicit_repo:
        params["repo"] = explicit_repo
    elif getattr(args, "all_repos", False):
        # v0.42: send the operator-wide opt-out explicitly rather than just
        # omitting ``repo``. A repo-scoped token then gets an honest 403
        # ("this token cannot access all repos") instead of being silently
        # narrowed to its own repo. Harmless for an unscoped/operator token
        # (the server returns every repo either way).
        params["all_repos"] = "true"
    else:
        scope = _scope_repo_id(ctx)
        if scope:
            params["repo"] = scope

    try:
        response = httpx.get(
            f"{ctx.service_url}/claims",
            params=params,
            headers=_auth_headers(ctx, args.engineer),
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        print(f"Could not reach service: {exc}")
        return 1

    if response.status_code != 200:
        print(f"Service returned {response.status_code}: {response.text}")
        return 1

    data = response.json()
    claims = data.get("claims", [])

    if args.json:
        print(json.dumps({"claims": claims, "count": len(claims)}))
        return 0

    if not claims:
        print("No active claims.")
        return 0

    for claim in claims:
        cid = claim.get("id", "?")
        engineer = claim.get("engineer", "?")
        pattern = claim.get("pattern", "?")
        expires = claim.get("expires_at", "?")
        print(f"{cid}  {engineer}  {pattern}  expires={expires}")
    return 0


# ---------------------------------------------------------------------------
# coord release
# ---------------------------------------------------------------------------


def run_release(args) -> int:
    ctx = _resolve_service()
    if ctx is None:
        print(
            "No coordination service configured. Run 'coord init' in a repo, or set "
            "COORD_API_URL and COORD_AUTH_TOKEN."
        )
        return 1

    engineer = args.engineer
    if not engineer:
        print(
            "Provide --engineer <id> (the claim owner). This cannot be inferred safely."
        )
        return 1

    try:
        response = httpx.request(
            "DELETE",
            f"{ctx.service_url}/claims/{args.claim_id}",
            params={"engineer": engineer},
            headers=_auth_headers(ctx, engineer),
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        print(f"Could not reach service: {exc}")
        return 1

    if response.status_code == 401 or response.status_code == 403:
        print(f"Authentication failed ({response.status_code}).")
        return 1
    if response.status_code != 200:
        print(f"Service returned {response.status_code}: {response.text}")
        return 1

    try:
        data = response.json()
    except ValueError:
        print(f"Unexpected response: {response.text}")
        return 1

    released = int(data.get("released", 0))
    if released < 1:
        print("Claim not released (already gone, wrong engineer, or bad id).")
        return 1

    print(f"Released: {args.claim_id}")
    return 0
