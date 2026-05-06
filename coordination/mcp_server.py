from __future__ import annotations

import atexit
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("coordination")


def _base_url() -> str:
    return os.environ.get("COORD_API_URL", "http://127.0.0.1:8080").rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("COORD_AUTH_TOKEN", "")
    h: dict[str, str] = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _resolve_session_id() -> str:
    """Compute the session id this MCP process should advertise.

    Honours an explicit ``COORD_SESSION_ID`` (useful for pinning the id
    across a restart, or for tests). Otherwise mints a fresh 16-char hex
    id once and reuses it for the lifetime of the process. Subagents
    spawned by Codex / Claude Code share the same MCP child process and
    therefore the same session id, which is the whole point: subagents
    inside one session must never block each other on overlapping
    claims, even when they pick distinct engineer names.
    """
    explicit = os.environ.get("COORD_SESSION_ID", "").strip()
    if explicit:
        return explicit
    return uuid.uuid4().hex[:16]


# Resolved exactly once per coord-mcp process. The conflict check on the
# server side self-excludes any active claim whose session_id matches
# this constant, so subagents spawned by the same parent are
# cooperative.
_SESSION_ID = _resolve_session_id()


@mcp.tool()
async def list_claims(
    active_only: bool = True,
    engineer: str | None = None,
    module: str | None = None,
) -> dict[str, Any]:
    """List coordination claims (who is working on which paths)."""
    params: dict[str, Any] = {"active_only": str(active_only).lower()}
    if engineer:
        params["engineer"] = engineer
    if module:
        params["module"] = module
    # Session_id doubles as an activity ping on the server side: a
    # session that is actively listing claims is alive, so its held
    # claims should not idle-expire.
    params["session_id"] = _SESSION_ID
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{_base_url()}/claims", params=params, headers=_headers())
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def check_conflicts(files: list[str], engineer: str) -> dict[str, Any]:
    """Check whether planned file paths conflict with other engineers' active claims."""
    # Annotated with the broader value type httpx.AsyncClient.get accepts so
    # mypy sees an invariant-compatible list when we pass it as `params`.
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("pattern", f) for f in files
    ]
    params.append(("engineer", engineer))
    # Session_id makes the conflict check session-aware (so an agent's
    # own subagents don't false-positive against each other) and acts
    # as an activity ping for idle expiration on the server side.
    params.append(("session_id", _SESSION_ID))
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{_base_url()}/conflicts", params=params, headers=_headers())
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def claim_files(
    engineer: str,
    patterns: list[str],
    description: str | None = None,
    branch: str | None = None,
    shared_files: list[str] | None = None,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    """Claim files or glob patterns before editing; returns claim_ids or conflicts."""
    claims = [{"type": "file", "pattern": p} for p in patterns]
    for sf in shared_files or []:
        claims.append({"type": "shared_file", "pattern": sf})
    body: dict[str, Any] = {
        "engineer": engineer,
        "branch": branch,
        "description": description,
        "claims": claims,
    }
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    repo_id = os.environ.get("COORD_REPO_ID", "").strip()
    if repo_id:
        body["repo"] = repo_id
    body["session_id"] = _SESSION_ID
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{_base_url()}/claims", json=body, headers={**_headers(), "Content-Type": "application/json"})
        if r.status_code in (400, 409):
            return r.json()
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def release_claims(claim_ids: list[str], engineer: str | None = None) -> dict[str, Any]:
    """Release claim IDs when work is finished."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_base_url()}/claims/release",
            json={"claim_ids": claim_ids, "engineer": engineer},
            headers={**_headers(), "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def pending_requests(session_id: str | None = None) -> dict[str, Any]:
    """List recent attempts by other sessions to claim files I currently hold.

    Each entry tells you who tried to take overlapping scope and what
    pattern they tried, so you can decide whether to release voluntarily.
    Call this between operations or before going idle. Defaults to the
    current process's session id; pass an explicit value only to inspect
    a different session's inbox.
    """
    sid = session_id if session_id is not None else _SESSION_ID
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{_base_url()}/sessions/{sid}/pending_requests",
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def request_release(
    claim_id: str,
    reason: str,
    urgency: str = "normal",
    wait_seconds: int = 60,
    requested_scope: str = "",
) -> dict[str, Any]:
    """File an explicit release request against another agent's claim.

    Use this when ``claim_files`` returned a 409 conflict and you need
    the scope urgently. Filing shortens the holder's claim TTL (so they
    must respond or auto-release within minutes) and surfaces in their
    next ``pending_requests`` poll where they can approve, deny,
    narrow, or coexist (v0.11+).

    ``requested_scope`` is what you actually need, often a sub-pattern
    of the holder's claim. The holder uses it to decide whether
    ``narrowed`` or ``coexist`` is the right response. Recorded in the
    audit trail.

    By default this blocks for up to ``wait_seconds`` (60s) waiting
    for the holder's decision -- one tool call usually returns the
    answer. Pass ``wait_seconds=0`` to fire-and-forget; later use
    ``wait_for_request`` to come back and block, or ``my_requests``
    to poll status.

    The full lifecycle is audit-logged; every state transition (filed,
    notified, responded, expired, resolved) is recorded against the
    request id so disputes about "did the holder ever see this?" or
    "when did they decide?" can be answered after the fact.
    """
    body: dict[str, Any] = {
        "claim_id": claim_id,
        "requester": os.environ.get("COORD_USER") or "agent",
        "session_id": _SESSION_ID,
        "reason": reason,
        "urgency": urgency,
        "wait_seconds": wait_seconds,
    }
    if requested_scope:
        body["requested_scope"] = requested_scope
    # Allow callers in pytest / non-default envs to override the
    # requester via COORD_REQUESTER for tracebility (the env mirrors
    # how engineer is plumbed into claim_files).
    explicit = os.environ.get("COORD_REQUESTER", "").strip()
    if explicit:
        body["requester"] = explicit
    async with httpx.AsyncClient(
        timeout=max(30.0, wait_seconds + 30.0)
    ) as client:
        r = await client.post(
            f"{_base_url()}/requests",
            json=body,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        if r.status_code in (404, 409):
            return r.json()
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def respond_to_request(
    request_id: str,
    decision: str,
    note: str = "",
    narrowed_pattern: str = "",
    coexist_pattern: str = "",
) -> dict[str, Any]:
    """Approve, deny, narrow, or coexist on a release request filed
    against your claim.

    ``decision`` is one of:

    - ``"approved"``: release the claim now (original v0.9 behaviour).
    - ``"denied"``: keep the claim, restore original TTL.
    - ``"narrowed"`` (v0.11+): close the claim and open a tighter one.
      Pass ``narrowed_pattern`` describing what you'll keep claimed.
      The server validates it's a subset of the original pattern and
      400s if not.
    - ``"coexist"`` (v0.11+): grant the requester a sibling claim on
      the same scope. Pass ``coexist_pattern``. Both agents end up
      with active claims, mutually self-excluded but still adversarial
      to anyone outside the pair. Cooperative not enforced -- imports
      and shared module-level state are still on the agents to handle.

    The decision and any pattern fields are audit-logged so the
    requester (and operators) can read the reasoning later.
    """
    body: dict[str, Any] = {
        "decision": decision,
        "session_id": _SESSION_ID,
        "note": note or None,
    }
    if narrowed_pattern:
        body["narrowed_pattern"] = narrowed_pattern
    if coexist_pattern:
        body["coexist_pattern"] = coexist_pattern
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_base_url()}/requests/{request_id}/respond",
            json=body,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def wait_for_request(
    request_id: str, timeout: int = 60
) -> dict[str, Any]:
    """Block until a previously-filed request reaches a terminal
    state (approved / denied / expired / resolved) or ``timeout``
    elapses. Useful when an earlier ``request_release`` was fired
    with ``wait_seconds=0``."""
    # Server-side wait via re-issuing GET with no client-side polling
    # complexity -- the API doesn't expose a wait endpoint, so we poll
    # /requests/{id} ourselves with a sleep loop and bail at deadline.
    import asyncio as _asyncio

    deadline = _asyncio.get_event_loop().time() + max(0, timeout)
    poll_interval = 1.0
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            r = await client.get(
                f"{_base_url()}/requests/{request_id}",
                headers=_headers(),
            )
            if r.status_code == 404:
                return r.json()
            r.raise_for_status()
            row = r.json()
            if row.get("decision") and row["decision"] != "pending":
                return row
            if _asyncio.get_event_loop().time() >= deadline:
                return row
            await _asyncio.sleep(poll_interval)


@mcp.tool()
async def my_requests(decision: str = "pending") -> dict[str, Any]:
    """List requests this engineer has filed, filtered by decision
    state. Defaults to ``pending`` so the most useful answer ('what
    am I still waiting on?') is the default. Pass ``decision=""`` to
    list every request you've ever filed."""
    requester = os.environ.get("COORD_REQUESTER", "").strip() or os.environ.get(
        "COORD_USER", ""
    ).strip() or "agent"
    params: dict[str, Any] = {"requester": requester}
    if decision:
        params["decision"] = decision
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{_base_url()}/requests",
            params=params,
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def release_session(session_id: str | None = None) -> dict[str, Any]:
    """Release every active claim that this MCP session created.

    Defaults to the current process's session id, so calling this with
    no arguments at end of work tears down every claim the parent agent
    and its subagents produced -- one call, no need to track ids or
    walk every engineer name. Pass an explicit session_id to clean up
    a different session (e.g. an orphaned session from a previous
    Codex restart whose id was logged elsewhere)."""
    sid = session_id if session_id is not None else _SESSION_ID
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_base_url()}/sessions/{sid}/release",
            headers=_headers(),
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# sessions.live marker
#
# coord-mcp publishes its own session id into <repo>/.coordination/sessions.live
# at startup. The pre-push hook reads this file and feeds each line back to
# the server as a `session_id=` exclusion when checking for blocking claims,
# so an agent's own subagents (which carry distinct engineer names but share
# this MCP process's session_id) cannot false-positive on its own push.
#
# All filesystem operations are best-effort: a corrupt, read-only, or absent
# .coordination/ directory must never break the MCP startup or shutdown
# path. Agents may run from non-coord repos (no .coordination/ at all), so
# the helpers return silently when the directory is missing rather than
# trying to create it -- mkdir-ing one would surprise users who never opted
# into coord on that repo.
# ---------------------------------------------------------------------------


