from __future__ import annotations

import atexit
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from coordination.overlap_symbols import format_symbol_path
from coordination.symbols import extract_symbols

mcp = FastMCP("coordination")


_LOCAL_ENV_KEYS = (
    "COORD_API_URL",
    "COORD_SERVICE_URL",
    "COORD_AUTH_TOKEN",
    "COORD_BRANCH",
    "COORD_REPO_ID",
    "COORD_REPO_ROOT",
    "COORD_USER",
    "COORD_REQUESTER",
)
_PLACEHOLDER_VALUES = frozenset(
    {
        "set-me",
        "example-org/example-repo",
        "http://127.0.0.1:8080",
    }
)


def _load_local_env(start: Path | None = None) -> Path | None:
    """Bootstrap COORD_* env vars from ``.coordination/local.env``.

    Walks ``start`` (default: cwd) up to filesystem root looking for a
    ``.coordination/local.env`` file. For each ``KEY=VALUE`` line, sets
    ``os.environ[KEY]`` only if the variable is currently unset or holds
    a known placeholder (e.g. ``set-me``). Explicit env (shell exports,
    .mcp.json env blocks with real values) wins; placeholders lose.

    The placeholder override is the load-bearing part: ``.mcp.json``
    ships as a checked-in template with ``COORD_AUTH_TOKEN="set-me"`` so
    OSS users see the shape, and ``coord init`` writes the real token
    to ``.coordination/local.env`` (gitignored). Without the override
    the template's placeholder would win because "set" beats "unset".

    Returns the path of the file actually loaded, or None.
    """
    base = (start or Path.cwd()).resolve()
    for d in [base, *base.parents]:
        env_file = d / ".coordination" / "local.env"
        if not env_file.is_file():
            continue
        try:
            text = env_file.read_text(encoding="utf-8")
        except OSError:
            return None
        from coordination.envfile import parse_env

        for key, val in parse_env(text).items():
            if key not in _LOCAL_ENV_KEYS:
                continue
            existing = os.environ.get(key, "")
            if existing and existing not in _PLACEHOLDER_VALUES:
                continue
            if val:
                os.environ[key] = val
        return env_file
    return None


def _base_url() -> str:
    return os.environ.get("COORD_API_URL", "http://127.0.0.1:8080").rstrip("/")


def _repo_id() -> str:
    """The repo id this client is scoped to, or "" when unset.

    Read from ``COORD_REPO_ID`` (bootstrapped from ``.coordination/local.env``
    by ``_load_local_env``). When set, the read tools scope their queries to
    this repo by default so a hosted shared service fronting multiple repos
    does not surface unrelated repos' claims (issue #30). When unset (the
    single-repo deployment shape) the tools behave exactly as before. Template
    placeholders are treated as unset so checked-in examples do not become a
    real repo scope.
    """
    repo_id = os.environ.get("COORD_REPO_ID", "").strip()
    if repo_id in _PLACEHOLDER_VALUES:
        return ""
    return repo_id


def _headers(engineer: str | None = None) -> dict[str, str]:
    token = os.environ.get("COORD_AUTH_TOKEN", "")
    h: dict[str, str] = {"Accept": "application/json"}
    if token and token not in _PLACEHOLDER_VALUES:
        h["Authorization"] = f"Bearer {token}"
    if engineer:
        stripped = engineer.strip()
        if stripped:
            h["X-Coord-Engineer"] = stripped
    return h


