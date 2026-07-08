"""Audit fixes for the TTL sweep and claim-lifetime data hygiene:

- ``expire_stale_claims`` re-checks the snapshot deadline in its release
  UPDATE, so an ``extend_claim`` or activity ping landing between the
  sweep's read and its write no longer gets force-released on stale data.
- A malformed ``expires_at`` fails closed (treated as expired) instead of
  crashing the sweep on every tick and 500ing every claim listing.
- ``purge_released_symbol_rows`` garbage-collects the claim_symbols /
  claim_symbol_callsites / claim_symbol_renames children of long-released
  claims, which previously leaked forever (soft release, no CASCADE).

The race tests interpose on ``Database._connect`` and mutate rows with a
sync sqlite3 connection between the sweep's two connections, which is
SQLite-shaped -- hence ``sqlite_only``.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from uuid import uuid4

import pytest

from coordination.db import Database

pytestmark = pytest.mark.sqlite_only


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _mk_claim(
    db: Database,
    *,
    engineer: str = "alice",
    pattern: str = "src/app.py",
    expires: datetime | None = None,
    session_id: str | None = None,
    last_activity: str | None = None,
) -> str:
    cid = str(uuid4())
    exp = _iso(expires or datetime.now(UTC) + timedelta(hours=1))
    await db.insert_claims_batch(
        engineer=engineer,
        branch=None,
        description=None,
        items=[(cid, "file", pattern, "soft", exp)],
        session_id=session_id,
        last_activity=last_activity,
    )
    return cid


def _sync_exec(path: Path, sql: str, params: tuple) -> None:
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


class _RacingDatabase(Database):
    """Runs a callback just before the sweep's SECOND connection opens
    (the release-UPDATE connection), i.e. after the expiry snapshot has
    been taken -- the exact window the guard must cover."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._armed_calls = 0
        self.armed = False
        self.between_connections: Callable[[], None] | None = None

    def _connect(self):
        inner = super()._connect()
        if not self.armed:
            return inner
        self._armed_calls += 1
        if self._armed_calls != 2 or self.between_connections is None:
            return inner
        callback = self.between_connections
        self.between_connections = None

        @asynccontextmanager
        async def _cm():
            callback()
            async with inner as conn:
                yield conn

        return _cm()


async def test_sweep_respects_extend_between_snapshot_and_release(
    tmp_path: Path,
) -> None:
    path = tmp_path / "race.sqlite"
    db = _RacingDatabase(path)
    cid = await _mk_claim(
        db, expires=datetime.now(UTC) - timedelta(minutes=1)
    )
    new_exp = _iso(datetime.now(UTC) + timedelta(hours=2))

    def extend_now() -> None:
        _sync_exec(
            path,
            "UPDATE claims SET expires_at = ? WHERE id = ?",
            (new_exp, cid),
        )

    db.armed = True
    db.between_connections = extend_now
    released = await db.expire_stale_claims()
    db.armed = False

    assert released == 0, (
        "the sweep force-released a claim whose TTL was extended after "
        "the expiry snapshot"
    )
    rows = await db.list_active_claims_rows()
    assert [r["id"] for r in rows] == [cid]
    assert rows[0]["expires_at"] == new_exp


async def test_sweep_respects_activity_ping_between_snapshot_and_release(
    tmp_path: Path,
) -> None:
    path = tmp_path / "race_idle.sqlite"
    db = _RacingDatabase(path)
    stale = _iso(datetime.now(UTC) - timedelta(hours=1))
    cid = await _mk_claim(db, session_id="sess-1", last_activity=stale)
    fresh = _iso(datetime.now(UTC))

    def ping_now() -> None:
        _sync_exec(
            path,
            "UPDATE claims SET last_activity = ? WHERE id = ?",
            (fresh, cid),
        )

    db.armed = True
    db.between_connections = ping_now
    released = await db.expire_stale_claims(idle_timeout_sec=60)
    db.armed = False

    assert released == 0, (
        "the sweep idle-released a claim that pinged after the snapshot"
    )
    rows = await db.list_active_claims_rows()
    assert [r["id"] for r in rows] == [cid]


