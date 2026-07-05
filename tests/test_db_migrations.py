from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import aiosqlite
import pytest

from coordination import db as db_module
from coordination.db import CURRENT_SCHEMA_VERSION, Database

# The SQLite migrations chain (incremental v1..vN upgrades, legacy detection,
# schema-version stamping) is a SQLite-internal mechanism. The Postgres backend
# bootstraps a single consolidated schema instead (design Section 7.4), so these
# tests are not meaningful on PG and are skipped there by the conftest hook.
pytestmark = pytest.mark.sqlite_only


async def _fetch_one(path: Path, sql: str, args: tuple = ()) -> tuple | None:
    async with aiosqlite.connect(path) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchone()


async def _fetch_all(path: Path, sql: str, args: tuple = ()) -> list[tuple]:
    async with aiosqlite.connect(path) as conn:
        cur = await conn.execute(sql, args)
        return list(await cur.fetchall())


async def _table_exists(path: Path, name: str) -> bool:
    row = await _fetch_one(
        path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    )
    return row is not None


async def test_fresh_db_gets_current_version(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.sqlite"
    db = Database(db_path)
    await db.init()

    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] == CURRENT_SCHEMA_VERSION

    for table in ("claims", "conflict_log", "ownership_config"):
        assert await _table_exists(db_path, table), f"missing table {table}"


async def test_existing_db_without_version_table_gets_stamped(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    # Pre-create a DB with the old SCHEMA (no schema_version table) plus some data.
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(db_module.SCHEMA)
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern, severity,
                created_at, expires_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy-1",
                "alice",
                "alice/legacy",
                "legacy row",
                "file",
                "src/legacy/**",
                "soft",
                "2026-01-01T00:00:00Z",
                "2099-01-01T00:00:00Z",
            ),
        )
        await conn.commit()

    # Sanity: confirm no schema_version table yet.
    assert not await _table_exists(db_path, "schema_version")

    db = Database(db_path)
    await db.init()

    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] == 1

    # Existing data should still be queryable.
    row = await _fetch_one(db_path, "SELECT id, engineer FROM claims WHERE id = 'legacy-1'")
    assert row is not None
    assert row[0] == "legacy-1"
    assert row[1] == "alice"


async def test_init_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "idem.sqlite"
    db = Database(db_path)
    await db.init()
    await db.init()

    rows = await _fetch_all(db_path, "SELECT version FROM schema_version")
    assert len(rows) == 1
    assert rows[0][0] == CURRENT_SCHEMA_VERSION


async def test_future_version_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "future.sqlite"
    db = Database(db_path)
    await db.init()

    # Force a higher version.
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE schema_version SET version = ?",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        await conn.commit()

    db2 = Database(db_path)
    with pytest.raises((RuntimeError, ValueError)) as exc_info:
        await db2.init()
    assert "newer version" in str(exc_info.value).lower()


async def test_migration_registry_runs_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic migrations beyond the current schema version must apply
    in registry order. Use versions one and two beyond CURRENT_SCHEMA_VERSION
    so we don't collide with real migrations -- adding a duplicate (2, ...)
    entry would re-apply the real v2 SQL during the test."""
    db_path = tmp_path / "multi.sqlite"
    next_v1 = db_module.CURRENT_SCHEMA_VERSION + 1
    next_v2 = db_module.CURRENT_SCHEMA_VERSION + 2

    extra_migrations = [
        (next_v1, f"CREATE TABLE mig_v{next_v1} (id INTEGER PRIMARY KEY);"),
        (next_v2, f"CREATE TABLE mig_v{next_v2} (id INTEGER PRIMARY KEY);"),
    ]

    real_registry = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", real_registry + extra_migrations)
    monkeypatch.setattr(db_module, "CURRENT_SCHEMA_VERSION", next_v2)

    db = Database(db_path)
    await db.init()

    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] == next_v2

    assert await _table_exists(db_path, f"mig_v{next_v1}")
    assert await _table_exists(db_path, f"mig_v{next_v2}")


async def test_partial_migration_failure_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "atomic.sqlite"

    # Bad migration: creates a table, then errors on invalid SQL.
    bad_sql = (
        "CREATE TABLE mig_v2_bad (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;"
    )

    real_registry = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", real_registry + [(2, bad_sql)])
    monkeypatch.setattr(db_module, "CURRENT_SCHEMA_VERSION", 2)

    db = Database(db_path)
    with pytest.raises(Exception):
        await db.init()

    # Version must NOT have advanced past 1.
    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    # Either no row (fresh DB didn't stamp) or version is 1.
    if row is not None:
        assert row[0] == 1

    # The partial table from the failed migration must not be visible.
    assert not await _table_exists(db_path, "mig_v2_bad")


async def test_init_creates_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "db.sqlite"
    assert not db_path.parent.exists()
    db = Database(db_path)
    await db.init()
    assert db_path.exists()

    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] == CURRENT_SCHEMA_VERSION


async def test_wal_mode_enabled_after_init(tmp_path: Path) -> None:
    db_path = tmp_path / "wal.sqlite"
    db = Database(db_path)
    await db.init()

    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute("PRAGMA journal_mode")
        row = await cur.fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"


async def test_foreign_keys_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "fk.sqlite"
    db = Database(db_path)
    await db.init()

    # foreign_keys is a per-connection pragma; Database's connections set it ON.
    # Verify by attempting a conflict_log insert referencing a non-existent claim.
    import aiosqlite as _aio

    async with _aio.connect(db_path) as conn:
        await conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(Exception):
            await conn.execute(
                """
                INSERT INTO conflict_log (id, claim_id, attempted_by, attempted_pattern, resolution, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("c1", "does-not-exist", "alice", "p", None, "2026-01-01T00:00:00Z"),
            )
            await conn.commit()