def _git_stdout(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _current_branch_or_worktree() -> str | None:
    explicit = os.environ.get("COORD_BRANCH", "").strip()
    if explicit:
        return explicit

    branch = _git_stdout("symbolic-ref", "--quiet", "--short", "HEAD")
    if branch:
        return branch

    worktree = _git_stdout("rev-parse", "--show-toplevel")
    commit = _git_stdout("rev-parse", "--short", "HEAD")
    if worktree and commit:
        return f"{Path(worktree).name}@{commit}"
    if worktree:
        return Path(worktree).name
    return commit


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
_MARKER_LOCK_TIMEOUT_SECONDS = 1.0
_MARKER_LOCK_POLL_SECONDS = 0.05
_MARKER_LOCK_STALE_SECONDS = 30.0


def _with_token_notice(result: Any, response: httpx.Response) -> Any:
    """Surface the server's ``X-Coord-Token-Warning`` header (v0.43) into a
    tool result so the agent actually sees the unscoped-token deprecation
    nudge -- a response header alone never reaches the model. No-op when the
    header is absent, when the result is not a dict, or when a ``coord_notice``
    is already present."""
    warning = response.headers.get("x-coord-token-warning")
    if warning and isinstance(result, dict) and "coord_notice" not in result:
        result["coord_notice"] = warning
    return result


@mcp.tool()
async def list_claims(
    active_only: bool = True,
    engineer: str | None = None,
    module: str | None = None,
    all_repos: bool = False,
) -> dict[str, Any]:
    """List coordination claims (who is working on which paths).

    Scoped to this client's ``COORD_REPO_ID`` by default so a hosted shared
    service does not surface unrelated repos' claims (issue #30). Pass
    ``all_repos=True`` for the operator view across every repo. With no
    ``COORD_REPO_ID`` configured (single-repo deployment) the scope is a
    no-op and every claim is returned regardless of this flag.
    """
    params: dict[str, Any] = {"active_only": str(active_only).lower()}
    if engineer:
        params["engineer"] = engineer
    if module:
        params["module"] = module
    repo_id = _repo_id()
    if all_repos:
        # Always transmit the explicit opt-out (not just an omitted
        # ``repo``) so a repo-scoped token gets an honest 403 ("you cannot
        # see all repos") rather than being silently narrowed -- even when
        # this client has no local COORD_REPO_ID to scope to.
        params["all_repos"] = "true"
    elif repo_id:
        params["repo"] = repo_id
    # Session_id doubles as an activity ping on the server side: a
    # session that is actively listing claims is alive, so its held
    # claims should not idle-expire.
    params["session_id"] = _SESSION_ID
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{_base_url()}/claims", params=params, headers=_headers(engineer)
        )
        r.raise_for_status()
        return _with_token_notice(r.json(), r)


@mcp.tool()
async def check_conflicts(
    files: list[str], engineer: str, all_repos: bool = False
) -> dict[str, Any]:
    """Check whether planned file paths conflict with other engineers' active claims.

    Scoped to this client's ``COORD_REPO_ID`` by default (issue #30): without
    a repo the server compares only against legacy NULL-repo claims, which
    under-reports conflicts on a repo-tagged deployment. Pass ``all_repos=True``
    to check against every repo's claims. A no-op when ``COORD_REPO_ID`` is
    unset.
    """
    # Annotated with the broader value type httpx.AsyncClient.get accepts so
    # mypy sees an invariant-compatible list when we pass it as `params`.
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("pattern", f) for f in files
    ]
    params.append(("engineer", engineer))
    repo_id = _repo_id()
    if all_repos:
        # Always transmit the explicit opt-out so a repo-scoped token is
        # rejected with a 403 rather than silently narrowed, even when this
        # client has no local COORD_REPO_ID.
        params.append(("all_repos", "true"))
    elif repo_id:
        params.append(("repo", repo_id))
    branch = _current_branch_or_worktree()
    if branch:
        params.append(("branch", branch))
    # Session_id makes the conflict check session-aware (so an agent's
    # own subagents don't false-positive against each other) and acts
    # as an activity ping for idle expiration on the server side.
    params.append(("session_id", _SESSION_ID))
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{_base_url()}/conflicts", params=params, headers=_headers(engineer)
        )
        r.raise_for_status()
        return _with_token_notice(r.json(), r)


