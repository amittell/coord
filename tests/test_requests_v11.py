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

from coordination.config import Settings
from coordination.db import Database, _configure_sqlite
from coordination.service import CoordinationService


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


# --- TTL floor: narrowed / coexist must not inherit a shortened TTL ---------


async def _file_request_with_shortening(
    db: Database,
    *,
    claim_id: str,
    requester_engineer: str = "bob",
    requester_session_id: str | None = "requester-sess",
    short_ttl_seconds: int = 30,
) -> dict:
    """File a request that actually shortens the claim TTL (unlike the
    standard _file_request helper which leaves TTL untouched)."""
    rid = str(uuid4())
    rows = await db.list_active_claims_rows()
    claim = next(r for r in rows if r["id"] == claim_id)
    original = str(claim["expires_at"])
    shortened = _iso(datetime.now(UTC) + timedelta(seconds=short_ttl_seconds))
    return await db.create_request(
        request_id=rid,
        claim_id=claim_id,
        requester_engineer=requester_engineer,
        requester_session_id=requester_session_id,
        requested_pattern=str(claim["pattern"]),
        reason="urgent",
        urgency="high",
        original_expires_at=original,
        shortened_expires_at=shortened,
        new_claim_expires_at=shortened,  # actually shorten the claim TTL
    )


@pytest.mark.asyncio
async def test_narrowed_floors_ttl_when_claim_was_shortened(db: Database) -> None:
    """When the holder's claim TTL was shortened by request_release, the
    new narrowed claim must receive at least min_expires_at rather than
    inheriting the compressed deadline."""
    cid = await _seed_claim(db, pattern="src/api/**", ttl_hours=4)
    req = await _file_request_with_shortening(db, claim_id=cid, short_ttl_seconds=30)

    min_exp = _iso(datetime.now(UTC) + timedelta(hours=1))
    result = await db.respond_to_request(
        request_id=req["id"],
        decision="narrowed",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        narrowed_pattern="src/api/billing/**",
        min_expires_at=min_exp,
    )
    assert result is not None

    active = await db.list_active_claims_rows()
    nc = next((c for c in active if c["engineer"] == "alice"), None)
    assert nc is not None, "new narrowed claim must exist"
    assert nc["expires_at"] >= min_exp, (
        f"narrowed claim TTL {nc['expires_at']!r} must be >= floor {min_exp!r}"
    )


@pytest.mark.asyncio
async def test_narrowed_does_not_floor_when_original_ttl_is_healthy(
    db: Database,
) -> None:
    """When no shortening occurred (min_expires_at is None), the narrowed
    claim inherits the original TTL unchanged."""
    cid = await _seed_claim(db, pattern="src/api/**", ttl_hours=4)
    rows = await db.list_active_claims_rows()
    original_exp = next(r for r in rows if r["id"] == cid)["expires_at"]

    req = await _file_request(db, claim_id=cid, requested_pattern="src/api/**")
    await db.respond_to_request(
        request_id=req["id"],
        decision="narrowed",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        narrowed_pattern="src/api/billing/**",
        min_expires_at=None,
    )
    active = await db.list_active_claims_rows()
    nc = next((c for c in active if c["engineer"] == "alice"), None)
    assert nc is not None
    assert nc["expires_at"] == original_exp


@pytest.mark.asyncio
async def test_coexist_floors_ttl_when_claim_was_shortened(db: Database) -> None:
    """The requester's sibling claim must receive at least min_expires_at
    rather than inheriting the holder's shortened TTL."""
    holder_cid = await _seed_claim(
        db, engineer="alice", pattern="src/api/handlers.py", ttl_hours=4
    )
    req = await _file_request_with_shortening(
        db, claim_id=holder_cid, short_ttl_seconds=30
    )

    min_exp = _iso(datetime.now(UTC) + timedelta(hours=1))
    result = await db.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_pattern="src/api/handlers.py",
        min_expires_at=min_exp,
    )
    assert result is not None

    active = await db.list_active_claims_rows()
    bob_claims = [c for c in active if c["engineer"] == "bob"]
    assert len(bob_claims) == 1
    assert bob_claims[0]["expires_at"] >= min_exp, (
        f"coexist claim TTL {bob_claims[0]['expires_at']!r} must be >= floor {min_exp!r}"
    )


