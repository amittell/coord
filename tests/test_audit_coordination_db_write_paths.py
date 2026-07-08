"""Audit fixes for the SQLite write paths:

- ``Database.init()`` is memoized (mirrors PostgresStore), so the
  BEGIN IMMEDIATE migration check no longer takes the write lock on
  every operation.
- With the writer queue OFF, ``transaction()`` opens with an explicit
  BEGIN IMMEDIATE so concurrent grant units-of-work serialize instead of
  reopening the double-grant TOCTOU, and ``_write`` checks the bound
  connection BEFORE the queue flag so a funnelled write inside a
  transaction cannot escape the unit-of-work on a second connection.
- ``touch_engineer_token`` rides the ``_write`` funnel and coalesces
  per-token: skipped touches accumulate (request_count is deferred, not
  dropped) and flush with the next write.

All of this is SQLite-internal mechanics (the PG store overrides
``init``/``transaction`` and is MVCC), hence ``sqlite_only``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from coordination import db as db_module
from coordination.db import Database

pytestmark = pytest.mark.sqlite_only


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _mk_claim(
    db: Database,
    *,
    engineer: str = "alice",
    pattern: str = "src/app.py",
    session_id: str | None = None,
) -> str:
    cid = str(uuid4())
    exp = _iso(datetime.now(UTC) + timedelta(hours=1))
    await db.insert_claims_batch(
        engineer=engineer,
        branch=None,
        description=None,
        items=[(cid, "file", pattern, "soft", exp)],
        session_id=session_id,
    )
    return cid


# --- init() memoization -------------------------------------------------


async def test_init_runs_migration_check_once_per_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    orig = db_module._migrate_to_current

    async def counting(conn) -> None:
        calls["n"] += 1
        await orig(conn)

    monkeypatch.setattr(db_module, "_migrate_to_current", counting)

    db = Database(tmp_path / "memo.sqlite")
    await db.list_active_claims_rows()
    await db.list_active_claims_rows()
    await db.expire_stale_claims()
    assert calls["n"] == 1, (
        "init() must be memoized: every extra run takes a BEGIN IMMEDIATE "
        f"write lock on a read path (ran {calls['n']} times)"
    )


async def test_init_memoization_is_per_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second instance on the same path still verifies the schema once
    (multi-process safety is _migrate_to_current's own job)."""
    calls = {"n": 0}
    orig = db_module._migrate_to_current

    async def counting(conn) -> None:
        calls["n"] += 1
        await orig(conn)

    monkeypatch.setattr(db_module, "_migrate_to_current", counting)

    path = tmp_path / "memo2.sqlite"
    db1 = Database(path)
    db2 = Database(path)
    await db1.init()
    await db2.init()
    await db1.init()
    await db2.init()
    assert calls["n"] == 2


async def test_concurrent_first_init_migrates_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    orig = db_module._migrate_to_current

    async def counting(conn) -> None:
        calls["n"] += 1
        await orig(conn)

    monkeypatch.setattr(db_module, "_migrate_to_current", counting)

    db = Database(tmp_path / "memo3.sqlite")
    await asyncio.gather(*(db.init() for _ in range(8)))
    assert calls["n"] == 1


# --- transaction() with the writer queue off ---------------------------


async def test_transaction_queue_off_takes_write_lock_up_front(
    tmp_path: Path,
) -> None:
    """With COORD_SQLITE_WRITER_QUEUE=false the unit-of-work must open
    with an explicit transaction (BEGIN IMMEDIATE) BEFORE the caller
    runs its overlap re-check, not lazily on the first write."""
    db = Database(tmp_path / "txn.sqlite", writer_queue=False)
    async with db.transaction() as conn:
        assert conn.in_transaction, (
            "transaction() must BEGIN IMMEDIATE up front with the writer "
            "queue off; deferred isolation reopens the double-grant TOCTOU"
        )


async def test_transaction_queue_off_serializes_concurrent_grants(
    tmp_path: Path,
) -> None:
    """Two concurrent read-then-insert units-of-work must serialize: the
    second's read has to observe the first's committed insert. Under the
    old deferred begin both read a pre-insert snapshot (both would pass
    a grant overlap re-check) -- the exact double-grant TOCTOU."""
    db = Database(tmp_path / "toctou.sqlite", writer_queue=False)
    await db.init()
    observed: list[int] = []

    async def unit() -> None:
        async with db.transaction() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM claims")
            row = await cur.fetchone()
            observed.append(int(row[0]))
            await asyncio.sleep(0.15)
            await conn.execute(
                "INSERT INTO claims (id, engineer, claim_type, pattern, "
                "severity, created_at, expires_at) "
                "VALUES (?, 'racer', 'file', 'src/hot.py', 'soft', ?, ?)",
                (
                    str(uuid4()),
                    _iso(datetime.now(UTC)),
                    _iso(datetime.now(UTC) + timedelta(hours=1)),
                ),
            )

    await asyncio.gather(unit(), unit())
    assert sorted(observed) == [0, 1], (
        "concurrent units-of-work read the same pre-insert snapshot "
        f"(observed counts {observed}); grants are not serialized"
    )


