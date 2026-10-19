"""Concurrency regression for the advisory-locked claim grant (design 5.1-5.4).

These tests are only meaningful on PostgreSQL: each ``create_claims`` call
opens its own ``db.transaction()`` -> its own pooled asyncpg connection, so a
fan-out of N concurrent claimers for the same scope is N *real* connections
contending on ``pg_advisory_xact_lock`` (design 5.1). On SQLite the single
event loop / single writer would make the test pass trivially and prove
nothing (design Section 9), so it is skipped there.

The invariant under test: among many concurrent claimers of an *overlapping*
scope in one repo -- including the NULL-repo bucket (design 5.4) and the
symbol-scoped path -- exactly one wins and the active-claim set never contains
a double grant.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from coordination.db import Database
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService, SessionClosedError

_PG_URL = os.environ.get("COORD_DATABASE_URL", "")
_PG_SELECTED = _PG_URL.startswith("postgresql://") or _PG_URL.startswith(
    "postgres://"
)

pytestmark = pytest.mark.skipif(
    not _PG_SELECTED,
    reason="advisory-lock concurrency is only meaningful on the PG backend",
)

# asyncpg pool max_size is 10; keep the fan-out comfortably above it so the
# contention is genuine (claimers queue for connections and for the lock).
_FANOUT = 16


def _make_service(
    tmp_path: Path, *, max_claims_per_engineer: int = 0
) -> CoordinationService:
    from coordination.config import Settings

    db = Database(tmp_path / "concurrency.sqlite")
    settings = Settings(
        database_path=tmp_path / "concurrency.sqlite",
        allow_insecure_no_auth=True,
        max_claim_ratio=1.0,
        max_claims_per_engineer=max_claims_per_engineer,
    )
    return CoordinationService(db=db, settings=settings)


def _second_instance(
    tmp_path: Path, *, max_claims_per_engineer: int = 0
) -> CoordinationService:
    """A SECOND, fully independent service over the SAME path -> the SAME PG
    schema, but with its own ``Database`` (its own in-process ``_engineer_locks``
    dict and its own connection from the shared pool). This is the in-process
    stand-in for a second replica: the in-process asyncio locks do NOT span the
    two instances, so anything that stays single-winner here is held single by
    the DB-level advisory lock / unit-of-work alone (design 5.3, 5.4) -- exactly
    the cross-replica property the SQLite single-writer model cannot prove."""
    return _make_service(
        tmp_path, max_claims_per_engineer=max_claims_per_engineer
    )


async def _active_rows_for_pattern(
    svc: CoordinationService, pattern: str, repo: str | None
) -> list[dict]:
    rows = await svc.db.list_active_claims_rows()
    return [r for r in rows if r.get("pattern") == pattern and r.get("repo") == repo]


async def _claim(
    svc: CoordinationService,
    *,
    engineer: str,
    pattern: str,
    repo: str | None,
    symbols: list[str] | None = None,
    session_id: str | None = None,
):
    return await svc.create_claims(
        CreateClaimsRequest(
            engineer=engineer,
            repo=repo,
            session_id=session_id,
            claims=[ClaimItem(type="file", pattern=pattern, symbols=symbols)],
        )
    )


async def test_concurrent_overlapping_file_claims_single_winner(
    tmp_path: Path,
) -> None:
    svc = _make_service(tmp_path)
    await svc.db.init()
    pattern = "src/contended.py"
    repo = "example-org/app"

    results = await asyncio.gather(
        *(
            _claim(svc, engineer=f"eng{i}", pattern=pattern, repo=repo)
            for i in range(_FANOUT)
        )
    )

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"expected exactly one winner, got {len(winners)}: "
        f"{[r.claim_ids for r in results]}"
    )
    # The decisive invariant: the active-claim set holds exactly one row for
    # the contended pattern. A double grant (the race the advisory lock
    # closes) would surface here as two.
    active = await _active_rows_for_pattern(svc, pattern, repo)
    assert len(active) == 1, f"double-active row: {active!r}"


async def test_concurrent_null_repo_claims_single_winner(
    tmp_path: Path,
) -> None:
    # design 5.4: pg_advisory_xact_lock(NULL) takes NO lock, so the NULL-repo
    # bucket would run unserialized and double-grant unless the key uses
    # coalesce(repo, ''). This is the regression guard for exactly that.
    svc = _make_service(tmp_path)
    await svc.db.init()
    pattern = "src/legacy_null.py"

    results = await asyncio.gather(
        *(
            _claim(svc, engineer=f"eng{i}", pattern=pattern, repo=None)
            for i in range(_FANOUT)
        )
    )

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"NULL-repo bucket double-granted: {[r.claim_ids for r in results]}"
    )
    active = await _active_rows_for_pattern(svc, pattern, None)
    assert len(active) == 1, f"double-active NULL-repo row: {active!r}"


async def test_concurrent_overlapping_symbol_claims_single_winner(
    tmp_path: Path,
) -> None:
    # Symbol-scoped claims on the SAME symbol of the SAME file overlap, so the
    # locked grant must still admit exactly one (disjoint symbols would
    # auto-coexist; identical symbols contend).
    svc = _make_service(tmp_path)
    await svc.db.init()
    pattern = "src/shared_module.py"
    repo = "example-org/app"

    results = await asyncio.gather(
        *(
            _claim(
                svc,
                engineer=f"eng{i}",
                pattern=pattern,
                repo=repo,
                symbols=["TargetClass"],
            )
            for i in range(_FANOUT)
        )
    )

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"symbol claim double-granted: {[r.claim_ids for r in results]}"
    )
    active = await _active_rows_for_pattern(svc, pattern, repo)
    assert len(active) == 1, f"double-active symbol row: {active!r}"


async def test_guarded_rename_serializes_with_concurrent_symbol_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename and a grant for its new symbol share the repo lock.

    Pause the rename immediately before its symbol-row UPDATE, after its
    collision check has seen the target path as free. While it is paused, a
    second service instance attempts to grant that target symbol. PostgreSQL
    must show the rename holding the advisory lock and the grant waiting for
    it; after the rename commits, the grant re-check sees the renamed claim
    and is rejected.
    """
    from coordination.pg_backend import _PGConnAdapter

    svc_a = _make_service(tmp_path)
    svc_b = _second_instance(tmp_path)
    await svc_a.db.init()
    await svc_b.db.init()
    pattern = "src/rename_race.py"
    repo = "example-org/app"

    seeded = await _claim(
        svc_a,
        engineer="renamer",
        pattern=pattern,
        repo=repo,
        symbols=["old_name"],
    )
    assert seeded.claim_ids
    claim_id = seeded.claim_ids[0]

    rename_at_update = asyncio.Event()
    allow_rename_update = asyncio.Event()
    grant_at_repo_lock = asyncio.Event()
    rename_pid: int | None = None
    grant_pid: int | None = None
    rename_task: asyncio.Task[bool] | None = None

    original_execute = _PGConnAdapter.execute

    async def _pause_rename_update(self, sql: str, params=()):
        nonlocal rename_pid
        normalized = " ".join(sql.lower().split())
        if (
            asyncio.current_task() is rename_task
            and normalized.startswith("update claim_symbols")
            and "set symbol_name" in normalized
        ):
            rename_pid = self._raw.get_server_pid()
            rename_at_update.set()
            await allow_rename_update.wait()
        return await original_execute(self, sql, params)

    monkeypatch.setattr(_PGConnAdapter, "execute", _pause_rename_update)

    original_grant_repo_lock = svc_b.db.repo_lock

    async def _observe_grant_repo_lock(conn, lock_repo: str | None) -> None:
        nonlocal grant_pid
        grant_pid = conn._raw.get_server_pid()
        grant_at_repo_lock.set()
        await original_grant_repo_lock(conn, lock_repo)

    monkeypatch.setattr(svc_b.db, "repo_lock", _observe_grant_repo_lock)

    rename_task = asyncio.create_task(
        svc_a.db.update_claim_symbol_rename(
            claim_id,
            file_path=pattern,
            old_symbol_name="old_name",
            new_symbol_name="new_name",
            new_start_line=1,
            new_start_col=None,
            new_end_line=2,
            new_end_col=None,
            resolved_by="parser",
            new_pattern=None,
            guard_new_path_collision=True,
            repo=repo,
        )
    )
    await asyncio.wait_for(rename_at_update.wait(), timeout=5)
    assert rename_pid is not None

    grant_task = asyncio.create_task(
        _claim(
            svc_b,
            engineer="concurrent-granter",
            pattern=pattern,
            repo=repo,
            symbols=["new_name"],
        )
    )
    await asyncio.wait_for(grant_at_repo_lock.wait(), timeout=5)
    assert grant_pid is not None

    rename_holds_lock = False
    grant_waits_for_lock = False
    try:
        async with svc_a.db._connect() as observer:
            for _ in range(100):
                cur = await observer.execute(
                    """
                    SELECT pid, granted
                    FROM pg_locks
                    WHERE locktype = 'advisory' AND pid IN (?, ?)
                    """,
                    (rename_pid, grant_pid),
                )
                lock_rows = await cur.fetchall()
                rename_holds_lock = any(
                    row["pid"] == rename_pid and row["granted"]
                    for row in lock_rows
                )
                grant_waits_for_lock = any(
                    row["pid"] == grant_pid and not row["granted"]
                    for row in lock_rows
                )
                if rename_holds_lock and grant_waits_for_lock:
                    break
                await asyncio.sleep(0.01)
    finally:
        # Never strand either transaction if an assertion or observer query
        # fails: releasing the gate lets the rename commit and the grant drain.
        allow_rename_update.set()

    rename_applied = await asyncio.wait_for(rename_task, timeout=5)
    grant_result = await asyncio.wait_for(grant_task, timeout=5)

    symbol_rows = await svc_a.db.get_symbol_rows_on_file(
        file_path=pattern,
        repo=repo,
    )
    matching = [
        row
        for row in symbol_rows
        if row.get("overlapping_symbol") == "new_name"
    ]
    assert len(matching) == 1, f"rename/grant race double-granted: {matching!r}"
    assert rename_holds_lock, "guarded rename did not hold the repo advisory lock"
    assert grant_waits_for_lock, "concurrent grant did not wait for the rename lock"
    assert rename_applied is True
    assert grant_result.claim_ids == []


