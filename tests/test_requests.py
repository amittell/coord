"""End-to-end coverage for the v0.9.0 release-request system.

The state machine has five terminal states (approved, denied, expired,
resolved, plus the live ``pending``) and four kinds of side effect
(claim TTL shortened on filing, claim released on approve, claim TTL
restored on deny, request cascade-resolved when claim ends for any
unrelated reason). Plus the audit log: every transition writes one
``request_events`` row that operators can replay later.

Tests are organised by:
1. file_request shortens TTL + writes ``filed`` event
2. respond approve releases claim + writes ``responded`` event
3. respond deny restores TTL + writes ``responded`` event
4. cleanup sweep on shortened TTL fires ``expired`` event
5. release_session cascade fires ``resolved`` event
6. release_claims cascade fires ``resolved`` event
7. pending_requests merges first-class requests with conflict_log
8. notified event fires once per session that observes the request
9. late respond is logged but doesn't change state
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from coordination.config import Settings
from coordination.db import Database
from coordination.service import CoordinationService


@pytest.fixture()
async def svc(tmp_path: Path) -> AsyncIterator[CoordinationService]:
    db_path = tmp_path / "requests.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
        request_ttl_short_sec=300,
        idle_timeout_sec=0,
    )
    yield CoordinationService(db=db, settings=settings)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_active_claim(
    svc: CoordinationService,
    *,
    engineer: str = "alice",
    pattern: str = "src/foo.py",
    repo: str = "amittell/coord",
    session_id: str = "holder-sess",
    ttl_hours: int = 4,
) -> str:
    cid = str(uuid4())
    exp = _iso(datetime.now(UTC) + timedelta(hours=ttl_hours))
    await svc.db.insert_claims_batch(
        engineer=engineer,
        branch=None,
        description=None,
        items=[(cid, "file", pattern, "soft", exp)],
        repo=repo,
        session_id=session_id,
    )
    return cid


# --- 1. filing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_request_shortens_claim_ttl_to_configured_window(
    svc: CoordinationService,
) -> None:
    """A 4h-TTL claim should be clamped to ~5min (the configured
    ``request_ttl_short_sec``) when a request is filed against it."""
    cid = await _seed_active_claim(svc)
    request = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id="requester-sess",
        reason="hot-fix #1234",
        urgency="high",
    )
    assert request["decision"] == "pending"
    assert request["claim_id"] == cid

    rows = await svc.db.list_active_claims_rows()
    claim = next(r for r in rows if r["id"] == cid)
    new_exp = datetime.fromisoformat(
        str(claim["expires_at"]).replace("Z", "+00:00")
    )
    seconds_left = (new_exp - datetime.now(UTC)).total_seconds()
    # Within a generous window of the configured shortened TTL.
    assert 200 < seconds_left <= 300, (
        f"shortened TTL should be ~300s, got {seconds_left:.1f}s"
    )


@pytest.mark.asyncio
async def test_file_request_never_extends_an_already_short_ttl(
    svc: CoordinationService,
) -> None:
    """If the holder's claim is already going to expire in 60s, the
    request must NOT extend it. The shortened deadline only applies
    when it would actually shrink the window."""
    cid = await _seed_active_claim(svc, ttl_hours=0)  # near-immediate
    # Force expires_at to ~60s out.
    soon = _iso(datetime.now(UTC) + timedelta(seconds=60))
    import aiosqlite

    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            "UPDATE claims SET expires_at = ? WHERE id = ?", (soon, cid)
        )
        await conn.commit()

    await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason=None,
        urgency="normal",
    )
    rows = await svc.db.list_active_claims_rows()
    claim = next(r for r in rows if r["id"] == cid)
    assert claim["expires_at"] == soon, (
        "filing should not extend a shorter pre-existing TTL"
    )


@pytest.mark.asyncio
async def test_file_request_writes_filed_audit_event(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc)
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id="requester-sess",
        reason="incident #7",
        urgency="blocking",
    )
    events = await svc.list_request_events(req["id"])
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "filed"
    assert e["actor_engineer"] == "bob"
    assert e["actor_session_id"] == "requester-sess"
    detail = json.loads(e["detail"])
    assert detail["urgency"] == "blocking"
    assert detail["reason"] == "incident #7"
    assert detail["claim_id"] == cid


@pytest.mark.asyncio
async def test_file_request_against_unknown_claim_raises(
    svc: CoordinationService,
) -> None:
    with pytest.raises(KeyError):
        await svc.file_request(
            claim_id="not-a-real-id",
            requester="bob",
            requester_session_id=None,
            reason=None,
            urgency="normal",
        )


@pytest.mark.asyncio
async def test_file_request_against_already_released_claim_raises_value_error(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc)
    await svc.db.release_claims([cid], None)
    with pytest.raises(ValueError, match="no longer active"):
        await svc.file_request(
            claim_id=cid,
            requester="bob",
            requester_session_id=None,
            reason=None,
            urgency="normal",
        )


# --- 2. respond approved -----------------------------------------------------


@pytest.mark.asyncio
async def test_approve_releases_claim_and_logs_responded_event(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc)
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id="requester-sess",
        reason="urgent",
        urgency="high",
    )
    result = await svc.respond_to_request(
        request_id=req["id"],
        decision="approved",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        note="ok, releasing",
    )
    assert result is not None
    assert result["decision"] == "approved"
    assert result["decided_by_engineer"] == "alice"

    # Claim is gone from active.
    active = await svc.db.list_active_claims_rows()
    assert all(c["id"] != cid for c in active)

    events = await svc.list_request_events(req["id"])
    types = [e["event_type"] for e in events]
    assert "filed" in types
    assert "responded" in types
    responded = next(e for e in events if e["event_type"] == "responded")
    detail = json.loads(responded["detail"])
    assert detail["decision"] == "approved"
    assert detail["note"] == "ok, releasing"


# --- 3. respond denied -------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_restores_original_ttl(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc)
    rows = await svc.db.list_active_claims_rows()
    original_exp = next(r for r in rows if r["id"] == cid)["expires_at"]

    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="please?",
        urgency="normal",
    )
    # Claim TTL is now ~300s.
    rows = await svc.db.list_active_claims_rows()
    shortened = next(r for r in rows if r["id"] == cid)["expires_at"]
    assert shortened != original_exp

    await svc.respond_to_request(
        request_id=req["id"],
        decision="denied",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        note="busy with hot fix",
    )

    rows = await svc.db.list_active_claims_rows()
    claim_after = next(r for r in rows if r["id"] == cid)
    assert claim_after["expires_at"] == original_exp, (
        "denied response must restore the claim's original TTL"
    )


@pytest.mark.asyncio
async def test_invalid_decision_raises(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc)
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason=None,
        urgency="normal",
    )
    with pytest.raises(ValueError, match="approved"):
        await svc.respond_to_request(
            request_id=req["id"],
            decision="maybe",
            actor_engineer="alice",
            actor_session_id=None,
        )


# --- 4. expire on shortened TTL ---------------------------------------------


@pytest.mark.asyncio
async def test_shortened_ttl_expiry_cascades_to_request(
    svc: CoordinationService,
) -> None:
    """If the shortened TTL fires before the holder responds, the
    cleanup sweep marks the claim released and the open request
    transitions to ``expired`` with an audit event."""
    cid = await _seed_active_claim(svc)
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="emergency",
        urgency="blocking",
    )

    # Force the claim's expires_at into the past so expire_stale_claims
    # picks it up.
    past = _iso(datetime.now(UTC) - timedelta(seconds=1))
    import aiosqlite

    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            "UPDATE claims SET expires_at = ? WHERE id = ?", (past, cid)
        )
        await conn.commit()

    expired = await svc.db.expire_stale_claims()
    assert expired == [cid]

    final = await svc.get_request(req["id"])
    assert final is not None
    assert final["decision"] == "expired"

    events = await svc.list_request_events(req["id"])
    types = [e["event_type"] for e in events]
    assert "expired" in types
    expired_event = next(e for e in events if e["event_type"] == "expired")
    detail = json.loads(expired_event["detail"])
    assert detail["release_kind"] == "ttl-shortened"


# --- 5. cascade on release_session ------------------------------------------


@pytest.mark.asyncio
async def test_release_session_resolves_open_requests(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc, session_id="bulk-sess")
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="block",
        urgency="high",
    )
    released = await svc.db.release_for_session("bulk-sess")
    assert released == [cid]

    final = await svc.get_request(req["id"])
    assert final is not None
    assert final["decision"] == "resolved"

    events = await svc.list_request_events(req["id"])
    resolved = next(e for e in events if e["event_type"] == "resolved")
    detail = json.loads(resolved["detail"])
    assert detail["release_kind"] == "session-bulk"


# --- 6. cascade on release_claims -------------------------------------------


@pytest.mark.asyncio
async def test_release_claims_cascades_open_requests_to_resolved(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc, engineer="alice")
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="please",
        urgency="normal",
    )
    # Holder voluntarily releases (e.g. via release_claims).
    released = await svc.db.release_claims([cid], engineer="alice")
    assert released == [cid]

    final = await svc.get_request(req["id"])
    assert final is not None
    assert final["decision"] == "resolved"

    events = await svc.list_request_events(req["id"])
    resolved = next(e for e in events if e["event_type"] == "resolved")
    detail = json.loads(resolved["detail"])
    assert detail["release_kind"] == "voluntary"


# --- 7. merged inbox --------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_requests_merges_first_class_with_conflict_log(
    svc: CoordinationService,
) -> None:
    """Holder polls one endpoint and gets both first-class requests
    and read-only auto-conflict-log entries, distinguished by ``kind``."""
    cid = await _seed_active_claim(svc, session_id="holder-sess")
    # First-class request
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id="requester-sess",
        reason="urgent",
        urgency="high",
    )
    # Auto-conflict-log entry (different attempted pattern -> logged
    # as a regular conflict).
    await svc.db.log_conflict(
        claim_id=cid,
        attempted_by="carol",
        attempted_pattern="src/foo.py",
        resolution=None,
        attempted_session_id="other-sess",
    )

    inbox = await svc.pending_requests("holder-sess")
    kinds = [r["kind"] for r in inbox]
    assert "request" in kinds
    assert "auto-conflict" in kinds
    request_row = next(r for r in inbox if r["kind"] == "request")
    assert request_row["id"] == req["id"]
    assert request_row["urgency"] == "high"


# --- 8. notified event ------------------------------------------------------


@pytest.mark.asyncio
async def test_notified_event_fires_once_per_holder_session(
    svc: CoordinationService,
) -> None:
    """Every time a holder polls pending_requests and observes an open
    request, the FIRST observation per session writes a ``notified``
    audit event. Subsequent polls from the same session don't re-fire
    (otherwise a 30s polling cadence would balloon the audit log)."""
    cid = await _seed_active_claim(svc, session_id="holder-sess")
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="please",
        urgency="normal",
    )

    # First poll -> writes notified
    await svc.pending_requests("holder-sess")
    # Second poll -> no new event
    await svc.pending_requests("holder-sess")

    events = await svc.list_request_events(req["id"])
    notify_events = [e for e in events if e["event_type"] == "notified"]
    assert len(notify_events) == 1, (
        f"expected exactly one notified event, got {len(notify_events)}"
    )


# --- 9. late respond logged but doesn't change state ------------------------


@pytest.mark.asyncio
async def test_late_respond_after_terminal_state_is_logged_but_inert(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc)
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="urgent",
        urgency="high",
    )
    # Terminal: approve.
    await svc.respond_to_request(
        request_id=req["id"],
        decision="approved",
        actor_engineer="alice",
        actor_session_id=None,
    )

    # Holder tries to deny after the fact (e.g. operator typo).
    final = await svc.respond_to_request(
        request_id=req["id"],
        decision="denied",
        actor_engineer="alice",
        actor_session_id=None,
        note="oops, didn't mean to approve",
    )
    assert final is not None
    assert final["decision"] == "approved"  # unchanged

    events = await svc.list_request_events(req["id"])
    types = [e["event_type"] for e in events]
    assert types.count("responded") == 1
    assert "responded-late" in types
    late = next(e for e in events if e["event_type"] == "responded-late")
    detail = json.loads(late["detail"])
    assert detail["attempted_decision"] == "denied"
    assert detail["current_decision"] == "approved"


# --- 10. wait_for_decision blocks until terminal or timeout -----------------


@pytest.mark.asyncio
async def test_wait_for_decision_returns_when_responded(
    svc: CoordinationService,
) -> None:
    """The long-poll helper resolves as soon as the holder responds.
    Simulate that by responding from a parallel task while wait is in
    flight."""
    cid = await _seed_active_claim(svc)
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="hot",
        urgency="high",
    )

    async def respond_after(delay: float) -> None:
        await asyncio.sleep(delay)
        await svc.respond_to_request(
            request_id=req["id"],
            decision="approved",
            actor_engineer="alice",
            actor_session_id=None,
        )

    task = asyncio.create_task(respond_after(0.2))
    final = await svc.wait_for_decision(
        req["id"], timeout_seconds=5, poll_interval_seconds=0.05
    )
    await task
    assert final is not None
    assert final["decision"] == "approved"


@pytest.mark.asyncio
async def test_wait_for_decision_returns_pending_on_timeout(
    svc: CoordinationService,
) -> None:
    cid = await _seed_active_claim(svc)
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="hot",
        urgency="high",
    )
    final = await svc.wait_for_decision(
        req["id"], timeout_seconds=0, poll_interval_seconds=0.05
    )
    # Timeout 0 -> one poll then return.
    assert final is not None
    assert final["decision"] == "pending"