@pytest.mark.asyncio
async def test_coexist_does_not_floor_when_holder_ttl_is_healthy(
    db: Database,
) -> None:
    """When no shortening occurred (min_expires_at is None), the coexist
    sibling inherits the holder's existing TTL unchanged."""
    holder_cid = await _seed_claim(
        db, engineer="alice", pattern="src/api/handlers.py", ttl_hours=4
    )
    rows = await db.list_active_claims_rows()
    holder_exp = next(r for r in rows if r["id"] == holder_cid)["expires_at"]

    req = await _file_request(db, claim_id=holder_cid)
    await db.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_pattern="src/api/handlers.py",
        min_expires_at=None,
    )
    active = await db.list_active_claims_rows()
    bob_claims = [c for c in active if c["engineer"] == "bob"]
    assert len(bob_claims) == 1
    assert bob_claims[0]["expires_at"] == holder_exp


# --- ttl_shortened column ----------------------------------------------------


@pytest.mark.asyncio
async def test_create_request_stamps_ttl_shortened_on_claim(db: Database) -> None:
    """create_request sets ttl_shortened=1 on the claim row when the TTL
    is actually shortened. If the existing TTL is already shorter than
    the requested shortened value, the column stays 0."""
    cid = await _seed_claim(db, ttl_hours=4)
    rows = await db.list_active_claims_rows()
    original_exp = next(r for r in rows if r["id"] == cid)["expires_at"]
    shortened = _iso(datetime.now(UTC) + timedelta(seconds=60))

    await db.create_request(
        request_id=str(uuid4()),
        claim_id=cid,
        requester_engineer="bob",
        requester_session_id=None,
        requested_pattern="src/foo.py",
        reason=None,
        urgency="normal",
        original_expires_at=original_exp,
        shortened_expires_at=shortened,
        new_claim_expires_at=shortened,
    )

    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT ttl_shortened FROM claims WHERE id = ?", (cid,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["ttl_shortened"] == 1


@pytest.mark.asyncio
async def test_denied_resets_ttl_shortened(db: Database) -> None:
    """A 'denied' decision restores the original TTL and must also reset
    ttl_shortened=0 so the claim is not mislabelled if it expires later."""
    cid = await _seed_claim(db, ttl_hours=4)
    rows = await db.list_active_claims_rows()
    original_exp = next(r for r in rows if r["id"] == cid)["expires_at"]
    shortened = _iso(datetime.now(UTC) + timedelta(seconds=60))

    req = await db.create_request(
        request_id=str(uuid4()),
        claim_id=cid,
        requester_engineer="bob",
        requester_session_id=None,
        requested_pattern="src/foo.py",
        reason=None,
        urgency="normal",
        original_expires_at=original_exp,
        shortened_expires_at=shortened,
        new_claim_expires_at=shortened,
    )
    await db.respond_to_request(
        request_id=req["id"],
        decision="denied",
        actor_engineer="alice",
        actor_session_id=None,
    )

    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT expires_at, ttl_shortened FROM claims WHERE id = ?", (cid,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row["ttl_shortened"] == 0, "denied must reset ttl_shortened"
    assert row["expires_at"] == original_exp, "denied must restore original TTL"


# --- v0.35: symbol-scoped coexist -------------------------------------------


async def _seed_symbol_claim(
    db: Database,
    *,
    engineer: str,
    pattern: str,
    symbols: list[str],
    repo: str = "amittell/coord",
    session_id: str | None = "sym-sess",
    ttl_hours: int = 4,
) -> str:
    """Insert a symbol-scope claim: a file row flipped to
    scope_type='symbol' plus one claim_symbols row per symbol path. The
    "Parent::child" notation is split into (parent_symbol, symbol_name)
    exactly as the production insert path does."""
    cid = await _seed_claim(
        db,
        engineer=engineer,
        pattern=pattern,
        repo=repo,
        session_id=session_id,
        branch=None,
        description="symbol holder",
        ttl_hours=ttl_hours,
    )
    async with aiosqlite.connect(db.path) as conn:
        await _configure_sqlite(conn)
        await conn.execute(
            "UPDATE claims SET scope_type = 'symbol', narrowable = 0 WHERE id = ?",
            (cid,),
        )
        await conn.commit()
    rows: list[tuple[str, str, str, str, str, str | None]] = []
    for raw in symbols:
        if "::" in raw:
            parent, _, leaf = raw.rpartition("::")
        else:
            parent, leaf = None, raw
        rows.append((str(uuid4()), cid, pattern, leaf, "unknown", parent))
    await db.insert_claim_symbols(rows=rows)
    return cid


