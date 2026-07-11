"""Audit fixes on the queue / request-event surface:

- ``_FAIRNESS_COUNTERS`` is pruned when a claim's queue is observed
  empty, so the module map no longer grows one permanent entry per claim
  id ever drained.
- An unrecognized ``urgency`` still coerces to 'normal' (older wrappers
  pass free-form strings) but now logs a warning instead of silently
  starving the requester at the wrong priority.
- ``record_request_notify`` runs its check-then-insert dedupe under
  BEGIN IMMEDIATE so concurrent polls cannot both pass the check.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from coordination import db as db_module
from coordination.db import Database


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _mk_claim(db: Database, *, pattern: str = "src/app.py") -> str:
    cid = str(uuid4())
    exp = _iso(datetime.now(UTC) + timedelta(hours=1))
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[(cid, "file", pattern, "soft", exp)],
    )
    return cid


async def _enqueue(
    db: Database, cid: str, *, engineer: str = "bob", priority: str = "normal"
) -> dict:
    return await db.enqueue_claim_request(
        blocking_claim_id=cid,
        requester_engineer=engineer,
        requester_session_id=None,
        requester_branch=None,
        requester_description=None,
        repo=None,
        claim_type="file",
        pattern="src/app.py",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=30,
        priority=priority,
    )


@pytest.fixture(autouse=True)
def _reset_fairness_counters():
    db_module._FAIRNESS_COUNTERS.clear()
    yield
    db_module._FAIRNESS_COUNTERS.clear()


async def test_fairness_counter_pruned_when_queue_is_empty(
    tmp_path: Path,
) -> None:
    """Every release drives at least one pop per claim id; an empty-queue
    pop must not leave a permanent counter entry behind (unbounded map
    growth proportional to lifetime claim churn)."""
    db = Database(tmp_path / "queue.sqlite")
    cid = await _mk_claim(db)

    assert (
        await db.pop_next_waiting_queue_entry(cid, fairness_interval=10)
        is None
    )
    assert cid not in db_module._FAIRNESS_COUNTERS, (
        "empty pop leaked a fairness counter entry"
    )


async def test_fairness_counter_kept_while_contested_then_pruned(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "queue2.sqlite")
    cid = await _mk_claim(db)
    await _enqueue(db, cid)

    popped = await db.pop_next_waiting_queue_entry(cid, fairness_interval=10)
    assert popped is not None
    assert cid in db_module._FAIRNESS_COUNTERS, (
        "the rotation phase must persist while the queue is live"
    )

    # The only waiter is now in_progress; the next pop observes an empty
    # waiting set and drops the counter.
    assert (
        await db.pop_next_waiting_queue_entry(cid, fairness_interval=10)
        is None
    )
    assert cid not in db_module._FAIRNESS_COUNTERS


async def test_fairness_disabled_never_touches_counters(
    tmp_path: Path,
) -> None:
    """The v0.27 byte-identical invariant: fairness_interval=0 must not
    read, advance, or prune the counter map."""
    db = Database(tmp_path / "queue3.sqlite")
    cid = await _mk_claim(db)
    db_module._FAIRNESS_COUNTERS[cid] = 7

    assert (
        await db.pop_next_waiting_queue_entry(cid, fairness_interval=0)
        is None
    )
    assert db_module._FAIRNESS_COUNTERS.get(cid) == 7


async def test_unknown_urgency_coerces_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = Database(tmp_path / "urgency.sqlite")
    cid = await _mk_claim(db)

    with caplog.at_level(logging.WARNING, logger="coordination.db"):
        row = await db.enqueue_claim_request(
            blocking_claim_id=cid,
            requester_engineer="bob",
            requester_session_id=None,
            requester_branch=None,
            requester_description=None,
            repo=None,
            claim_type="file",
            pattern="src/app.py",
            symbols=None,
            narrowable=None,
            ttl_hours=None,
            wait_seconds=30,
            priority="urgent",
        )
    assert row["priority"] == "normal", (
        "unknown urgency must coerce, not 422 (older wrappers pass "
        "free-form urgency)"
    )
    warnings = [
        r for r in caplog.records if "unrecognized urgency" in r.message
    ]
    assert warnings, "the coercion must be logged, not silent"
    assert "'urgent'" in warnings[0].message


async def test_valid_urgency_passes_through_without_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = Database(tmp_path / "urgency2.sqlite")
    cid = await _mk_claim(db)

    with caplog.at_level(logging.WARNING, logger="coordination.db"):
        row = await _enqueue(db, cid, priority="blocking")
    assert row["priority"] == "blocking"
    assert not [
        r for r in caplog.records if "unrecognized urgency" in r.message
    ]


async def test_record_request_notify_dedupes_per_session(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "notify.sqlite")
    cid = await _mk_claim(db)
    now = datetime.now(UTC)
    rid = str(uuid4())
    await db.create_request(
        request_id=rid,
        claim_id=cid,
        requester_engineer="bob",
        requester_session_id=None,
        requested_pattern="src/app.py",
        reason=None,
        urgency="normal",
        original_expires_at=_iso(now + timedelta(hours=1)),
        shortened_expires_at=_iso(now + timedelta(minutes=5)),
        new_claim_expires_at=_iso(now + timedelta(minutes=5)),
    )

    assert (
        await db.record_request_notify(
            rid, holder_engineer="alice", holder_session_id="sess-1"
        )
        is True
    )
    # Same session polling again: deduped (now under BEGIN IMMEDIATE so
    # concurrent polls serialize on the write lock).
    assert (
        await db.record_request_notify(
            rid, holder_engineer="alice", holder_session_id="sess-1"
        )
        is False
    )
    # A different holder session still records its own first-sight event.
    assert (
        await db.record_request_notify(
            rid, holder_engineer="alice", holder_session_id="sess-2"
        )
        is True
    )