def _validate_symbols_locally(
    symbols: dict[str, list[str]],
) -> str | None:
    """Pre-validate symbol claims against the local working tree (v0.17).

    For each ``(file_path, [symbol_name, ...])`` pair, open the file from
    disk (resolved against ``Path.cwd()`` -- the MCP wrapper runs in the
    agent's checkout) and parse it with the same ``extract_symbols``
    dispatcher the server uses. Any claimed symbol that isn't found is
    collected and reported as a single combined error string so the
    caller can short-circuit the POST and surface the typo without a
    network round-trip.

    Files that don't exist on disk are SKIPPED, not flagged: the agent
    may be claiming a path that's about to be created on the same
    branch, and refusing the claim would block a legitimate workflow.
    The server-side validator (the source of truth) makes the final
    call against whatever it can see in the repo root.

    Valid-symbol set construction mirrors the server: every
    ``format_symbol_path(s.parent, s.name)`` plus every distinct parent
    path. The parent expansion is what makes ``"Outer"`` valid even
    when only ``"Outer::Inner::method"`` was emitted by the parser --
    a bare-class claim must always be acceptable when any of its
    methods are visible.

    Set ``COORD_DISABLE_CLIENT_VALIDATION=1`` to bypass this helper
    entirely. Useful when the wrapper runs outside a checkout (server-
    only deployments, CI shells) or filesystem access is unreliable.
    The server-side check still runs in those cases; this knob only
    affects the local fast path.

    Returns ``None`` when every claimed symbol resolves (or every file
    is skipped); returns a human-readable error string otherwise. The
    error string truncates to the first 5 problem files and the first
    20 missing symbols per file so a single typo doesn't produce an
    unreadable wall of text.
    """
    if os.environ.get("COORD_DISABLE_CLIENT_VALIDATION", "").strip() == "1":
        return None
    cwd = Path.cwd()
    missing_by_file: dict[str, list[str]] = {}
    for raw_path, syms in symbols.items():
        if not syms:
            continue
        file_path = cwd / raw_path
        if not file_path.is_file():
            # Path may be about to be created; let the server adjudicate.
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable file (permissions, race with delete, etc.) --
            # skip rather than block the claim on a local I/O glitch.
            continue
        parsed = extract_symbols(raw_path, content)
        if not parsed:
            # Extension unsupported by any backend -- nothing to validate.
            # The server-side check will handle this the same way.
            continue
        valid: set[str] = set()
        for sym in parsed:
            valid.add(format_symbol_path(sym.parent, sym.name))
            if sym.parent:
                # Expand every ancestor path so a bare-class claim
                # ("Outer") matches even when only descendants were
                # emitted ("Outer::Inner::method"). Walks the ``::``
                # chain so each intermediate namespace ("Outer",
                # "Outer::Inner") is also accepted.
                parts = sym.parent.split("::")
                for i in range(1, len(parts) + 1):
                    valid.add("::".join(parts[:i]))
        missing = [s for s in syms if s not in valid]
        if missing:
            missing_by_file[raw_path] = missing
    if not missing_by_file:
        return None
    file_items = list(missing_by_file.items())
    truncated_files = file_items[:5]
    rendered: list[str] = []
    for fpath, syms in truncated_files:
        shown = syms[:20]
        more = len(syms) - len(shown)
        joined = ", ".join(shown)
        if more > 0:
            joined += f" (+{more} more)"
        rendered.append(f"{fpath}: {joined}")
    extra_files = len(file_items) - len(truncated_files)
    suffix = f" (+{extra_files} more files)" if extra_files > 0 else ""
    return (
        "client-side validation: symbols not found in local source: "
        + "; ".join(rendered)
        + suffix
    )