async def test_two_instances_session_close_is_terminal(
    tmp_path: Path,
) -> None:
    svc_a = _make_service(tmp_path)
    svc_b = _second_instance(tmp_path)
    await svc_a.db.init()
    await svc_b.db.init()
    repo = "example-org/app"

    for idx in range(8):
        session_id = f"terminal-race-{idx}"
        pattern = f"src/terminal-race-{idx}.py"
        await svc_a.open_session(session_id, repo=repo)
        released, claimed = await asyncio.gather(
            svc_a.release_session(session_id, repo=repo),
            _claim(
                svc_b,
                engineer=f"late-{idx}",
                pattern=pattern,
                repo=repo,
                session_id=session_id,
            ),
            return_exceptions=True,
        )

        assert not isinstance(released, BaseException)
        if isinstance(claimed, BaseException):
            assert isinstance(claimed, SessionClosedError)
            assert released == 0
        else:
            assert claimed.claim_ids
            assert released == 1
        assert await _active_rows_for_pattern(svc_a, pattern, repo) == []


async def test_direct_db_session_close_uses_the_same_terminal_lock(
    tmp_path: Path,
) -> None:
    svc_a = _make_service(tmp_path)
    svc_b = _second_instance(tmp_path)
    await svc_a.db.init()
    await svc_b.db.init()
    repo = "example-org/app"

    for idx in range(8):
        session_id = f"direct-terminal-race-{idx}"
        pattern = f"src/direct-terminal-race-{idx}.py"
        await svc_a.open_session(session_id, repo=repo)
        released, claimed = await asyncio.gather(
            svc_a.db.release_for_session(session_id, repo=repo),
            _claim(
                svc_b,
                engineer=f"direct-late-{idx}",
                pattern=pattern,
                repo=repo,
                session_id=session_id,
            ),
            return_exceptions=True,
        )

        assert not isinstance(released, BaseException)
        if isinstance(claimed, BaseException):
            assert isinstance(claimed, SessionClosedError)
            assert released == []
        else:
            assert claimed.claim_ids
            assert len(released) == 1
        assert await _active_rows_for_pattern(svc_a, pattern, repo) == []


