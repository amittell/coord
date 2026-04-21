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
    db_path = tmp_path / "multi.sqlite"

    extra_migrations = [
        (2, "CREATE TABLE mig_v2 (id INTEGER PRIMARY KEY);"),
        (3, "CREATE TABLE mig_v3 (id INTEGER PRIMARY KEY);"),
    ]

    real_registry = list(db_module.MIGRATIONS)
    monkeypatch.setattr(db_module, "MIGRATIONS", real_registry + extra_migrations)
    monkeypatch.setattr(db_module, "CURRENT_SCHEMA_VERSION", 3)

    db = Database(db_path)
    await db.init()

    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] == 3

    assert await _table_exists(db_path, "mig_v2")
    assert await _table_exists(db_path, "mig_v3")


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