def _service(db: Database) -> CoordinationService:
    return CoordinationService(db=db, settings=Settings())


async def _symbol_claims_for(db: Database, claim_id: str) -> set[str]:
    """Return the set of canonical symbol paths recorded for a claim."""
    rows = await db.get_claim_symbols(claim_id)
    out: set[str] = set()
    for r in rows:
        parent = r.get("parent_symbol")
        name = str(r["symbol_name"])
        out.add(f"{parent}::{name}" if parent else name)
    return out


@pytest.mark.asyncio
async def test_symbol_coexist_disjoint_grants_symbol_claim(db: Database) -> None:
    """A symbol coexist with disjoint symbols succeeds: both claims stay
    active and mutually linked, and the requester's new claim is
    symbol-scoped carrying exactly the granted claim_symbols."""
    holder_cid = await _seed_symbol_claim(
        db,
        engineer="alice",
        pattern="src/auth/login.ts",
        symbols=["Login::handleLogin"],
    )
    # Requester already holds a disjoint symbol claim on the same file.
    await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/auth/login.ts",
        symbols=["Login::validateCredentials", "Login::logSignin"],
        session_id="bob-sess",
    )
    req = await _file_request(
        db, claim_id=holder_cid, requested_pattern="src/auth/login.ts"
    )

    svc = _service(db)
    result = await svc.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_symbols={"src/auth/login.ts": ["Login::validateCredentials"]},
    )
    assert result is not None
    assert result["decision"] == "coexist"

    active = await db.list_active_claims_rows()
    by_id = {c["id"]: c for c in active}
    assert holder_cid in by_id, "holder claim must remain active"
    # The new coexist sibling is the bob claim that is symbol-scoped and
    # linked to the holder.
    grant = next(
        c
        for c in active
        if c["engineer"] == "bob"
        and holder_cid in (json.loads(c["coexists_with"]) if c["coexists_with"] else [])
    )
    assert grant["scope_type"] == "symbol"
    assert await _symbol_claims_for(db, grant["id"]) == {"Login::validateCredentials"}

    # Mutual coexists_with linkage.
    holder_partners = json.loads(by_id[holder_cid]["coexists_with"])
    assert grant["id"] in holder_partners
    assert holder_cid in json.loads(grant["coexists_with"])


@pytest.mark.asyncio
async def test_symbol_coexist_rejects_file_outside_holder_claim(db: Database) -> None:
    """A holder cannot grant a symbol coexist on a file outside this
    request's subject -- e.g. an unrelated claim the requester also holds
    on a different file. The grant is restricted to files the holder
    actually claims, closing the cross-file consent hole."""
    holder_cid = await _seed_symbol_claim(
        db,
        engineer="alice",
        pattern="src/auth/login.ts",
        symbols=["Login::handleLogin"],
    )
    # Bob holds the on-subject claim on login.ts ...
    await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/auth/login.ts",
        symbols=["Login::validateCredentials"],
        session_id="bob-sess",
    )
    # ... and a completely unrelated symbol claim on another file.
    await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/billing/charge.ts",
        symbols=["Charge::run"],
        session_id="bob-sess",
    )
    req = await _file_request(
        db, claim_id=holder_cid, requested_pattern="src/auth/login.ts"
    )

    svc = _service(db)
    # The holder claims nothing on charge.ts, so granting a sibling there
    # (borrowing bob's unrelated claim) must be refused.
    with pytest.raises(ValueError, match="holder's symbol claim"):
        await svc.respond_to_request(
            request_id=req["id"],
            decision="coexist",
            actor_engineer="alice",
            actor_session_id="holder-sess",
            coexist_symbols={"src/billing/charge.ts": ["Charge::run"]},
        )
    # No sibling was minted on the unrelated file.
    active = await db.list_active_claims_rows()
    charge_grants = [
        c
        for c in active
        if c["engineer"] == "bob"
        and "charge.ts" in str(c["pattern"])
        and c["coexists_with"]
    ]
    assert charge_grants == []


