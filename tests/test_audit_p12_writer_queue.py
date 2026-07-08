"""v0.45 SQLite writer-queue coverage: failure paths, serialization, metrics.

The production default is ``sqlite_writer_queue = True`` (config.py), yet the
only pre-existing test that passed ``writer_queue=True`` asserted the flag is
forced OFF for a Postgres DSN. These tests construct ``Database(path,
writer_queue=True)`` on plain SQLite and pin the queue's own mechanics:

- poison recovery: a write whose rollback also fails drops the shared writer
  connection (``_drop_writer``) so the next write reopens a fresh one instead
  of wedging every future write on a broken handle -- both for the
  single-statement ``_write`` seam and the ``transaction()`` unit-of-work;
- ``aclose()`` racing an in-flight write waits on ``_writer_lock`` so the
  write commits before the connection closes underneath it;
- N concurrent writes serialize onto the ONE persistent writer connection,
  all land, and the ``sqlite_writes_total`` / ``sqlite_writer_wait_seconds_total``
  metrics account for them;
- a write issued inside an explicit ``transaction()`` reuses the bound
  connection (no re-acquire of the non-reentrant ``_writer_lock``, which
  would deadlock) and defers its commit to the transaction exit.

SQLite-internal by definition: PostgresStore forces ``_writer_queue`` off, so
the whole module is ``sqlite_only``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from coordination import metrics
from coordination.db import Database

pytestmark = pytest.mark.sqlite_only


def _counter_value(counter: metrics.Counter) -> float:
    """Current sample of an unlabelled counter (0.0 when never incremented)."""
    return counter.values.get((), 0.0)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    """A queue-enabled SQLite Database, guaranteed not to dispatch to the
    Postgres backend even when the suite runs with COORD_DATABASE_URL set."""
    monkeypatch.delenv("COORD_DATABASE_URL", raising=False)
    return Database(tmp_path / "db.sqlite", writer_queue=True)


async def _committed_rows(db: Database, table: str) -> list[tuple]:
    """Read via an INDEPENDENT connection so only committed data is visible
    (WAL readers never see another connection's open write transaction)."""
    async with aiosqlite.connect(db.path) as conn:
        cur = await conn.execute(f"SELECT * FROM {table} ORDER BY 1")
        return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_write_poison_drops_writer_and_next_write_recovers(
    db: Database,
) -> None:
    """A failed write whose rollback ALSO fails (poisoned handle) must drop
    the shared writer connection; the next write reopens a fresh one and
    succeeds instead of wedging on the broken handle."""
    await db.init()
    async with db._write() as conn:
        await conn.execute("CREATE TABLE audit_wq (v TEXT)")
    first_conn = db._writer_conn
    assert first_conn is not None, "writer connection must persist across ops"

    with pytest.raises(RuntimeError, match="boom"):
        async with db._write() as conn:
            # Simulate a handle that dies mid-write: closing it makes the
            # rollback in _write's except path raise too, which is exactly
            # the poison-recovery branch (_drop_writer under _writer_lock).
            await conn.close()
            raise RuntimeError("boom")

    assert db._writer_conn is None, "poisoned writer must be dropped"
    assert not db._writer_lock.locked(), "lock must be released after poison"

    async with db._write() as conn:
        await conn.execute("INSERT INTO audit_wq (v) VALUES ('after')")
    assert db._writer_conn is not None, "recovery must reopen a writer"
    assert db._writer_conn is not first_conn, "must be a FRESH connection"
    assert await _committed_rows(db, "audit_wq") == [("after",)]


@pytest.mark.asyncio
async def test_transaction_poison_drops_writer_and_next_write_recovers(
    db: Database,
) -> None:
    """transaction() mirrors _write's poison recovery: a unit-of-work whose
    rollback fails drops the writer connection, releases the lock, and the
    next write path (touch_session_activity funnels through _write) reopens
    a fresh connection."""
    await db.init()

    with pytest.raises(RuntimeError, match="txn boom"):
        async with db.transaction() as conn:
            await conn.close()
            raise RuntimeError("txn boom")

    assert db._writer_conn is None, "poisoned writer must be dropped"
    assert not db._writer_lock.locked(), "lock must be released after poison"

    touched = await db.touch_session_activity("no-such-session")
    assert touched == 0
    assert db._writer_conn is not None, "recovery must reopen a writer"


@pytest.mark.asyncio
async def test_aclose_waits_for_inflight_write_then_closes(
    db: Database,
) -> None:
    """aclose() acquires _writer_lock, so an in-flight write finishes (and
    commits) before the connection is closed underneath it; afterwards the
    handle is gone."""
    await db.init()
    async with db._write() as conn:
        await conn.execute("CREATE TABLE audit_wq (v TEXT)")

    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_write() -> None:
        async with db._write() as conn:
            await conn.execute("INSERT INTO audit_wq (v) VALUES ('slow')")
            entered.set()
            await proceed.wait()

    write_task = asyncio.create_task(slow_write())
    await asyncio.wait_for(entered.wait(), timeout=5)

    close_task = asyncio.create_task(db.aclose())
    await asyncio.sleep(0.05)
    assert not close_task.done(), (
        "aclose must block on _writer_lock while a write is in flight"
    )
    assert db._writer_conn is not None, (
        "the in-flight write's connection must not be torn down early"
    )

    proceed.set()
    await asyncio.wait_for(write_task, timeout=5)
    await asyncio.wait_for(close_task, timeout=5)

    assert db._writer_conn is None, "aclose must drop the writer connection"
    assert await _committed_rows(db, "audit_wq") == [("slow",)], (
        "the in-flight write must have committed before the close"
    )


@pytest.mark.asyncio
async def test_concurrent_writes_serialize_on_one_connection_with_metrics(
    db: Database,
) -> None:
    """N concurrent writes all reuse the SINGLE persistent writer connection,
    all land committed, and both writer-queue metrics account for them."""
    await db.init()
    async with db._write() as conn:
        await conn.execute("CREATE TABLE audit_wq (v INTEGER)")
    writer = db._writer_conn
    assert writer is not None

    writes_before = _counter_value(metrics.sqlite_writes_total)
    wait_before = _counter_value(metrics.sqlite_writer_wait_seconds_total)

    async def write_one(i: int) -> None:
        async with db._write() as conn:
            assert conn is writer, (
                "queued writes must reuse the one persistent writer connection"
            )
            await conn.execute("INSERT INTO audit_wq (v) VALUES (?)", (i,))
            # Yield inside the critical section so the other writers pile up
            # on _writer_lock, exercising real contention.
            await asyncio.sleep(0)

    await asyncio.gather(*(write_one(i) for i in range(20)))

    rows = await _committed_rows(db, "audit_wq")
    assert [r[0] for r in rows] == list(range(20)), (
        f"all 20 concurrent writes must land exactly once; got {rows}"
    )
    assert _counter_value(metrics.sqlite_writes_total) >= writes_before + 20, (
        "each queued commit must increment sqlite_writes_total"
    )
    assert _counter_value(metrics.sqlite_writer_wait_seconds_total) > wait_before, (
        "lock-wait time must accumulate in sqlite_writer_wait_seconds_total"
    )


@pytest.mark.asyncio
async def test_write_inside_transaction_reuses_bound_conn_and_defers_commit(
    db: Database,
) -> None:
    """_write issued inside an explicit transaction() must take the bound-
    connection branch: same connection object, NO re-acquire of the non-
    reentrant _writer_lock (which would deadlock right here), and the commit
    deferred to the transaction exit."""
    await db.init()
    async with db._write() as conn:
        await conn.execute("CREATE TABLE audit_wq (v TEXT)")

    async with db.transaction() as txn_conn:
        async with db._write() as inner:
            assert inner is txn_conn, (
                "_write inside transaction() must reuse the bound connection"
            )
            await inner.execute("INSERT INTO audit_wq (v) VALUES ('inside')")
        assert db._writer_lock.locked(), (
            "the transaction must still hold the writer lock after the "
            "inner _write exits (the inner block must not release it)"
        )
        assert await _committed_rows(db, "audit_wq") == [], (
            "the inner write must NOT commit early -- commit belongs to the "
            "outer transaction"
        )

    assert not db._writer_lock.locked()
    assert await _committed_rows(db, "audit_wq") == [("inside",)]
