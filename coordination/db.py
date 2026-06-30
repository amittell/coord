from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite


# Transaction seam (HA re-architecture, design Sections 5/7). When a
# claim-grant runs inside :meth:`Database.transaction`, the bound
# connection is published here so every Database method invoked in that
# scope reuses it (via :meth:`Database._acquire`) instead of opening its
# own connection. Task-local: concurrent requests each see their own
# bound connection (or ``None`` when not inside a transaction), so the
# legacy connection-per-op path is byte-identical when unbound. Declared
# at module scope per the contextvars guidance (never created per-call).
_BOUND_CONN: contextvars.ContextVar[aiosqlite.Connection | None] = (
    contextvars.ContextVar("coord_bound_conn", default=None)
)


# Per-engineer grant locks acquired during the current claim-grant
# transaction, released by :meth:`Database.transaction` only after commit (so
# the SQLite in-process lock matches Postgres ``pg_advisory_xact_lock``, which
# is held to end-of-transaction). Task-local: each grant transaction owns its
# own list; ``None`` when not inside a transaction.
_TXN_ENG_LOCKS: contextvars.ContextVar[list[Any] | None] = (
    contextvars.ContextVar("coord_txn_eng_locks", default=None)
)


# Sentinel returned by :func:`acquire_instance_lock` on platforms or
# configurations where flock is unavailable or explicitly bypassed. It
# is not a real file descriptor; callers must treat it as opaque and
# must not pass it to :func:`os.close`.
_LOCK_SKIPPED = -1


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def acquire_instance_lock(db_path: Path | str) -> int:
    """Acquire an exclusive advisory lock on ``<db_path>.lock``.

    Used to detect a second coord service process pointed at the same
    SQLite file. SQLite itself tolerates multiple processes, but this
    service assumes single-writer semantics for its in-process caches
    and background cleanup loop; two live instances on one DB is almost
    always a misconfiguration.

    Returns an open file descriptor on POSIX. The caller is expected to
    keep the fd alive for the lifetime of the process; :func:`fcntl.flock`
    auto-releases when the fd is closed or the process exits, so no
    explicit teardown is required.

    Raises :class:`RuntimeError` with the holding PID embedded in the
    message when the lock is already held.

    Behaviour knobs:

    - ``COORD_DISABLE_INSTANCE_LOCK`` (truthy): return a sentinel without
      attempting the lock. Intended for debugging or NFS-backed shared
      volumes where advisory flock semantics are unreliable.
    - Non-POSIX platforms (``sys.platform == "win32"``): return a
      sentinel without attempting the lock, because :mod:`fcntl` is not
      importable. Production runs inside a Linux container; Windows-host
      dev runs skip the check.
    """
    if _env_truthy("COORD_DISABLE_INSTANCE_LOCK"):
        return _LOCK_SKIPPED
    if sys.platform == "win32":
        return _LOCK_SKIPPED

    import fcntl  # POSIX-only; imported lazily to keep Windows importable.

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    # Open for read+write without truncation so we can still read the
    # holder's PID on failure. O_CREAT ensures the file exists.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another process holds the lock. Try to surface its PID, which
        # the holder wrote when it acquired; fall back to "unknown" if
        # the file is empty or unreadable (e.g. racy read).
        holder = "unknown"
        try:
            with open(lock_path, "r", encoding="utf-8") as fh:
                holder = fh.read().strip() or "unknown"
        except OSError:
            pass
        os.close(fd)
        raise RuntimeError(
            f"Another coord service (pid {holder}) is already using "
            f"database '{path}'. Refusing to start a second instance. "
            "Stop the other process, or set "
            "COORD_DISABLE_INSTANCE_LOCK=true to bypass (not recommended "
            "except for debugging or shared-filesystem deployments)."
        ) from None

    # Lock acquired: record our PID so subsequent failing callers can
    # identify us. Truncate first to avoid stale content from a previous
    # holder that did not clean up.
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("utf-8"))
    except OSError:
        # Writing the PID is best-effort; the lock itself is what
        # matters. Do not abort startup on a write failure.
        pass
    return fd

SCHEMA_VERSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    engineer TEXT NOT NULL,
    branch TEXT,
    description TEXT,
    claim_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'soft',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS conflict_log (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    attempted_by TEXT NOT NULL,
    attempted_pattern TEXT NOT NULL,
    resolution TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES claims(id)
);

