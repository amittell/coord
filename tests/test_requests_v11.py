"""DB-layer coverage for v0.11.0 release-request decision verbs.

v0.11 adds two new terminal decisions on top of approved / denied /
expired / resolved:

- ``narrowed``: holder closes their original claim and opens a new one
  with a narrower pattern. The released portion is what the requester
  needs; the rest stays held by the holder.
- ``coexist``: holder grants the requester a sibling claim on the same
  scope. Both claims live; they self-exclude from each other via the
  new ``claims.coexists_with`` JSON array but stay adversarial to
  anyone outside the pair.

These tests exercise the database layer directly (``db.create_request``,
``db.respond_to_request``, ``db.release_claims`` etc.) so they do not
depend on the service-layer plumbing landing in phase 2.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest

from coordination.db import Database


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    db_path = tmp_path / "v11.sqlite"
    d = Database(db_path)
    await d.init()
    yield d


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _seed_claim(
    db: Database,
    *,
    engineer: str = "alice",
    pattern: str = "src/foo.py",
    repo: str = "amittell/coord",
    session_id: str | None = "holder-sess",
    branch: str | None = "alice/feature",
    description: str | None = "holder claim",
    severity: str = "soft",
    ttl_hours: int = 4,
) -> str:
    cid = str(uuid4())
    exp = _iso(datetime.now(UTC) + timedelta(hours=ttl_hours))
    await db.insert_claims_batch(
        engineer=engineer,
        branch=branch,
        description=description,
        items=[(cid, "file", pattern, severity, exp)],
        repo=repo,
        session_id=session_id,
    )
    return cid


async def _file_request(
    db: Database,
    *,
    claim_id: str,
    requester_engineer: str = "bob",
    requester_session_id: str | None = "requester-sess",
    requested_pattern: str = "src/foo.py",
    requested_scope: str | None = None,
    reason: str | None = "need access",
    urgency: str = "high",
) -> dict:
    rid = str(uuid4())
    rows = await db.list_active_claims_rows()
    claim = next(r for r in rows if r["id"] == claim_id)
    original = str(claim["expires_at"])
    # We deliberately leave the claim's TTL untouched in these tests
    # (passing new_claim_expires_at == original) so the inheritance
    # assertions on `narrowed` / `coexist` aren't entangled with the
    # filing-time shortening logic. The service layer in phase 2 owns
    # the shortening; phase 1 only proves the decision branches.
    shortened = _iso(datetime.now(UTC) + timedelta(seconds=300))
    return await db.create_request(
        request_id=rid,
        claim_id=claim_id,
        requester_engineer=requester_engineer,
        requester_session_id=requester_session_id,
        requested_pattern=requested_pattern,
        requested_scope=requested_scope,
        reason=reason,
        urgency=urgency,
        original_expires_at=original,
        shortened_expires_at=shortened,
        new_claim_expires_at=original,
    )


# --- create_request: requested_scope round-trip -----------------------------


@pytest.mark.asyncio
async def test_create_request_persists_requested_scope(db: Database) -> None:
    """The requester can specify a narrower scope than the holder's
    pattern; the value round-trips through the request row and the
    ``filed`` audit event detail."""
    cid = await _seed_claim(db, pattern="src/api/**")
    req = await _file_request(
        db,
        claim_id=cid,
        requested_pattern="src/api/**",
        requested_scope="src/api/auth.py",
        reason="just the auth handler",
    )
    assert req.get("requested_scope") == "src/api/auth.py"

    # Round-trip through the public reader too.
    fresh = await db.get_request(req["id"])
    assert fresh is not None
    assert fresh["requested_scope"] == "src/api/auth.py"

    events = await db.list_request_events(req["id"])
    filed = next(e for e in events if e["event_type"] == "filed")
    detail = json.loads(filed["detail"])
    assert detail["requested_scope"] == "src/api/auth.py"


# --- decision: narrowed -----------------------------------------------------


@pytest.mark.asyncio
async def test_narrowed_closes_original_and_opens_new_with_inherited_ttl(
    db: Database,
) -> None:
    """``narrowed`` releases the original claim and opens a new one
    under a tighter pattern. Engineer / repo / session / TTL are
    inherited so the holder's identity and deadline are preserved."""
    cid = await _seed_claim(
        db,
        engineer="alice",
        pattern="src/api/**",
        repo="amittell/coord",
        session_id="holder-sess",
        branch="alice/api",
        description="api work",
        ttl_hours=4,
    )
    rows = await db.list_active_claims_rows()
    original = next(r for r in rows if r["id"] == cid)
    original_exp = original["expires_at"]

    req = await _file_request(
        db,
        claim_id=cid,
        requested_pattern="src/api/**",
        requested_scope="src/api/auth.py",
        reason="auth bug",
    )

    result = await db.respond_to_request(
        request_id=req["id"],
        decision="narrowed",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        narrowed_pattern="src/api/billing/**",
        note="releasing the auth handler, keeping billing",
    )
    assert result is not None
    assert result["decision"] == "narrowed"

    # Original claim is released.
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM claims WHERE id = ?", (cid,))
        row = await cur.fetchone()
    assert row is not None
    assert row["released_at"] is not None

    # A new claim exists for the same engineer with the narrowed pattern
    # and the same expiry as the original.
    active = await db.list_active_claims_rows()
    new_claims = [
        c for c in active
        if c["engineer"] == "alice" and c["id"] != cid
    ]
    assert len(new_claims) == 1, (
        f"expected exactly one new claim, got {len(new_claims)}"
    )
    nc = new_claims[0]
    assert nc["pattern"] == "src/api/billing/**"
    assert nc["repo"] == "amittell/coord"
    assert nc["session_id"] == "holder-sess"
    assert nc["expires_at"] == original_exp, (
        "narrowed claim should inherit the original TTL"
    )
    assert nc["branch"] == "alice/api"
    assert nc["description"] == "api work"
    assert nc["claim_type"] == "file"