def _repo_root_for_marker() -> Path | None:
    """Return the enclosing repo's ``.coordination/`` directory if present.

    Shells out to ``git rev-parse --show-toplevel`` to find the repo root.
    Returns ``None`` if git is unavailable, the cwd is not inside a repo, or
    the repo has no ``.coordination/`` subdirectory yet (i.e. ``coord init``
    has not been run). Never raises -- the marker is a best-effort hint, not
    a hard contract.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root_str = result.stdout.strip()
    if not root_str:
        return None
    coord_dir = Path(root_str) / ".coordination"
    try:
        if not coord_dir.is_dir():
            return None
    except OSError:
        return None
    return coord_dir


def _read_marker_lines(path: Path) -> list[str]:
    """Read the live session ids from the marker file.

    Blank lines and ``#`` comments are dropped (the file format reserves
    them for human annotation). Returns an empty list if the file is
    absent or unreadable -- callers treat both as "no other sessions".
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    """Write ``lines`` to ``path`` atomically.

    Strategy: open a tempfile in the same directory, write + fsync, then
    ``os.replace`` over the destination. Same-directory tempfile is
    required for ``replace`` to be atomic on POSIX. This guarantees a
    crash mid-write can never leave a partial line in sessions.live, so
    the pre-push hook never sees a half-written id.
    """
    parent = path.parent
    fd, tmp_str = tempfile.mkstemp(prefix=".sessions.live.", dir=str(parent))
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file; suppress any error here so
        # the original exception (if any) propagates intact to the caller.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _register_session_marker() -> None:
    """Idempotently add ``_SESSION_ID`` to ``sessions.live`` on startup.

    No-op when the ``.coordination/`` directory does not exist (the agent
    is running outside a coord-managed repo). Any filesystem error is
    swallowed so MCP startup is never blocked by a marker glitch.
    """
    try:
        coord_dir = _repo_root_for_marker()
        if coord_dir is None:
            return
        marker = coord_dir / "sessions.live"
        existing = _read_marker_lines(marker)
        if _SESSION_ID in existing:
            return
        existing.append(_SESSION_ID)
        _atomic_write_lines(marker, existing)
    except Exception:
        # Marker is best-effort; never let it break MCP startup.
        pass