CREATE TABLE IF NOT EXISTS ownership_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    yaml_text TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_active ON claims (released_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_claims_engineer ON claims (engineer);
"""


CURRENT_SCHEMA_VERSION = 18


# v0.28 fairness pass: per-blocking-claim counter that
# pop_next_waiting_queue_entry increments on every call. When
# (counter % fairness_interval) == 0 the pop bypasses the priority
# CASE and orders by raw FIFO position so low/normal-priority
# waiters eventually win. Keyed on blocking_claim_id so unrelated
# queues don't interfere with each other's fairness rotations.
# Per-process state: a restart resets the counters, which is fine
# because the fairness guarantee is statistical, not absolute.
_FAIRNESS_COUNTERS: dict[str, int] = {}


def _next_fairness_count(blocking_claim_id: str) -> int:
    """Increment + return the per-blocking-claim fairness counter.
    Internal helper for pop_next_waiting_queue_entry; exposed at module
    level so tests can reset state via _FAIRNESS_COUNTERS.clear()."""

    n = _FAIRNESS_COUNTERS.get(blocking_claim_id, 0) + 1
    _FAIRNESS_COUNTERS[blocking_claim_id] = n
    return n

# Migration registry: list of (version, upgrade_sql) tuples applied in order.
# Entry for version N is the SQL that upgrades a DB from version N-1 to N.
# Version 1 creates the initial core schema; future versions append here.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA),
    # v2: capture which repo a claim came from. Nullable for backfill: existing
    # claims pre-dating this column show up under "(unattributed)" in repo views.
    (2, "ALTER TABLE claims ADD COLUMN repo TEXT;"),
    # v3: per-MCP-process session id so subagents inside one Codex/Claude
    # session don't conflict with each other when they use distinct
    # engineer names. Nullable -- pre-v3 claims keep session_id=NULL and
    # behave like the legacy engineer-only self-exclusion path.
    (3, "ALTER TABLE claims ADD COLUMN session_id TEXT;"),
    # v4: idle-expiration timestamp on claims (bumped on every coord call
    # from the holder's session) plus the requester's session_id on
    # conflict_log entries so the holder can distinguish foreign sessions
    # from its own subagents in the pending-requests inbox. Both nullable
    # for backfill -- legacy rows keep last_activity=NULL and skip idle
    # expiration entirely, falling back to the TTL-only behaviour.
    (
        4,
        "ALTER TABLE claims ADD COLUMN last_activity TEXT;\n"
        "ALTER TABLE conflict_log ADD COLUMN attempted_session_id TEXT;",
    ),
    # v5: first-class release-request tracking. `requests` holds the
    # current state per request (pending / approved / denied / expired /
    # resolved) and `request_events` is an append-only audit log -- one
    # row per state transition with actor, timestamp, and a JSON detail
    # blob. Splitting current state from the immutable event stream
    # keeps the operator timeline queryable without modifying request
    # rows after creation, and gives requesters / holders / operators
    # a single shared source of truth for "what happened to this ask?".
    (
        5,
        """
        CREATE TABLE requests (
            id TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL,
            requester_engineer TEXT NOT NULL,
            requester_session_id TEXT,
            requested_pattern TEXT NOT NULL,
            reason TEXT,
            urgency TEXT NOT NULL DEFAULT 'normal',
            decision TEXT NOT NULL DEFAULT 'pending',
            decided_at TEXT,
            decided_by_engineer TEXT,
            decided_by_session_id TEXT,
            note TEXT,
            original_expires_at TEXT NOT NULL,
            shortened_expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (claim_id) REFERENCES claims(id)
        );
        CREATE INDEX idx_requests_claim ON requests (claim_id);
        CREATE INDEX idx_requests_decision ON requests (decision);
        CREATE INDEX idx_requests_requester ON requests (requester_engineer);

        CREATE TABLE request_events (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_engineer TEXT,
            actor_session_id TEXT,
            detail TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (request_id) REFERENCES requests(id)
        );
        CREATE INDEX idx_request_events_request ON request_events (request_id);
        CREATE INDEX idx_request_events_created ON request_events (created_at);
        """,
    ),
    # v6: two new decision verbs on `respond_to_request` -- `narrowed`
    # and `coexist` -- need somewhere to land their inputs.
    #
    # `requests.requested_scope` records the narrower target the
    # requester actually wants, distinct from the holder's pattern.
    # The operator timeline can then show "holder claimed src/api/**,
    # requester wanted src/api/auth.py" without needing to reconstruct
    # the ask from audit events.
    #
    # `claims.coexists_with` is a JSON array of partner claim ids
    # (TEXT keeps SQLite happy; readers do `json.loads(row["coexists_with"])`).
    # NULL means no partners. When two claims agree to coexist, both
    # ids land in each other's array -- they self-exclude from each
    # other but stay adversarial to anyone outside the pair.
    #
    # Both columns are nullable so existing rows backfill cleanly.
    (
        6,
        "ALTER TABLE requests ADD COLUMN requested_scope TEXT;\n"
        "ALTER TABLE claims ADD COLUMN coexists_with TEXT;",
    ),
    # v7: track whether a claim's TTL was shortened by a `request_release`
    # call. Previously `expire_stale_claims` inferred this by joining the
    # requests table for pending decisions -- which produced false positives
    # (claim expired with a pending request that arrived just before the
    # natural deadline) and false negatives (TTL was shortened but the
    # requester later withdrew, leaving no pending request). Storing the
    # fact on the claim row makes the audit label deterministic. The
    # `denied` decision path resets this to 0 when restoring the original
    # TTL.
    (
        7,
        "ALTER TABLE claims ADD COLUMN ttl_shortened BOOLEAN DEFAULT 0;",
    ),
    # v8: sub-file (symbol-level) claims. `claims.scope_type` distinguishes
    # whole-file claims (legacy default) from symbol-scoped claims; the
    # latter cover only the symbols enumerated in `claim_symbols`.
    # `claims.narrowable` controls whether an incoming symbol claim is
    # allowed to auto-narrow this row's effective scope (default 1; v0.13
    # behaviour preserved because pre-v0.14 clients never submit symbol
    # claims, so no auto-narrow can fire against legacy rows in practice).
    #
    # `claim_symbols` is the symbol set for a `scope_type='symbol'` claim;
    # `(file_path, symbol_name)` is the join key the overlap engine uses
    # to detect symbol intersection across two claims on the same file.
    # symbol_kind is informational only -- 'function' | 'class' |
    # 'interface' | 'type' | 'const' | 'enum' | 'unknown'.
    (
        8,
        "ALTER TABLE claims ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'file';\n"
        "ALTER TABLE claims ADD COLUMN narrowable BOOLEAN NOT NULL DEFAULT 1;\n"
        "CREATE TABLE claim_symbols (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    claim_id TEXT NOT NULL,\n"
        "    file_path TEXT NOT NULL,\n"
        "    symbol_name TEXT NOT NULL,\n"
        "    symbol_kind TEXT NOT NULL,\n"
        "    UNIQUE (claim_id, file_path, symbol_name),\n"
        "    FOREIGN KEY (claim_id) REFERENCES claims(id)\n"
        ");\n"
        "CREATE INDEX idx_claim_symbols_file_symbol ON claim_symbols (file_path, symbol_name);\n"
        "CREATE INDEX idx_claim_symbols_claim ON claim_symbols (claim_id);",
    ),
    # v9: relax `request_events.request_id` to nullable. v0.14 auto-coexist
    # and auto-narrow resolutions are server-side decisions that skip the
    # requests table entirely; the design doc (docs/design/sub-file-claims.md,
    # "State machine deltas") specifies these events land in request_events
    # with `request_id=NULL` so the join becomes a left join. SQLite cannot
    # alter a column's NOT NULL or FK constraint in place, so the migration
    # rebuilds the table: rename old -> copy rows -> drop old -> recreate
    # indexes. All inside the v9 BEGIN IMMEDIATE so a crash mid-migration
    # rolls back cleanly.
    (
        9,
        "ALTER TABLE request_events RENAME TO request_events_v8;\n"
        "CREATE TABLE request_events (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    request_id TEXT,\n"
        "    event_type TEXT NOT NULL,\n"
        "    actor_engineer TEXT,\n"
        "    actor_session_id TEXT,\n"
        "    detail TEXT,\n"
        "    created_at TEXT NOT NULL,\n"
        "    FOREIGN KEY (request_id) REFERENCES requests(id)\n"
        ");\n"
        "INSERT INTO request_events (id, request_id, event_type, "
        "actor_engineer, actor_session_id, detail, created_at) "
        "SELECT id, request_id, event_type, actor_engineer, "
        "actor_session_id, detail, created_at FROM request_events_v8;\n"
        "DROP TABLE request_events_v8;\n"
        "CREATE INDEX idx_request_events_request ON request_events (request_id);\n"
        "CREATE INDEX idx_request_events_created ON request_events (created_at);",
    ),
    # v10: method-level (namespaced) symbol claims. ``claim_symbols``
    # gains ``parent_symbol`` so a row can represent ``Foo::handleA``:
    # parent_symbol='Foo', symbol_name='handleA'. NULL parent means
    # top-level (legacy v8 semantics preserved). Two symbol claims
    # overlap iff one's full path is a prefix of the other -- this
    # gives v0.14 class/method auto-coexist mechanics without bumping
    # the API surface: clients send ``"Foo::handleA"`` as a single
    # string and the service splits it at the ``::`` separator.
    #
    # No table rebuild required: ALTER TABLE ADD COLUMN with a NULL
    # default is in-place. The composite index on
    # (file_path, symbol_name) stays useful for the v0.14 fast path; a
    # new (file_path, parent_symbol) index supports the v0.16
    # namespace overlap query which fetches every symbol row for a
    # file and filters in Python.
    (
        10,
        "ALTER TABLE claim_symbols ADD COLUMN parent_symbol TEXT;\n"
        "CREATE INDEX idx_claim_symbols_file_parent "
        "ON claim_symbols (file_path, parent_symbol);",
    ),
    # v11: FIFO queue for blocked claim_files requests. When the caller
    # passes ``wait_seconds > 0`` and the requested claim overlaps an
    # active holder, the request is enqueued instead of returning 409.
    # The release path (manual release_claims, TTL expiry,
    # release_session, request_release approval, narrowed/coexist
    # decisions) drains the head of the queue: try to grant the next
    # waiting row against the now-released scope and notify any
    # in-process long-poll via an asyncio.Event keyed on the queue id.
    #
    # ``state`` is the canonical lifecycle field: waiting -> granted /
    # expired / cancelled. ``position`` is the per-blocking-claim
    # ordering used for FIFO. ``symbols`` is a JSON-serialised list of
    # ``"Parent::child"`` notation strings so the requester's symbol
    # payload survives the round-trip across the release boundary.
    (
        11,
        "CREATE TABLE claim_queue (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    blocking_claim_id TEXT NOT NULL,\n"
        "    requester_engineer TEXT NOT NULL,\n"
        "    requester_session_id TEXT,\n"
        "    requester_branch TEXT,\n"
        "    requester_description TEXT,\n"
        "    repo TEXT,\n"
        "    claim_type TEXT NOT NULL,\n"
        "    pattern TEXT NOT NULL,\n"
        "    symbols TEXT,\n"
        "    narrowable INTEGER,\n"
        "    ttl_hours INTEGER,\n"
        "    position INTEGER NOT NULL,\n"
        "    state TEXT NOT NULL DEFAULT 'waiting',\n"
        "    granted_claim_id TEXT,\n"
        "    enqueued_at TEXT NOT NULL,\n"
        "    expires_at TEXT NOT NULL,\n"
        "    FOREIGN KEY (blocking_claim_id) REFERENCES claims(id)\n"
        ");\n"
        "CREATE INDEX idx_claim_queue_blocking "
        "ON claim_queue (blocking_claim_id, state, position);\n"
        "CREATE INDEX idx_claim_queue_requester "
        "ON claim_queue (requester_engineer, state);",
    ),
    # v12: queue priority hints. ``claim_queue.priority`` lifts the
    # v0.9 release-request ``urgency`` vocabulary (low|normal|high|
    # blocking) into the v0.21 FIFO queue so a high-urgency requester
    # can jump ahead of normal traffic. Default 'normal' preserves
    # strict FIFO for pre-v0.25 callers (every existing row backfills
    # to normal and orders by position as before).
    #
    # pop_next_waiting_queue_entry orders by a CASE expression that
    # maps the string priority to an integer ordinal -- SQLite has no
    # ENUM type, and storing a TEXT lets us match the v0.9 wire
    # format without an extra translation layer at the API boundary.
    (
        12,
        "ALTER TABLE claim_queue ADD COLUMN priority TEXT NOT NULL "
        "DEFAULT 'normal';\n"
        "CREATE INDEX idx_claim_queue_blocking_priority "
        "ON claim_queue (blocking_claim_id, state, priority, position);",
    ),
    # v13: webhook delivery outbox. The conflict pipeline emits
    # events (auto-coexist, auto-narrow, auto-promote, auto-demote,
    # claim_granted, queue_grant, request_release, queue_cancel)
    # into this table; a background delivery loop POSTs each row
    # to ``COORD_WEBHOOK_URL`` with an ``X-Coord-Signature`` HMAC
    # header and retries with exponential backoff until success or
    # exhaustion. Persisting first means a transient receiver
    # outage never loses events; the loop catches up on next run.
    #
    # ``status`` lifecycle: pending -> delivered | failed | exhausted.
    # ``hmac_signature`` is computed at emit time so the receiver
    # can verify provenance even after the row sits in the outbox
    # through process restarts.
    (
        13,
        "CREATE TABLE webhook_outbox (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    url TEXT NOT NULL,\n"
        "    event_type TEXT NOT NULL,\n"
        "    payload_json TEXT NOT NULL,\n"
        "    hmac_signature TEXT NOT NULL,\n"
        "    status TEXT NOT NULL DEFAULT 'pending',\n"
        "    retry_count INTEGER NOT NULL DEFAULT 0,\n"
        "    last_attempt_at TEXT,\n"
        "    last_error TEXT,\n"
        "    next_attempt_at TEXT NOT NULL,\n"
        "    created_at TEXT NOT NULL,\n"
        "    delivered_at TEXT\n"
        ");\n"
        "CREATE INDEX idx_webhook_outbox_pending "
        "ON webhook_outbox (status, next_attempt_at);\n"
        "CREATE INDEX idx_webhook_outbox_event_type "
        "ON webhook_outbox (event_type, created_at);",
    ),
    # v14: per-engineer bearer tokens. Replaces the single shared
    # ``COORD_AUTH_TOKEN`` for everyday agent work; the shared token
    # stays valid by default for backwards compat and is only
    # rejected when the operator sets
    # ``COORD_REQUIRE_PER_ENGINEER_TOKEN=true``.
    #
    # Tokens are stored as ``sha256(raw_token)`` only -- the raw
    # string is returned exactly once at creation time and never
    # again, matching every modern PAT system. ``revoked_at`` is
    # nullable; a non-NULL value means the token no longer
    # authenticates. ``last_used_at`` is bumped opportunistically on
    # successful auth so operators can spot stale tokens via
    # ``coord tokens list``.
    #
    # The unique index on ``token_sha256`` is what makes the auth
    # path O(1): the request hashes the incoming bearer once and
    # does a single index lookup to resolve it to an engineer.
    (
        14,
        "CREATE TABLE engineer_tokens (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    engineer TEXT NOT NULL,\n"
        "    token_sha256 TEXT NOT NULL UNIQUE,\n"
        "    description TEXT,\n"
        "    created_at TEXT NOT NULL,\n"
        "    revoked_at TEXT,\n"
        "    last_used_at TEXT\n"
        ");\n"
        "CREATE INDEX idx_engineer_tokens_engineer "
        "ON engineer_tokens (engineer);\n"
        "CREATE INDEX idx_engineer_tokens_active "
        "ON engineer_tokens (revoked_at) "
        "WHERE revoked_at IS NULL;",
    ),
    # v15: token lifecycle columns (v0.29.4). ``expires_at`` makes a
    # token self-terminating (NULL = no expiry, matching legacy rows);
    # ``rotated_from`` links a successor token back to the token it
    # replaced so the rotation chain is walkable; a non-NULL
    # ``rotation_grace_until`` on the OLD token marks it as replaced --
    # it keeps authenticating until that instant so cached copies in
    # long-lived tools survive the swap, then goes dark.
    #
    # ``request_count`` / ``last_source_ip`` / ``last_user_agent`` are
    # last-state activity tracking bumped opportunistically on auth
    # (same best-effort contract as ``last_used_at``): enough for the
    # operator questions "is this token dead?" and "is this token
    # being used from somewhere unexpected?" without a per-request
    # history table contending with the auth hot path.
    (
        15,
        "ALTER TABLE engineer_tokens ADD COLUMN expires_at TEXT;\n"
        "ALTER TABLE engineer_tokens ADD COLUMN rotated_from TEXT;\n"
        "ALTER TABLE engineer_tokens ADD COLUMN rotation_grace_until TEXT;\n"
        "ALTER TABLE engineer_tokens ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0;\n"
        "ALTER TABLE engineer_tokens ADD COLUMN last_source_ip TEXT;\n"
        "ALTER TABLE engineer_tokens ADD COLUMN last_user_agent TEXT;\n"
        "CREATE INDEX idx_engineer_tokens_rotated_from "
        "ON engineer_tokens (rotated_from);",
    ),
    # v16: LSP-aware symbol claims (v0.31). One migration carries the
    # whole feature's schema -- wave 1 (spans) populates the new
    # ``claim_symbols`` columns immediately; wave 2 (callsites, rename
    # auto-follow) ships later but gets its tables now so operators
    # upgrade their database exactly once for v0.31.
    #
    # ``claim_symbols`` span columns record WHERE in the file each
    # claimed symbol lived at claim time. Convention: ``start_line`` /
    # ``end_line`` are 1-based (operators read line numbers the way
    # their editor shows them), ``start_col`` / ``end_col`` are 0-based
    # exactly as LSP reports them -- we convert lines once at the LSP
    # boundary and never touch columns, so a span can always be pasted
    # straight back into an LSP Range. All five columns are nullable:
    # NULL means a pre-v16 row, or no extraction ran (no COORD_REPO_ROOT),
    # or the parser could not produce a span for that symbol.
    # ``resolved_by`` says who produced the span: 'parser' for
    # tree-sitter/regex extraction (lines only, columns stay NULL) or
    # 'lsp' for a language-server documentSymbol range (full precision).
    #
    # ``claim_symbol_callsites`` (wave 2): one row per reference the
    # language server found for a claimed symbol, so the conflict
    # engine can warn when an edit lands on a line that calls into
    # someone else's claimed scope. ``character`` is nullable because
    # some servers report line-only locations. Wave 1 only creates it.
    #
    # ``claim_symbol_renames`` (wave 2): audit trail for rename
    # auto-follow -- when the server detects a claimed symbol was
    # renamed, the old/new names, paths, and spans land here with the
    # resolver identity. Wave 1 only creates it.
    (
        16,
        "ALTER TABLE claim_symbols ADD COLUMN start_line INTEGER;\n"
        "ALTER TABLE claim_symbols ADD COLUMN start_col INTEGER;\n"
        "ALTER TABLE claim_symbols ADD COLUMN end_line INTEGER;\n"
        "ALTER TABLE claim_symbols ADD COLUMN end_col INTEGER;\n"
        "ALTER TABLE claim_symbols ADD COLUMN resolved_by TEXT;\n"
        "CREATE TABLE claim_symbol_callsites (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    claim_id TEXT NOT NULL,\n"
        "    file_path TEXT NOT NULL,\n"
        "    line INTEGER NOT NULL,\n"
        "    character INTEGER,\n"
        "    symbol_path TEXT,\n"
        "    created_at TEXT NOT NULL,\n"
        "    FOREIGN KEY (claim_id) REFERENCES claims(id)\n"
        ");\n"
        "CREATE INDEX idx_claim_symbol_callsites_claim_id "
        "ON claim_symbol_callsites (claim_id);\n"
        "CREATE INDEX idx_claim_symbol_callsites_file_line "
        "ON claim_symbol_callsites (file_path, line);\n"
        "CREATE TABLE claim_symbol_renames (\n"
        "    id TEXT PRIMARY KEY,\n"
        "    claim_id TEXT NOT NULL,\n"
        "    file_path TEXT NOT NULL,\n"
        "    old_symbol_name TEXT NOT NULL,\n"
        "    new_symbol_name TEXT NOT NULL,\n"
        "    old_symbol_path TEXT,\n"
        "    new_symbol_path TEXT,\n"
        "    old_start_line INTEGER,\n"
        "    old_end_line INTEGER,\n"
        "    new_start_line INTEGER,\n"
        "    new_end_line INTEGER,\n"
        "    detected_at TEXT NOT NULL,\n"
        "    resolved_by TEXT NOT NULL,\n"
        "    FOREIGN KEY (claim_id) REFERENCES claims(id)\n"
        ");\n"
        "CREATE INDEX idx_claim_symbol_renames_claim_id "
        "ON claim_symbol_renames (claim_id);",
    ),
    # v17: GitHub PR-comment integration (v0.34). The webhook_outbox
    # gains a ``kind`` discriminator so the delivery loop can route a
    # row to the right transport: 'webhook' (the v0.27 default) POSTs
    # the payload to ``COORD_WEBHOOK_URL`` with the HMAC header, while
    # 'github' hands the row's ``detail`` to the GitHub adapter, which
    # finds-or-updates a de-duplicated comment on the open PR for the
    # pushing branch. The DEFAULT keeps every pre-v17 row (and every
    # existing INSERT that omits the column) behaving exactly as a
    # webhook row, so the migration is transparent to legacy callers.
    (
        17,
        "ALTER TABLE webhook_outbox ADD COLUMN "
        "kind TEXT NOT NULL DEFAULT 'webhook';",
    ),
    # v18: symbol-level coexist (v0.35). ``requests.coexist_symbols`` records
    # the symbol-scoped grant a holder made when responding ``coexist`` with
    # ``coexist_symbols`` instead of a file-scope ``coexist_pattern``. The
    # value is a JSON dict mapping ``file_path`` -> list of symbol-path
    # strings (``"Foo::handleA"`` notation) -- exactly the symbols the
    # requester's new sibling claim was granted. NULL keeps every pre-v0.35
    # row (file-scope coexist or any other decision) behaving identically;
    # the column rides only the new symbol-scoped path. No change to the
    # ``claims`` table -- the pairwise ``coexists_with`` edges stay as-is.
    (
        18,
        "ALTER TABLE requests ADD COLUMN coexist_symbols TEXT;",
    ),
]


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ts_elapsed(ts: Any, ref: datetime) -> bool:
    """True when ``ts`` is a non-empty timestamp at or before ``ref``.
    Unparseable values count as elapsed -- fail closed, because a
    corrupt expiry must not turn into an immortal token."""
    if not ts:
        return False
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")) <= ref
    except ValueError:
        return True


# SQLite waits this long (ms) for a contended write lock before raising
# SQLITE_BUSY. Set at the start of every connection opened by this module so
# that concurrent Database.init() calls from multiple processes serialize
# cleanly instead of surfacing BUSY errors to callers.
BUSY_TIMEOUT_MS = 10000


async def _configure_sqlite(conn: aiosqlite.Connection) -> None:
    # busy_timeout must be set before any other pragma so that later
    # statements wait on contended locks rather than surfacing BUSY.
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

    # Switching a fresh database into WAL mode requires an EXCLUSIVE lock
    # and SQLite does NOT honour busy_timeout for that specific state
    # change: if any other connection holds a lock, the PRAGMA raises
    # SQLITE_BUSY immediately. Under concurrent startup from multiple
    # processes this is common and harmless, since only one of them
    # needs to perform the switch. We retry briefly, and if the file is
    # already in WAL mode we treat a BUSY error as success.
    await _set_wal_mode(conn)

    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA foreign_keys=ON")


async def _set_wal_mode(conn: aiosqlite.Connection) -> None:
    """Best-effort switch to WAL journal mode under concurrency.

    Tries the PRAGMA up to a small number of times, sleeping between
    attempts. If the file is already in WAL mode, returns cleanly even
    if the most recent attempt raised BUSY.
    """
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            cur = await conn.execute("PRAGMA journal_mode=WAL")
            row = await cur.fetchone()
            mode = str(row[0]).lower() if row else ""
            if mode == "wal":
                return
            # PRAGMA returned a non-WAL mode without raising; retry.
            last_err = RuntimeError(f"journal_mode remained {mode!r}")
        except sqlite3.OperationalError as exc:
            last_err = exc
            # If another connection already completed the switch we are
            # done even though our attempt failed.
            try:
                cur = await conn.execute("PRAGMA journal_mode")
                row = await cur.fetchone()
                if row is not None and str(row[0]).lower() == "wal":
                    return
            except sqlite3.OperationalError:
                pass
        await asyncio.sleep(0.05 * (attempt + 1))
    if last_err is not None:
        raise last_err


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    )
    row = await cur.fetchone()
    return row is not None


async def _get_schema_version(conn: aiosqlite.Connection) -> int | None:
    if not await _table_exists(conn, "schema_version"):
        return None
    cur = await conn.execute("SELECT version FROM schema_version WHERE id = 1")
    row = await cur.fetchone()
    if row is None:
        return None
    return int(row[0])


async def _stamp_schema_version(conn: aiosqlite.Connection, version: int) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)",
        (version,),
    )


def _split_sql_statements(script: str) -> list[str]:
    """Split a multi-statement SQL script into individual statements.

    Uses sqlite3.complete_statement to respect string literals and comments
    without a naive semicolon split. Empty/whitespace-only fragments are
    dropped. Trailing incomplete fragments are kept so SQLite can raise a
    clear syntax error when the statement is executed.
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                statements.append(stmt)
            buf = ""
    tail = buf.strip()
    if tail:
        statements.append(tail)
    return statements


async def _apply_migration(
    conn: aiosqlite.Connection, version: int, upgrade_sql: str
) -> None:
    """Run a single migration's SQL and stamp its version. Caller owns the
    surrounding transaction; this helper does not BEGIN or COMMIT. It is
    invoked from _migrate_to_current, which wraps all pending migrations
    in a single BEGIN IMMEDIATE transaction so that a failure rolls the
    whole batch back and concurrent callers serialize on the write lock.

    Note: we cannot use ``executescript`` here because it implicitly
    commits the active transaction before running, breaking atomicity.
    Instead we split the migration SQL into statements and execute each
    one individually inside the caller's transaction.
    """
    statements = _split_sql_statements(upgrade_sql)
    for stmt in statements:
        await conn.execute(stmt)
    await _stamp_schema_version(conn, version)


async def _ensure_schema_version_table(conn: aiosqlite.Connection) -> None:
    """Create the schema_version table if it is missing. Safe to call
    concurrently from multiple processes because CREATE TABLE IF NOT
    EXISTS is idempotent at the SQLite level."""
    await conn.executescript(SCHEMA_VERSION_TABLE_SQL)
    await conn.commit()


async def _migrate_to_current(conn: aiosqlite.Connection) -> None:
    """Advance the database to CURRENT_SCHEMA_VERSION under a single
    BEGIN IMMEDIATE transaction.

    The immediate transaction acquires a RESERVED write lock up front, so
    if a second process is already running migrations this call waits on
    that lock (bounded by PRAGMA busy_timeout). Once we hold the lock we
    re-read the schema version: the other process may have finished the
    work we were about to do, in which case there are simply no pending
    migrations and we commit a no-op.

    The whole batch of pending migrations runs inside the one transaction,
    so if any migration fails the version and all table changes roll back
    together. This also means we never leave schema_version ahead of the
    actual tables.
    """
    await conn.execute("BEGIN IMMEDIATE")
    try:
        current = await _get_schema_version(conn)

        if current is None:
            # No version row. Either a brand new DB, or a legacy DB that
            # predates migrations and already has the core tables.
            legacy = await _table_exists(conn, "claims")
            if legacy:
                await _stamp_schema_version(conn, 1)
                await conn.commit()
                return
            current = 0

        if current > CURRENT_SCHEMA_VERSION:
            await conn.rollback()
            raise RuntimeError(
                f"database schema_version={current} is from a newer version "
                f"of coord (expected <= {CURRENT_SCHEMA_VERSION}); refusing "
                f"to start. Upgrade coord or restore an older database."
            )

        pending = sorted(
            (v, sql) for (v, sql) in MIGRATIONS if v > current
        )
        for version, upgrade_sql in pending:
            await _apply_migration(conn, version, upgrade_sql)
    except Exception:
        # rollback() is a no-op if the txn already rolled back above.
        try:
            await conn.rollback()
        except Exception:
            pass
        raise
    await conn.commit()


def _postgres_url_selected() -> str | None:
    """Return the configured Postgres DSN when ``COORD_DATABASE_URL`` selects
    the PG backend, else None. Read from the environment (not Settings) so the
    dispatch works for every ``Database(path)`` call site -- including the many
    tests that construct ``Database`` directly -- without threading settings
    through each one."""
    url = os.environ.get("COORD_DATABASE_URL", "")
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        return url
    return None


class Database:
    def __new__(cls, *args: Any, **kwargs: Any) -> "Database":
        # Backend selection (design Section 4): a ``postgresql://``
        # COORD_DATABASE_URL routes the bare ``Database(path)`` constructor to
        # the asyncpg-backed PostgresStore, so the whole codebase (and the
        # full test suite) runs on Postgres without touching call sites. The
        # SQLite default is untouched. Constructing PostgresStore directly
        # (cls is not Database) bypasses the dispatch and never recurses.
        if cls is Database and _postgres_url_selected() is not None:
            from coordination.pg_backend import PostgresStore

            return object.__new__(PostgresStore)
        return object.__new__(cls)

    def __init__(self, path: Path) -> None:
        self.path = path
        # In-process per-engineer locks for the active-claim cap (design
        # 5.3). The cap is per-engineer and GLOBAL across repos, so the
        # per-repo grant lock does not cover it. On SQLite a single writer
        # process (guaranteed by the flock instance lock) makes an
        # in-process asyncio.Lock the exact equivalent of the per-engineer
        # advisory lock the Postgres backend uses; keyed per engineer so
        # disjoint engineers never serialize against each other. Created
        # lazily in :meth:`engineer_lock`.
        self._engineer_locks: dict[str, asyncio.Lock] = {}

    def _connect(self):
        """Open a fresh per-operation connection.

        The single seam every non-grant Database method funnels through
        (``async with self._connect() as conn``). On SQLite this is
        literally :func:`aiosqlite.connect` -- byte-identical to the
        legacy connection-per-op behaviour. The Postgres backend (P3,
        :mod:`coordination.pg_backend`) overrides it to hand back a pool
        connection wrapped in an aiosqlite-shaped adapter that translates
        the dialect, so the same method body runs unchanged on either
        store.
        """
        return aiosqlite.connect(self.path)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)

            # Create the version table outside the migration transaction.
            # CREATE TABLE IF NOT EXISTS is idempotent so concurrent calls
            # from multiple processes are safe here.
            await _ensure_schema_version_table(conn)

            # Everything version-sensitive (version read, legacy detection,
            # pending migrations) runs under a single BEGIN IMMEDIATE
            # transaction so only one process can drive the upgrade at a
            # time. Others wait for the write lock, then observe that the
            # version is already current and do nothing.
            await _migrate_to_current(conn)

    @asynccontextmanager
    async def transaction(self):
        """Yield ONE connection bound for the duration of the block.

        This is the transaction seam at the heart of the HA
        re-architecture (design Sections 5 and 7). Every Database method
        invoked inside the block reuses the yielded connection (via
        :meth:`_acquire`), so the claims-table overlap re-check, the
        claim insert, the symbol/scope finalization and the
        auto-resolution bookkeeping all commit atomically on exit -- the
        single unit-of-work the design requires before a per-repo lock
        can make the grant correct under concurrent writers.

        Re-entrant: a nested ``transaction()`` (e.g. a queue drain that
        re-enters ``create_claims``) reuses the already-bound connection
        and defers commit to the outermost block, so the grant never
        deadlocks against its own open write transaction.

        On SQLite the connection runs in the default deferred-isolation
        mode: writes accumulate in one implicit transaction and commit
        once here; an exception rolls the whole unit back.
        """
        existing = _BOUND_CONN.get()
        if existing is not None:
            # Re-entrant: reuse the outer connection; the outermost
            # ``transaction`` owns commit/rollback/close.
            yield existing
            return
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            token = _BOUND_CONN.set(conn)
            held_eng_token = _TXN_ENG_LOCKS.set([])
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                # Release any per-engineer grant locks acquired in this
                # transaction AFTER commit/rollback -- matching the Postgres
                # ``pg_advisory_xact_lock``, which only drops at end-of-txn.
                # Releasing them earlier (at the inner ``engineer_lock`` block)
                # let a same-engineer peer read a pre-insert snapshot and
                # overshoot the cap. The connection's writes are durable by the
                # time we release, so the next waiter's count sees them.
                for held in reversed(_TXN_ENG_LOCKS.get() or []):
                    held.release()
                _TXN_ENG_LOCKS.reset(held_eng_token)
                _BOUND_CONN.reset(token)

    async def repo_lock(
        self, conn: aiosqlite.Connection, repo: str | None
    ) -> None:
        """Per-repo serialization hook for the claim-grant transaction.

        No-op on SQLite: the single-writer instance lock plus the bound
        write transaction already serialize grants within the one
        process that may touch the file. The Postgres backend (design
        P3) overrides this to issue a transaction-scoped
        ``pg_advisory_xact_lock`` keyed on ``coalesce(repo, '')`` so
        concurrent replicas serialize per repo -- always via
        ``coalesce`` so the NULL-repo bucket is locked too rather than
        running unserialized (design 5.4).

        Takes the bound ``conn`` explicitly so the lock is acquired on
        the very connection the grant commits on; a lock on any other
        connection would not serialize the right transaction.
        """
        return None

    @asynccontextmanager
    async def engineer_lock(
        self, conn: aiosqlite.Connection, engineer: str
    ):
        """Serialize the per-engineer active-claim cap's count+insert.

        The ``max_claims_per_engineer`` cap is per-engineer and GLOBAL
        across repos, so the per-repo :meth:`repo_lock` does not cover it
        (design 5.3): without a per-engineer guard, three replicas each
        read an under-cap count before any insert lands and the cap
        overshoots ~3x. This wraps the count and the insert so they are
        atomic against any concurrent grant for the SAME engineer.

        On SQLite this is an in-process ``asyncio.Lock`` keyed on the
        engineer: a single event loop plus the flock instance lock mean
        exactly one process touches the database, so an in-process lock
        is byte-for-byte the serialization the advisory lock provides on
        Postgres -- and the cap behaves exactly as it did under the old
        service-level ``_quota_lock``. The Postgres backend (design 5.3,
        P3) overrides this to issue ``pg_advisory_xact_lock(
        hashtextextended('eng:'||engineer))`` on ``conn`` so the lock is
        held until the grant commits and serializes across replicas.

        Takes ``conn`` explicitly -- the bound grant connection -- so the
        Postgres override locks the very transaction the grant commits
        on; SQLite ignores it (the in-process lock needs no connection).

        Hold-to-commit: like Postgres ``pg_advisory_xact_lock``, the lock is
        held until the surrounding :meth:`transaction` commits, not merely
        until this ``with`` block exits. The grant inserts its claim *after*
        the cap check but commits later (scope/symbol finalize, auto-resolution
        bookkeeping all run first), so releasing here would let a same-engineer
        peer acquire the lock and count a pre-insert snapshot -> cap overshoot.
        We register the held lock with the transaction, which releases it
        post-commit. Outside a transaction (no registry) we fall back to
        block-scoped release so the lock never leaks.
        """
        lock = self._engineer_locks.get(engineer)
        if lock is None:
            lock = asyncio.Lock()
            self._engineer_locks[engineer] = lock
        registry = _TXN_ENG_LOCKS.get()
        if registry is None:
            # Not inside a grant transaction: behave like a plain scoped lock.
            async with lock:
                yield
            return
        await lock.acquire()
        registry.append(lock)
        # Released by transaction() after commit; do NOT release on block exit.
        yield

    async def acquire_leader_lease(
        self, *, lease_name: str, holder_id: str, ttl_sec: float
    ) -> bool:
        """Single-leader election for the multi-replica background loops
        (design Section 6).

        The cleanup / auto-demote / rename-sweep / webhook-delivery loops
        run in every replica's lifespan. On Postgres that means three
        replicas would each expire claims, each auto-demote, and -- worst
        -- each POST every due webhook row, breaking at-least-once-once
        delivery with 3x duplicate PR comments. The fix is to let exactly
        one replica (the leader) run those loops.

        On SQLite there is a single writer process (the flock instance
        lock guarantees it), so it is unconditionally the leader: this
        returns True without touching the database and the loops run
        exactly as they always have. The Postgres backend (design Section
        6, P3) overrides this with a TTL lease row -- claim
        ``(lease_name, holder_id, expires_at)`` only when the current
        lease is unheld or expired, returning True when this replica owns
        it -- so leadership survives a leader's death after ``ttl_sec``.

        ``holder_id`` identifies the calling process for the Postgres
        lease; ``ttl_sec`` bounds how long a dead leader blocks failover.
        Both are ignored on SQLite.
        """
        return True

    @asynccontextmanager
    async def _acquire(self):
        """Yield ``(conn, owns)`` for a single Database operation.

        Inside a :meth:`transaction` block, yields the task-bound
        connection with ``owns=False`` -- the operation must NOT commit
        or close it, because the surrounding unit-of-work owns the
        commit. Outside a transaction, opens a fresh connection with
        ``owns=True``, reproducing the legacy connection-per-op
        behaviour exactly (configure, run, the caller commits when
        ``owns`` is true, the context closes it). The bound connection
        already has ``row_factory`` set and the pragmas configured by
        :meth:`transaction`, so both branches yield a ready connection.
        """
        bound = _BOUND_CONN.get()
        if bound is not None:
            yield bound, False
        else:
            await self.init()
            async with self._connect() as conn:
                conn.row_factory = aiosqlite.Row
                await _configure_sqlite(conn)
                yield conn, True

    async def list_active_claims_rows(self, exclude_engineer: str | None = None) -> list[dict[str, Any]]:
        q = """
        SELECT * FROM claims
        WHERE released_at IS NULL
        """
        args: list[Any] = []
        if exclude_engineer:
            q += " AND engineer != ?"
            args.append(exclude_engineer)
        async with self._acquire() as (conn, owns):
            cur = await conn.execute(q, args)
            rows = await cur.fetchall()
            if owns:
                await conn.commit()
        now = datetime.now(UTC)
        out: list[dict[str, Any]] = []
        for r in rows:
            exp_raw = str(r["expires_at"])
            exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
            if exp <= now:
                continue
            out.append(dict(r))
        return out

    async def insert_claims_batch(
        self,
        *,
        engineer: str,
        branch: str | None,
        description: str | None,
        items: list[tuple[str, str, str, str, str]],  # id, claim_type, pattern, severity, expires_at
        repo: str | None = None,
        session_id: str | None = None,
        last_activity: str | None = None,
    ) -> list[str]:
        """Insert a batch of claims atomically.

        ``last_activity`` is stamped only when the caller supplies a
        ``session_id``: idle expiration is opt-in via session-tagging.
        Legacy NULL-session inserts get ``last_activity = NULL`` and so
        keep the pre-v0.6 TTL-only behaviour. Tests and the recovery
        path can pass ``last_activity`` explicitly; the service layer
        leaves it as ``None`` and we default to "now" so the standard
        flow doesn't have to think about it.
        """
        now = _utcnow()
        # Stamp activity only when this is a session-tagged claim; idle
        # expiration is opt-in.
        activity_value: str | None = None
        if session_id:
            activity_value = last_activity if last_activity is not None else now
        async with self._acquire() as (conn, owns):
            for cid, claim_type, pattern, severity, expires_at in items:
                await conn.execute(
                    """
                    INSERT INTO claims (
                        id, engineer, branch, description, claim_type, pattern, severity,
                        created_at, expires_at, released_at, repo, session_id, last_activity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        cid,
                        engineer,
                        branch,
                        description,
                        claim_type,
                        pattern,
                        severity,
                        now,
                        expires_at,
                        repo,
                        session_id,
                        activity_value,
                    ),
                )
            if owns:
                await conn.commit()
        return [i[0] for i in items]

    async def insert_claim_symbols(
        self,
        *,
        rows: list[tuple[Any, ...]],
    ) -> None:
        """Insert symbol rows for ``scope_type='symbol'`` claims.

        Each row is ``(id, claim_id, file_path, symbol_name, symbol_kind,
        parent_symbol)`` optionally extended with the v16 span columns
        ``(start_line, start_col, end_line, end_col, resolved_by)``.
        Six-element rows are padded with NULL spans so pre-v0.31 callers
        and tests keep working unchanged; eleven-element rows persist
        the spans verbatim. Span convention (see the v16 migration):
        lines are 1-based, columns 0-based, ``resolved_by`` is
        ``'parser'`` or ``'lsp'``, and NULL spans mean nobody produced
        a span for this symbol.

        ``parent_symbol`` is ``None`` for top-level
        (v0.14 legacy semantics) and a class / receiver name for
        method-level claims (v0.16). The caller (service layer) parses
        the ``"Parent::child"`` API notation into the column pair before
        calling this helper.

        Idempotent against the ``UNIQUE (claim_id, file_path,
        symbol_name)`` index: a duplicate insert is silently ignored so
        retries don't 500. (Note: the UNIQUE index does NOT include
        parent_symbol, so two rows on the same claim that share
        symbol_name but differ on parent_symbol would collide; v0.16
        treats this as a non-event because the API doesn't generate
        such collisions -- "Foo::handleA" and "Bar::handleA" go into
        different claims, not the same one.)
        """
        if not rows:
            return
        padded = [tuple(r) + (None,) * (11 - len(r)) for r in rows]
        async with self._acquire() as (conn, owns):
            await conn.executemany(
                """
                INSERT OR IGNORE INTO claim_symbols
                    (id, claim_id, file_path, symbol_name, symbol_kind,
                     parent_symbol, start_line, start_col, end_line,
                     end_col, resolved_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                padded,
            )
            if owns:
                await conn.commit()

    async def get_claim_symbols(
        self, claim_id: str
    ) -> list[dict[str, Any]]:
        """Return all symbol rows for a claim. Empty list for file-scope
        claims or unknown claim_id. ``parent_symbol`` is ``None`` for
        top-level rows (v0.14) and a class / receiver name for
        method-level rows (v0.16). The v16 span columns ride along:
        1-based lines, 0-based columns, NULL when no span was resolved
        at claim time (pre-v16 rows, no repo root, parser miss)."""
        async with self._acquire() as (conn, _owns):
            cur = await conn.execute(
                """
                SELECT file_path, symbol_name, symbol_kind, parent_symbol,
                       start_line, start_col, end_line, end_col, resolved_by
                FROM claim_symbols
                WHERE claim_id = ?
                ORDER BY file_path, parent_symbol, symbol_name
                """,
                (claim_id,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def insert_claim_callsites(
        self,
        claim_id: str,
        callsites: list[tuple[str, int, int | None, str | None]],
        *,
        now: str | None = None,
    ) -> int:
        """v0.31 wave 2: persist the language server's reference list
        for a claim. Each tuple is ``(file_path, line, character,
        symbol_path)`` -- ``line`` 1-based, ``character`` 0-based or
        ``None`` for servers that report line-only locations,
        ``symbol_path`` the claimed symbol the reference points at.

        Enrichment replaces wholesale: there is no unique index on the
        table that INSERT OR IGNORE could lean on, so the claim's
        existing rows are DELETEd and the new set inserted inside one
        BEGIN IMMEDIATE transaction. A re-enrichment therefore never
        accretes duplicates and a crash mid-call never leaves a mixed
        old/new set behind. Returns the number of rows inserted.
        """
        await self.init()
        created_at = now or _utcnow()
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                "DELETE FROM claim_symbol_callsites WHERE claim_id = ?",
                (claim_id,),
            )
            if callsites:
                await conn.executemany(
                    """
                    INSERT INTO claim_symbol_callsites
                        (id, claim_id, file_path, line, character,
                         symbol_path, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(uuid4()),
                            claim_id,
                            file_path,
                            line,
                            character,
                            symbol_path,
                            created_at,
                        )
                        for file_path, line, character, symbol_path in callsites
                    ],
                )
            await conn.commit()
        return len(callsites)

    async def list_callsites_for_claims(
        self, claim_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Return every recorded callsite row for the given claims,
        ordered by (claim_id, file_path, line) so callers can render a
        stable list. Empty input short-circuits to an empty list."""
        if not claim_ids:
            return []
        await self.init()
        placeholders = ",".join("?" for _ in claim_ids)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM claim_symbol_callsites "
                f"WHERE claim_id IN ({placeholders}) "
                "ORDER BY claim_id, file_path, line",
                claim_ids,
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def callsites_intersecting(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
    ) -> list[dict[str, Any]]:
        """v0.31 wave 2: callsite rows landing inside
        ``file_path[start_line..end_line]`` (inclusive, 1-based) joined
        to their owning ACTIVE claims. The advisory CALLSITE_OVERLAP
        pass uses this to ask "whose claimed symbol is called from the
        range I am about to edit?".

        Active means ``released_at IS NULL`` plus a Python-side TTL
        check on ``expires_at`` -- the same pattern as
        :meth:`list_active_claims_rows`, so string-vs-datetime
        comparison quirks live in exactly one idiom.
        """
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                SELECT cs.claim_id, cs.file_path, cs.line, cs.character,
                       cs.symbol_path,
                       c.engineer, c.pattern, c.session_id, c.repo,
                       c.expires_at
                FROM claim_symbol_callsites cs
                JOIN claims c ON c.id = cs.claim_id
                WHERE cs.file_path = ?
                  AND cs.line >= ?
                  AND cs.line <= ?
                  AND c.released_at IS NULL
                """,
                (file_path, start_line, end_line),
            )
            rows = await cur.fetchall()
        now = datetime.now(UTC)
        out: list[dict[str, Any]] = []
        for r in rows:
            exp_raw = str(r["expires_at"])
            try:
                exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if exp <= now:
                continue
            out.append(dict(r))
        return out

    async def insert_claim_symbol_rename(
        self,
        *,
        claim_id: str,
        file_path: str,
        old_symbol_name: str,
        new_symbol_name: str,
        old_symbol_path: str | None = None,
        new_symbol_path: str | None = None,
        old_start_line: int | None = None,
        old_end_line: int | None = None,
        new_start_line: int | None = None,
        new_end_line: int | None = None,
        resolved_by: str,
        detected_at: str | None = None,
    ) -> str:
        """v0.31 wave 2: append one rename audit row (v16
        ``claim_symbol_renames`` columns). Standalone variant for
        callers that detected a rename without needing the atomic
        claim-row update -- the auto-follow sweep goes through
        :meth:`update_claim_symbol_rename` instead, which writes the
        same row inside its transaction. Returns the new row id."""
        await self.init()
        new_id = str(uuid4())
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                """
                INSERT INTO claim_symbol_renames
                    (id, claim_id, file_path, old_symbol_name,
                     new_symbol_name, old_symbol_path, new_symbol_path,
                     old_start_line, old_end_line, new_start_line,
                     new_end_line, detected_at, resolved_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    claim_id,
                    file_path,
                    old_symbol_name,
                    new_symbol_name,
                    old_symbol_path,
                    new_symbol_path,
                    old_start_line,
                    old_end_line,
                    new_start_line,
                    new_end_line,
                    detected_at or _utcnow(),
                    resolved_by,
                ),
            )
            await conn.commit()
        return new_id

    async def list_symbol_renames_for_claims(
        self, claim_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Return every rename audit row for the given claims, newest
        first within each claim so the dashboard can show the latest
        rename without sorting client-side."""
        if not claim_ids:
            return []
        await self.init()
        placeholders = ",".join("?" for _ in claim_ids)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM claim_symbol_renames "
                f"WHERE claim_id IN ({placeholders}) "
                "ORDER BY claim_id, detected_at DESC, id",
                claim_ids,
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def update_claim_symbol_rename(
        self,
        claim_id: str,
        *,
        file_path: str,
        old_symbol_name: str,
        new_symbol_name: str,
        new_start_line: int | None,
        new_start_col: int | None,
        new_end_line: int | None,
        new_end_col: int | None,
        resolved_by: str,
        new_pattern: str | None,
    ) -> bool:
        """v0.31 wave 2: apply a detected rename atomically.

        In ONE BEGIN IMMEDIATE transaction:

        1. read the matching ``claim_symbols`` row (old span + parent,
           needed for the audit trail);
        2. update its ``symbol_name``, span columns, and
           ``resolved_by``;
        3. update ``claims.pattern`` when ``new_pattern`` is not None
           (today the pattern is the file path and never embeds the
           symbol, so the sweep always passes None -- the column update
           exists for any future pattern scheme that does embed it);
        4. insert the ``claim_symbol_renames`` audit row.

        Partial application is impossible: any failure rolls the whole
        transaction back. Returns False (with nothing written) when no
        ``claim_symbols`` row matches ``(claim_id, file_path,
        old_symbol_name)`` -- e.g. a concurrent release or a second
        sweep racing this one.
        """
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    """
                    SELECT id, parent_symbol, start_line, end_line
                    FROM claim_symbols
                    WHERE claim_id = ? AND file_path = ?
                      AND symbol_name = ?
                    """,
                    (claim_id, file_path, old_symbol_name),
                )
                row = await cur.fetchone()
                if row is None:
                    await conn.rollback()
                    return False
                parent = row["parent_symbol"]
                old_path = (
                    f"{parent}::{old_symbol_name}"
                    if parent
                    else old_symbol_name
                )
                new_path = (
                    f"{parent}::{new_symbol_name}"
                    if parent
                    else new_symbol_name
                )
                await conn.execute(
                    """
                    UPDATE claim_symbols
                    SET symbol_name = ?, start_line = ?, start_col = ?,
                        end_line = ?, end_col = ?, resolved_by = ?
                    WHERE id = ?
                    """,
                    (
                        new_symbol_name,
                        new_start_line,
                        new_start_col,
                        new_end_line,
                        new_end_col,
                        resolved_by,
                        row["id"],
                    ),
                )
                if new_pattern is not None:
                    await conn.execute(
                        "UPDATE claims SET pattern = ? WHERE id = ?",
                        (new_pattern, claim_id),
                    )
                await conn.execute(
                    """
                    INSERT INTO claim_symbol_renames
                        (id, claim_id, file_path, old_symbol_name,
                         new_symbol_name, old_symbol_path,
                         new_symbol_path, old_start_line, old_end_line,
                         new_start_line, new_end_line, detected_at,
                         resolved_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        claim_id,
                        file_path,
                        old_symbol_name,
                        new_symbol_name,
                        old_path,
                        new_path,
                        row["start_line"],
                        row["end_line"],
                        new_start_line,
                        new_end_line,
                        _utcnow(),
                        resolved_by,
                    ),
                )
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise
            await conn.commit()
        return True

    async def get_symbol_rows_on_file(
        self,
        *,
        file_path: str,
        exclude_engineer: str | None = None,
        exclude_session_ids: list[str] | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every active symbol-scope row on ``file_path`` joined
        with its owning claim. v0.16 namespace overlap is computed in
        Python on top of this set: two-level prefix matching is cheap
        enough that we don't push it into SQL.

        Excludes the caller's own engineer / session_ids the same way
        the file-overlap check does.
        """
        await self.init()
        sql = (
            "SELECT c.*, cs.symbol_name AS overlapping_symbol, "
            "cs.symbol_kind AS overlapping_symbol_kind, "
            "cs.parent_symbol AS overlapping_parent_symbol, "
            "cs.start_line AS overlapping_symbol_start_line, "
            "cs.start_col AS overlapping_symbol_start_col, "
            "cs.end_line AS overlapping_symbol_end_line, "
            "cs.end_col AS overlapping_symbol_end_col, "
            "cs.resolved_by AS overlapping_symbol_resolved_by "
            "FROM claim_symbols cs JOIN claims c ON c.id = cs.claim_id "
            "WHERE cs.file_path = ? "
            "AND c.released_at IS NULL "
            "AND c.scope_type = 'symbol'"
        )
        params: list[Any] = [file_path]
        if exclude_engineer:
            sql += " AND c.engineer != ?"
            params.append(exclude_engineer)
        if exclude_session_ids:
            ph = ",".join("?" for _ in exclude_session_ids)
            sql += f" AND (c.session_id IS NULL OR c.session_id NOT IN ({ph}))"
            params.extend(exclude_session_ids)
        if repo is not None:
            sql += " AND c.repo IS ?"
            params.append(repo)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def find_symbol_overlaps(
        self,
        *,
        file_path: str,
        symbol_names: list[str],
        exclude_engineer: str | None = None,
        exclude_session_ids: list[str] | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return active symbol-scope claim rows whose symbol set intersects
        the supplied ``symbol_names`` on the supplied ``file_path``.

        Excludes the caller's own engineer / session_ids the same way the
        path-overlap check does. Used by the conflict engine when both
        holder and requester are symbol-scoped; an empty intersection
        means the conflict engine returns AUTO_COEXIST instead of 409.
        """
        if not symbol_names:
            return []
        await self.init()
        placeholders = ",".join("?" for _ in symbol_names)
        params: list[Any] = [file_path, *symbol_names]
        sql = (
            "SELECT c.*, cs.symbol_name AS overlapping_symbol, "
            "cs.symbol_kind AS overlapping_symbol_kind "
            "FROM claim_symbols cs JOIN claims c ON c.id = cs.claim_id "
            "WHERE cs.file_path = ? AND cs.symbol_name IN (" + placeholders + ") "
            "AND c.released_at IS NULL "
            "AND c.scope_type = 'symbol'"
        )
        if exclude_engineer:
            sql += " AND c.engineer != ?"
            params.append(exclude_engineer)
        if exclude_session_ids:
            ph = ",".join("?" for _ in exclude_session_ids)
            sql += f" AND (c.session_id IS NULL OR c.session_id NOT IN ({ph}))"
            params.extend(exclude_session_ids)
        if repo is not None:
            sql += " AND c.repo IS ?"
            params.append(repo)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def find_narrowable_file_claims_on(
        self,
        *,
        file_path: str,
        exclude_engineer: str | None = None,
        exclude_session_ids: list[str] | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return active narrowable file-scope claims whose pattern matches
        the given ``file_path``. Used by the auto-narrow path: a symbol
        requester scans this list to find holders it can auto-narrow.

        Pattern matching is left to the caller via ``compute_overlap`` --
        we return every narrowable file claim in the same repo and the
        caller filters by pattern intersection.
        """
        await self.init()
        sql = (
            "SELECT * FROM claims "
            "WHERE released_at IS NULL "
            "AND scope_type = 'file' "
            "AND narrowable = 1 "
            "AND claim_type != 'shared_file'"
        )
        params: list[Any] = []
        if exclude_engineer:
            sql += " AND engineer != ?"
            params.append(exclude_engineer)
        if exclude_session_ids:
            ph = ",".join("?" for _ in exclude_session_ids)
            sql += f" AND (session_id IS NULL OR session_id NOT IN ({ph}))"
            params.extend(exclude_session_ids)
        if repo is not None:
            sql += " AND repo IS ?"
            params.append(repo)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def attach_coexist_partner(
        self, claim_id: str, partner_id: str
    ) -> None:
        """Add ``partner_id`` to ``claims[claim_id].coexists_with`` (JSON
        array). Idempotent: a partner already present is not duplicated.

        Used by auto-coexist / auto-narrow paths to mark two claims as
        cooperative without going through the request flow.
        """
        async with self._acquire() as (conn, owns):
            cur = await conn.execute(
                "SELECT coexists_with FROM claims WHERE id = ? AND released_at IS NULL",
                (claim_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return
            raw = row["coexists_with"]
            partners: list[str] = []
            if raw:
                try:
                    partners = list(json.loads(raw))
                except (ValueError, TypeError):
                    partners = []
            if partner_id in partners:
                return
            partners.append(partner_id)
            await conn.execute(
                "UPDATE claims SET coexists_with = ? WHERE id = ?",
                (json.dumps(partners), claim_id),
            )
            if owns:
                await conn.commit()

    async def enqueue_claim_request(
        self,
        *,
        blocking_claim_id: str,
        requester_engineer: str,
        requester_session_id: str | None,
        requester_branch: str | None,
        requester_description: str | None,
        repo: str | None,
        claim_type: str,
        pattern: str,
        symbols: list[str] | None,
        narrowable: bool | None,
        ttl_hours: int | None,
        wait_seconds: int,
        priority: str = "normal",
    ) -> dict[str, Any]:
        """Enqueue a v0.21 FIFO claim request behind a blocking holder.

        Returns the inserted queue row (with ``id``, ``position``,
        ``expires_at``). The service layer's long-poll uses the ``id``
        as the key into the in-memory asyncio.Event dispatch so a
        release-time grant wakes the right waiter.

        Position is computed inside a BEGIN IMMEDIATE so concurrent
        enqueues on the same blocking_claim_id never collide on the
        ordering field.
        """
        await self.init()
        now = datetime.now(UTC)
        now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        expires = now + timedelta(seconds=max(1, int(wait_seconds)))
        expires_iso = (
            expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        symbols_json = json.dumps(symbols) if symbols else None
        narrowable_int: int | None
        if narrowable is None:
            narrowable_int = None
        else:
            narrowable_int = 1 if narrowable else 0
        new_id = str(uuid4())
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT COALESCE(MAX(position), 0) AS p FROM claim_queue "
                "WHERE blocking_claim_id = ?",
                (blocking_claim_id,),
            )
            row = await cur.fetchone()
            next_position = int((row["p"] if row else 0) or 0) + 1
            # v0.25: priority is one of low|normal|high|blocking. Unknown
            # values silently coerce to 'normal' so a typo never breaks
            # the enqueue path.
            normalised_priority = (
                priority
                if priority in ("low", "normal", "high", "blocking")
                else "normal"
            )
            await conn.execute(
                "INSERT INTO claim_queue ("
                "id, blocking_claim_id, requester_engineer, "
                "requester_session_id, requester_branch, "
                "requester_description, repo, claim_type, pattern, "
                "symbols, narrowable, ttl_hours, position, state, "
                "enqueued_at, expires_at, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'waiting', ?, ?, ?)",
                (
                    new_id,
                    blocking_claim_id,
                    requester_engineer,
                    requester_session_id,
                    requester_branch,
                    requester_description,
                    repo,
                    claim_type,
                    pattern,
                    symbols_json,
                    narrowable_int,
                    ttl_hours,
                    next_position,
                    now_iso,
                    expires_iso,
                    normalised_priority,
                ),
            )
            await conn.commit()
        return {
            "id": new_id,
            "blocking_claim_id": blocking_claim_id,
            "position": next_position,
            "state": "waiting",
            "enqueued_at": now_iso,
            "expires_at": expires_iso,
            "priority": normalised_priority,
        }

    async def pop_next_waiting_queue_entry(
        self,
        blocking_claim_id: str,
        *,
        age_boost_seconds: int = 0,
        fairness_interval: int = 0,
        priority_decay_sec: int = 0,
    ) -> dict[str, Any] | None:
        """Return the head-of-queue waiting entry for ``blocking_claim_id``,
        marking it as ``in_progress`` so a concurrent release-drain on the
        same claim doesn't double-grant. The service layer either calls
        :meth:`mark_queue_granted` (success) or :meth:`mark_queue_expired`
        / re-enqueue (failure) before returning.

        v0.26: ``age_boost_seconds`` lifts a waiting entry's effective
        priority by one level once it has been waiting longer than that
        many seconds. Prevents low/normal-priority waiters from starving
        under a steady stream of high/blocking entries. ``0`` disables
        the boost (strict declared-priority ordering, the v0.25 behaviour).

        v0.28: ``fairness_interval`` triggers a fairness pop on every Nth
        call (per blocking_claim_id) that ignores priority entirely and
        orders by raw FIFO position ASC. Guarantees low/normal-priority
        waiters eventually win even when high/blocking entries arrive
        steadily. ``0`` disables the fairness override and the per-process
        counter is not advanced -- preserving v0.25--v0.27 behaviour
        byte-identically.

        v0.28: ``priority_decay_sec`` subtracts one rank level per
        ``priority_decay_sec`` seconds in queue (blocking->high->normal
        ->low, floored at 'low'=1). Counterpart to age boost: prevents a
        misclassified urgent request from monopolising the head of the
        queue indefinitely. ``0`` disables decay and the priority-rank
        computation matches v0.27 exactly.
        """
        await self.init()
        boost_threshold = max(0, int(age_boost_seconds))
        fairness_n = max(0, int(fairness_interval))
        decay_sec = max(0, int(priority_decay_sec))
        # v0.28: only advance the fairness counter when the feature is
        # enabled. With ``fairness_interval == 0`` we MUST NOT touch
        # _FAIRNESS_COUNTERS -- otherwise toggling the setting between
        # calls would shift the modulo phase and the v0.27 byte-identical
        # invariant would break for callers that disable fairness.
        fairness_pop = False
        if fairness_n > 0:
            count = _next_fairness_count(blocking_claim_id)
            fairness_pop = (count % fairness_n) == 0
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            if fairness_pop:
                # v0.28 fairness override: bypass the priority CASE
                # entirely and pop the oldest waiting entry (lowest
                # position). This is the starvation safety valve so
                # low/normal-priority waiters eventually win.
                cur = await conn.execute(
                    "SELECT * FROM claim_queue "
                    "WHERE blocking_claim_id = ? AND state = 'waiting' "
                    "ORDER BY position ASC LIMIT 1",
                    (blocking_claim_id,),
                )
            else:
                # v0.25: order by priority DESC (blocking > high > normal
                # > low) before position ASC so urgent waiters jump ahead
                # of earlier-but-lower-priority entries. SQLite has no
                # ENUM, so we map the priority string to an integer
                # ordinal via CASE.
                #
                # v0.26: when ``age_boost_seconds`` > 0, add +1 to the
                # rank of any entry whose age (now - enqueued_at) exceeds
                # the threshold, so an old normal waiter floats to an
                # effective 'high' rank and breaks the tie via position
                # ASC. The age expression uses ``strftime('%s', ...)``
                # for an integer epoch comparison; if enqueued_at is
                # malformed the inner strftime returns NULL and the
                # COALESCE keeps the age at 0 (no boost), so the v0.25
                # behaviour is preserved on bad data.
                #
                # v0.28: when ``priority_decay_sec`` > 0, subtract one
                # rank level per ``decay_sec`` seconds in the queue. The
                # MAX(0, ...) inside the inner CAST clamps the integer
                # division to be non-negative on malformed timestamps;
                # the outer MAX(_prio_rank, 1) at ORDER BY time floors
                # the effective rank at 'low'=1 so a very old entry
                # still pops in declared FIFO order against equally
                # low-floored peers.
                cur = await conn.execute(
                    # Wrap the ranked rows in a subquery so ``_prio_rank`` is a
                    # real column the outer ORDER BY can use inside a CASE.
                    # SQLite tolerates a SELECT alias inside an ORDER BY
                    # expression; PostgreSQL only allows a bare alias there, so
                    # the subquery keeps both dialects happy (behaviour-identical
                    # -- the same columns, including _prio_rank, are returned).
                    "SELECT * FROM (SELECT *, ("
                    "CASE priority "
                    "WHEN 'blocking' THEN 4 "
                    "WHEN 'high' THEN 3 "
                    "WHEN 'normal' THEN 2 "
                    "WHEN 'low' THEN 1 "
                    "ELSE 2 END"
                    " + CASE WHEN ? > 0 AND COALESCE("
                    "CAST(strftime('%s','now') AS INTEGER) - "
                    "CAST(strftime('%s', enqueued_at) AS INTEGER), 0) > ? "
                    "THEN 1 ELSE 0 END"
                    " - CASE WHEN ? > 0 "
                    "THEN CAST(MAX(0, COALESCE("
                    "CAST(strftime('%s','now') AS INTEGER) - "
                    "CAST(strftime('%s', enqueued_at) AS INTEGER), 0)) / ? "
                    "AS INTEGER) "
                    "ELSE 0 END"
                    ") AS _prio_rank "
                    "FROM claim_queue "
                    "WHERE blocking_claim_id = ? AND state = 'waiting'"
                    ") AS q "
                    "ORDER BY (CASE WHEN _prio_rank < 1 THEN 1 "
                    "ELSE _prio_rank END) DESC, position ASC LIMIT 1",
                    (
                        boost_threshold,
                        boost_threshold,
                        decay_sec,
                        decay_sec if decay_sec > 0 else 1,
                        blocking_claim_id,
                    ),
                )
            row = await cur.fetchone()
            if row is None:
                await conn.commit()
                return None
            await conn.execute(
                "UPDATE claim_queue SET state = 'in_progress' WHERE id = ?",
                (row["id"],),
            )
            await conn.commit()
            return dict(row)

    async def mark_queue_granted(
        self, queue_id: str, granted_claim_id: str
    ) -> None:
        """Finalise a queue entry whose drain attempt produced a real
        claim. The in-memory long-poll signal is fired by the service
        layer; this method just persists the state transition."""
        await self.init()
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                "UPDATE claim_queue SET state = 'granted', "
                "granted_claim_id = ? WHERE id = ?",
                (granted_claim_id, queue_id),
            )
            await conn.commit()

    async def mark_queue_expired(self, queue_id: str) -> None:
        """Mark a queue entry as expired (wait_seconds elapsed without
        a grant, or the drain attempt re-conflicted). The long-poll on
        the requester side surfaces the original 409 to the caller."""
        await self.init()
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                "UPDATE claim_queue SET state = 'expired' WHERE id = ?",
                (queue_id,),
            )
            await conn.commit()

    async def cancel_queue_entry(
        self,
        queue_id: str,
        *,
        requester_engineer: str | None = None,
    ) -> bool:
        """v0.26: mark a waiting queue entry as cancelled.

        When ``requester_engineer`` is set, the cancellation only takes
        effect if the queue row belongs to that engineer -- prevents
        other engineers from cancelling someone else's wait via a stolen
        queue_id. Returns True when a row was actually transitioned,
        False when the row is missing or in a non-waiting/in-progress
        state.
        """
        await self.init()
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            if requester_engineer is not None:
                cur = await conn.execute(
                    "UPDATE claim_queue SET state = 'cancelled' "
                    "WHERE id = ? "
                    "AND state IN ('waiting', 'in_progress') "
                    "AND requester_engineer = ?",
                    (queue_id, requester_engineer),
                )
            else:
                cur = await conn.execute(
                    "UPDATE claim_queue SET state = 'cancelled' "
                    "WHERE id = ? "
                    "AND state IN ('waiting', 'in_progress')",
                    (queue_id,),
                )
            await conn.commit()
            return (cur.rowcount or 0) > 0

    async def get_queue_entry(self, queue_id: str) -> dict[str, Any] | None:
        """v0.24: fetch a single claim_queue row by id. Returns None if
        the row was deleted/cascade-released. Used by the cross-process
        queue poll path so a waiter can see state changes made by another
        Python process (no in-memory asyncio.Event will fire for those).
        """
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM claim_queue WHERE id = ?",
                (queue_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def expire_stale_queue_entries(self, now_iso: str | None = None) -> int:
        """Mark every waiting queue entry whose ``expires_at`` has
        passed as ``expired``. Called from the background cleanup
        loop so a long-poll timeout always converges even if the
        in-memory event was missed (process restart, server crash)."""
        await self.init()
        ts = now_iso or _utcnow()
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "UPDATE claim_queue SET state = 'expired' "
                "WHERE state IN ('waiting', 'in_progress') "
                "AND datetime(expires_at) <= datetime(?)",
                (ts,),
            )
            await conn.commit()
            return cur.rowcount or 0

    async def list_queue_for_requester(
        self, engineer: str
    ) -> list[dict[str, Any]]:
        """Return every queue row this engineer currently has, ordered
        by enqueued_at. Powers a v0.21 ``GET /requests`` extension and
        the dashboard's per-engineer queue panel (follow-up release)."""
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM claim_queue WHERE requester_engineer = ? "
                "ORDER BY enqueued_at DESC",
                (engineer,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def count_active_claims_for_engineer(
        self,
        engineer: str,
        *,
        now: datetime | None = None,
    ) -> tuple[int, str | None]:
        """v0.30: count this engineer's active claims and report the
        soonest ``expires_at`` among them.

        "Active" means ``released_at IS NULL`` and not TTL-expired.
        The TTL filter runs in Python -- parse ``expires_at`` and drop
        rows at-or-past ``now`` -- exactly like
        :meth:`list_active_claims_rows` does, and deliberately NOT via
        a SQLite ``datetime()`` comparison: the active-claim cap must
        agree byte-for-byte with what the conflict pipeline considers
        active, and two filters written in two dialects will
        eventually disagree on some edge (timezone suffix handling,
        a malformed timestamp). One pattern, mirrored.

        Returns ``(count, soonest_expires_at)``; the second element is
        ``None`` when the count is zero, and otherwise the raw stored
        ISO string so the caller can compute a Retry-After without a
        round-trip through reformatting.
        """
        async with self._acquire() as (conn, _owns):
            cur = await conn.execute(
                "SELECT expires_at FROM claims "
                "WHERE released_at IS NULL AND engineer = ?",
                (engineer,),
            )
            rows = await cur.fetchall()
        ref = now or datetime.now(UTC)
        count = 0
        soonest_raw: str | None = None
        soonest_dt: datetime | None = None
        for r in rows:
            exp_raw = str(r["expires_at"])
            exp = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
            if exp <= ref:
                continue
            count += 1
            if soonest_dt is None or exp < soonest_dt:
                soonest_dt = exp
                soonest_raw = exp_raw
        return count, soonest_raw

    async def count_queue_entries_for_engineer(
        self,
        engineer: str,
        *,
        states: tuple[str, ...] = ("waiting", "in_progress"),
    ) -> int:
        """v0.30: count this engineer's live claim_queue entries.

        Defaults to ``waiting`` + ``in_progress`` -- both represent a
        slot the engineer is occupying in someone's line (an
        ``in_progress`` row is a waiting row mid-drain). Terminal
        states (granted/expired/cancelled) never count against the
        per-engineer queue cap. Distinct from
        :meth:`list_queued_with_holder` (which feeds the v0.28
        backpressure header and counts waiting only); keeping this
        separate means the header's semantics stay untouched.
        """
        if not states:
            return 0
        await self.init()
        placeholders = ",".join("?" for _ in states)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM claim_queue "
                f"WHERE requester_engineer = ? AND state IN ({placeholders})",
                (engineer, *states),
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def queue_depth_for_repo(self, repo: str | None) -> int:
        """v0.30: count ``state='waiting'`` queue rows in one repo
        bucket. The NULL bucket only matches requests that were
        themselves enqueued with ``repo=None`` -- mirroring how the
        conflict pipeline buckets NULL-repo claims separately -- so a
        busy named repo never inflates the legacy un-tagged bucket and
        vice versa.
        """
        await self.init()
        if repo is None:
            sql = (
                "SELECT COUNT(*) AS n FROM claim_queue "
                "WHERE state = 'waiting' AND repo IS NULL"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = (
                "SELECT COUNT(*) AS n FROM claim_queue "
                "WHERE state = 'waiting' AND repo = ?"
            )
            params = (repo,)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def enqueue_webhook(
        self,
        *,
        url: str,
        event_type: str,
        payload_json: str,
        hmac_signature: str,
        next_attempt_at: str | None = None,
        kind: str = "webhook",
    ) -> str:
        """v0.27: insert a row into ``webhook_outbox`` for the
        background delivery loop to POST. Returns the new row id.

        ``next_attempt_at`` defaults to now -- the loop picks it up
        on its next tick. Callers don't need to know the retry
        schedule; mark_webhook_failed advances it.

        ``kind`` (v0.34) discriminates the delivery transport:
        'webhook' (default) POSTs to ``url`` with the HMAC header;
        'github' routes the row's ``detail`` to the GitHub adapter.
        """
        new_id = str(uuid4())
        now_iso = _utcnow()
        next_iso = next_attempt_at or now_iso
        async with self._acquire() as (conn, owns):
            await conn.execute(
                "INSERT INTO webhook_outbox "
                "(id, url, event_type, payload_json, hmac_signature, "
                " status, retry_count, next_attempt_at, created_at, kind) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                (new_id, url, event_type, payload_json,
                 hmac_signature, next_iso, now_iso, kind),
            )
            if owns:
                await conn.commit()
        return new_id

    async def list_pending_webhooks(
        self,
        *,
        now_iso: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """v0.27: return outbox rows whose ``status`` is pending or
        failed AND whose ``next_attempt_at`` has elapsed. Ordered by
        next_attempt_at ASC so the oldest-due rows are tried first.
        """
        await self.init()
        ts = now_iso or _utcnow()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM webhook_outbox "
                "WHERE status IN ('pending', 'failed') "
                "AND datetime(next_attempt_at) <= datetime(?) "
                "ORDER BY datetime(next_attempt_at) ASC LIMIT ?",
                (ts, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def mark_webhook_delivered(self, outbox_id: str) -> None:
        """v0.27: finalise an outbox row that the delivery loop
        successfully POSTed. delivered_at is stamped; status flips."""
        await self.init()
        now_iso = _utcnow()
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                "UPDATE webhook_outbox SET status = 'delivered', "
                "delivered_at = ?, last_attempt_at = ? WHERE id = ?",
                (now_iso, now_iso, outbox_id),
            )
            await conn.commit()

    async def mark_webhook_failed(
        self,
        outbox_id: str,
        *,
        last_error: str,
        next_attempt_at: str,
        exhausted: bool = False,
    ) -> None:
        """v0.27: record a failed delivery attempt. Advances retry_count,
        stamps last_error and last_attempt_at, sets next_attempt_at to
        the backoff-computed timestamp. When ``exhausted`` is True the
        status flips to 'exhausted' instead of 'failed' so the loop
        stops considering it."""
        await self.init()
        now_iso = _utcnow()
        new_status = "exhausted" if exhausted else "failed"
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                "UPDATE webhook_outbox SET status = ?, "
                "retry_count = retry_count + 1, last_error = ?, "
                "last_attempt_at = ?, next_attempt_at = ? WHERE id = ?",
                (new_status, last_error[:500], now_iso,
                 next_attempt_at, outbox_id),
            )
            await conn.commit()

    async def webhook_delivery_stats(
        self,
        *,
        window_hours: int = 24,
        now: datetime | None = None,
    ) -> dict[str, dict[str, int]]:
        """v0.27: per-event-type delivery counts for the dashboard.
        Returns ``{event_type: {delivered: N, failed: M, pending: K,
        exhausted: J}}``. Rolling window keyed off ``created_at``."""
        await self.init()
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(hours=window_hours)).replace(microsecond=0)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT event_type, status, COUNT(*) AS n "
                "FROM webhook_outbox "
                "WHERE datetime(created_at) >= datetime(?) "
                "GROUP BY event_type, status",
                (cutoff_iso,),
            )
            rows = await cur.fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            et = str(r["event_type"])
            st = str(r["status"])
            out.setdefault(et, {
                "delivered": 0, "failed": 0,
                "pending": 0, "exhausted": 0,
            })[st] = int(r["n"] or 0)
        return out

    async def recent_webhook_outbox(
        self, *, limit: int
    ) -> list[dict[str, Any]]:
        """v0.27.1 (HA port): return the ``limit`` most recent outbox rows,
        newest-first (created_at DESC, id DESC). ``coord outbox tail`` reverses
        the slice for oldest-first display. Only the columns the CLI renders
        are selected, so the operator path stays on the backend abstraction
        instead of opening a raw sqlite3 connection."""
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT id, status, event_type, created_at, last_error, "
                "retry_count, next_attempt_at FROM webhook_outbox "
                "ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def count_webhook_outbox(self, statuses: list[str]) -> int:
        """v0.27.1 (HA port): count outbox rows whose ``status`` is in
        ``statuses``. Backs the row-count message and the ``--dry-run``
        preview of ``coord outbox retry`` / ``purge``."""
        if not statuses:
            return 0
        await self.init()
        placeholders = ",".join(["?"] * len(statuses))
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                f"SELECT COUNT(*) AS n FROM webhook_outbox "
                f"WHERE status IN ({placeholders})",
                statuses,
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def reset_webhook_outbox(
        self, statuses: list[str], *, now_iso: str | None = None
    ) -> int:
        """v0.27.1 (HA port): reset every outbox row in ``statuses`` back to
        'pending' with retry_count=0, next_attempt_at=now and last_error
        cleared so the delivery loop re-tries it. Returns the rows reset.
        Backs ``coord outbox retry``."""
        if not statuses:
            return 0
        await self.init()
        now = now_iso or _utcnow()
        placeholders = ",".join(["?"] * len(statuses))
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            cur = await conn.execute(
                f"UPDATE webhook_outbox SET status='pending', retry_count=0, "
                f"next_attempt_at=?, last_error=NULL "
                f"WHERE status IN ({placeholders})",
                [now, *statuses],
            )
            await conn.commit()
            return int(cur.rowcount or 0)

    async def delete_webhook_outbox(self, statuses: list[str]) -> int:
        """v0.27.1 (HA port): DELETE every outbox row whose ``status`` is in
        ``statuses`` (terminal states only -- enforced by the caller).
        Returns the rows removed. Backs ``coord outbox purge``."""
        if not statuses:
            return 0
        await self.init()
        placeholders = ",".join(["?"] * len(statuses))
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            cur = await conn.execute(
                f"DELETE FROM webhook_outbox WHERE status IN ({placeholders})",
                statuses,
            )
            await conn.commit()
            return int(cur.rowcount or 0)

    async def create_engineer_token(
        self,
        engineer: str,
        token_sha256: str,
        *,
        description: str | None = None,
        expires_at: datetime | None = None,
        rotated_from: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """v0.29: insert a per-engineer bearer token row. The caller is
        responsible for hashing the raw token (sha256 hex digest) and
        for safely returning the raw value to the user exactly once --
        this method never sees the raw token. Returns the new row's id.

        v0.29.4: ``expires_at`` (None = never expires) and
        ``rotated_from`` (id of the predecessor token when this row is
        minted by a rotation) land in the v15 columns.
        """
        await self.init()
        token_id = str(uuid4())
        ts = (now or datetime.now(UTC)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        exp_ts = (
            expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if expires_at
            else None
        )
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                "INSERT INTO engineer_tokens "
                "(id, engineer, token_sha256, description, created_at, "
                "expires_at, rotated_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, engineer, token_sha256, description, ts, exp_ts, rotated_from),
            )
            await conn.commit()
        return token_id

    async def resolve_engineer_token(
        self,
        token_sha256: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """v0.29.4: diagnostic lookup behind ``lookup_engineer_token``.
        Returns None for missing or revoked tokens (indistinguishable
        on purpose -- both mean "not a credential"). Otherwise returns
        the row dict plus a computed ``status`` field: ``ok`` when the
        token authenticates, ``expired`` when ``expires_at`` has
        passed, or ``rotation_grace_elapsed`` when the token was
        rotated away and its grace window has closed. The distinct
        statuses let the auth layer emit actionable 401 hints without
        weakening the valid-only contract of ``lookup_engineer_token``.
        """
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT id, engineer, description, created_at, last_used_at, "
                "expires_at, rotated_from, rotation_grace_until, "
                "request_count, last_source_ip, last_user_agent "
                "FROM engineer_tokens "
                "WHERE token_sha256 = ? AND revoked_at IS NULL",
                (token_sha256,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        out = dict(row)
        ref = now or datetime.now(UTC)
        if _ts_elapsed(out.get("expires_at"), ref):
            out["status"] = "expired"
        elif _ts_elapsed(out.get("rotation_grace_until"), ref):
            out["status"] = "rotation_grace_elapsed"
        else:
            out["status"] = "ok"
        return out

    async def lookup_engineer_token(
        self, token_sha256: str
    ) -> dict[str, Any] | None:
        """v0.29: O(1) lookup on the unique index. Returns the row dict
        only when the token currently authenticates (not revoked, not
        expired, not past its rotation grace window); None otherwise.
        Callers needing to know WHY a token stopped authenticating use
        ``resolve_engineer_token``. The caller is expected to bump
        ``last_used_at`` separately via ``touch_engineer_token`` after
        a successful auth -- splitting the two keeps the hot read path
        off the write contention."""
        resolved = await self.resolve_engineer_token(token_sha256)
        if resolved is None or resolved["status"] != "ok":
            return None
        return resolved

    async def touch_engineer_token(
        self,
        token_sha256: str,
        *,
        source_ip: str | None = None,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """v0.29: bump ``last_used_at`` after a successful auth. Best
        effort: a failure here must NOT cause the request itself to
        401, so callers should swallow exceptions. Skipped silently
        when no row matches.

        v0.29.4: also increments ``request_count`` and records the
        last-seen source IP / user agent. Both are untrusted proxy
        metadata, so they are truncated before storage; request_count
        is approximate by design (touch failures are swallowed). A
        request with no IP/UA keeps the previous last-seen values."""
        await self.init()
        ts = (now or datetime.now(UTC)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ip = (source_ip or "")[:128] or None
        ua = (user_agent or "")[:512] or None
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                "UPDATE engineer_tokens SET last_used_at = ?, "
                "request_count = COALESCE(request_count, 0) + 1, "
                "last_source_ip = COALESCE(?, last_source_ip), "
                "last_user_agent = COALESCE(?, last_user_agent) "
                "WHERE token_sha256 = ? AND revoked_at IS NULL",
                (ts, ip, ua, token_sha256),
            )
            await conn.commit()

    async def list_engineer_tokens(
        self,
        *,
        engineer: str | None = None,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        """v0.29: list token metadata for ``coord tokens list`` and the
        dashboard. The raw token and its hash are never returned --
        callers see id, engineer, description, created_at, revoked_at,
        last_used_at. Sorted newest first."""
        await self.init()
        sql = (
            "SELECT id, engineer, description, created_at, "
            "revoked_at, last_used_at, expires_at, rotated_from, "
            "rotation_grace_until, request_count, last_source_ip, "
            "last_user_agent FROM engineer_tokens"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if engineer:
            clauses.append("engineer = ?")
            params.append(engineer)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_engineer_token_by_id(
        self, token_id: str
    ) -> dict[str, Any] | None:
        """v0.29.5: fetch one token row by id for the dashboard token
        panel. Returns the same safe column set as
        ``list_engineer_tokens`` (never ``token_sha256``), including
        ``revoked_at`` so callers can distinguish "already revoked"
        from "not yours"; None when the id is unknown."""
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT id, engineer, description, created_at, "
                "revoked_at, last_used_at, expires_at, rotated_from, "
                "rotation_grace_until, request_count, last_source_ip, "
                "last_user_agent FROM engineer_tokens WHERE id = ?",
                (token_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def revoke_engineer_token(
        self,
        token_id: str,
        *,
        engineer: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """v0.29: idempotent revocation. Returns True if a previously
        live token was newly revoked; False if the id does not exist or
        the token was already revoked. The row is preserved (not
        deleted) so audit queries against ``engineer_tokens`` keep their
        historical shape -- a revoked token simply never matches in
        ``lookup_engineer_token``.

        v0.29.5: ``engineer`` scopes the revocation -- the UPDATE only
        matches when the row belongs to that engineer, so a
        self-service dashboard revoke is atomic (no read-then-write
        race against an operator acting on the same row)."""
        await self.init()
        ts = (now or datetime.now(UTC)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        sql = (
            "UPDATE engineer_tokens SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL"
        )
        params: list[Any] = [ts, token_id]
        if engineer is not None:
            sql += " AND engineer = ?"
            params.append(engineer)
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, params)
            await conn.commit()
            return cur.rowcount > 0

    async def rotate_engineer_token(
        self,
        token_id: str,
        new_token_sha256: str,
        *,
        grace_until: datetime,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """v0.29.4: atomically mint a successor token and put the old
        token into its rotation grace window. Returns ``{"ok": True,
        "new_token_id": ..., "engineer": ..., "description": ...,
        "grace_until": ...}`` on success, or ``{"ok": False, "error":
        <reason>}`` where reason is one of ``not_found`` / ``revoked``
        / ``expired`` / ``already_rotated``.

        Rules (deliberately strict so a rotation can never revive a
        dead credential): revoked tokens cannot rotate; expired tokens
        cannot rotate (mint a fresh token instead); a token that
        already has a ``rotation_grace_until`` cannot rotate again --
        that would either fork the chain or extend a window that
        already closed. Rotate the current successor instead.

        Insert-successor and set-old-grace happen in one BEGIN
        IMMEDIATE transaction so the pair cannot partially apply."""
        await self.init()
        ref = now or datetime.now(UTC)
        ts = ref.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        grace_ts = (
            grace_until.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        exp_ts = (
            expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if expires_at
            else None
        )
        new_id = str(uuid4())
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "SELECT id, engineer, description, revoked_at, "
                "expires_at, rotation_grace_until "
                "FROM engineer_tokens WHERE id = ?",
                (token_id,),
            )
            old = await cur.fetchone()
            if old is None:
                await conn.rollback()
                return {"ok": False, "error": "not_found"}
            if old["revoked_at"] is not None:
                await conn.rollback()
                return {"ok": False, "error": "revoked"}
            if _ts_elapsed(old["expires_at"], ref):
                await conn.rollback()
                return {"ok": False, "error": "expired"}
            if old["rotation_grace_until"] is not None:
                await conn.rollback()
                return {"ok": False, "error": "already_rotated"}
            await conn.execute(
                "INSERT INTO engineer_tokens "
                "(id, engineer, token_sha256, description, created_at, "
                "expires_at, rotated_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    old["engineer"],
                    new_token_sha256,
                    old["description"],
                    ts,
                    exp_ts,
                    old["id"],
                ),
            )
            await conn.execute(
                "UPDATE engineer_tokens SET rotation_grace_until = ? "
                "WHERE id = ?",
                (grace_ts, old["id"]),
            )
            await conn.commit()
        return {
            "ok": True,
            "new_token_id": new_id,
            "engineer": str(old["engineer"]),
            "description": old["description"],
            "grace_until": grace_ts,
        }

    async def list_queued_with_holder(
        self,
        *,
        engineer: str | None = None,
        state: str | None = "waiting",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """v0.22: queue rows joined with the blocking holder's engineer
        and pattern so a single query answers ``who am I waiting on?``.

        Filters on requester engineer when ``engineer`` is set, and on
        queue state when ``state`` is set (default 'waiting' so the
        operator-facing endpoint only sees live entries; pass None to
        include granted/expired for forensic queries).

        The join is LEFT so a queue row whose holder has been
        cascade-deleted still surfaces with NULL holder fields rather
        than disappearing -- the requester deserves to see "the holder
        you were behind no longer exists" instead of a silent miss.
        """
        await self.init()
        sql = (
            "SELECT cq.*, c.engineer AS blocking_engineer, "
            "c.pattern AS blocking_pattern "
            "FROM claim_queue cq "
            "LEFT JOIN claims c ON c.id = cq.blocking_claim_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if engineer:
            clauses.append("cq.requester_engineer = ?")
            params.append(engineer)
        if state:
            clauses.append("cq.state = ?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY cq.enqueued_at DESC LIMIT ?"
        params.append(limit)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def touch_session_activity(self, session_id: str) -> int:
        """Bump ``last_activity`` on every active claim that belongs to
        the given session. Returns the rowcount so callers can log /
        verify. No-op when ``session_id`` is empty.
        """
        if not session_id:
            return 0
        now = _utcnow()
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                UPDATE claims SET last_activity = ?
                WHERE session_id = ? AND released_at IS NULL
                """,
                (now, session_id),
            )
            await conn.commit()
            return cur.rowcount or 0

    async def pending_requests_for_session(
        self, session_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return recent conflict-log entries logged against active
        claims that this session currently holds. Used by the holder
        to poll "has anyone been blocked on my scope?" so they can
        decide whether to release.
        """
        if not session_id:
            return []
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                SELECT cl.id, cl.claim_id, cl.attempted_by,
                       cl.attempted_pattern, cl.attempted_session_id,
                       cl.created_at,
                       c.pattern AS holder_pattern,
                       c.engineer AS holder_engineer
                FROM conflict_log cl
                JOIN claims c ON cl.claim_id = c.id
                WHERE c.session_id = ?
                  AND c.released_at IS NULL
                ORDER BY datetime(cl.created_at) DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = await cur.fetchall()
            await conn.commit()
        return [dict(r) for r in rows]

    async def release_for_session(self, session_id: str) -> int:
        """Release every active claim that was created with the given
        session_id, regardless of engineer name. Returns the count of
        rows actually closed. Intended for end-of-work cleanup so an
        agent process can reliably tear down everything it produced
        even when it spawned subagents under multiple engineer names.
        """
        if not session_id:
            return 0
        now = _utcnow()
        await self.init()
        # SELECT and UPDATE in a single BEGIN IMMEDIATE transaction so
        # there is no TOCTOU window where a concurrent release_claims
        # could close some of these IDs between our SELECT and UPDATE,
        # causing spurious cascade-resolve calls on already-closed claims.
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "SELECT id FROM claims WHERE session_id = ? AND released_at IS NULL",
                    (session_id,),
                )
                to_close = [str(r["id"]) for r in await cur.fetchall()]
                if to_close:
                    cur = await conn.execute(
                        "UPDATE claims SET released_at = ? "
                        "WHERE session_id = ? AND released_at IS NULL",
                        (now, session_id),
                    )
                    n = cur.rowcount or 0
                else:
                    n = 0
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        # Detach this session's released claims from any coexisting
        # partners BEFORE cascade-resolving requests, so a partner that
        # is about to receive a coexist-related event sees a clean
        # graph (no dangling reference to a claim that no longer
        # exists).
        for cid in to_close:
            await self._detach_coexist_partners(cid)
        for cid in to_close:
            await self.cascade_resolve_requests_for_claim(
                cid, release_kind="session-bulk", actor_engineer=None
            )
        return n

    async def release_claims(self, claim_ids: list[str], engineer: str | None = None) -> int:
        now = _utcnow()
        await self.init()
        released_ids: list[str] = []
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            n = 0
            for cid in claim_ids:
                if engineer:
                    cur = await conn.execute(
                        "UPDATE claims SET released_at = ? WHERE id = ? AND engineer = ? AND released_at IS NULL",
                        (now, cid, engineer),
                    )
                else:
                    cur = await conn.execute(
                        "UPDATE claims SET released_at = ? WHERE id = ? AND released_at IS NULL",
                        (now, cid),
                    )
                if cur.rowcount and cur.rowcount > 0:
                    released_ids.append(cid)
                    n += cur.rowcount
            await conn.commit()
        # Detach the released claims from any coexisting partners
        # BEFORE cascade-resolve, so a partner that's about to receive
        # a coexist-related event sees a clean graph rather than a
        # dangling reference to a claim that no longer exists.
        for cid in released_ids:
            await self._detach_coexist_partners(cid)
        # Cascade-resolve any open requests against these claims.
        # Done outside the txn so a long pending list doesn't hold the
        # write lock; each cascade is its own short transaction.
        for cid in released_ids:
            await self.cascade_resolve_requests_for_claim(
                cid, release_kind="voluntary", actor_engineer=engineer
            )
        return n

    async def release_active_claims_for_engineers(
        self, engineers: list[str], *, now_iso: str | None = None
    ) -> dict[str, int]:
        """v0.28 (HA port): release every active (unreleased) claim owned by
        each engineer in ``engineers`` by stamping ``released_at=now``.

        Returns ``{engineer: rows_released}`` preserving the input order so
        ``coord engineers stale --release`` can report a per-engineer
        breakdown. This is the backend-abstraction replacement for the CLI's
        former raw ``sqlite3`` UPDATE; it deliberately mirrors that bulk
        UPDATE (no cascade-resolve) to keep the operator output identical."""
        await self.init()
        now = now_iso or _utcnow()
        released: dict[str, int] = {}
        async with self._connect() as conn:
            await _configure_sqlite(conn)
            for engineer in engineers:
                cur = await conn.execute(
                    "UPDATE claims SET released_at = ? "
                    "WHERE engineer = ? AND released_at IS NULL",
                    (now, engineer),
                )
                released[engineer] = int(cur.rowcount or 0)
            await conn.commit()
        return released

    async def extend_claim(self, claim_id: str, engineer: str, new_expires_at: str) -> bool:
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                UPDATE claims SET expires_at = ?
                WHERE id = ? AND engineer = ? AND released_at IS NULL
                """,
                (new_expires_at, claim_id, engineer),
            )
            await conn.commit()
            return (cur.rowcount or 0) > 0

    async def expire_stale_claims(self, idle_timeout_sec: int = 0) -> int:
        """Close claims whose hard TTL has passed, plus any
        session-tagged claim whose ``last_activity`` is older than
        ``idle_timeout_sec``. Idle expiration only fires when both the
        timeout is positive AND the row has a non-NULL last_activity,
        so legacy NULL-session claims keep their TTL-only behaviour.
        """
        now = _utcnow()
        cutoff = datetime.now(UTC)
        idle_cutoff: datetime | None = None
        if idle_timeout_sec and idle_timeout_sec > 0:
            idle_cutoff = cutoff - timedelta(seconds=idle_timeout_sec)
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT id, expires_at, last_activity, ttl_shortened "
                "FROM claims WHERE released_at IS NULL",
            )
            rows = await cur.fetchall()
            await conn.commit()

        to_close: list[str] = []
        # ttl_shortened_ids: claims whose TTL was explicitly shortened by a
        # request_release call. Read directly from the claims.ttl_shortened
        # column (set in create_request, reset in denied respond_to_request)
        # rather than inferring from the requests table, which produces false
        # positives (claim expired with a pending request that arrived just
        # before natural deadline) and false negatives (TTL shortened but
        # requester withdrew so no pending request remains).
        ttl_shortened_ids: set[str] = set()
        for r in rows:
            exp = datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00"))
            if exp <= cutoff:
                to_close.append(str(r["id"]))
                if r["ttl_shortened"]:
                    ttl_shortened_ids.add(str(r["id"]))
                continue
            la_raw = r["last_activity"]
            if idle_cutoff is not None and la_raw:
                try:
                    la = datetime.fromisoformat(str(la_raw).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if la <= idle_cutoff:
                    to_close.append(str(r["id"]))
                    if r["ttl_shortened"]:
                        ttl_shortened_ids.add(str(r["id"]))
        if not to_close:
            return 0

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            n = 0
            actually_closed: list[str] = []
            for cid in to_close:
                cur = await conn.execute(
                    "UPDATE claims SET released_at = ? WHERE id = ? AND released_at IS NULL",
                    (now, cid),
                )
                if cur.rowcount and cur.rowcount > 0:
                    actually_closed.append(cid)
                    n += cur.rowcount
            await conn.commit()

        # Detach before cascade-resolve so a partner that's about to
        # receive a coexist-related event sees a clean graph.
        for cid in actually_closed:
            await self._detach_coexist_partners(cid)
        for cid in actually_closed:
            kind = "ttl-shortened" if cid in ttl_shortened_ids else "ttl-or-idle"
            await self.cascade_resolve_requests_for_claim(
                cid, release_kind=kind, actor_engineer=None
            )
        return n

    async def log_conflict(
        self,
        *,
        claim_id: str,
        attempted_by: str,
        attempted_pattern: str,
        resolution: str | None,
        attempted_session_id: str | None = None,
    ) -> None:
        async with self._acquire() as (conn, owns):
            await conn.execute(
                """
                INSERT INTO conflict_log (
                    id, claim_id, attempted_by, attempted_pattern,
                    resolution, created_at, attempted_session_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    claim_id,
                    attempted_by,
                    attempted_pattern,
                    resolution,
                    _utcnow(),
                    attempted_session_id,
                ),
            )
            if owns:
                await conn.commit()

    async def get_ownership_yaml(self) -> str | None:
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute("SELECT yaml_text FROM ownership_config WHERE id = 1")
            row = await cur.fetchone()
            await conn.commit()
            if not row:
                return None
            return str(row[0])

    async def set_ownership_yaml(self, yaml_text: str) -> None:
        now = _utcnow()
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute(
                "INSERT OR REPLACE INTO ownership_config (id, yaml_text, updated_at) VALUES (1, ?, ?)",
                (yaml_text, now),
            )
            await conn.commit()

    async def recent_conflicts(self, limit: int = 50) -> list[dict[str, Any]]:
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                SELECT * FROM conflict_log ORDER BY datetime(created_at) DESC LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
            await conn.commit()
            return [dict(r) for r in rows]

    async def hotspot_files(
        self,
        *,
        days: int = 30,
        min_attempts: int = 5,
        limit: int = 20,
        repo: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Files with the most blocked claim attempts in the window (v0.20).

        Joins ``conflict_log`` (which records every ``claim_files`` 409)
        to ``claims`` so we can pick up the holder's repo. Groups by
        ``(claims.repo, conflict_log.attempted_pattern)`` and returns
        rows where the attempt count is at least ``min_attempts``,
        ordered by attempts DESC then last_attempt DESC, capped at
        ``limit``.

        Operators read this list to decide which files belong in a
        ``shared_file`` rule (declared hotspots that don't 409 by
        design), or which paths need to be split into smaller modules
        (the boundary itself is wrong). v0.20 is read-only signal;
        auto-promote is queued for v0.21.
        """
        await self.init()
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=days)).replace(microsecond=0)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        params: list[Any] = [cutoff_iso]
        repo_filter = ""
        if repo is not None:
            repo_filter = " AND c.repo IS ?"
            params.append(repo)
        params.extend([min_attempts, limit])
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT c.repo AS repo, "
                "cl.attempted_pattern AS pattern, "
                "COUNT(*) AS attempts, "
                "COUNT(DISTINCT cl.attempted_by) AS distinct_attempters, "
                "MAX(cl.created_at) AS last_attempt "
                "FROM conflict_log cl JOIN claims c ON c.id = cl.claim_id "
                "WHERE datetime(cl.created_at) >= datetime(?)"
                + repo_filter
                + " GROUP BY c.repo, cl.attempted_pattern "
                # Reference the aggregate directly (not the SELECT alias)
                # in HAVING: SQLite accepts the alias, PostgreSQL does not.
                "HAVING COUNT(*) >= ? "
                "ORDER BY attempts DESC, last_attempt DESC "
                "LIMIT ?",
                params,
            )
            rows = await cur.fetchall()
        return [
            {
                "repo": r["repo"],
                "pattern": r["pattern"],
                "attempts": int(r["attempts"] or 0),
                "distinct_attempters": int(r["distinct_attempters"] or 0),
                "last_attempt": r["last_attempt"],
            }
            for r in rows
        ]

    async def daily_auto_resolutions(
        self,
        *,
        days: int = 30,
        repo: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Daily ``auto-coexist`` / ``auto-narrow`` buckets for the last
        ``days`` days (v0.18).

        Returns one row per (repo, date) bucket ordered by ``(repo,
        date ASC)``. Empty buckets are omitted -- the dashboard fills
        the gaps to render the heatmap cells. When ``repo`` is None
        every repo with at least one event in the window appears.

        The query joins ``request_events`` to ``claims`` through the
        ``detail`` JSON's ``holder_claim_id`` so the bucket is tagged
        with the holder's repo (the requester's repo is the same in
        practice; we pick one to keep the query single-join).
        """
        await self.init()
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=days)).replace(microsecond=0)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        params: list[Any] = [cutoff_iso]
        repo_filter = ""
        if repo is not None:
            repo_filter = " AND c.repo IS ?"
            params.append(repo)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT c.repo AS repo, "
                "strftime('%Y-%m-%d', re.created_at) AS date, "
                "SUM(CASE WHEN re.event_type='auto-coexist' "
                "        THEN 1 ELSE 0 END) AS auto_coexist, "
                "SUM(CASE WHEN re.event_type='auto-narrow' "
                "        THEN 1 ELSE 0 END) AS auto_narrow "
                "FROM request_events re "
                "JOIN claims c ON c.id = "
                "  json_extract(re.detail, '$.holder_claim_id') "
                "WHERE re.event_type IN ('auto-coexist','auto-narrow') "
                "AND datetime(re.created_at) >= datetime(?)"
                + repo_filter
                + " GROUP BY c.repo, date ORDER BY c.repo, date",
                params,
            )
            rows = await cur.fetchall()
        return [
            {
                "repo": r["repo"],
                "date": r["date"],
                "auto_coexist": int(r["auto_coexist"] or 0),
                "auto_narrow": int(r["auto_narrow"] or 0),
            }
            for r in rows
        ]

    async def count_auto_resolutions_since(
        self,
        *,
        window_hours: int = 24,
        now: datetime | None = None,
        repo: str | None = None,
    ) -> dict[str, int]:
        """Count v0.14 ``auto-coexist`` / ``auto-narrow`` audit events in
        the rolling window. ``repo`` is matched via the holder claim's
        repo field (joining ``request_events`` -> ``claims`` through the
        ``detail`` JSON's ``holder_claim_id``). Returns a dict with keys
        ``auto_coexist`` and ``auto_narrow``; callers can sum for a single
        "auto-resolutions" stat.

        v0.14.1 dashboard surface and ``/repos`` extension both consume
        this helper. Repo filter is optional because the dashboard's
        global panel ignores it.
        """
        await self.init()
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(hours=window_hours)).replace(microsecond=0)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            if repo is None:
                cur = await conn.execute(
                    "SELECT event_type, COUNT(*) AS n "
                    "FROM request_events "
                    "WHERE event_type IN ('auto-coexist','auto-narrow') "
                    "AND datetime(created_at) >= datetime(?) "
                    "GROUP BY event_type",
                    (cutoff_iso,),
                )
            else:
                # JSON1 extension is on by default in the aiosqlite build
                # we ship; json_extract drops the holder_claim_id out of
                # the detail blob so we can join against claims.repo.
                cur = await conn.execute(
                    "SELECT re.event_type, COUNT(*) AS n "
                    "FROM request_events re "
                    "JOIN claims c ON c.id = "
                    "  json_extract(re.detail, '$.holder_claim_id') "
                    "WHERE re.event_type IN ('auto-coexist','auto-narrow') "
                    "AND datetime(re.created_at) >= datetime(?) "
                    "AND c.repo IS ? "
                    "GROUP BY re.event_type",
                    (cutoff_iso, repo),
                )
            rows = await cur.fetchall()
        counts = {"auto_coexist": 0, "auto_narrow": 0}
        for r in rows:
            key = (
                "auto_coexist"
                if r["event_type"] == "auto-coexist"
                else "auto_narrow"
            )
            counts[key] = int(r["n"] or 0)
        return counts

    async def list_repos(
        self,
        *,
        window_hours: int = 24,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate claim activity per repo.

        Returns one row per distinct repo seen in the claims table
        (excluding NULL repo, which we treat as legacy / unattributed).
        Each row reports counts inside the rolling ``window_hours`` window
        plus a ``last_activity`` timestamp from the most recent claim.

        ``active_claims`` is window-independent: it counts claims that
        are unreleased and not yet expired right now, regardless of
        when they were created.

        ``auto_resolutions_24h`` (v0.14.1) breaks down the count of
        server-side auto-resolved overlaps -- ``auto_coexist`` and
        ``auto_narrow`` -- attributed to the repo by joining the audit
        event through ``detail.holder_claim_id`` -> ``claims.repo``.
        Implemented as a per-repo query after the aggregate fetch:
        N+1, but the row count is small (one per repo this service has
        ever seen) and the dashboard / ``/repos`` endpoint are operator
        surfaces, not high-traffic.
        """
        await self.init()
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(hours=window_hours)).replace(microsecond=0)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                SELECT
                    repo,
                    MAX(created_at) AS last_activity,
                    SUM(CASE WHEN datetime(created_at) >= datetime(?)
                             THEN 1 ELSE 0 END) AS claims_in_window,
                    COUNT(DISTINCT CASE WHEN datetime(created_at) >= datetime(?)
                                        THEN engineer END) AS engineers_in_window,
                    SUM(CASE WHEN released_at IS NULL
                                  AND datetime(expires_at) > datetime(?)
                             THEN 1 ELSE 0 END) AS active_claims
                FROM claims
                WHERE repo IS NOT NULL
                GROUP BY repo
                ORDER BY active_claims DESC, claims_in_window DESC, repo ASC
                """,
                (cutoff_iso, cutoff_iso, now_iso),
            )
            rows = await cur.fetchall()
            await conn.commit()

        out: list[dict[str, Any]] = []
        for r in rows:
            repo_name = r["repo"]
            auto_counts = await self.count_auto_resolutions_since(
                window_hours=window_hours, now=now, repo=repo_name
            )
            out.append(
                {
                    "repo": repo_name,
                    "last_activity": r["last_activity"],
                    "claims_24h": int(r["claims_in_window"] or 0),
                    "engineers_24h": int(r["engineers_in_window"] or 0),
                    "active_claims": int(r["active_claims"] or 0),
                    "auto_resolutions_24h": auto_counts,
                }
            )
        return out

    async def list_stale_engineers(
        self,
        *,
        days: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """v0.28: return engineers whose most-recent active-claim
        ``last_activity`` is older than ``days`` days ago. Useful for
        housekeeping abandoned worktrees that never released their
        claims.

        Returns one row per engineer:
        ``{engineer, last_activity, active_claim_count, repos: [...]}``.

        Engineers with zero active claims are not included (their
        last_activity has nothing to bound). ``last_activity IS NULL``
        rows (pre-v0.6 claims without session tagging) are also
        excluded because the idle-expiration path does not track them
        either -- including them would surface every legacy claim as
        "stale" forever.

        Ordering: oldest activity first so the most-abandoned
        engineers float to the top of the dashboard panel and CLI
        output.
        """
        if days <= 0:
            return []
        await self.init()
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=days)).replace(microsecond=0)
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                SELECT
                    engineer,
                    MAX(last_activity) AS last_activity,
                    COUNT(*) AS active_claim_count,
                    GROUP_CONCAT(DISTINCT repo) AS repos
                FROM claims
                WHERE released_at IS NULL
                  AND last_activity IS NOT NULL
                GROUP BY engineer
                HAVING datetime(MAX(last_activity)) < datetime(?)
                ORDER BY datetime(MAX(last_activity)) ASC, engineer ASC
                """,
                (cutoff_iso,),
            )
            rows = await cur.fetchall()
            await conn.commit()

        out: list[dict[str, Any]] = []
        for r in rows:
            # GROUP_CONCAT returns NULL if every row in the group has a
            # NULL repo; treat that case as an empty list rather than
            # leaking a literal "None" string into the output.
            repos_raw = r["repos"]
            if repos_raw:
                repos = sorted({s for s in str(repos_raw).split(",") if s})
            else:
                repos = []
            out.append(
                {
                    "engineer": str(r["engineer"]),
                    "last_activity": str(r["last_activity"]),
                    "active_claim_count": int(r["active_claim_count"] or 0),
                    "repos": repos,
                }
            )
        return out

    # --- Release requests (v5) ----------------------------------------

    async def create_request(
        self,
        *,
        request_id: str,
        claim_id: str,
        requester_engineer: str,
        requester_session_id: str | None,
        requested_pattern: str,
        reason: str | None,
        urgency: str,
        original_expires_at: str,
        shortened_expires_at: str,
        new_claim_expires_at: str,
        requested_scope: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a request, log the ``filed`` audit event,
        and shorten the holder's claim TTL.

        ``new_claim_expires_at`` is what the claim's ``expires_at``
        gets updated to (the shortened deadline). ``original_expires_at``
        is preserved on the request row so a ``denied`` decision can
        restore the claim's full TTL. Both transitions happen in a
        single transaction so a crash between steps can't leave the
        claim shortened with no request to track.

        ``requested_scope`` (v0.11+) is an optional narrower target the
        requester actually needs, distinct from the holder's pattern.
        The operator timeline records this so "holder claimed src/api/**,
        requester wanted src/api/auth.py" is reconstructible from the
        audit log without parsing free-text reason fields.
        """
        await self.init()
        now = _utcnow()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                # Update the claim's TTL to the shortened value -- but
                # never extend (clamp on the existing expires_at). Also
                # stamp ttl_shortened=1 so expire_stale_claims can label
                # the audit event correctly without a requests-table join.
                await conn.execute(
                    """
                    UPDATE claims
                    SET expires_at = ?, ttl_shortened = 1
                    WHERE id = ?
                      AND released_at IS NULL
                      AND datetime(expires_at) > datetime(?)
                    """,
                    (new_claim_expires_at, claim_id, new_claim_expires_at),
                )
                await conn.execute(
                    """
                    INSERT INTO requests (
                        id, claim_id, requester_engineer, requester_session_id,
                        requested_pattern, requested_scope, reason, urgency,
                        decision, original_expires_at, shortened_expires_at,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        request_id,
                        claim_id,
                        requester_engineer,
                        requester_session_id,
                        requested_pattern,
                        requested_scope,
                        reason,
                        urgency,
                        original_expires_at,
                        shortened_expires_at,
                        now,
                    ),
                )
                await self._record_event_in_txn(
                    conn,
                    request_id=request_id,
                    event_type="filed",
                    actor_engineer=requester_engineer,
                    actor_session_id=requester_session_id,
                    detail={
                        "claim_id": claim_id,
                        "pattern": requested_pattern,
                        "requested_scope": requested_scope,
                        "urgency": urgency,
                        "reason": reason,
                        "original_expires_at": original_expires_at,
                        "shortened_expires_at": shortened_expires_at,
                    },
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            cur = await conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else {}

    @staticmethod
    async def _record_event_in_txn(
        conn: aiosqlite.Connection,
        *,
        request_id: str | None,
        event_type: str,
        actor_engineer: str | None,
        actor_session_id: str | None,
        detail: dict[str, Any] | None,
    ) -> None:
        """Append a request_events row inside the caller's transaction.

        Detail is JSON-encoded so the column stays TEXT and SQLite
        can be queried via JSON1 functions if we ever need to grep
        the audit trail by detail field. The caller commits.

        ``request_id`` is nullable as of schema v9: v0.14 auto-coexist /
        auto-narrow events have no parent request row and pass None.
        """
        import json as _json

        await conn.execute(
            """
            INSERT INTO request_events (
                id, request_id, event_type, actor_engineer,
                actor_session_id, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                request_id,
                event_type,
                actor_engineer,
                actor_session_id,
                _json.dumps(detail) if detail is not None else None,
                _utcnow(),
            ),
        )

    async def respond_to_request(
        self,
        *,
        request_id: str,
        decision: str,
        actor_engineer: str | None,
        actor_session_id: str | None,
        note: str | None = None,
        narrowed_pattern: str | None = None,
        coexist_pattern: str | None = None,
        coexist_symbols: dict[str, list[str]] | None = None,
        min_expires_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Record the holder's decision and apply its side-effect.

        - ``approved``: claim is released immediately.
        - ``denied``: claim's expires_at is restored to the request's
          ``original_expires_at`` so the holder keeps the time they
          would have had absent the request.
        - ``narrowed`` (v0.11+): release the holder's original claim
          and open a new claim under ``narrowed_pattern`` inheriting
          the holder's engineer / branch / repo / session / TTL. The
          released portion is what the requester needed; the rest
          stays held. Requires ``narrowed_pattern``.
        - ``coexist`` (v0.11+): grant the requester a sibling claim on
          the same scope. Both claims persist and self-exclude from
          each other via ``claims.coexists_with`` (a JSON array of
          partner ids). Useful when two agents want to edit different
          functions in the same file. Requires ``coexist_pattern``
          (file scope) OR ``coexist_symbols`` (v0.35 symbol scope);
          for v1 the linkage is pairwise (requester <-> holder), not
          transitive across the holder's existing partners.

          v0.35: when ``coexist_symbols`` (a JSON-able dict mapping
          ``file_path`` -> list of ``"Foo::handleA"`` symbol paths) is
          supplied, the requester's sibling claim is created
          ``scope_type='symbol'`` with ``claim_symbols`` rows for
          exactly those symbols, so downstream overlap checks see real
          symbols rather than a blanket file grant. The grant is also
          recorded on the request row and the responded audit event.
          The respond-time validation (both sides symbol-scoped,
          symbols subset of the requester's claim, disjoint from the
          holder) lives in the service layer; this layer trusts the
          already-validated grant.

        The request row's ``decision`` is moved from ``pending`` only;
        if the request already has a terminal decision the call is a
        no-op (returns the existing row). The audit event is recorded
        either way so an attempted late response is still visible.
        """
        valid = {"approved", "denied", "narrowed", "coexist"}
        if decision not in valid:
            raise ValueError(
                "decision must be one of 'approved', 'denied', "
                f"'narrowed', 'coexist', got {decision!r}"
            )
        if decision == "narrowed" and not narrowed_pattern:
            raise ValueError(
                "decision='narrowed' requires a non-empty 'narrowed_pattern' kwarg"
            )
        if decision == "coexist" and not coexist_pattern and not coexist_symbols:
            raise ValueError(
                "decision='coexist' requires a non-empty 'coexist_pattern' "
                "or 'coexist_symbols' kwarg"
            )

        await self.init()
        now = _utcnow()
        # Holds extra detail fields that narrowed / coexist branches
        # populate (new claim id, original pattern, etc.) so the
        # responded audit event records the full transition.
        extra_detail: dict[str, Any] = {}
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "SELECT * FROM requests WHERE id = ?", (request_id,)
                )
                req = await cur.fetchone()
                if req is None:
                    await conn.rollback()
                    return None

                if req["decision"] != "pending":
                    # Already-terminal request. Record the late attempt
                    # in the audit log so we have evidence the holder
                    # did try to respond, but don't change state.
                    await self._record_event_in_txn(
                        conn,
                        request_id=request_id,
                        event_type="responded-late",
                        actor_engineer=actor_engineer,
                        actor_session_id=actor_session_id,
                        detail={
                            "attempted_decision": decision,
                            "current_decision": req["decision"],
                            "note": note,
                        },
                    )
                    await conn.commit()
                    return dict(req)

                # Per-repo serialization for the grant (design 5.2): the
                # narrowed / coexist branches mutate the active-claim set
                # and must hold the same per-repo lock the create path
                # does. No-op on SQLite; the Postgres backend (P3)
                # acquires ``pg_advisory_xact_lock`` on this very
                # connection. Keyed off the holder claim's repo (coalesced
                # to '' for the NULL-repo bucket inside ``repo_lock``).
                repo_cur = await conn.execute(
                    "SELECT repo FROM claims WHERE id = ?", (req["claim_id"],)
                )
                claim_repo_row = await repo_cur.fetchone()
                await self.repo_lock(
                    conn,
                    claim_repo_row["repo"] if claim_repo_row is not None else None,
                )

                if decision == "approved":
                    await conn.execute(
                        "UPDATE claims SET released_at = ? "
                        "WHERE id = ? AND released_at IS NULL",
                        (now, req["claim_id"]),
                    )
                elif decision == "denied":
                    # Restore the original TTL so the holder isn't
                    # punished for the request having shortened it.
                    # Also reset ttl_shortened so a claim that expires
                    # naturally after denial is not mislabelled in the
                    # audit trail.
                    await conn.execute(
                        "UPDATE claims SET expires_at = ?, ttl_shortened = 0 "
                        "WHERE id = ? AND released_at IS NULL",
                        (req["original_expires_at"], req["claim_id"]),
                    )
                elif decision == "narrowed":
                    extra_detail = await self._apply_narrowed(
                        conn,
                        request_row=req,
                        narrowed_pattern=str(narrowed_pattern),
                        now=now,
                        min_expires_at=min_expires_at,
                    )
                elif decision == "coexist":
                    extra_detail = await self._apply_coexist(
                        conn,
                        request_row=req,
                        coexist_pattern=(
                            str(coexist_pattern) if coexist_pattern else None
                        ),
                        coexist_symbols=coexist_symbols,
                        now=now,
                        min_expires_at=min_expires_at,
                    )

                await conn.execute(
                    """
                    UPDATE requests
                    SET decision = ?, decided_at = ?, decided_by_engineer = ?,
                        decided_by_session_id = ?, note = ?
                    WHERE id = ?
                    """,
                    (decision, now, actor_engineer, actor_session_id, note, request_id),
                )
                detail: dict[str, Any] = {"decision": decision, "note": note}
                detail.update(extra_detail)
                await self._record_event_in_txn(
                    conn,
                    request_id=request_id,
                    event_type="responded",
                    actor_engineer=actor_engineer,
                    actor_session_id=actor_session_id,
                    detail=detail,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            cur = await conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    async def _apply_narrowed(
        conn: aiosqlite.Connection,
        *,
        request_row: aiosqlite.Row,
        narrowed_pattern: str,
        now: str,
        min_expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Release the original claim and open a new claim under the
        narrower pattern, all inside the caller's transaction. Returns
        a dict of fields to merge into the responded-event detail
        (original_pattern, narrowed_pattern, original_claim_id,
        new_claim_id) so callers don't have to reload the rows.
        """
        original_claim_id = str(request_row["claim_id"])
        cur = await conn.execute(
            "SELECT * FROM claims WHERE id = ?", (original_claim_id,)
        )
        orig = await cur.fetchone()
        if orig is None:
            raise ValueError(
                f"narrowed: original claim {original_claim_id!r} not found"
            )
        # Release the original. If it was already released we still
        # proceed to insert the new claim so the responder isn't left
        # in a half-finished state, but the transaction-level outcome
        # is the same either way.
        await conn.execute(
            "UPDATE claims SET released_at = ? "
            "WHERE id = ? AND released_at IS NULL",
            (now, original_claim_id),
        )
        new_claim_id = str(uuid4())
        # last_activity is stamped only when the inherited row had a
        # session_id (matches the v0.6+ idle-expiration opt-in rule
        # used by insert_claims_batch).
        new_last_activity = now if orig["session_id"] else None
        # Floor the new claim's TTL at min_expires_at when provided.
        # If the original claim's TTL was shortened by a request_release
        # call, the new narrowed claim would otherwise inherit that
        # compressed deadline, which may be only minutes away. The
        # service layer passes min_expires_at = now + default_ttl so the
        # holder's narrowed scope gets a fresh working window.
        new_expires_at = str(orig["expires_at"])
        if min_expires_at:
            try:
                orig_dt = datetime.fromisoformat(
                    new_expires_at.replace("Z", "+00:00")
                )
                floor_dt = datetime.fromisoformat(
                    min_expires_at.replace("Z", "+00:00")
                )
                if orig_dt < floor_dt:
                    new_expires_at = min_expires_at
            except ValueError:
                pass
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern,
                severity, created_at, expires_at, released_at, repo,
                session_id, last_activity, coexists_with
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)
            """,
            (
                new_claim_id,
                orig["engineer"],
                orig["branch"],
                orig["description"],
                orig["claim_type"],
                narrowed_pattern,
                orig["severity"],
                now,
                new_expires_at,
                orig["repo"],
                orig["session_id"],
                new_last_activity,
            ),
        )
        return {
            "narrowed_pattern": narrowed_pattern,
            "original_pattern": str(orig["pattern"]),
            "original_claim_id": original_claim_id,
            "new_claim_id": new_claim_id,
        }

    @staticmethod
    async def _apply_coexist(
        conn: aiosqlite.Connection,
        *,
        request_row: aiosqlite.Row,
        coexist_pattern: str | None,
        coexist_symbols: dict[str, list[str]] | None = None,
        now: str,
        min_expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Grant the requester a sibling claim and wire the pairwise
        coexists_with edge between holder and requester. Returns
        responded-event detail fields (coexist_pattern, holder_claim_id,
        requester_claim_id).

        The linkage is pairwise (holder <-> requester) for v1: we do
        NOT add the holder's existing partners to the requester's
        list, because each coexistence is a separate consent and
        adding C as a partner of B because B coexisted with A would
        bind C without C's holder agreeing.

        v0.35 symbol scope: when ``coexist_symbols`` is supplied the
        requester's sibling is created ``scope_type='symbol'`` (and
        ``narrowable=0`` to mirror normal symbol claims) with
        ``claim_symbols`` rows for exactly the granted symbol paths, the
        grant is persisted on the request row's ``coexist_symbols``
        column, and the responded-event detail carries it. When
        ``coexist_symbols`` is None the path is byte-identical to the
        v0.11 file-scope grant.
        """
        import json as _json

        holder_claim_id = str(request_row["claim_id"])
        cur = await conn.execute(
            "SELECT * FROM claims WHERE id = ?", (holder_claim_id,)
        )
        holder = await cur.fetchone()
        if holder is None:
            raise ValueError(
                f"coexist: holder claim {holder_claim_id!r} not found"
            )
        requester_claim_id = str(uuid4())
        requester_engineer = str(request_row["requester_engineer"])
        requester_session_id = request_row["requester_session_id"]
        requester_last_activity = now if requester_session_id else None
        request_id = str(request_row["id"])

        # The sibling's pattern: an explicit file-scope ``coexist_pattern``
        # wins; otherwise (symbol scope) inherit the holder's pattern,
        # which for a symbol-scoped holder is the file path the granted
        # symbols live in.
        requester_pattern = coexist_pattern or str(holder["pattern"])
        is_symbol = bool(coexist_symbols)
        scope_type = "symbol" if is_symbol else "file"
        # Symbol claims are non-narrowable, matching the create path
        # (insert_claims_batch defaults symbol scope to narrowable=0);
        # file-scope coexist keeps the legacy default of 1.
        narrowable = 0 if is_symbol else 1

        # Floor the requester's new claim TTL at min_expires_at when
        # provided. The holder's TTL may have been shortened by the
        # request_release call; the requester's sibling should not
        # inherit that compressed deadline.
        requester_expires_at = str(holder["expires_at"])
        if min_expires_at:
            try:
                holder_dt = datetime.fromisoformat(
                    requester_expires_at.replace("Z", "+00:00")
                )
                floor_dt = datetime.fromisoformat(
                    min_expires_at.replace("Z", "+00:00")
                )
                if holder_dt < floor_dt:
                    requester_expires_at = min_expires_at
            except ValueError:
                pass

        # Insert the requester's sibling claim. Pattern matches what
        # the holder approved; severity is 'soft' because cooperative
        # coexistence is the whole point of this verb.
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern,
                severity, created_at, expires_at, released_at, repo,
                session_id, last_activity, coexists_with,
                scope_type, narrowable
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                requester_claim_id,
                requester_engineer,
                None,
                f"coexist via request {request_id}",
                "file",
                requester_pattern,
                "soft",
                now,
                requester_expires_at,
                holder["repo"],
                requester_session_id,
                requester_last_activity,
                _json.dumps([holder_claim_id]),
                scope_type,
                narrowable,
            ),
        )

        # v0.35: persist the granted symbol rows so downstream overlap
        # checks see real symbols, not a blanket file grant. Reuse the
        # same INSERT OR IGNORE path normal symbol claims use; spans are
        # NULL (the grant references already-validated symbol paths, not
        # a fresh extraction) and symbol_kind is 'unknown'.
        if coexist_symbols:
            symbol_rows: list[tuple[Any, ...]] = []
            for file_path, syms in coexist_symbols.items():
                for raw in syms:
                    if "::" in raw:
                        parent, _, leaf = raw.rpartition("::")
                    else:
                        parent, leaf = None, raw
                    symbol_rows.append(
                        (
                            str(uuid4()),
                            requester_claim_id,
                            str(file_path),
                            leaf,
                            "unknown",
                            parent,
                            None,
                            None,
                            None,
                            None,
                            None,
                        )
                    )
            if symbol_rows:
                await conn.executemany(
                    """
                    INSERT OR IGNORE INTO claim_symbols
                        (id, claim_id, file_path, symbol_name, symbol_kind,
                         parent_symbol, start_line, start_col, end_line,
                         end_col, resolved_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    symbol_rows,
                )
            # Record the grant on the request row so it round-trips
            # through get_request and the operator timeline.
            await conn.execute(
                "UPDATE requests SET coexist_symbols = ? WHERE id = ?",
                (_json.dumps(coexist_symbols), request_id),
            )

        # Append the requester's claim to the holder's coexists_with
        # array (creating it if the holder had no partners before).
        holder_partners_raw = holder["coexists_with"]
        if holder_partners_raw:
            holder_partners = list(_json.loads(holder_partners_raw))
        else:
            holder_partners = []
        if requester_claim_id not in holder_partners:
            holder_partners.append(requester_claim_id)
        await conn.execute(
            "UPDATE claims SET coexists_with = ? WHERE id = ?",
            (_json.dumps(holder_partners), holder_claim_id),
        )

        detail: dict[str, Any] = {
            "coexist_pattern": coexist_pattern,
            "holder_claim_id": holder_claim_id,
            "requester_claim_id": requester_claim_id,
        }
        if coexist_symbols:
            detail["coexist_symbols"] = coexist_symbols
        return detail

    async def _detach_coexist_partners(self, claim_id: str) -> int:
        """Walk a (recently released) claim's ``coexists_with`` array
        and strip ``claim_id`` from each partner's array. Idempotent:
        partners that are already gone or that don't list ``claim_id``
        are left alone.

        When a partner ends up with an empty array we write back NULL
        rather than ``"[]"`` so the column stays semantically "no
        partners" and any reader doing ``if row["coexists_with"]:``
        keeps working.

        Returns the number of partner rows actually updated, mostly
        for tests / future logging.
        """
        import json as _json

        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "SELECT coexists_with FROM claims WHERE id = ?", (claim_id,)
                )
                row = await cur.fetchone()
                if row is None or not row["coexists_with"]:
                    await conn.commit()
                    return 0
                try:
                    partners = list(_json.loads(row["coexists_with"]))
                except (ValueError, TypeError):
                    # Corrupt JSON: nothing safe to do here; the column
                    # is intentionally tolerant of NULL/empty.
                    await conn.commit()
                    return 0

                updated = 0
                for partner_id in partners:
                    cur = await conn.execute(
                        "SELECT coexists_with FROM claims WHERE id = ?",
                        (partner_id,),
                    )
                    p_row = await cur.fetchone()
                    if p_row is None or not p_row["coexists_with"]:
                        continue
                    try:
                        p_partners = list(_json.loads(p_row["coexists_with"]))
                    except (ValueError, TypeError):
                        continue
                    if claim_id not in p_partners:
                        continue
                    p_partners = [pid for pid in p_partners if pid != claim_id]
                    new_value = _json.dumps(p_partners) if p_partners else None
                    await conn.execute(
                        "UPDATE claims SET coexists_with = ? WHERE id = ?",
                        (new_value, partner_id),
                    )
                    updated += 1
                await conn.commit()
                return updated
            except Exception:
                await conn.rollback()
                raise

    async def cascade_resolve_requests_for_claim(
        self,
        claim_id: str,
        *,
        release_kind: str,
        actor_engineer: str | None = None,
    ) -> int:
        """When a claim is released for unrelated reasons (TTL sweep,
        idle expiration, voluntary release, release_session bulk),
        every still-pending request against it transitions to
        ``resolved`` (or ``expired`` if the cause was the shortened
        request-induced TTL). Each transition is logged as an audit
        event so the requester can see what happened.

        Returns the number of requests transitioned.
        """
        await self.init()
        now = _utcnow()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "SELECT id, shortened_expires_at FROM requests "
                    "WHERE claim_id = ? AND decision = 'pending'",
                    (claim_id,),
                )
                pending = await cur.fetchall()
                count = 0
                for row in pending:
                    rid = row["id"]
                    # If the shortened TTL boundary is the trigger for
                    # this release, the request expired waiting on the
                    # holder. Otherwise the underlying claim went away
                    # for an unrelated reason -> resolved.
                    is_expired = release_kind == "ttl-shortened"
                    new_decision = "expired" if is_expired else "resolved"
                    event_type = "expired" if is_expired else "resolved"
                    await conn.execute(
                        "UPDATE requests SET decision = ?, decided_at = ? "
                        "WHERE id = ? AND decision = 'pending'",
                        (new_decision, now, rid),
                    )
                    await self._record_event_in_txn(
                        conn,
                        request_id=rid,
                        event_type=event_type,
                        actor_engineer=actor_engineer,
                        actor_session_id=None,
                        detail={"release_kind": release_kind, "claim_id": claim_id},
                    )
                    count += 1
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            return count

    async def record_request_notify(
        self,
        request_id: str,
        *,
        holder_engineer: str | None,
        holder_session_id: str | None,
    ) -> bool:
        """Record a ``notified`` event the FIRST time a holder session
        sees a request via pending_requests. Subsequent polls from the
        same session don't re-fire (a request that's polled every 30s
        for an hour shouldn't write 120 events). Returns True iff a
        new event was written.
        """
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT 1 FROM request_events "
                "WHERE request_id = ? AND event_type = 'notified' "
                "  AND COALESCE(actor_session_id, '') = COALESCE(?, '')",
                (request_id, holder_session_id),
            )
            if await cur.fetchone() is not None:
                return False
            await self._record_event_in_txn(
                conn,
                request_id=request_id,
                event_type="notified",
                actor_engineer=holder_engineer,
                actor_session_id=holder_session_id,
                detail=None,
            )
            await conn.commit()
            return True

    async def record_request_event(
        self,
        event_type: str,
        *,
        request_id: str | None = None,
        actor_engineer: str | None = None,
        actor_session_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append a single ``request_events`` row in its own transaction.

        Public counterpart to ``_record_event_in_txn`` for callers that
        aren't already inside a transaction. ``request_id`` is optional
        (nullable as of schema v9) so v0.14 auto-resolution paths
        (``auto-coexist``, ``auto-narrow``) can record audit rows even
        though they bypass the ``requests`` table entirely.
        """
        async with self._acquire() as (conn, owns):
            await self._record_event_in_txn(
                conn,
                request_id=request_id,
                event_type=event_type,
                actor_engineer=actor_engineer,
                actor_session_id=actor_session_id,
                detail=detail,
            )
            if owns:
                await conn.commit()

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_requests(
        self,
        *,
        requester_engineer: str | None = None,
        claim_id: str | None = None,
        decision: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return requests filtered by the given criteria. Joined with
        the holder's claim row so callers can render holder/pattern
        without an extra round trip."""
        await self.init()
        clauses: list[str] = []
        args: list[Any] = []
        if requester_engineer:
            clauses.append("r.requester_engineer = ?")
            args.append(requester_engineer)
        if claim_id:
            clauses.append("r.claim_id = ?")
            args.append(claim_id)
        if decision:
            clauses.append("r.decision = ?")
            args.append(decision)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT r.*, c.engineer AS holder_engineer, c.pattern AS holder_pattern,
                   c.repo AS holder_repo, c.released_at AS claim_released_at
            FROM requests r
            JOIN claims c ON r.claim_id = c.id
            {where}
            ORDER BY datetime(r.created_at) DESC
            LIMIT ?
        """
        args.append(limit)
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def list_open_requests_for_session(
        self, session_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return open (decision='pending') requests filed against
        claims this session currently holds. The holder's
        pending_requests inbox merges this with the conflict-log feed."""
        if not session_id:
            return []
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                SELECT r.*,
                       c.engineer AS holder_engineer,
                       c.pattern  AS holder_pattern,
                       c.repo     AS holder_repo
                FROM requests r
                JOIN claims c ON r.claim_id = c.id
                WHERE c.session_id = ?
                  AND c.released_at IS NULL
                  AND r.decision = 'pending'
                ORDER BY datetime(r.created_at) DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def list_request_events(
        self, request_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Return the full event timeline for a request, oldest first."""
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM request_events WHERE request_id = ? "
                "ORDER BY datetime(created_at) ASC LIMIT ?",
                (request_id, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def list_recent_claims(self, limit: int = 200) -> list[dict[str, Any]]:
        await self.init()
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM claims ORDER BY datetime(created_at) DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            await conn.commit()
            return [dict(r) for r in rows]