async def test_two_database_instances_same_path(tmp_path: Path) -> None:
    db_path = tmp_path / "twoinst.sqlite"
    db1 = Database(db_path)
    db2 = Database(db_path)

    await db1.init()
    await db2.init()

    rows = await _fetch_all(db_path, "SELECT version FROM schema_version")
    assert len(rows) == 1
    assert rows[0][0] == CURRENT_SCHEMA_VERSION


async def test_v19_adds_repo_column_to_engineer_tokens(tmp_path: Path) -> None:
    """Migration v19 introduces a nullable `repo` column on engineer_tokens
    (issue #30 slice 2/3). NULL = unscoped token; the migration is inert."""
    db_path = tmp_path / "v19.sqlite"
    db = Database(db_path)
    await db.init()

    rows = await _fetch_all(db_path, "PRAGMA table_info(engineer_tokens)")
    cols = {r[1] for r in rows}  # column name is index 1
    assert "repo" in cols, f"engineer_tokens missing repo column; saw: {cols}"

    # Nullable: notnull flag (index 3) must be 0 so existing tokens are NULL.
    repo_row = next(r for r in rows if r[1] == "repo")
    assert repo_row[3] == 0, "repo column must be nullable (NULL = unscoped)"


async def test_v2_adds_repo_column_to_claims(tmp_path: Path) -> None:
    """Migration v2 introduces a nullable `repo` column on claims."""
    db_path = tmp_path / "v2.sqlite"
    db = Database(db_path)
    await db.init()

    # Inspect the column list via PRAGMA table_info.
    rows = await _fetch_all(db_path, "PRAGMA table_info(claims)")
    cols = {r[1] for r in rows}  # column name is index 1
    assert "repo" in cols, f"claims table missing repo column; saw: {cols}"

    # Verify the column is nullable: the notnull flag (index 3) must be 0.
    repo_row = next(r for r in rows if r[1] == "repo")
    assert repo_row[3] == 0, "repo column must be nullable for backfill"