@mcp.tool()
async def claim_files(
    engineer: str,
    patterns: list[str],
    description: str | None = None,
    branch: str | None = None,
    shared_files: list[str] | None = None,
    ttl_hours: int | None = None,
    symbols: dict[str, list[str]] | None = None,
    narrowable: bool | None = None,
    wait_seconds: int | None = None,
    urgency: str | None = None,  # NEW v0.25
) -> dict[str, Any]:
    """Claim files or glob patterns before editing; returns claim_ids or conflicts.

    ``symbols`` (v0.14+) maps file paths to lists of top-level symbol
    names within those files (functions, classes, types, etc.). When a
    file appears as a key, that claim becomes symbol-scoped: only the
    listed symbols are covered, not the whole file. Two symbol claims
    on the same file with disjoint symbol sets auto-coexist server-side
    (no 409). If a path appears in both ``patterns`` and ``symbols`` the
    symbol form wins -- a single symbol-scope claim is sent instead of
    a whole-file claim. Paths in ``symbols`` that are not in ``patterns``
    are still claimed (as symbol-scope rows).

    ``narrowable`` (v0.14+) controls whether the server may auto-narrow
    this claim when a later symbol-scope claim arrives on an overlapping
    file. Defaults are decided server-side (file=True, shared_file=False,
    symbol=False); pass ``False`` here to force the legacy 409+request
    flow instead of auto-narrow on a normal file claim.

    ``wait_seconds`` (v0.21+) opts the request into the FIFO queue. When
    set to a positive int and the request would 409, the service
    enqueues this caller behind the blocking holder and long-polls for
    up to ``wait_seconds`` seconds. If the holder releases within that
    window the service grants the next FIFO entry and returns the new
    claim ids; otherwise the original conflict payload is returned.
    ``wait_seconds=0`` or ``None`` preserves the v0.13-v0.20
    immediate-409 behaviour and the key is omitted from the POST body
    so pre-v0.21 servers see a byte-identical request shape.

    ``urgency`` (v0.25+) is a queue priority hint. One of ``'low'``,
    ``'normal'``, ``'high'``, ``'blocking'`` (matches the v0.9
    release-request urgency vocabulary). When set together with a
    positive ``wait_seconds`` the queue entry the conflict path
    enqueues lands at the requested priority, jumping ahead of
    earlier-but-lower-priority waiters. ``None`` (the default) omits
    the key from the POST body so pre-v0.25 servers see a
    byte-identical request shape and the DB layer falls back to the
    strict-FIFO default of ``'normal'``. Unknown values are silently
    coerced to ``'normal'`` server-side.

    For backward compatibility the wrapper omits the ``symbols`` and
    ``narrowable`` keys from each ``claims[i]`` payload entry when they
    would be ``None`` or empty, so a pre-v0.14 server sees the exact
    same shape it always did.
    """
    # Decide which paths are symbol-scoped. A non-empty list in `symbols`
    # for a given path turns that path into a symbol-scope claim; empty
    # lists are treated as "no symbols specified" (same as omitted), so
    # the legacy whole-file shape goes out.
    symbol_map: dict[str, list[str]] = {
        path: list(syms)
        for path, syms in (symbols or {}).items()
        if syms
    }
    # v0.17: client-side pre-validation. When the caller actually asked
    # for symbol-scope claims, parse the local files first so an obvious
    # typo ("missingFn") fails fast without a network round-trip. The
    # server-side check (added in parallel) is still the source of
    # truth; this is purely a UX fast-path. Skipped under the
    # COORD_DISABLE_CLIENT_VALIDATION=1 escape hatch.
    if symbol_map:
        err = _validate_symbols_locally(symbol_map)
        if err:
            return {
                "claim_ids": [],
                "warnings": [err],
                "options": ["narrow_claim"],
                "client_validated": True,
            }
    claims: list[dict[str, Any]] = []
    seen_symbol_paths: set[str] = set()
    for p in patterns:
        entry: dict[str, Any] = {"type": "file", "pattern": p}
        if p in symbol_map:
            entry["symbols"] = list(symbol_map[p])
            seen_symbol_paths.add(p)
        if narrowable is not None:
            entry["narrowable"] = narrowable
        claims.append(entry)
    # Symbol-scope paths not already represented by an entry in `patterns`
    # still need a claim: emit them as additional symbol-scope claim rows.
    for path, syms in symbol_map.items():
        if path in seen_symbol_paths:
            continue
        entry = {"type": "file", "pattern": path, "symbols": list(syms)}
        if narrowable is not None:
            entry["narrowable"] = narrowable
        claims.append(entry)
    for sf in shared_files or []:
        sf_entry: dict[str, Any] = {"type": "shared_file", "pattern": sf}
        if narrowable is not None:
            sf_entry["narrowable"] = narrowable
        claims.append(sf_entry)
    body: dict[str, Any] = {
        "engineer": engineer,
        "branch": branch or _current_branch_or_worktree(),
        "description": description,
        "claims": claims,
    }
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    repo_id = _repo_id()
    if repo_id:
        body["repo"] = repo_id
    body["session_id"] = _SESSION_ID
    # v0.21: only forward wait_seconds when it asks for FIFO waiting.
    # None and 0 both mean "immediate-409", and omitting the key keeps
    # the POST body byte-identical to the v0.13-v0.20 shape so pre-v0.21
    # servers see no difference.
    if wait_seconds is not None and wait_seconds > 0:
        body["wait_seconds"] = wait_seconds
    # v0.25: only forward urgency when explicitly set. Mirrors the
    # wait_seconds passthrough so pre-v0.25 servers see a byte-identical
    # request shape; the DB layer defaults absent priority to 'normal'.
    if urgency is not None:
        body["urgency"] = urgency
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_base_url()}/claims",
            json=body,
            headers={**_headers(engineer), "Content-Type": "application/json"},
        )
        if r.status_code == 429:
            # v0.30 rate limit. Like 400/409 below this is structured
            # data the agent should reason about, not an exception:
            # ``scope`` says which quota fired (claims / queue /
            # repo_queue) and ``retry_after`` is the server's hint, in
            # seconds, for when a retry might succeed.
            payload = r.json()
            return _with_token_notice(
                {
                    "error": payload.get("detail"),
                    "scope": payload.get("scope"),
                    "retry_after": payload.get("retry_after"),
                },
                r,
            )
        if r.status_code in (400, 409):
            return _with_token_notice(r.json(), r)
        r.raise_for_status()
        return _with_token_notice(r.json(), r)