async def test_two_instances_lease_session_cleanup_once(
    tmp_path: Path,
) -> None:
    svc_a = _make_service(tmp_path)
    svc_b = _second_instance(tmp_path)
    await svc_a.db.init()
    await svc_b.db.init()
    repo = "example-org/app"
    session_id = "cleanup-lease-race"
    claimed = await _claim(
        svc_a,
        engineer="cleanup-holder",
        pattern="src/cleanup-lease.py",
        repo=repo,
        session_id=session_id,
    )
    claim_id = claimed.claim_ids[0]
    async with svc_a.db.transaction() as conn:
        await svc_a.db.repo_lock(conn, repo)
        await svc_a.db.session_lock(conn, session_id)
        assert await svc_a.db.close_session_claim_rows(
            session_id, repo=repo
        ) == [claim_id]

    calls = 0
    original_a = svc_a._drain_queue_for
    original_b = svc_b._drain_queue_for

    async def counted_a(closing_id: str) -> None:
        nonlocal calls
        calls += 1
        await original_a(closing_id)

    async def counted_b(closing_id: str) -> None:
        nonlocal calls
        calls += 1
        await original_b(closing_id)

    svc_a._drain_queue_for = counted_a  # type: ignore[method-assign]
    svc_b._drain_queue_for = counted_b  # type: ignore[method-assign]
    try:
        completed = await asyncio.gather(
            svc_a.drain_session_release_cleanup(),
            svc_b.drain_session_release_cleanup(),
        )
    finally:
        svc_a._drain_queue_for = original_a  # type: ignore[method-assign]
        svc_b._drain_queue_for = original_b  # type: ignore[method-assign]

    assert sum(completed) == 1
    assert calls == 1