async def test_v1_to_v2_preserves_existing_claims_with_null_repo(tmp_path: Path) -> None:
    """A pre-v2 DB with claims must migrate cleanly: rows are preserved,
    repo column appears, and old rows get repo=NULL by default."""
    db_path = tmp_path / "legacy_to_v2.sqlite"

    # Pre-create a v1 database with a claim row but no repo column.
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            CREATE TABLE claims (
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
            CREATE TABLE conflict_log (
                id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                attempted_by TEXT NOT NULL,
                attempted_pattern TEXT NOT NULL,
                resolution TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (claim_id) REFERENCES claims(id)
            );
            CREATE TABLE ownership_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                yaml_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 1);
            """
        )
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern,
                severity, created_at, expires_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "legacy-claim-1",
                "alice",
                "alice/feature",
                "pre-v2 claim",
                "file",
                "src/legacy.py",
                "soft",
                "2026-01-01T00:00:00Z",
                "2099-01-01T00:00:00Z",
            ),
        )
        await conn.commit()

    db = Database(db_path)
    await db.init()

    # Schema must be at v2 (or higher) now.
    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] >= 2

    # The legacy row must still exist and have repo=NULL.
    row = await _fetch_one(
        db_path, "SELECT id, repo FROM claims WHERE id = 'legacy-claim-1'"
    )
    assert row is not None
    assert row[0] == "legacy-claim-1"
    assert row[1] is None


async def test_v2_allows_inserting_claim_with_repo(tmp_path: Path) -> None:
    """After v2, new claim inserts can include a repo value."""
    db_path = tmp_path / "v2_insert.sqlite"
    db = Database(db_path)
    await db.init()

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern,
                severity, created_at, expires_at, released_at, repo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                "new-claim-1",
                "alice",
                None,
                "first repo-tagged claim",
                "file",
                "src/foo.py",
                "soft",
                "2026-05-01T12:00:00Z",
                "2026-05-01T16:00:00Z",
                "amittell/coord",
            ),
        )
        await conn.commit()

    row = await _fetch_one(
        db_path, "SELECT repo FROM claims WHERE id = 'new-claim-1'"
    )
    assert row is not None
    assert row[0] == "amittell/coord"


async def test_init_sets_busy_timeout_pragma(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """init() must raise the connection's busy_timeout to at least 10000 ms
    so that transient contention is absorbed rather than surfacing BUSY
    errors to callers. We verify by wrapping _configure_sqlite and
    reading busy_timeout immediately after it runs."""
    db_path = tmp_path / "busy_pragma.sqlite"

    seen: list[int] = []
    real_configure = db_module._configure_sqlite

    async def probe_configure(conn) -> None:  # type: ignore[no-untyped-def]
        await real_configure(conn)
        cur = await conn.execute("PRAGMA busy_timeout")
        row = await cur.fetchone()
        if row is not None:
            seen.append(int(row[0]))

    monkeypatch.setattr(db_module, "_configure_sqlite", probe_configure)

    db = Database(db_path)
    await db.init()

    assert seen, "probe did not capture any busy_timeout reading"
    assert seen[0] >= 10000, (
        f"expected busy_timeout >= 10000ms after _configure_sqlite; saw {seen[0]}"
    )

    # Silence unused imports if other tests evolve.
    _ = (sqlite3, threading, time, asyncio)


async def test_v3_adds_session_id_column_to_claims(tmp_path: Path) -> None:
    """Migration v3 introduces a nullable `session_id` column on claims
    so the conflict check can self-exclude a session's prior claims even
    when subagent engineer names differ."""
    db_path = tmp_path / "v3.sqlite"
    db = Database(db_path)
    await db.init()

    rows = await _fetch_all(db_path, "PRAGMA table_info(claims)")
    cols = {r[1] for r in rows}
    assert "session_id" in cols, (
        f"claims table missing session_id column; saw: {cols}"
    )
    sid_row = next(r for r in rows if r[1] == "session_id")
    assert sid_row[3] == 0, "session_id column must be nullable for back-compat"


async def test_v4_adds_last_activity_column_and_pending_columns(
    tmp_path: Path,
) -> None:
    """Migration v4 introduces:
    - claims.last_activity TEXT NULL: idle-expiration timestamp,
      bumped on every coord call from the session.
    - conflict_log.attempted_session_id TEXT NULL: lets the holder
      see which session tried to claim their scope.
    Both are nullable for backfill safety.
    """
    db_path = tmp_path / "v4.sqlite"
    db = Database(db_path)
    await db.init()

    rows = await _fetch_all(db_path, "PRAGMA table_info(claims)")
    cols = {r[1] for r in rows}
    assert "last_activity" in cols, (
        f"claims missing last_activity column; saw: {cols}"
    )
    last_activity = next(r for r in rows if r[1] == "last_activity")
    assert last_activity[3] == 0, "last_activity must be nullable"

    rows = await _fetch_all(db_path, "PRAGMA table_info(conflict_log)")
    cols = {r[1] for r in rows}
    assert "attempted_session_id" in cols, (
        f"conflict_log missing attempted_session_id; saw: {cols}"
    )


async def test_v2_to_v3_preserves_existing_claims_with_null_session(
    tmp_path: Path,
) -> None:
    """A v2 DB with claim rows must migrate cleanly to v3 with old rows
    keeping session_id=NULL."""
    db_path = tmp_path / "v2_to_v3.sqlite"

    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            CREATE TABLE claims (
                id TEXT PRIMARY KEY,
                engineer TEXT NOT NULL,
                branch TEXT,
                description TEXT,
                claim_type TEXT NOT NULL,
                pattern TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'soft',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                repo TEXT
            );
            CREATE TABLE conflict_log (
                id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                attempted_by TEXT NOT NULL,
                attempted_pattern TEXT NOT NULL,
                resolution TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (claim_id) REFERENCES claims(id)
            );
            CREATE TABLE ownership_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                yaml_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 2);
            """
        )
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern,
                severity, created_at, expires_at, released_at, repo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                "v2-claim-1",
                "alice",
                None,
                "v2-era claim",
                "file",
                "src/x.py",
                "soft",
                "2026-04-01T00:00:00Z",
                "2099-01-01T00:00:00Z",
                "amittell/coord",
            ),
        )
        await conn.commit()

    db = Database(db_path)
    await db.init()

    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] >= 3

    row = await _fetch_one(
        db_path,
        "SELECT id, repo, session_id FROM claims WHERE id = 'v2-claim-1'",
    )
    assert row is not None
    assert row == ("v2-claim-1", "amittell/coord", None)


