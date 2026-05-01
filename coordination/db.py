from __future__ import annotations

import asyncio
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


CURRENT_SCHEMA_VERSION = 2

# Migration registry: list of (version, upgrade_sql) tuples applied in order.
# Entry for version N is the SQL that upgrades a DB from version N-1 to N.
# Version 1 creates the initial core schema; future versions append here.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA),
    # v2: capture which repo a claim came from. Nullable for backfill: existing
    # claims pre-dating this column show up under "(unattributed)" in repo views.
    (2, "ALTER TABLE claims ADD COLUMN repo TEXT;"),
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
    ) -> list[str]:
        now = _utcnow()
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            for cid, claim_type, pattern, severity, expires_at in items:
                await conn.execute(
                    """
                    INSERT INTO claims (
                        id, engineer, branch, description, claim_type, pattern, severity,
                        created_at, expires_at, released_at, repo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
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
                    ),
                )
            await conn.commit()
        return [i[0] for i in items]

    async def release_claims(self, claim_ids: list[str], engineer: str | None = None) -> int:
        now = _utcnow()
        await self.init()
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
                n += cur.rowcount or 0
            await conn.commit()
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

    async def expire_stale_claims(self) -> int:
        now = _utcnow()
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            cur = await conn.execute(
                "SELECT id, expires_at FROM claims WHERE released_at IS NULL",
            )
            rows = await cur.fetchall()
            await conn.commit()

        to_close: list[str] = []
        cutoff = datetime.now(UTC)
        for r in rows:
            exp = datetime.fromisoformat(str(r["expires_at"]).replace("Z", "+00:00"))
            if exp <= cutoff:
                to_close.append(str(r["id"]))
        if not to_close:
            return 0

        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            n = 0
            for cid in to_close:
                cur = await conn.execute(
                    "UPDATE claims SET released_at = ? WHERE id = ? AND released_at IS NULL",
                    (now, cid),
                )
                n += cur.rowcount or 0
            await conn.commit()
        return n

    async def log_conflict(
        self,
        *,
        claim_id: str,
        attempted_by: str,
        attempted_pattern: str,
        resolution: str | None,
    ) -> None:
        await self.init()
        async with aiosqlite.connect(self.path) as conn:
            conn.row_factory = aiosqlite.Row
            await _configure_sqlite(conn)
            await conn.execute(
                """
                INSERT INTO conflict_log (id, claim_id, attempted_by, attempted_pattern, resolution, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), claim_id, attempted_by, attempted_pattern, resolution, _utcnow()),
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

        return [
            {
                "repo": r["repo"],
                "last_activity": r["last_activity"],
                "claims_24h": int(r["claims_in_window"] or 0),
                "engineers_24h": int(r["engineers_in_window"] or 0),
                "active_claims": int(r["active_claims"] or 0),
            }
            for r in rows
        ]

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