async def test_write_inside_transaction_queue_off_joins_unit_of_work(
    tmp_path: Path,
) -> None:
    """A ``_write``-based method (touch_session_activity) invoked inside
    ``transaction()`` with the queue off must reuse the bound connection:
    rolling back the transaction must roll the write back too. The old
    branch order opened (and committed on) a separate connection."""
    db = Database(tmp_path / "bound.sqlite", writer_queue=False)
    cid = await _mk_claim(db, session_id="sess-1")

    row0 = (await db.list_active_claims_rows())[0]
    baseline_activity = row0["last_activity"]

    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction():
            n = await db.touch_session_activity("sess-1")
            assert n == 1
            raise RuntimeError("boom")

    rows = await db.list_active_claims_rows()
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["last_activity"] == baseline_activity, (
        "_write escaped the transaction: the activity bump survived a "
        "rollback, so it committed on a separate connection"
    )


async def test_write_inside_transaction_queue_on_still_joins(
    tmp_path: Path,
) -> None:
    """The reordered bound-connection check must not regress the
    queue-on path, which already deferred to the bound transaction."""
    db = Database(tmp_path / "boundq.sqlite", writer_queue=True)
    try:
        cid = await _mk_claim(db, session_id="sess-2")
        row0 = (await db.list_active_claims_rows())[0]
        baseline_activity = row0["last_activity"]

        with pytest.raises(RuntimeError, match="boom"):
            async with db.transaction():
                assert await db.touch_session_activity("sess-2") == 1
                raise RuntimeError("boom")

        rows = await db.list_active_claims_rows()
        assert [r["id"] for r in rows] == [cid]
        assert rows[0]["last_activity"] == baseline_activity
    finally:
        await db.aclose()


# --- touch_engineer_token: funnel + coalescing --------------------------


async def test_touch_token_coalesces_and_defers_counts(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "a" * 64
    sha = _sha256(raw)
    await db.create_engineer_token("alice", sha)

    # First touch flushes; the next two inside the interval coalesce.
    await db.touch_engineer_token(sha, source_ip="10.0.0.1", min_interval_sec=60)
    await db.touch_engineer_token(sha, source_ip="10.0.0.2", min_interval_sec=60)
    await db.touch_engineer_token(sha, source_ip="10.0.0.3", min_interval_sec=60)

    row = await db.lookup_engineer_token(sha)
    assert row is not None
    assert row["request_count"] == 1, "coalesced touches must not write"
    assert row["last_source_ip"] == "10.0.0.1"

    # A flush (interval disabled) delivers the accumulated increments and
    # the latest pending metadata: nothing was dropped, only deferred.
    await db.touch_engineer_token(sha, source_ip="10.0.0.4", min_interval_sec=0)
    row = await db.lookup_engineer_token(sha)
    assert row is not None
    assert row["request_count"] == 4
    assert row["last_source_ip"] == "10.0.0.4"


async def test_touch_token_flushes_after_interval_elapses(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "tok2.sqlite")
    raw = "coordt_" + "b" * 64
    sha = _sha256(raw)
    await db.create_engineer_token("alice", sha)

    await db.touch_engineer_token(sha, min_interval_sec=0.05)
    await db.touch_engineer_token(sha, min_interval_sec=0.05)  # coalesced
    deadline = time.monotonic() + 5.0
    while True:
        await asyncio.sleep(0.06)
        await db.touch_engineer_token(sha, min_interval_sec=0.05)
        row = await db.lookup_engineer_token(sha)
        assert row is not None
        if row["request_count"] >= 3 or time.monotonic() > deadline:
            break
    assert row["request_count"] == 3


async def test_touch_token_default_interval_is_30s() -> None:
    assert Database.TOKEN_TOUCH_MIN_INTERVAL_SEC == 30.0


async def test_touch_token_goes_through_write_funnel(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok3.sqlite", writer_queue=True)
    try:
        raw = "coordt_" + "c" * 64
        sha = _sha256(raw)
        await db.create_engineer_token("alice", sha)

        entered: list[int] = []
        orig = db._write

        def wrapped():
            entered.append(1)
            return orig()

        db._write = wrapped  # type: ignore[method-assign]
        await db.touch_engineer_token(sha, min_interval_sec=0)
        assert entered, (
            "touch_engineer_token must write via the _write funnel, not a "
            "private connection that contends with the writer queue"
        )
        row = await db.lookup_engineer_token(sha)
        assert row is not None
        assert row["request_count"] == 1
    finally:
        await db.aclose()