async def test_v5_introduces_requests_and_request_events_tables(
    tmp_path: Path,
) -> None:
    """v5 adds first-class release-request tracking. The `requests`
    table holds the current state per request (pending / approved /
    denied / expired / resolved) and the `request_events` table is an
    append-only audit log of every transition. Splitting current state
    from the immutable event stream keeps the operator timeline
    queryable without modifying request rows after creation."""
    db_path = tmp_path / "v5.sqlite"
    db = Database(db_path)
    await db.init()

    assert await _table_exists(db_path, "requests")
    assert await _table_exists(db_path, "request_events")

    cols = {r[1] for r in await _fetch_all(db_path, "PRAGMA table_info(requests)")}
    for required in (
        "id",
        "claim_id",
        "requester_engineer",
        "requester_session_id",
        "requested_pattern",
        "reason",
        "urgency",
        "decision",
        "decided_at",
        "decided_by_engineer",
        "decided_by_session_id",
        "note",
        "original_expires_at",
        "shortened_expires_at",
        "created_at",
    ):
        assert required in cols, (
            f"requests table missing column {required!r}; saw: {cols}"
        )

    cols = {
        r[1] for r in await _fetch_all(db_path, "PRAGMA table_info(request_events)")
    }
    for required in (
        "id",
        "request_id",
        "event_type",
        "actor_engineer",
        "actor_session_id",
        "detail",
        "created_at",
    ):
        assert required in cols, (
            f"request_events table missing column {required!r}; saw: {cols}"
        )


async def test_v6_adds_requested_scope_to_requests(tmp_path: Path) -> None:
    """v6 adds a nullable `requested_scope` column on requests so the
    operator timeline records what the requester actually asked for vs
    what the holder claimed (the holder's pattern may be broader than
    the scope the requester needs)."""
    db_path = tmp_path / "v6_requested_scope.sqlite"
    db = Database(db_path)
    await db.init()

    rows = await _fetch_all(db_path, "PRAGMA table_info(requests)")
    cols = {r[1] for r in rows}
    assert "requested_scope" in cols, (
        f"requests table missing requested_scope column; saw: {cols}"
    )
    scope_row = next(r for r in rows if r[1] == "requested_scope")
    assert scope_row[3] == 0, "requested_scope must be nullable for backfill"


async def test_v6_adds_coexists_with_to_claims(tmp_path: Path) -> None:
    """v6 adds a nullable `coexists_with` column on claims that stores a
    JSON array of partner claim ids. NULL means no coexisting partners
    (the default for every existing claim and every fresh claim that
    isn't a coexist sibling)."""
    db_path = tmp_path / "v6_coexists_with.sqlite"
    db = Database(db_path)
    await db.init()

    rows = await _fetch_all(db_path, "PRAGMA table_info(claims)")
    cols = {r[1] for r in rows}
    assert "coexists_with" in cols, (
        f"claims table missing coexists_with column; saw: {cols}"
    )
    coex_row = next(r for r in rows if r[1] == "coexists_with")
    assert coex_row[3] == 0, "coexists_with must be nullable for backfill"