def _remove_session_marker() -> None:
    """Idempotently drop ``_SESSION_ID`` from ``sessions.live`` on shutdown.

    If our id was the only line left, the file is unlinked entirely (so a
    fresh run starts from a clean state). Other sessions' lines are
    preserved untouched.
    """
    try:
        coord_dir = _repo_root_for_marker()
        if coord_dir is None:
            return
        marker = coord_dir / "sessions.live"
        if not marker.exists():
            return
        existing = _read_marker_lines(marker)
        if _SESSION_ID not in existing:
            return
        remaining = [sid for sid in existing if sid != _SESSION_ID]
        if not remaining:
            try:
                marker.unlink()
            except OSError:
                pass
            return
        _atomic_write_lines(marker, remaining)
    except Exception:
        # Marker is best-effort; never let it break MCP shutdown.
        pass


def _install_marker_handlers() -> None:
    """Wire atexit handler for graceful marker cleanup.

    Idempotent (atexit.register on the same callable twice is harmless
    because _remove_session_marker is itself idempotent).

    Pre-v0.11 we ALSO installed SIGTERM/SIGINT handlers that re-raised
    the signal under SIG_DFL after cleanup. That fought with FastMCP's
    own signal handling: the MCP library's stdio event loop expects to
    catch SIGINT/SIGTERM cleanly, and re-raising SIG_DFL aborted the
    process before the library could drain its pipes. Symptom for
    operators was a "Transport closed" error on the next tool call --
    the parent (Codex / Claude Code) had a stale stdio handle to a
    child that had already died on a signal it shouldn't have. atexit
    fires for both clean exits AND signal-triggered exits via the
    interpreter's normal shutdown path, so dropping the explicit
    signal handlers loses no cleanup coverage and lets FastMCP own the
    signal disposition.
    """
    atexit.register(_remove_session_marker)


def main() -> None:
    _register_session_marker()
    _install_marker_handlers()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