@pytest.mark.asyncio
async def test_symbol_coexist_overlapping_symbols_rejected(db: Database) -> None:
    """When the granted symbols overlap the holder's claimed symbols
    under the prefix rule, the grant is refused (400) so a coexist
    cannot hide a real symbol conflict."""
    holder_cid = await _seed_symbol_claim(
        db,
        engineer="alice",
        pattern="src/auth/login.ts",
        symbols=["Login"],  # whole class covers every method
    )
    await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/auth/login.ts",
        symbols=["Login::handleLogin"],
        session_id="bob-sess",
    )
    req = await _file_request(
        db, claim_id=holder_cid, requested_pattern="src/auth/login.ts"
    )

    svc = _service(db)
    with pytest.raises(ValueError, match="overlaps"):
        await svc.respond_to_request(
            request_id=req["id"],
            decision="coexist",
            actor_engineer="alice",
            actor_session_id="holder-sess",
            coexist_symbols={"src/auth/login.ts": ["Login::handleLogin"]},
        )


@pytest.mark.asyncio
async def test_symbol_coexist_requires_subset_of_requester_claim(
    db: Database,
) -> None:
    """A symbol that the requester never claimed cannot be granted."""
    holder_cid = await _seed_symbol_claim(
        db,
        engineer="alice",
        pattern="src/auth/login.ts",
        symbols=["Login::handleLogin"],
    )
    await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/auth/login.ts",
        symbols=["Login::validateCredentials"],
        session_id="bob-sess",
    )
    req = await _file_request(
        db, claim_id=holder_cid, requested_pattern="src/auth/login.ts"
    )

    svc = _service(db)
    with pytest.raises(ValueError, match="not within the requester"):
        await svc.respond_to_request(
            request_id=req["id"],
            decision="coexist",
            actor_engineer="alice",
            actor_session_id="holder-sess",
            # bob never claimed logSignin.
            coexist_symbols={"src/auth/login.ts": ["Login::logSignin"]},
        )


@pytest.mark.asyncio
async def test_symbol_coexist_rejected_when_holder_is_file_scoped(
    db: Database,
) -> None:
    """coexist_symbols against a file-scoped holder is a contract
    violation (400): symbol coexist needs both sides symbol-scoped."""
    holder_cid = await _seed_claim(
        db, engineer="alice", pattern="src/auth/login.ts"
    )
    await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/auth/login.ts",
        symbols=["Login::validateCredentials"],
        session_id="bob-sess",
    )
    req = await _file_request(
        db, claim_id=holder_cid, requested_pattern="src/auth/login.ts"
    )

    svc = _service(db)
    with pytest.raises(ValueError, match="symbol-scoped"):
        await svc.respond_to_request(
            request_id=req["id"],
            decision="coexist",
            actor_engineer="alice",
            actor_session_id="holder-sess",
            coexist_symbols={"src/auth/login.ts": ["Login::validateCredentials"]},
        )


@pytest.mark.asyncio
async def test_symbol_coexist_rejected_when_requester_not_symbol_scoped(
    db: Database,
) -> None:
    """The requester must hold an active symbol-scoped claim; a bare
    file-scope requester cannot receive a symbol coexist grant."""
    holder_cid = await _seed_symbol_claim(
        db,
        engineer="alice",
        pattern="src/auth/login.ts",
        symbols=["Login::handleLogin"],
    )
    # bob holds only a file-scope claim elsewhere.
    await _seed_claim(
        db, engineer="bob", pattern="src/other.ts", session_id="bob-sess"
    )
    req = await _file_request(
        db, claim_id=holder_cid, requested_pattern="src/auth/login.ts"
    )

    svc = _service(db)
    with pytest.raises(ValueError, match="symbol-scoped claim"):
        await svc.respond_to_request(
            request_id=req["id"],
            decision="coexist",
            actor_engineer="alice",
            actor_session_id="holder-sess",
            coexist_symbols={"src/auth/login.ts": ["Login::handleLogin"]},
        )