@mcp.tool()
async def claim_refactor(
    engineer: str,
    file: str,
    symbol: str,
    new_name: str | None = None,
    wait_seconds: int | None = None,
    description: str | None = None,
    branch: str | None = None,
    ttl_hours: int | None = None,
    urgency: str | None = None,
) -> dict[str, Any]:
    """Reserve a symbol's definition plus every callsite before a refactor.

    Use this instead of ``claim_files`` when you are about to rename or
    change the signature of ``symbol`` (claim notation: ``'handler'``,
    ``'Outer::method'``): the server asks its language server for every
    reference and claims them all in one batch -- a symbol claim on the
    tightest enclosing symbol of each callsite, a whole-file claim for
    module-level callsites, and always the definition symbol itself.
    Other agents then see your refactor coming instead of colliding
    with it mid-rename.

    ``new_name`` is informational: it seeds the claim description
    ("refactor: rename X -> Y") so holders of overlapping scope know
    what is changing. The server does not perform the rename.

    ``wait_seconds`` / ``urgency`` behave exactly as on ``claim_files``
    (v0.21 FIFO queue + v0.25 priority): the generated batch goes
    through the normal conflict pipeline, so conflicts 409, queueing
    queues, and rate limits 429.

    Requires the server to run with COORD_LSP_ENABLED=true and a
    healthy language server for the file's language; otherwise the
    server answers 503 and this tool returns a structured
    ``{"error": ..., "status": 503}`` result -- fall back to plain
    ``claim_files`` on the files you know about.
    """
    body: dict[str, Any] = {
        "engineer": engineer,
        "file": file,
        "symbol": symbol,
    }
    if new_name:
        body["new_name"] = new_name
    if description:
        body["description"] = description
    claim_branch = branch or _current_branch_or_worktree()
    if claim_branch:
        body["branch"] = claim_branch
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    # Mirror claim_files: only forward the queue knobs when they ask
    # for queue behaviour, keeping the POST body minimal otherwise.
    if wait_seconds is not None and wait_seconds > 0:
        body["wait_seconds"] = wait_seconds
    if urgency is not None:
        body["urgency"] = urgency
    repo_id = os.environ.get("COORD_REPO_ID", "").strip()
    if repo_id:
        body["repo"] = repo_id
    body["session_id"] = _SESSION_ID
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_base_url()}/claims/refactor",
            json=body,
            headers={**_headers(engineer), "Content-Type": "application/json"},
        )
        if r.status_code == 503:
            # LSP disabled or unavailable server-side. Structured, not
            # an exception: the agent should degrade to claim_files,
            # not crash its tool call.
            payload = r.json()
            return {"error": payload.get("detail"), "status": 503}
        if r.status_code == 429:
            payload = r.json()
            return _with_token_notice(
                {
                    "error": payload.get("detail"),
                    "scope": payload.get("scope"),
                    "retry_after": payload.get("retry_after"),
                },
                r,
            )
        if r.status_code in (400, 409):
            return _with_token_notice(r.json(), r)
        r.raise_for_status()
        return _with_token_notice(r.json(), r)