# ---------------------------------------------------------------------------
# Two-instance variants (design Section 5 / 9): the single-winner invariant
# must hold across SEPARATE service instances that do NOT share an in-process
# lock -- so only the PG advisory lock / unit-of-work can be enforcing it.
# ---------------------------------------------------------------------------


async def test_two_instances_overlapping_file_claims_single_winner(
    tmp_path: Path,
) -> None:
    # repo_lock path across instances: half the claimers go through svc_a, half
    # through svc_b. The two services share no in-process state; if the per-repo
    # advisory lock did not serialize the grant across connections, more than
    # one would win.
    svc_a = _make_service(tmp_path)
    svc_b = _second_instance(tmp_path)
    await svc_a.db.init()
    await svc_b.db.init()
    pattern = "src/two_inst_contended.py"
    repo = "example-org/app"

    tasks = []
    for i in range(_FANOUT):
        svc = svc_a if i % 2 == 0 else svc_b
        tasks.append(_claim(svc, engineer=f"eng{i}", pattern=pattern, repo=repo))
    results = await asyncio.gather(*tasks)

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"cross-instance double grant: {[r.claim_ids for r in results]}"
    )
    active = await _active_rows_for_pattern(svc_a, pattern, repo)
    assert len(active) == 1, f"double-active row across instances: {active!r}"


async def test_two_instances_null_repo_claims_single_winner(
    tmp_path: Path,
) -> None:
    # The NULL-repo bucket (design 5.4) across two instances: coalesce(repo,'')
    # must keep the advisory key non-NULL so the bucket serializes even when the
    # contention spans separate service objects.
    svc_a = _make_service(tmp_path)
    svc_b = _second_instance(tmp_path)
    await svc_a.db.init()
    await svc_b.db.init()
    pattern = "src/two_inst_null.py"

    tasks = []
    for i in range(_FANOUT):
        svc = svc_a if i % 2 == 0 else svc_b
        tasks.append(_claim(svc, engineer=f"eng{i}", pattern=pattern, repo=None))
    results = await asyncio.gather(*tasks)

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"cross-instance NULL-repo double grant: {[r.claim_ids for r in results]}"
    )
    active = await _active_rows_for_pattern(svc_a, pattern, None)
    assert len(active) == 1, f"double-active NULL-repo across instances: {active!r}"