@pytest.mark.asyncio
async def test_symbol_coexist_audit_event_records_coexist_symbols(
    db: Database,
) -> None:
    """The responded audit event and the request row both carry the
    granted coexist_symbols for the operator timeline."""
    holder_cid = await _seed_symbol_claim(
        db,
        engineer="alice",
        pattern="src/auth/login.ts",
        symbols=["Login::handleLogin"],
    )
    await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/auth/login.ts",
        symbols=["Login::validateCredentials"],
        session_id="bob-sess",
    )
    req = await _file_request(
        db, claim_id=holder_cid, requested_pattern="src/auth/login.ts"
    )
    grant = {"src/auth/login.ts": ["Login::validateCredentials"]}

    svc = _service(db)
    await svc.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_symbols=grant,
    )

    events = await db.list_request_events(req["id"])
    responded = next(e for e in events if e["event_type"] == "responded")
    detail = json.loads(responded["detail"])
    assert detail["decision"] == "coexist"
    assert detail["coexist_symbols"] == grant
    assert detail["holder_claim_id"] == holder_cid

    # Round-trips on the request row too.
    fresh = await db.get_request(req["id"])
    assert fresh is not None
    assert json.loads(fresh["coexist_symbols"]) == grant


@pytest.mark.asyncio
async def test_file_scope_coexist_pattern_still_works_unchanged(
    db: Database,
) -> None:
    """File-scope coexist (coexist_pattern, no coexist_symbols) is
    byte-identical to pre-v0.35: a file-scoped sibling claim, no
    claim_symbols, and no coexist_symbols on the request row."""
    holder_cid = await _seed_claim(
        db, engineer="alice", pattern="src/api/handlers.py"
    )
    req = await _file_request(db, claim_id=holder_cid)

    result = await db.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_pattern="src/api/handlers.py",
    )
    assert result is not None
    assert result["decision"] == "coexist"

    active = await db.list_active_claims_rows()
    bob_claims = [c for c in active if c["engineer"] == "bob"]
    assert len(bob_claims) == 1
    assert bob_claims[0]["scope_type"] == "file"
    assert await _symbol_claims_for(db, bob_claims[0]["id"]) == set()

    fresh = await db.get_request(req["id"])
    assert fresh is not None
    assert fresh["coexist_symbols"] is None

    events = await db.list_request_events(req["id"])
    responded = next(e for e in events if e["event_type"] == "responded")
    detail = json.loads(responded["detail"])
    assert detail["coexist_pattern"] == "src/api/handlers.py"
    assert "coexist_symbols" not in detail


# --- v0.35 conflict-engine: symbol-aware partner exclusion ------------------
#
# Two symbol-scoped claims that coexist on a file share that file only on
# DISJOINT symbols. A later THIRD claim from one partner's own session must
# therefore be judged against the OTHER partner's granted symbols via the
# normal symbol-overlap path -- it 409s when it hits them and auto-coexists
# when it doesn't. A file-scoped coexist partner keeps the legacy
# blanket-skip (the pair agreed to share the whole file).


async def _seed_symbol_coexist_pair(db: Database) -> tuple[str, str, str]:
    """Set up a symbol coexist pair on ``src/auth/login.ts``:

    - alice (session 'holder-sess') holds ``Login::handleLogin``.
    - bob (session 'bob-sess') holds ``Login::validateCredentials``.
    - alice grants bob a symbol coexist on ``Login::validateCredentials``.

    Returns ``(alice_claim_id, bob_original_claim_id, bob_coexist_claim_id)``.
    After this, bob's session has alice's claim as a symbol-scoped coexist
    partner -- the row that v0.35 must NOT blanket-skip on bob's next claim.
    """
    alice_cid = await _seed_symbol_claim(
        db,
        engineer="alice",
        pattern="src/auth/login.ts",
        symbols=["Login::handleLogin"],
        session_id="holder-sess",
    )
    bob_cid = await _seed_symbol_claim(
        db,
        engineer="bob",
        pattern="src/auth/login.ts",
        symbols=["Login::validateCredentials"],
        session_id="bob-sess",
    )
    req = await _file_request(
        db,
        claim_id=alice_cid,
        requester_session_id="bob-sess",
        requested_pattern="src/auth/login.ts",
    )
    svc = _service(db)
    result = await svc.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_symbols={"src/auth/login.ts": ["Login::validateCredentials"]},
    )
    assert result is not None and result["decision"] == "coexist"
    active = await db.list_active_claims_rows()
    bob_grant = next(
        c
        for c in active
        if c["engineer"] == "bob"
        and alice_cid in (json.loads(c["coexists_with"]) if c["coexists_with"] else [])
    )
    return alice_cid, bob_cid, str(bob_grant["id"])