@mcp.tool()
async def release_claims(claim_ids: list[str], engineer: str | None = None) -> dict[str, Any]:
    """Release claim IDs when work is finished."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_base_url()}/claims/release",
            json={"claim_ids": claim_ids, "engineer": engineer},
            headers={**_headers(engineer), "Content-Type": "application/json"},
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
    coexist_symbols: dict[str, list[str]] | None = None,
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
      the same scope. Pass ``coexist_pattern`` (file scope) or, for
      v0.35 symbol-scoped grants, ``coexist_symbols`` -- a dict mapping
      file path to the list of symbol paths the requester is granted
      (e.g. ``{"src/auth.py": ["Login::handle"]}``). Symbol coexist
      requires both the holder's claim and the requester's original
      claim to be symbol-scoped; the granted symbols must be a subset
      of the requester's claimed symbols and disjoint from the
      holder's, else the server 400s. Both agents end up with active
      claims, mutually self-excluded but still adversarial to anyone
      outside the pair. Cooperative not enforced -- imports and shared
      module-level state are still on the agents to handle.

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
    if coexist_symbols:
        body["coexist_symbols"] = coexist_symbols
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
async def my_requests(
    decision: str = "pending",
    queued: bool | None = None,
) -> dict[str, Any]:
    """List requests this engineer has filed, filtered by decision
    state. Defaults to ``pending`` so the most useful answer ('what
    am I still waiting on?') is the default. Pass ``decision=""`` to
    list every request you've ever filed.

    ``queued`` (v0.22+) flips the view to the live FIFO queue
    (``claim_queue``) rather than the request_events table. When
    ``True`` the response items carry ``kind='queued'`` and include
    the blocking holder's engineer / pattern so you can see who you
    are waiting on without a second call. ``None`` (default) or
    ``False`` preserves the v0.21 request shape byte-identically so
    pre-v0.22 servers see no difference.
    """
    requester = os.environ.get("COORD_REQUESTER", "").strip() or os.environ.get(
        "COORD_USER", ""
    ).strip() or "agent"
    params: dict[str, Any] = {"requester": requester}
    if decision:
        params["decision"] = decision
    if queued is not None and queued:
        params["queued"] = "true"
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


@mcp.tool()
async def cancel_queue_request(
    queue_id: str,
    engineer: str | None = None,
) -> dict[str, Any]:
    """Cancel a queued claim_files request you previously started
    with wait_seconds > 0. The queue_id is the value the wrapper
    returns when it long-polls (look in /requests?queued=true if
    you need to discover it). When engineer is provided the
    cancellation is scoped to that engineer -- prevents an agent
    from accidentally cancelling another agent's wait."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        params: dict[str, Any] = {}
        if engineer:
            params["engineer"] = engineer
        r = await client.delete(
            f"{_base_url()}/requests/{queue_id}",
            params=params,
            headers=_headers(engineer),
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

    Shells out to ``git rev-parse --git-common-dir`` and takes its parent,
    so the MAIN worktree root is found even from a linked git worktree
    (``--show-toplevel`` would point at the linked worktree root, which has
    no ``.coordination/`` of its own). Returns ``None`` if git is
    unavailable, the cwd is not inside a repo, or
    the repo has no ``.coordination/`` subdirectory yet (i.e. ``coord init``
    has not been run). Never raises -- the marker is a best-effort hint, not
    a hard contract.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    common_str = result.stdout.strip()
    if not common_str:
        return None
    # --git-common-dir resolves to the shared .git directory: relative
    # (".git") in the main worktree, absolute ("/path/main/.git") in a
    # linked worktree. Its parent is the MAIN worktree root, where
    # .coordination/ lives. Resolving ".." collapses both forms (and any
    # subdir-relative "../../.git") to that root.
    try:
        repo_root = (Path(common_str) / "..").resolve()
    except OSError:
        return None
    coord_dir = repo_root / ".coordination"
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


def _process_start_time_ns(pid: int) -> int:
    """Return the start time of ``pid`` in nanoseconds since epoch, or 0
    if it can't be determined cheaply on this platform.

    Linux: read field 22 of ``/proc/<pid>/stat`` (clock ticks since boot)
    and combine with the system boot time. macOS: skip (no /proc); we
    fall back to "PID exists is good enough" -- PID reuse over the
    minutes-to-hours timescale of an agent session is rare enough in
    practice. The 0 sentinel means "unknown, don't pin reuse defense
    on this".
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        # Field 2 is comm in parens (may itself contain spaces and parens),
        # so split on the LAST ')' to isolate fields 3..N.
        right = data.rsplit(b")", 1)[-1].split()
        # right[0] is field 3; field 22 is right[19] (0-indexed).
        clock_ticks = int(right[19])
    except (OSError, ValueError, IndexError):
        return 0
    try:
        # Linux only -- raises AttributeError elsewhere.
        ticks_per_sec = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return 0
    if ticks_per_sec <= 0:
        return 0
    try:
        with open("/proc/stat", "rb") as fh:
            for line in fh.read().splitlines():
                if line.startswith(b"btime "):
                    boot_time_s = int(line.split()[1])
                    break
            else:
                return 0
    except (OSError, ValueError):
        return 0
    process_start_s = boot_time_s + (clock_ticks / ticks_per_sec)
    return int(process_start_s * 1_000_000_000)


def _is_live_pid(pid: int, expected_start_time_ns: int = 0) -> bool:
    """Return True iff process ``pid`` exists, optionally also matching
    ``expected_start_time_ns`` to defend against PID reuse.

    ``expected_start_time_ns == 0`` skips the start-time check (the
    platform couldn't supply a value at write time, or the entry pre-
    dates v0.12). ``os.kill(pid, 0)`` is the POSIX-portable existence
    probe: signal 0 isn't sent, only the permission/existence checks
    fire. PermissionError on a foreign-uid process still proves the
    process exists, so it counts as live.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            import ctypes.wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            windll = getattr(ctypes, "WinDLL", None)
            if windll is None:
                return False
            kernel32 = windll("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            exit_code = ctypes.wintypes.DWORD()
            try:
                if not kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if expected_start_time_ns <= 0:
        return True
    actual = _process_start_time_ns(pid)
    if actual <= 0:
        # Couldn't read it now (race with process exit, or platform
        # support disappeared). Treat as live -- the next sweep will
        # catch it if it really is dead.
        return True
    # Allow a 100ms tolerance for floating-point drift from clock_ticks
    # arithmetic.
    return abs(actual - expected_start_time_ns) < 100_000_000


def _format_marker_line(session_id: str, pid: int, start_time_ns: int) -> str:
    """Compose a sessions.live entry. v0.12 format: 3 fields,
    space-separated, terminated by newline at write time. Reader
    splits on first/second whitespace and accepts trailing fields
    for forward-compat."""
    return f"{session_id} {pid} {start_time_ns}"


def _parse_marker_entry(raw: str) -> tuple[str, int, int] | None:
    """Parse a single sessions.live line into (session_id, pid, start_time_ns).

    Pre-v0.12 entries (just a session id with no PID) parse to
    ``(session_id, 0, 0)``. Callers should treat pid<=0 as "stale,
    prune on next pass" -- there's no way to verify liveness without
    a PID, and migrating away from the legacy format means dropping
    those entries on first contact.

    Returns None for blank/comment lines or unparseable garbage.
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split()
    sid = parts[0]
    if not sid:
        return None
    pid_raw = parts[1] if len(parts) > 1 else "0"
    start_raw = parts[2] if len(parts) > 2 else "0"
    try:
        pid = int(pid_raw)
        start_time_ns = int(start_raw)
    except ValueError:
        # Garbage in pid/start fields -> treat the rest as legacy.
        return (sid, 0, 0)
    return (sid, pid, start_time_ns)


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


def _unlink_marker_if_empty(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _acquire_marker_lock(parent: Path) -> Path | None:
    """Best-effort cross-platform lock for sessions.live rewrites.

    The marker file is runtime hygiene, never a hard dependency for MCP
    startup. A tiny mkdir lock lets startup compact stale entries without
    reintroducing the old read/modify/write race. If the lock is contended
    or unavailable, callers skip the rewrite and leave the next startup or
    doctor run to try again.
    """
    lock = parent / ".sessions.live.lock"
    deadline = time.monotonic() + _MARKER_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock.mkdir()
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age >= _MARKER_LOCK_STALE_SECONDS:
                    lock.rmdir()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return None
            time.sleep(_MARKER_LOCK_POLL_SECONDS)
        except OSError:
            return None


def _release_marker_lock(lock: Path | None) -> None:
    if lock is None:
        return
    try:
        lock.rmdir()
    except OSError:
        pass


def _sweep_stale_entries(raw_lines: list[str]) -> list[tuple[str, int, int]]:
    """Filter ``sessions.live`` lines to only those whose PID is live.

    Pre-v0.12 entries (no PID, parsed pid<=0) are stale by construction
    -- the v0.12 format always carries a PID. Live PIDs are kept; dead
    PIDs are dropped. This is the self-healing core of v0.12: every
    coord-mcp startup sweeps its predecessors' graves before adding
    its own headstone.
    """
    out: list[tuple[str, int, int]] = []
    for raw in raw_lines:
        parsed = _parse_marker_entry(raw)
        if parsed is None:
            continue
        sid, pid, start = parsed
        if pid <= 0:
            continue
        if not _is_live_pid(pid, start):
            continue
        out.append((sid, pid, start))
    return out


def _compact_session_marker(
    marker: Path,
    *,
    add_entry: tuple[str, int, int] | None = None,
    drop_session_id: str | None = None,
) -> bool:
    """Rewrite sessions.live with only live entries.

    Returns True when the caller fully handled the marker file. Returns
    False when the file could not be read/written; callers treat marker
    cleanup as best-effort and skip unsafe unlocked writes.
    """
    raw: list[str] = []
    if marker.exists():
        try:
            raw = marker.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return False

    kept = [
        entry
        for entry in _sweep_stale_entries(raw)
        if drop_session_id is None or entry[0] != drop_session_id
    ]
    if add_entry is not None:
        kept.append(add_entry)

    if not kept:
        _unlink_marker_if_empty(marker)
        return True

    new_lines = [_format_marker_line(s, p, st) for s, p, st in kept]
    try:
        _atomic_write_lines(marker, new_lines)
    except Exception:
        return False
    return True


def _register_session_marker() -> None:
    """Publish our session line to sessions.live.

    Registration first tries to compact stale rows under a repo-local lock,
    then writes the live set plus our row atomically. The lock keeps the old
    read-modify-write race closed: simultaneous coord-mcp startups serialize
    their rewrites instead of clobbering each other's session ids.

    If the lock cannot be acquired quickly, registration skips the marker
    rather than appending behind a concurrent compactor. Stale entries from
    SIGKILL/OOM-killed predecessors are still swept by the next locked
    registration/removal, and the pre-push hook skips dead PIDs on every read.

    No-op when ``.coordination/`` doesn't exist (agent running outside
    a coord-managed repo). Any filesystem error is swallowed so MCP
    startup is never blocked by a marker glitch.
    """
    try:
        coord_dir = _repo_root_for_marker()
        if coord_dir is None:
            return
        marker = coord_dir / "sessions.live"
        my_pid = os.getpid()
        my_start = _process_start_time_ns(my_pid)
        lock = _acquire_marker_lock(coord_dir)
        if lock is None:
            return
        try:
            _compact_session_marker(
                marker,
                add_entry=(_SESSION_ID, my_pid, my_start),
            )
        finally:
            _release_marker_lock(lock)
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
        lock = _acquire_marker_lock(coord_dir)
        if lock is None:
            return
        try:
            _compact_session_marker(marker, drop_session_id=_SESSION_ID)
        finally:
            _release_marker_lock(lock)
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
    _load_local_env()
    _register_session_marker()
    _install_marker_handlers()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