@pytest.mark.asyncio
async def test_narrowed_audit_event_records_old_and_new_pattern(
    db: Database,
) -> None:
    cid = await _seed_claim(db, pattern="src/api/**")
    req = await _file_request(
        db, claim_id=cid, requested_pattern="src/api/**",
        requested_scope="src/api/auth.py",
    )
    await db.respond_to_request(
        request_id=req["id"],
        decision="narrowed",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        narrowed_pattern="src/api/billing/**",
        note="releasing auth",
    )

    events = await db.list_request_events(req["id"])
    responded = next(e for e in events if e["event_type"] == "responded")
    detail = json.loads(responded["detail"])
    assert detail["decision"] == "narrowed"
    assert detail["narrowed_pattern"] == "src/api/billing/**"
    assert detail["original_pattern"] == "src/api/**"
    assert detail["original_claim_id"] == cid
    assert detail.get("new_claim_id"), "responded detail must name the new claim id"
    assert detail["note"] == "releasing auth"


@pytest.mark.asyncio
async def test_narrowed_request_decision_is_narrowed_not_approved(
    db: Database,
) -> None:
    cid = await _seed_claim(db, pattern="src/api/**")
    req = await _file_request(db, claim_id=cid, requested_pattern="src/api/**")
    await db.respond_to_request(
        request_id=req["id"],
        decision="narrowed",
        actor_engineer="alice",
        actor_session_id=None,
        narrowed_pattern="src/api/billing/**",
    )
    final = await db.get_request(req["id"])
    assert final is not None
    assert final["decision"] == "narrowed", (
        "request decision must be the literal 'narrowed' so the "
        "audit trail and dashboard can distinguish it from 'approved'"
    )


@pytest.mark.asyncio
async def test_narrowed_without_pattern_kwarg_raises(db: Database) -> None:
    """``narrowed`` requires a ``narrowed_pattern`` kwarg; calling
    without one is a programmer error and must surface loudly."""
    cid = await _seed_claim(db)
    req = await _file_request(db, claim_id=cid)
    with pytest.raises(ValueError, match="narrowed_pattern"):
        await db.respond_to_request(
            request_id=req["id"],
            decision="narrowed",
            actor_engineer="alice",
            actor_session_id=None,
        )


# --- decision: coexist ------------------------------------------------------