async def test_two_instances_engineer_cap_not_overshot(tmp_path: Path) -> None:
    # engineer_lock path across instances (design 5.3): the per-engineer cap is
    # GLOBAL across repos and replicas. With cap=1 and the SAME engineer issuing
    # DISJOINT (non-overlapping) claims concurrently on two instances, only the
    # per-engineer pg_advisory_xact_lock prevents both from reading count=0 and
    # both inserting (overshoot to 2). The in-process asyncio.Lock cannot --
    # the two instances have separate _engineer_locks dicts.
    from coordination.service import RateLimitExceeded

    cap = 1
    svc_a = _make_service(tmp_path, max_claims_per_engineer=cap)
    svc_b = _second_instance(tmp_path, max_claims_per_engineer=cap)
    await svc_a.db.init()
    await svc_b.db.init()
    engineer = "capped-eng"

    async def _one(svc: CoordinationService, pattern: str):
        try:
            return await _claim(svc, engineer=engineer, pattern=pattern, repo="r/x")
        except RateLimitExceeded:
            return None

    results = await asyncio.gather(
        *(
            _one(svc_a if i % 2 == 0 else svc_b, f"src/cap_{i}.py")
            for i in range(_FANOUT)
        )
    )

    granted = [r for r in results if r is not None and r.claim_ids]
    assert len(granted) == cap, (
        f"engineer cap overshot across instances: {len(granted)} > {cap}"
    )
    rows = await svc_a.db.list_active_claims_rows()
    held = [r for r in rows if r.get("engineer") == engineer]
    assert len(held) == cap, f"cap overshoot in active set: {held!r}"


async def test_two_instances_queue_drain_grants_single_winner(
    tmp_path: Path,
) -> None:
    # The queue-drain grant path (the same locked create_claims machinery that
    # respond_to_request's drain re-enters) across instances: a holder on svc_a,
    # two waiters queued on the two different instances, then the holder is
    # released on svc_b. Exactly one waiter is granted; the active set holds one
    # row for the contended pattern.
    svc_a = _make_service(tmp_path)
    svc_b = _second_instance(tmp_path)
    await svc_a.db.init()
    await svc_b.db.init()
    pattern = "src/two_inst_queue.py"
    repo = "example-org/app"

    holder = await _claim(svc_a, engineer="holder", pattern=pattern, repo=repo)
    assert holder.claim_ids, "holder should have been granted"

    async def _queued(svc: CoordinationService, engineer: str):
        return await svc.create_claims(
            CreateClaimsRequest(
                engineer=engineer,
                repo=repo,
                claims=[ClaimItem(type="file", pattern=pattern)],
                wait_seconds=30,
            )
        )

    waiter_a = asyncio.create_task(_queued(svc_a, "waiterA"))
    waiter_b = asyncio.create_task(_queued(svc_b, "waiterB"))

    # Let both waiters enqueue behind the holder before releasing it.
    for _ in range(100):
        await asyncio.sleep(0.05)
        if await svc_a.db.queue_depth_for_repo(repo) >= 2:
            break
    assert await svc_a.db.queue_depth_for_repo(repo) >= 2, "waiters did not enqueue"

    # Release the holder from the OTHER instance; this drains the queue and
    # grants exactly one waiter.
    await svc_b.release_claims(holder.claim_ids, engineer="holder")

    res_a, res_b = await asyncio.gather(waiter_a, waiter_b)
    granted = [r for r in (res_a, res_b) if r.claim_ids]
    assert len(granted) == 1, (
        f"queue drain across instances granted {len(granted)}, expected 1"
    )
    active = await _active_rows_for_pattern(svc_a, pattern, repo)
    assert len(active) == 1, f"double-active after drain: {active!r}"
