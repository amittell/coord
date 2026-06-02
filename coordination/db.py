from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite


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


CURRENT_SCHEMA_VERSION = 9

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
]


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
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

    async def list_active_claims_rows(self, exclude_engineer: str | None = None) -> list[dict[str, Any]]:
        await self.init()
        q = """
        SELECT * FROM claims
        WHERE released_at IS NULL
        """
        args: list[Any] = []
        if exclude_engineer:
            q += " AND engineer != ?"
            args.append(exclude_engineer)
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(q, args)
            rows = await cur.fetchall()
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
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
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
            await conn.commit()
        return [i[0] for i in items]

    async def insert_claim_symbols(
        self,
        *,
        rows: list[tuple[str, str, str, str, str]],
    ) -> None:
        """Insert symbol rows for ``scope_type='symbol'`` claims.

        Each row is ``(id, claim_id, file_path, symbol_name, symbol_kind)``.
        The caller (service layer) generates the row ids and groups symbols
        per claim. Idempotent against the ``UNIQUE (claim_id, file_path,
        symbol_name)`` index: a duplicate insert is silently ignored so
        retries don't 500.
        """
        if not rows:
            return
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            await _configure_sqlite(conn)
            await conn.executemany(
                """
                INSERT OR IGNORE INTO claim_symbols
                    (id, claim_id, file_path, symbol_name, symbol_kind)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            await conn.commit()

    async def get_claim_symbols(
        self, claim_id: str
    ) -> list[dict[str, str]]:
        """Return all symbol rows for a claim. Empty list for file-scope
        claims or unknown claim_id."""
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                """
                SELECT file_path, symbol_name, symbol_kind
                FROM claim_symbols
                WHERE claim_id = ?
                ORDER BY file_path, symbol_name
                """,
                (claim_id,),
            )
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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
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
            await conn.commit()

    async def touch_session_activity(self, session_id: str) -> int:
        """Bump ``last_activity`` on every active claim that belongs to
        the given session. Returns the rowcount so callers can log /
        verify. No-op when ``session_id`` is empty.
        """
        if not session_id:
            return 0
        now = _utcnow()
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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

    async def extend_claim(self, claim_id: str, engineer: str, new_expires_at: str) -> bool:
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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

        async with aiosqlite.connect(self.path) as conn:
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
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
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
            await conn.commit()

    async def get_ownership_yaml(self) -> str | None:
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute(
                "INSERT OR REPLACE INTO ownership_config (id, yaml_text, updated_at) VALUES (1, ?, ?)",
                (yaml_text, now),
            )
            await conn.commit()

    async def recent_conflicts(self, limit: int = 50) -> list[dict[str, Any]]:
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
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

        async with aiosqlite.connect(self.path) as conn:
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

        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
          functions in the same file. Requires ``coexist_pattern``;
          for v1 the linkage is pairwise (requester <-> holder), not
          transitive across the holder's existing partners.

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
        if decision == "coexist" and not coexist_pattern:
            raise ValueError(
                "decision='coexist' requires a non-empty 'coexist_pattern' kwarg"
            )

        await self.init()
        now = _utcnow()
        # Holds extra detail fields that narrowed / coexist branches
        # populate (new claim id, original pattern, etc.) so the
        # responded audit event records the full transition.
        extra_detail: dict[str, Any] = {}
        async with aiosqlite.connect(self.path) as conn:
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
                        coexist_pattern=str(coexist_pattern),
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
        coexist_pattern: str,
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
                session_id, last_activity, coexists_with
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                requester_claim_id,
                requester_engineer,
                None,
                f"coexist via request {request_id}",
                "file",
                coexist_pattern,
                "soft",
                now,
                requester_expires_at,
                holder["repo"],
                requester_session_id,
                requester_last_activity,
                _json.dumps([holder_claim_id]),
            ),
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

        return {
            "coexist_pattern": coexist_pattern,
            "holder_claim_id": holder_claim_id,
            "requester_claim_id": requester_claim_id,
        }

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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await self._record_event_in_txn(
                conn,
                request_id=request_id,
                event_type=event_type,
                actor_engineer=actor_engineer,
                actor_session_id=actor_session_id,
                detail=detail,
            )
            await conn.commit()

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
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
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT * FROM claims ORDER BY datetime(created_at) DESC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            await conn.commit()
            return [dict(r) for r in rows]