async def test_sweep_still_releases_genuinely_expired_claims(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "plain.sqlite")
    cid = await _mk_claim(
        db, expires=datetime.now(UTC) - timedelta(minutes=1)
    )
    keeper = await _mk_claim(db, pattern="src/other.py")

    assert await db.expire_stale_claims() == 1
    rows = await db.list_active_claims_rows()
    assert [r["id"] for r in rows] == [keeper]
    assert cid not in {r["id"] for r in rows}


async def test_malformed_expires_at_fails_closed_everywhere(
    tmp_path: Path,
) -> None:
    """One corrupt expires_at must not crash the sweep (which would make
    every claim immortal) nor 500 the listing / cap-count paths. It is
    treated as already expired: hidden from active listings and released
    by the next sweep."""
    path = tmp_path / "corrupt.sqlite"
    db = Database(path)
    bad = await _mk_claim(db, pattern="src/bad.py")
    good = await _mk_claim(db, pattern="src/good.py")
    _sync_exec(
        path,
        "UPDATE claims SET expires_at = ? WHERE id = ?",
        ("not-a-timestamp", bad),
    )

    rows = await db.list_active_claims_rows()
    assert [r["id"] for r in rows] == [good]

    count, soonest = await db.count_active_claims_for_engineer("alice")
    assert count == 1
    assert soonest is not None

    assert await db.expire_stale_claims() == 1
    async_rows = await db.list_active_claims_rows()
    assert [r["id"] for r in async_rows] == [good]
    # The corrupt row is now soft-released, not left active forever.
    conn = sqlite3.connect(path)
    try:
        released = conn.execute(
            "SELECT released_at FROM claims WHERE id = ?", (bad,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert released is not None


async def test_purge_released_symbol_rows_reaps_children(
    tmp_path: Path,
) -> None:
    path = tmp_path / "purge.sqlite"
    db = Database(path)
    released_cid = await _mk_claim(db, pattern="src/sym.py")
    live_cid = await _mk_claim(db, pattern="src/live.py")

    for cid in (released_cid, live_cid):
        await db.insert_claim_symbols(
            rows=[
                (str(uuid4()), cid, "src/sym.py", "handle", "function", None)
            ]
        )
        await db.insert_claim_callsites(
            cid, [("src/caller.py", 10, 4, "handle")]
        )
        _sync_exec(
            path,
            "INSERT INTO claim_symbol_renames "
            "(id, claim_id, file_path, old_symbol_name, new_symbol_name, "
            "detected_at, resolved_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()),
                cid,
                "src/sym.py",
                "handle",
                "handle_v2",
                _iso(datetime.now(UTC)),
                "parser",
            ),
        )

    assert await db.release_claims([released_cid]) == 1

    # Inside the retention window: nothing is reaped yet.
    counts = await db.purge_released_symbol_rows(older_than_sec=3600)
    assert counts == {
        "claim_symbols": 0,
        "claim_symbol_callsites": 0,
        "claim_symbol_renames": 0,
    }

    # Past the window: the released claim's children go, the live
    # claim's stay, and the released claims row itself is kept (audit).
    counts = await db.purge_released_symbol_rows(older_than_sec=0)
    assert counts == {
        "claim_symbols": 1,
        "claim_symbol_callsites": 1,
        "claim_symbol_renames": 1,
    }

    assert await db.get_claim_symbols(released_cid) == []
    live_symbols = await db.get_claim_symbols(live_cid)
    assert [s["symbol_name"] for s in live_symbols] == ["handle"]

    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM claim_symbol_callsites"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM claim_symbol_renames"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM claims WHERE id = ?", (released_cid,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()
