from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
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


async def test_two_async_init_calls_same_path(tmp_path: Path) -> None:
    """Two Database instances on the same path calling init() concurrently
    from the same event loop must both succeed and leave exactly one
    schema_version row at the current version."""
    db_path = tmp_path / "async_concurrent.sqlite"
    db1 = Database(db_path)
    db2 = Database(db_path)

    await asyncio.gather(db1.init(), db2.init())

    rows = await _fetch_all(db_path, "SELECT version FROM schema_version")
    assert len(rows) == 1
    assert rows[0][0] == CURRENT_SCHEMA_VERSION


def _write_init_script(script_path: Path, db_path: Path) -> None:
    """Write a small standalone Python script that opens the given DB path
    and calls Database.init(), then prints the resulting schema_version."""
    src = textwrap.dedent(
        f"""
        import asyncio
        import sys
        from pathlib import Path

        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})

        import aiosqlite
        from coordination.db import Database


        async def main() -> int:
            db = Database(Path({str(db_path)!r}))
            await db.init()
            async with aiosqlite.connect({str(db_path)!r}) as conn:
                cur = await conn.execute("SELECT version FROM schema_version")
                row = await cur.fetchone()
            assert row is not None
            print(f"version={{row[0]}}")
            return 0


        if __name__ == "__main__":
            sys.exit(asyncio.run(main()))
        """
    )
    script_path.write_text(src)


def test_two_subprocess_init_calls_same_path(tmp_path: Path) -> None:
    """Two independent Python processes calling Database.init() on the same
    SQLite file must both succeed, report version=1, and leave no duplicate
    or corrupt state."""
    db_path = tmp_path / "subproc.sqlite"
    script = tmp_path / "do_init.py"
    _write_init_script(script, db_path)

    env = os.environ.copy()
    # Launch both procs before either finishes so they race on init().
    p1 = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    p2 = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    out1, err1 = p1.communicate(timeout=15)
    out2, err2 = p2.communicate(timeout=15)

    assert p1.returncode == 0, (
        f"proc1 exited {p1.returncode}; stdout={out1!r} stderr={err1!r}"
    )
    assert p2.returncode == 0, (
        f"proc2 exited {p2.returncode}; stdout={out2!r} stderr={err2!r}"
    )
    assert b"version=1" in out1, f"proc1 stdout={out1!r}"
    assert b"version=1" in out2, f"proc2 stdout={out2!r}"

    # Final state must be clean.
    async def _check() -> None:
        rows = await _fetch_all(db_path, "SELECT version FROM schema_version")
        assert len(rows) == 1
        assert rows[0][0] == 1
        # Core tables must exist exactly once.
        dup = await _fetch_all(
            db_path,
            "SELECT name, COUNT(*) FROM sqlite_master "
            "WHERE type='table' GROUP BY name HAVING COUNT(*) > 1",
        )
        assert dup == []

    asyncio.run(_check())


async def test_concurrent_init_does_not_run_migration_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a custom migration whose SQL is instrumented to append a line
    to a counter file every time it is executed, two concurrent init()
    calls must apply migration 2 exactly once."""
    db_path = tmp_path / "counter.sqlite"
    counter_path = tmp_path / "counter.log"

    # Seed the DB at version 1 so only the new migration 2 is pending.
    seed = Database(db_path)
    await seed.init()

    # Patch _split_sql_statements so that when it is called on our marker
    # migration SQL, it records an execution. This catches every code path
    # that actually tries to apply the migration's statements (i.e. every
    # real execution, not version checks).
    real_split = db_module._split_sql_statements

    marker = "CREATE TABLE counter_mig (id INTEGER PRIMARY KEY);"

    def counting_split(script: str) -> list[str]:
        if script == marker:
            with open(counter_path, "a") as fh:
                fh.write("ran\n")
        return real_split(script)

    monkeypatch.setattr(
        db_module,
        "MIGRATIONS",
        list(db_module.MIGRATIONS) + [(2, marker)],
    )
    monkeypatch.setattr(db_module, "CURRENT_SCHEMA_VERSION", 2)
    monkeypatch.setattr(db_module, "_split_sql_statements", counting_split)

    db1 = Database(db_path)
    db2 = Database(db_path)
    await asyncio.gather(db1.init(), db2.init())

    # Migration 2 must have been split (i.e. applied) exactly once.
    text = counter_path.read_text() if counter_path.exists() else ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 1, (
        f"expected migration 2 to run exactly once, got lines={lines!r}"
    )

    # And the final version is 2.
    row = await _fetch_one(db_path, "SELECT version FROM schema_version")
    assert row is not None
    assert row[0] == 2


async def test_future_version_raises_under_concurrency(tmp_path: Path) -> None:
    """Two concurrent init() calls against a DB stamped at a higher-than-known
    version must both raise RuntimeError with 'newer version' in the message,
    and must not lose or alter the stored future version."""
    db_path = tmp_path / "future_concurrent.sqlite"

    # Seed schema_version=999 directly.
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(db_module.SCHEMA_VERSION_TABLE_SQL)
        await conn.execute(
            "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, 999)"
        )
        await conn.commit()

    db1 = Database(db_path)
    db2 = Database(db_path)

    results = await asyncio.gather(
        db1.init(), db2.init(), return_exceptions=True
    )
    for r in results:
        assert isinstance(r, RuntimeError), f"expected RuntimeError, got {r!r}"
        assert "newer version" in str(r).lower()

    # Stored version must still be 999.
    rows = await _fetch_all(db_path, "SELECT version FROM schema_version")
    assert len(rows) == 1
    assert rows[0][0] == 999
