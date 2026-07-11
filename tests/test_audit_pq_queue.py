"""Audit fixes: queue lifecycle correctness on every claim-closing path.

Covers the P-queue cluster of the 2026-07-08 audit:

- the FIFO queue drains on EVERY path that closes a claim: explicit
  release, session bulk release, respond_to_request approved/narrowed,
  and TTL/idle expiry -- not just POST /claims/release;
- a no-op release (wrong engineer, nothing actually closed) does NOT
  drain: the waiters keep their queue positions instead of being
  expelled with immediate 409s;
- mark_queue_granted / mark_queue_expired carry state guards so a
  waiter timeout racing an in-flight drain cannot mint an orphan claim
  (both interleavings are exercised);
- a client disconnect (handler cancellation) during the long-poll
  drives the queue row to a terminal state instead of leaking it in
  'waiting' forever;
- a multi-item batch that resolves via a queued grant names the
  patterns that were NOT reserved instead of returning a clean success
  for one claimed file.

All waiting is done by polling, never bare sleeps for a fixed outcome.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from coordination.config import Settings
from coordination.db import Database
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService

REPO = "amittell/coord"


@pytest.fixture()
async def svc(tmp_path: Path) -> CoordinationService:
    db_path = tmp_path / "pq_queue.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
    )
    return CoordinationService(db=db, settings=settings)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _claim(
    svc: CoordinationService,
    engineer: str,
    pattern: str,
    *,
    session_id: str | None = None,
) -> str:
    resp = await svc.create_claims(
        CreateClaimsRequest(
            engineer=engineer,
            repo=REPO,
            session_id=session_id,
            claims=[ClaimItem(type="file", pattern=pattern)],
        )
    )
    assert resp.claim_ids, f"seed claim failed: {resp}"
    return resp.claim_ids[0]


async def _enqueue(
    svc: CoordinationService,
    blocking_cid: str,
    *,
    engineer: str = "bob",
    pattern: str,
    wait_seconds: int = 120,
) -> dict[str, Any]:
    """Insert a waiting claim_queue row directly, the same shape
    _enqueue_and_wait writes, without holding a long-poll open."""
    return await svc.db.enqueue_claim_request(
        blocking_claim_id=blocking_cid,
        requester_engineer=engineer,
        requester_session_id=None,
        requester_branch=None,
        requester_description=None,
        repo=REPO,
        claim_type="file",
        pattern=pattern,
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=wait_seconds,
        priority="normal",
    )


async def _poll_until(
    predicate: Callable[[], Awaitable[Any]], *, timeout: float = 5.0
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


async def _active_ids(svc: CoordinationService) -> set[str]:
    rows = await svc.db.list_active_claims_rows()
    return {str(r["id"]) for r in rows}


async def _active_for(
    svc: CoordinationService, engineer: str
) -> list[dict[str, Any]]:
    rows = await svc.db.list_active_claims_rows()
    return [r for r in rows if r["engineer"] == engineer]


# --- drain on session release ----------------------------------------------


async def test_release_session_drains_queue(svc: CoordinationService) -> None:
    """POST /sessions/{id}/release is the protocol-recommended cleanup
    call; a waiter queued behind one of the session's claims must be
    granted when the session tears down."""
    cid = await _claim(svc, "alice", "src/a.py", session_id="sess-h")
    entry = await _enqueue(svc, cid, pattern="src/a.py")

    n = await svc.release_session("sess-h")
    assert n == 1

    row = await svc.db.get_queue_entry(entry["id"])
    assert row is not None
    assert row["state"] == "granted", row
    assert row["granted_claim_id"]
    bob_claims = await _active_for(svc, "bob")
    assert [c["pattern"] for c in bob_claims] == ["src/a.py"]
    assert row["granted_claim_id"] in {c["id"] for c in bob_claims}


# --- drain on respond_to_request -------------------------------------------


async def test_respond_approved_drains_queue(svc: CoordinationService) -> None:
    cid = await _claim(svc, "alice", "src/b.py", session_id="sess-a")
    entry = await _enqueue(svc, cid, pattern="src/b.py")
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="need it",
        urgency="high",
    )

    result = await svc.respond_to_request(
        request_id=req["id"],
        decision="approved",
        actor_engineer="alice",
        actor_session_id="sess-a",
    )
    assert result is not None
    assert result["decision"] == "approved"
    # The private DB->service transport key must never escape the
    # service layer into an API response.
    assert "_released_claim_ids" not in result

    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "granted", row
    bob_claims = await _active_for(svc, "bob")
    assert [c["pattern"] for c in bob_claims] == ["src/b.py"]


async def test_respond_narrowed_drains_queue(svc: CoordinationService) -> None:
    """'narrowed' closes the holder's original claim before opening the
    tighter one; a waiter whose scope falls outside the narrowed
    pattern must be granted."""
    cid = await _claim(svc, "alice", "src/n/**", session_id="sess-a")
    entry = await _enqueue(svc, cid, pattern="src/n/file.py")
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="need file.py",
        urgency="normal",
    )

    result = await svc.respond_to_request(
        request_id=req["id"],
        decision="narrowed",
        actor_engineer="alice",
        actor_session_id="sess-a",
        narrowed_pattern="src/n/keep/**",
    )
    assert result is not None
    assert result["decision"] == "narrowed"

    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "granted", row
    bob_claims = await _active_for(svc, "bob")
    assert [c["pattern"] for c in bob_claims] == ["src/n/file.py"]
    # Alice keeps her narrowed scope.
    alice_patterns = {c["pattern"] for c in await _active_for(svc, "alice")}
    assert alice_patterns == {"src/n/keep/**"}


# --- drain on TTL expiry ----------------------------------------------------


async def test_ttl_expiry_sweep_drains_queue(svc: CoordinationService) -> None:
    """request_release exists to shorten the holder's TTL; when that
    TTL fires in the sweep, the waiter queued behind the claim must be
    granted instead of burning its whole wait_seconds."""
    cid = await _claim(svc, "alice", "src/d.py")
    entry = await _enqueue(svc, cid, pattern="src/d.py")

    past = _iso(datetime.now(UTC) - timedelta(minutes=1))
    async with svc.db._connect() as conn:
        await conn.execute(
            "UPDATE claims SET expires_at = ? WHERE id = ?", (past, cid)
        )
        await conn.commit()

    expired = await svc.expire_stale_claims()
    assert expired == [cid]

    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "granted", row
    bob_claims = await _active_for(svc, "bob")
    assert [c["pattern"] for c in bob_claims] == ["src/d.py"]


# --- no drain on a no-op release --------------------------------------------


async def test_noop_release_does_not_flush_queue(
    svc: CoordinationService,
) -> None:
    """A release that closes nothing (wrong engineer) must not touch
    the queue: the claim stays active, the waiter keeps its position,
    and a later real release still grants it."""
    cid = await _claim(svc, "alice", "src/e.py")
    entry = await _enqueue(svc, cid, pattern="src/e.py")

    n = await svc.release_claims([cid], "mallory")
    assert n == 0

    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "waiting", (
        f"no-op release must not disturb the queue; row moved to "
        f"{row['state']!r}"
    )
    assert cid in await _active_ids(svc)

    # The queue survives intact: the legitimate release grants it.
    n = await svc.release_claims([cid], "alice")
    assert n == 1
    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "granted", row


# --- waiter-timeout vs in-flight drain, both orderings ----------------------


async def test_waiter_timeout_beats_drain_grant(
    svc: CoordinationService,
) -> None:
    """Ordering A: the waiter's deadline fires while the drain's grant
    re-issue is still in flight (row 'in_progress'). The timeout wins,
    mark_queue_granted loses its state guard, and the drain releases
    the orphan claim instead of finalising a grant nobody will see."""
    cid = await _claim(svc, "alice", "src/f.py")
    entry = await _enqueue(svc, cid, pattern="src/f.py")

    original_create = svc.create_claims
    release_gate = asyncio.Event()

    async def slow_create(body, *, auto_promote_allowed=True):
        await release_gate.wait()
        return await original_create(
            body, auto_promote_allowed=auto_promote_allowed
        )

    svc.create_claims = slow_create  # type: ignore[method-assign]
    try:
        released = await svc.db.release_claims([cid], "alice")
        assert released == [cid]
        drain = asyncio.create_task(svc._drain_queue_for(cid))

        async def _in_progress() -> bool:
            row = await svc.db.get_queue_entry(entry["id"])
            return row is not None and row["state"] == "in_progress"

        await _poll_until(_in_progress)

        # The waiter's deadline fires mid-grant.
        adopted = await svc._finalise_queue_wait(entry["id"])
        assert adopted is None
        row = await svc.db.get_queue_entry(entry["id"])
        assert row["state"] == "expired", row

        release_gate.set()
        await asyncio.wait_for(drain, timeout=5.0)
    finally:
        release_gate.set()
        svc.create_claims = original_create  # type: ignore[method-assign]

    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "expired", (
        f"grant must not overwrite the waiter's expiry; row is "
        f"{row['state']!r}"
    )
    assert await _active_for(svc, "bob") == [], (
        "the drain must release the orphan claim it created for the "
        "departed waiter"
    )


async def test_drain_grant_beats_waiter_timeout(
    svc: CoordinationService,
) -> None:
    """Ordering B: the drain finalises the grant a hair before the
    waiter's deadline. The waiter's expiry loses the state guard and
    adopts the grant instead of surfacing a 409 for a claim that now
    exists in its name."""
    cid = await _claim(svc, "alice", "src/g.py")
    entry = await _enqueue(svc, cid, pattern="src/g.py")

    released = await svc.db.release_claims([cid], "alice")
    assert released == [cid]
    await svc._drain_queue_for(cid)

    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "granted", row
    granted_cid = row["granted_claim_id"]
    assert granted_cid

    adopted = await svc._finalise_queue_wait(entry["id"])
    assert adopted == granted_cid, (
        "the losing timeout must adopt the drain's grant, not report "
        "a 409 for scope the requester now holds"
    )
    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "granted", (
        f"timeout must not clobber the granted state; row is "
        f"{row['state']!r}"
    )
    bob_claims = await _active_for(svc, "bob")
    assert granted_cid in {c["id"] for c in bob_claims}


async def test_cancel_during_drain_wins_over_grant(
    svc: CoordinationService,
) -> None:
    """A requester cancel racing the in-flight drain: cancel_queue_entry
    may terminalise an 'in_progress' row, so the subsequent
    mark_queue_granted must report the loss instead of silently
    overwriting the cancellation."""
    cid = await _claim(svc, "alice", "src/h.py")
    entry = await _enqueue(svc, cid, pattern="src/h.py")

    popped = await svc.db.pop_next_waiting_queue_entry(cid)
    assert popped is not None and popped["id"] == entry["id"]

    cancelled = await svc.db.cancel_queue_entry(
        entry["id"], requester_engineer="bob"
    )
    assert cancelled is True

    granted = await svc.db.mark_queue_granted(entry["id"], "would-be-cid")
    assert granted is False, (
        "grant must lose to a cancellation that landed first"
    )
    row = await svc.db.get_queue_entry(entry["id"])
    assert row["state"] == "cancelled", row
    assert not row["granted_claim_id"]


# --- client disconnect during the long-poll ---------------------------------


async def test_disconnected_long_poll_does_not_leak_waiting_row(
    svc: CoordinationService,
) -> None:
    """When the ASGI server cancels the handler on client disconnect,
    the queue row must reach a terminal state instead of leaking in
    'waiting' -- a leaked row counts against the queue caps forever and
    stays poppable by a future drain, minting a claim for a requester
    who is long gone."""
    await _claim(svc, "alice", "src/i.py")

    waiter = asyncio.create_task(
        svc.create_claims(
            CreateClaimsRequest(
                engineer="bob",
                repo=REPO,
                claims=[ClaimItem(type="file", pattern="src/i.py")],
                wait_seconds=30,
            )
        )
    )

    async def _waiting_row() -> dict[str, Any] | None:
        rows = await svc.db.list_queue_for_requester("bob")
        for r in rows:
            if r["state"] == "waiting":
                return r
        return None

    row = await _poll_until(_waiting_row)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    async def _terminal() -> bool:
        current = await svc.db.get_queue_entry(row["id"])
        return current is not None and current["state"] == "expired"

    await _poll_until(_terminal)
    final = await svc.db.get_queue_entry(row["id"])
    assert final["state"] == "expired", (
        f"disconnected long-poll leaked the row in {final['state']!r}"
    )


# --- multi-item batch: partial queued grant is named -------------------------


async def test_multi_item_queued_grant_warns_about_uncovered_patterns(
    svc: CoordinationService,
) -> None:
    """A queued grant covers exactly one item of the batch. The
    response keeps its success shape (claim_ids non-empty, no
    conflicts) but must name every pattern that was NOT reserved so
    the caller does not edit files it never claimed."""
    holder_cid = await _claim(svc, "alice", "src/m1.py")

    waiter = asyncio.create_task(
        svc.create_claims(
            CreateClaimsRequest(
                engineer="bob",
                repo=REPO,
                claims=[
                    ClaimItem(type="file", pattern="src/m1.py"),
                    ClaimItem(type="file", pattern="src/m2.py"),
                ],
                wait_seconds=30,
            )
        )
    )

    async def _queued() -> bool:
        rows = await svc.db.list_queue_for_requester("bob")
        return any(r["state"] == "waiting" for r in rows)

    await _poll_until(_queued)

    n = await svc.release_claims([holder_cid], "alice")
    assert n == 1

    result = await asyncio.wait_for(waiter, timeout=5.0)
    assert len(result.claim_ids) == 1
    assert result.conflicts == []
    assert len(result.warnings) == 1, result.warnings
    warning = result.warnings[0]
    assert "PARTIAL" in warning
    assert "src/m2.py" in warning
    assert "src/m1.py" in warning

    bob_patterns = {c["pattern"] for c in await _active_for(svc, "bob")}
    assert bob_patterns == {"src/m1.py"}, (
        "only the queued item is reserved; the warning (not a silent "
        "claim) covers the rest"
    )


async def test_single_item_queued_grant_has_no_partial_warning(
    svc: CoordinationService,
) -> None:
    """The common single-item wait keeps its clean response."""
    holder_cid = await _claim(svc, "alice", "src/s1.py")

    waiter = asyncio.create_task(
        svc.create_claims(
            CreateClaimsRequest(
                engineer="bob",
                repo=REPO,
                claims=[ClaimItem(type="file", pattern="src/s1.py")],
                wait_seconds=30,
            )
        )
    )

    async def _queued() -> bool:
        rows = await svc.db.list_queue_for_requester("bob")
        return any(r["state"] == "waiting" for r in rows)

    await _poll_until(_queued)
    assert await svc.release_claims([holder_cid], "alice") == 1

    result = await asyncio.wait_for(waiter, timeout=5.0)
    assert len(result.claim_ids) == 1
    assert result.conflicts == []
    assert result.warnings == []