async def test_v15_adds_token_lifecycle_columns(tmp_path: Path) -> None:
    """v15 adds the token lifecycle columns to engineer_tokens. The
    expiry/rotation columns must be nullable (NULL = legacy semantics:
    no expiry, not rotated) and request_count must default to 0 so
    pre-v15 rows read as never-counted rather than NULL."""
    db_path = tmp_path / "v15_token_lifecycle.sqlite"
    db = Database(db_path)
    await db.init()

    rows = await _fetch_all(db_path, "PRAGMA table_info(engineer_tokens)")
    cols = {r[1] for r in rows}
    for expected in (
        "expires_at",
        "rotated_from",
        "rotation_grace_until",
        "request_count",
        "last_source_ip",
        "last_user_agent",
    ):
        assert expected in cols, (
            f"engineer_tokens missing {expected}; saw: {cols}"
        )
    for nullable in ("expires_at", "rotated_from", "rotation_grace_until"):
        row = next(r for r in rows if r[1] == nullable)
        assert row[3] == 0, f"{nullable} must be nullable for backfill"
    count_row = next(r for r in rows if r[1] == "request_count")
    assert count_row[4] == "0", "request_count must default to 0"


async def test_v15_preserves_existing_token_rows(tmp_path: Path) -> None:
    """A v14 database with live tokens upgrades to v15 with every row
    intact and legacy semantics (no expiry, zero request count)."""
    db_path = tmp_path / "v15_upgrade.sqlite"

    # Build a genuine v14 database: replay the migration registry up
    # to and including v14, stamp it, and insert a token row with the
    # v14 column set only.
    import aiosqlite

    from coordination import db as db_module

    async with aiosqlite.connect(db_path) as conn:
        for version, sql in db_module.MIGRATIONS:
            if version > 14:
                break
            for stmt in db_module._split_sql_statements(sql):
                await conn.execute(stmt)
        await conn.execute(db_module.SCHEMA_VERSION_TABLE_SQL)
        await conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, 14)"
        )
        await conn.execute(
            "INSERT INTO engineer_tokens "
            "(id, engineer, token_sha256, description, created_at) "
            "VALUES ('tok-1', 'alex/claude/main', 'deadbeef', 'laptop', "
            "'2026-06-01T00:00:00Z')"
        )
        await conn.commit()

    db = Database(db_path)
    await db.init()

    version_row = await _fetch_one(
        db_path, "SELECT version FROM schema_version WHERE id = 1"
    )
    assert version_row[0] == db_module.CURRENT_SCHEMA_VERSION

    row = await _fetch_one(
        db_path,
        "SELECT engineer, expires_at, rotated_from, rotation_grace_until, "
        "request_count FROM engineer_tokens WHERE id = 'tok-1'",
    )
    assert row[0] == "alex/claude/main"
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
    assert row[4] == 0

    # And the upgraded row still authenticates through the v15 code.
    hit = await db.lookup_engineer_token("deadbeef")
    assert hit is not None
    assert hit["status"] == "ok"