@pytest.mark.asyncio
async def test_coexist_creates_sibling_claim_with_mutual_coexists_with(
    db: Database,
) -> None:
    """``coexist`` grants the requester a sibling claim on the same
    scope. Both claims persist; both have the partner's id in their
    ``coexists_with`` JSON array. The request itself transitions to
    decision='coexist' (not 'approved') so the audit trail is clear."""
    holder_cid = await _seed_claim(
        db,
        engineer="alice",
        pattern="src/api/handlers.py",
        repo="amittell/coord",
        session_id="holder-sess",
    )
    rows = await db.list_active_claims_rows()
    holder = next(r for r in rows if r["id"] == holder_cid)
    holder_exp = holder["expires_at"]

    req = await _file_request(
        db,
        claim_id=holder_cid,
        requester_engineer="bob",
        requester_session_id="requester-sess",
        requested_pattern="src/api/handlers.py",
        reason="different function in same file",
    )

    result = await db.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_pattern="src/api/handlers.py",
        note="we're editing different functions",
    )
    assert result is not None
    assert result["decision"] == "coexist"

    # Both claims are active.
    active = await db.list_active_claims_rows()
    by_id = {c["id"]: c for c in active}
    assert holder_cid in by_id, "holder claim must remain active after coexist"
    bob_claims = [c for c in active if c["engineer"] == "bob"]
    assert len(bob_claims) == 1, (
        f"expected exactly one new requester claim, got {len(bob_claims)}"
    )
    bob_cid = bob_claims[0]["id"]

    # Requester claim inherits the holder's TTL and uses the agreed pattern.
    assert bob_claims[0]["pattern"] == "src/api/handlers.py"
    assert bob_claims[0]["repo"] == "amittell/coord"
    assert bob_claims[0]["session_id"] == "requester-sess"
    assert bob_claims[0]["expires_at"] == holder_exp

    # Mutual coexists_with linkage. Holder's array contains the
    # requester's claim id; requester's contains the holder's.
    holder_partners = json.loads(by_id[holder_cid]["coexists_with"])
    requester_partners = json.loads(bob_claims[0]["coexists_with"])
    assert bob_cid in holder_partners
    assert holder_cid in requester_partners

    # And the request itself moved to coexist (terminal).
    final = await db.get_request(req["id"])
    assert final is not None
    assert final["decision"] == "coexist"


@pytest.mark.asyncio
async def test_coexist_audit_event_records_both_claim_ids(
    db: Database,
) -> None:
    holder_cid = await _seed_claim(db)
    req = await _file_request(db, claim_id=holder_cid)
    await db.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id=None,
        coexist_pattern="src/foo.py",
        note="cooperating on shared file",
    )

    events = await db.list_request_events(req["id"])
    responded = next(e for e in events if e["event_type"] == "responded")
    detail = json.loads(responded["detail"])
    assert detail["decision"] == "coexist"
    assert detail["coexist_pattern"] == "src/foo.py"
    assert detail["holder_claim_id"] == holder_cid
    assert detail.get("requester_claim_id"), (
        "responded detail must name the requester's new claim id"
    )
    assert detail["note"] == "cooperating on shared file"


@pytest.mark.asyncio
async def test_coexist_without_pattern_kwarg_raises(db: Database) -> None:
    cid = await _seed_claim(db)
    req = await _file_request(db, claim_id=cid)
    with pytest.raises(ValueError, match="coexist_pattern"):
        await db.respond_to_request(
            request_id=req["id"],
            decision="coexist",
            actor_engineer="alice",
            actor_session_id=None,
        )


# --- detach on release ------------------------------------------------------


