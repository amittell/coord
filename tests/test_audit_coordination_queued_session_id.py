"""Audit: session_id widening of the queued-request listing.

The MCP wrapper always sends its own session_id alongside the requester
name on my_requests(queued=True). The server threads that through
GET /requests?queued=true into Database.list_queued_with_holder, which
widens (ORs) the requester filter: a queue row matches when its
requester_engineer equals the named engineer OR its
requester_session_id equals the caller's session. An MCP client whose
engineer name drifted since enqueue time (renamed worker, regenerated
identity) therefore still sees its own queue entries.

Covers:
- DB-level OR semantics (engineer-only, session-only, both, neither);
- endpoint plumbing: the session_id query param reaches the DB call and
  defaults to None when omitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from coordination.config import Settings
from coordination.db import Database
from coordination.main import app
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService

REPO = "amittell/coord"
SHARED = "shared-test-token"
_SHARED_AUTH = {"Authorization": f"Bearer {SHARED}"}


# ---------------------------------------------------------------------------
# DB-level OR semantics
# ---------------------------------------------------------------------------


@pytest.fixture()
async def svc(tmp_path: Path) -> CoordinationService:
    db_path = tmp_path / "queued_session_id.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
    )
    return CoordinationService(db=db, settings=settings)


async def _seed_queue_row(
    svc: CoordinationService,
    *,
    requester_engineer: str,
    requester_session_id: str | None,
    pattern: str,
) -> dict[str, Any]:
    resp = await svc.create_claims(
        CreateClaimsRequest(
            engineer="holder",
            repo=REPO,
            claims=[ClaimItem(type="file", pattern=pattern)],
        )
    )
    assert resp.claim_ids, f"seed claim failed: {resp}"
    return await svc.db.enqueue_claim_request(
        blocking_claim_id=resp.claim_ids[0],
        requester_engineer=requester_engineer,
        requester_session_id=requester_session_id,
        requester_branch=None,
        requester_description=None,
        repo=REPO,
        claim_type="file",
        pattern=pattern,
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="normal",
    )


async def test_list_queued_ors_engineer_and_session(
    svc: CoordinationService,
) -> None:
    """A renamed worker (engineer name drifted since enqueue) still sees
    its row via the session_id leg of the OR; other sessions' rows stay
    out."""
    mine = await _seed_queue_row(
        svc,
        requester_engineer="old-name",
        requester_session_id="sess-mine",
        pattern="src/a.py",
    )
    other = await _seed_queue_row(
        svc,
        requester_engineer="carol",
        requester_session_id="sess-other",
        pattern="src/b.py",
    )

    # Engineer-only: exact name match, session ignored.
    rows = await svc.db.list_queued_with_holder(engineer="old-name")
    assert {r["id"] for r in rows} == {mine["id"]}

    # Drifted name alone matches nothing...
    rows = await svc.db.list_queued_with_holder(engineer="new-name")
    assert rows == []

    # ...but the session leg of the OR recovers the row.
    rows = await svc.db.list_queued_with_holder(
        engineer="new-name", session_id="sess-mine"
    )
    assert {r["id"] for r in rows} == {mine["id"]}

    # The OR is inclusive: a name match plus a session match on two
    # different rows returns both.
    rows = await svc.db.list_queued_with_holder(
        engineer="carol", session_id="sess-mine"
    )
    assert {r["id"] for r in rows} == {mine["id"], other["id"]}

    # Session-only filtering works without an engineer name.
    rows = await svc.db.list_queued_with_holder(session_id="sess-other")
    assert {r["id"] for r in rows} == {other["id"]}


# ---------------------------------------------------------------------------
# endpoint plumbing: session_id query param -> DB call
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


async def test_queued_listing_threads_session_id_param(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordination.deps import get_service

    svc = get_service()
    seen: list[dict] = []

    async def rec_list(
        *, engineer=None, session_id=None, state="waiting", repo=None, limit=100
    ):
        seen.append({"engineer": engineer, "session_id": session_id})
        return []

    monkeypatch.setattr(svc.db, "list_queued_with_holder", rec_list)

    r = await client.get(
        "/requests?queued=true&requester=eng&session_id=sess-x",
        headers=_SHARED_AUTH,
    )
    assert r.status_code == 200, r.text
    assert seen[-1] == {"engineer": "eng", "session_id": "sess-x"}

    # Omitted param defaults to None: the legacy engineer-only filter.
    r = await client.get(
        "/requests?queued=true&requester=eng", headers=_SHARED_AUTH
    )
    assert r.status_code == 200, r.text
    assert seen[-1] == {"engineer": "eng", "session_id": None}