@pytest.mark.asyncio
async def test_third_claim_409s_against_symbol_coexist_partner(
    db: Database,
) -> None:
    """A third claim by bob's session that hits alice's granted symbol must
    409: a symbol-scoped coexist partner is re-evaluated, not blanket-skipped,
    so the real collision surfaces."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    alice_cid, _bob_cid, _bob_grant = await _seed_symbol_coexist_pair(db)

    svc = _service(db)
    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="bob-sess",
            repo="amittell/coord",
            claims=[
                ClaimItem(
                    type="file",
                    pattern="src/auth/login.ts",
                    symbols=["Login::handleLogin"],
                )
            ],
        )
    )
    assert result.claim_ids == [], "collision with partner's symbol must 409"
    assert result.conflicts, "expected a conflict entry against alice's symbol"
    entry = result.conflicts[0]
    assert str(entry.conflicting_claim.id) == alice_cid
    so = entry.symbol_overlap
    assert so and "Login::handleLogin" in so[0].symbols


@pytest.mark.asyncio
async def test_third_claim_auto_coexists_when_disjoint_from_partner(
    db: Database,
) -> None:
    """A third claim by bob's session on a symbol disjoint from BOTH the
    partner's and bob's own symbols auto-coexists (no 409): the symbol-scoped
    partner stays visible but the overlap engine finds them disjoint."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    await _seed_symbol_coexist_pair(db)

    svc = _service(db)
    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="bob-sess",
            repo="amittell/coord",
            claims=[
                ClaimItem(
                    type="file",
                    pattern="src/auth/login.ts",
                    symbols=["Login::logout"],
                )
            ],
        )
    )
    assert result.conflicts == [], "disjoint symbol must not 409"
    assert result.claim_ids, "disjoint third claim should be granted"
    new_cid = result.claim_ids[0]
    assert await _symbol_claims_for(db, new_cid) == {"Login::logout"}


@pytest.mark.asyncio
async def test_file_scope_coexist_partner_still_blanket_skipped(
    db: Database,
) -> None:
    """Regression: a FILE-scoped coexist partner keeps the pre-v0.35
    blanket-skip. After alice and bob file-coexist on a whole file, bob's
    next claim on that same file does NOT 409 against alice."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    alice_cid = await _seed_claim(
        db,
        engineer="alice",
        pattern="src/api/handlers.py",
        session_id="holder-sess",
    )
    # bob requests from his own session so the granted file sibling lands
    # in 'bob-sess' -- the real coexist flow -- making alice's claim bob's
    # file-scoped coexist partner.
    req = await _file_request(
        db,
        claim_id=alice_cid,
        requester_session_id="bob-sess",
        requested_pattern="src/api/handlers.py",
    )
    svc = _service(db)
    granted = await svc.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="alice",
        actor_session_id="holder-sess",
        coexist_pattern="src/api/handlers.py",
    )
    assert granted is not None and granted["decision"] == "coexist"

    # bob's next whole-file claim must be invisible to alice's file partner.
    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="bob-sess",
            repo="amittell/coord",
            claims=[ClaimItem(type="file", pattern="src/api/handlers.py")],
        )
    )
    assert result.conflicts == [], (
        "file-scoped coexist partner must stay blanket-skipped"
    )
    assert result.claim_ids, "bob's file claim should be granted"