@pytest.mark.asyncio
async def test_releasing_a_coexisting_claim_detaches_partner(
    db: Database,
) -> None:
    """When one of a coexisting pair is released, the surviving partner's
    ``coexists_with`` array no longer references the gone claim. Cleans
    up the mutual-exclusion graph as it shrinks."""
    holder_cid = await _seed_claim(db, engineer="alice", pattern="src/foo.py")
    req = await _file_request(db, claim_id=holder_cid)
    await db.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id=None,
        coexist_pattern="src/foo.py",
    )
    active = await db.list_active_claims_rows()
    bob_cid = next(c["id"] for c in active if c["engineer"] == "bob")

    # Sanity: holder lists bob as a partner.
    holder = next(c for c in active if c["id"] == holder_cid)
    assert bob_cid in json.loads(holder["coexists_with"])

    # Release bob.
    await db.release_claims([bob_cid], engineer="bob")

    # Holder's coexists_with no longer references bob.
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT coexists_with FROM claims WHERE id = ?", (holder_cid,)
        )
        row = await cur.fetchone()
    assert row is not None
    raw = row["coexists_with"]
    if raw is None:
        partners = []
    else:
        partners = json.loads(raw)
    assert bob_cid not in partners, (
        f"holder.coexists_with still references released partner: {partners}"
    )


@pytest.mark.asyncio
async def test_releasing_a_coexisting_claim_with_remaining_partners_keeps_others(
    db: Database,
) -> None:
    """Three-way coexistence: A coexists with B and C. Release A;
    B and C must still coexist with each other (their lists must keep
    the partner-of-partner that is still alive). Pairwise edges are
    independent: releasing A only strips A's id from B and C, it does
    NOT touch the B<->C edge."""
    a_cid = await _seed_claim(
        db, engineer="alice", pattern="src/foo.py", session_id="a-sess"
    )
    # B coexists with A.
    req_b = await _file_request(
        db,
        claim_id=a_cid,
        requester_engineer="bob",
        requester_session_id="b-sess",
    )
    await db.respond_to_request(
        request_id=req_b["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="a-sess",
        coexist_pattern="src/foo.py",
    )
    active = await db.list_active_claims_rows()
    b_cid = next(c["id"] for c in active if c["engineer"] == "bob")

    # C coexists with A.
    req_c = await _file_request(
        db,
        claim_id=a_cid,
        requester_engineer="carol",
        requester_session_id="c-sess",
    )
    await db.respond_to_request(
        request_id=req_c["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="a-sess",
        coexist_pattern="src/foo.py",
    )
    active = await db.list_active_claims_rows()
    c_cid = next(c["id"] for c in active if c["engineer"] == "carol")

    # Manually wire the B<->C pairwise edge so the three-way invariant
    # is set up (the DB layer doesn't auto-build full transitive closures
    # at coexist time; that's a service-layer choice for v0.11). We test
    # that detach respects existing edges it didn't create.
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT coexists_with FROM claims WHERE id = ?", (b_cid,)
        )
        row = await cur.fetchone()
        b_partners = json.loads(row["coexists_with"]) if row and row["coexists_with"] else []
        if c_cid not in b_partners:
            b_partners.append(c_cid)
        await conn.execute(
            "UPDATE claims SET coexists_with = ? WHERE id = ?",
            (json.dumps(b_partners), b_cid),
        )
        cur = await conn.execute(
            "SELECT coexists_with FROM claims WHERE id = ?", (c_cid,)
        )
        row = await cur.fetchone()
        c_partners = json.loads(row["coexists_with"]) if row and row["coexists_with"] else []
        if b_cid not in c_partners:
            c_partners.append(b_cid)
        await conn.execute(
            "UPDATE claims SET coexists_with = ? WHERE id = ?",
            (json.dumps(c_partners), c_cid),
        )
        await conn.commit()

    # Release A.
    await db.release_claims([a_cid], engineer="alice")

    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT coexists_with FROM claims WHERE id = ?", (b_cid,)
        )
        b_row = await cur.fetchone()
        cur = await conn.execute(
            "SELECT coexists_with FROM claims WHERE id = ?", (c_cid,)
        )
        c_row = await cur.fetchone()
    assert b_row is not None
    assert c_row is not None
    b_after = json.loads(b_row["coexists_with"]) if b_row["coexists_with"] else []
    c_after = json.loads(c_row["coexists_with"]) if c_row["coexists_with"] else []
    assert a_cid not in b_after
    assert a_cid not in c_after
    # B<->C edge must survive.
    assert c_cid in b_after, f"expected c in b's partners; got {b_after}"
    assert b_cid in c_after, f"expected b in c's partners; got {c_after}"