async def test_v16_adds_span_columns_and_wave2_tables(tmp_path: Path) -> None:
    """v16 (v0.31) extends claim_symbols with nullable span columns
    (1-based lines, 0-based columns, NULL = no span resolved) plus a
    resolved_by provenance column, and creates the wave-2 tables
    claim_symbol_callsites / claim_symbol_renames so the whole v0.31
    feature needs exactly one database upgrade."""
    db_path = tmp_path / "v16_spans.sqlite"
    db = Database(db_path)
    await db.init()

    rows = await _fetch_all(db_path, "PRAGMA table_info(claim_symbols)")
    cols = {r[1] for r in rows}
    for expected in (
        "start_line",
        "start_col",
        "end_line",
        "end_col",
        "resolved_by",
    ):
        assert expected in cols, f"claim_symbols missing {expected}; saw: {cols}"
        col_row = next(r for r in rows if r[1] == expected)
        assert col_row[3] == 0, f"{expected} must be nullable for backfill"

    assert await _table_exists(db_path, "claim_symbol_callsites")
    rows = await _fetch_all(
        db_path, "PRAGMA table_info(claim_symbol_callsites)"
    )
    cols = {r[1] for r in rows}
    for required in (
        "id",
        "claim_id",
        "file_path",
        "line",
        "character",
        "symbol_path",
        "created_at",
    ):
        assert required in cols, (
            f"claim_symbol_callsites missing {required!r}; saw: {cols}"
        )
    # line is mandatory; character / symbol_path are nullable (some
    # servers report line-only locations).
    assert next(r for r in rows if r[1] == "line")[3] == 1
    assert next(r for r in rows if r[1] == "character")[3] == 0
    assert next(r for r in rows if r[1] == "symbol_path")[3] == 0

    assert await _table_exists(db_path, "claim_symbol_renames")
    rows = await _fetch_all(db_path, "PRAGMA table_info(claim_symbol_renames)")
    cols = {r[1] for r in rows}
    for required in (
        "id",
        "claim_id",
        "file_path",
        "old_symbol_name",
        "new_symbol_name",
        "old_symbol_path",
        "new_symbol_path",
        "old_start_line",
        "old_end_line",
        "new_start_line",
        "new_end_line",
        "detected_at",
        "resolved_by",
    ):
        assert required in cols, (
            f"claim_symbol_renames missing {required!r}; saw: {cols}"
        )
    assert next(r for r in rows if r[1] == "old_symbol_name")[3] == 1
    assert next(r for r in rows if r[1] == "old_symbol_path")[3] == 0
    assert next(r for r in rows if r[1] == "resolved_by")[3] == 1

    # Indexes the wave-2 queries depend on.
    idx_rows = await _fetch_all(
        db_path,
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name IN ('claim_symbol_callsites', 'claim_symbol_renames')",
    )
    idx_names = {r[0] for r in idx_rows}
    for expected_idx in (
        "idx_claim_symbol_callsites_claim_id",
        "idx_claim_symbol_callsites_file_line",
        "idx_claim_symbol_renames_claim_id",
    ):
        assert expected_idx in idx_names, (
            f"missing index {expected_idx}; saw: {idx_names}"
        )


async def test_v16_preserves_existing_claim_symbols_with_null_spans(
    tmp_path: Path,
) -> None:
    """A v15 database with live claim_symbols rows upgrades to v16 with
    every row intact and NULL spans (the documented meaning: pre-v16
    row, nobody ever resolved a span for it)."""
    db_path = tmp_path / "v16_upgrade.sqlite"

    # Build a genuine v15 database: replay the migration registry up
    # to and including v15, stamp it, and insert a symbol row with the
    # v15 column set only.
    async with aiosqlite.connect(db_path) as conn:
        for version, sql in db_module.MIGRATIONS:
            if version > 15:
                break
            for stmt in db_module._split_sql_statements(sql):
                await conn.execute(stmt)
        await conn.execute(db_module.SCHEMA_VERSION_TABLE_SQL)
        await conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, 15)"
        )
        await conn.execute(
            "INSERT INTO claims (id, engineer, branch, description, "
            "claim_type, pattern, severity, created_at, expires_at, "
            "released_at, scope_type) VALUES ('claim-1', 'alice', NULL, "
            "'v15-era symbol claim', 'file', 'src/auth/login.ts', 'soft', "
            "'2026-06-01T00:00:00Z', '2099-01-01T00:00:00Z', NULL, 'symbol')"
        )
        await conn.execute(
            "INSERT INTO claim_symbols (id, claim_id, file_path, "
            "symbol_name, symbol_kind, parent_symbol) VALUES "
            "('sym-1', 'claim-1', 'src/auth/login.ts', 'handleLogin', "
            "'function', NULL)"
        )
        await conn.commit()

    db = Database(db_path)
    await db.init()

    version_row = await _fetch_one(
        db_path, "SELECT version FROM schema_version WHERE id = 1"
    )
    assert version_row is not None
    assert version_row[0] == db_module.CURRENT_SCHEMA_VERSION

    row = await _fetch_one(
        db_path,
        "SELECT symbol_name, start_line, start_col, end_line, end_col, "
        "resolved_by FROM claim_symbols WHERE id = 'sym-1'",
    )
    assert row is not None
    assert row[0] == "handleLogin"
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
    assert row[4] is None
    assert row[5] is None

    # And the upgraded row still flows through the v16 read path.
    symbols = await db.get_claim_symbols("claim-1")
    assert len(symbols) == 1
    assert symbols[0]["symbol_name"] == "handleLogin"
    assert symbols[0]["start_line"] is None
    assert symbols[0]["resolved_by"] is None
