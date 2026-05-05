from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination.db import Database
from coordination.main import app


class _RecordingHandler(logging.Handler):
    """Capture log records emitted on the coordination.access logger.

    We install this directly on the ``coordination.access`` logger
    because :func:`configure_logging` sets ``propagate=False`` on the
    parent ``coordination`` logger, which prevents pytest's ``caplog``
    (rooted at the root logger) from observing access-log records.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        self.records.append(record)


@pytest.fixture()
def access_log_records() -> list[logging.LogRecord]:
    logger = logging.getLogger("coordination.access")
    handler = _RecordingHandler()
    prior_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


@pytest.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    # Fresh service per test process: clear lru cache on deps.get_service
    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


@pytest.mark.asyncio
async def test_health_no_auth(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.asyncio
async def test_readyz_reports_metadata(client: AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["auth_mode"] == "bearer"
    assert body["database_path"].endswith("db.sqlite")


@pytest.mark.asyncio
async def test_claims_flow(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/claims",
        json={
            "engineer": "alice",
            "branch": "alice/test",
            "description": "test",
            "claims": [{"type": "file", "pattern": "src/auth/**"}],
            "ttl_hours": 4,
        },
        headers=h,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["claim_ids"]
    assert data["conflicts"] == []

    r2 = await client.get("/conflicts", params=[("pattern", "src/auth/foo.ts"), ("engineer", "bob")], headers=h)
    assert r2.status_code == 200
    body = r2.json()
    assert body["has_conflicts"] is True
    assert body.get("safe") is False
    assert body.get("suggestion")

    r3 = await client.post(
        "/claims",
        json={
            "engineer": "bob",
            "claims": [{"type": "file", "pattern": "src/auth/foo.ts"}],
        },
        headers=h,
    )
    assert r3.status_code == 409
    body3 = r3.json()
    assert body3["claim_ids"] == []
    assert body3["conflicts"]
    assert "options" in body3

    rel = await client.post(
        "/claims/release",
        json={"claim_ids": data["claim_ids"], "engineer": "alice"},
        headers=h,
    )
    assert rel.status_code == 200
    assert rel.json()["released"] >= 1


@pytest.mark.asyncio
async def test_ownership_hard_severity(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}
    yaml_text = """
modules:
  shared:
    paths: ["src/shared/**"]
    severity: hard
    owners: [all]
"""
    r = await client.post("/config/ownership", content=yaml_text, headers=h)
    assert r.status_code == 200

    r2 = await client.post(
        "/claims",
        json={"engineer": "alice", "claims": [{"type": "file", "pattern": "src/shared/types.ts"}]},
        headers=h,
    )
    assert r2.status_code == 200
    ids = r2.json()["claim_ids"]
    assert ids

    db = Database(Path(os.environ["COORD_DATABASE_PATH"]))
    rows = await db.list_active_claims_rows()
    assert any(x["severity"] == "hard" for x in rows)


@pytest.mark.asyncio
async def test_invalid_ownership_yaml_is_rejected(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post("/config/ownership", content="modules: []", headers=h)
    assert r.status_code == 400
    assert "modules" in r.json()["detail"]


@pytest.mark.asyncio
async def test_negation_pattern_in_claim_returns_400(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/claims",
        json={
            "engineer": "alice",
            "claims": [{"type": "file", "pattern": "!src/auth/**"}],
        },
        headers=h,
    )
    assert r.status_code == 400
    body = r.json()
    assert "negation" in str(body).lower() or "!" in str(body)


@pytest.mark.asyncio
async def test_negation_pattern_in_conflicts_returns_400(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.get(
        "/conflicts",
        params=[("pattern", "!src/auth/**"), ("engineer", "alice")],
        headers=h,
    )
    assert r.status_code == 400
    body = r.json()
    assert "negation" in str(body).lower() or "!" in str(body)


@pytest.mark.asyncio
async def test_request_id_echoed_when_client_provides_one(client: AsyncClient) -> None:
    r = await client.get("/health", headers={"X-Request-ID": "custom-123"})
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID") == "custom-123"


@pytest.mark.asyncio
async def test_request_id_generated_when_absent(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid
    assert len(rid) >= 8


@pytest.mark.asyncio
async def test_request_id_survives_an_http_exception(client: AsyncClient) -> None:
    # Auth-gated endpoint without a bearer token returns 401 via
    # HTTPException raised in a dependency. The middleware must still
    # stamp X-Request-ID on the response.
    r = await client.get("/claims", headers={"X-Request-ID": "err-trace-42"})
    assert r.status_code == 401
    assert r.headers.get("X-Request-ID") == "err-trace-42"


@pytest.mark.asyncio
async def test_access_log_emitted_per_request(
    client: AsyncClient, access_log_records: list[logging.LogRecord]
) -> None:
    r = await client.get("/health")
    assert r.status_code == 200

    access_records = [
        rec for rec in access_log_records if rec.name == "coordination.access"
    ]
    assert len(access_records) == 1, (
        f"expected exactly one access log record, got {len(access_records)}"
    )
    rec = access_records[0]
    assert rec.levelno == logging.INFO
    assert getattr(rec, "event", None) == "http_request"
    assert getattr(rec, "method", None) == "GET"
    assert getattr(rec, "path", None) == "/health"
    assert getattr(rec, "status", None) == 200
    duration = getattr(rec, "duration_ms", None)
    assert isinstance(duration, (int, float))
    assert duration >= 0.0


@pytest.mark.asyncio
async def test_access_log_includes_request_id(
    client: AsyncClient, access_log_records: list[logging.LogRecord]
) -> None:
    r = await client.get("/health", headers={"X-Request-ID": "custom-xyz"})
    assert r.status_code == 200
    access_records = [
        rec for rec in access_log_records if rec.name == "coordination.access"
    ]
    assert len(access_records) == 1
    assert getattr(access_records[0], "request_id", None) == "custom-xyz"


@pytest.mark.asyncio
async def test_access_log_uses_matched_route_template_for_path(
    client: AsyncClient, access_log_records: list[logging.LogRecord]
) -> None:
    # Create a claim first so we can address it by id
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/claims",
        json={
            "engineer": "alice",
            "claims": [{"type": "file", "pattern": "src/routing/one.ts"}],
        },
        headers=h,
    )
    assert r.status_code == 200
    claim_id = r.json()["claim_ids"][0]

    # Reset captured records so we only inspect the DELETE call below
    access_log_records.clear()

    r2 = await client.delete(f"/claims/{claim_id}", headers=h)
    assert r2.status_code == 200

    access_records = [
        rec for rec in access_log_records if rec.name == "coordination.access"
    ]
    assert len(access_records) == 1
    rec = access_records[0]
    # Matched route template, not the substituted path; this keeps
    # label cardinality bounded for log aggregation.
    assert getattr(rec, "path", None) == "/claims/{claim_id}"
    assert getattr(rec, "method", None) == "DELETE"
    assert getattr(rec, "status", None) == 200


@pytest.mark.asyncio
async def test_create_claim_accepts_repo_and_persists_it(
    client: AsyncClient,
) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "alice",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/foo.py"}],
        },
    )
    assert r.status_code == 200, r.text
    claim_ids = r.json()["claim_ids"]
    assert len(claim_ids) == 1

    # Read back via the API and confirm repo round-trips.
    r = await client.get("/claims?active_only=true", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["claims"][0]["repo"] == "amittell/coord"


@pytest.mark.asyncio
async def test_create_claim_without_repo_stores_null(client: AsyncClient) -> None:
    """Backward compat: clients that don't supply repo still work."""
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "alice",
            "claims": [{"type": "file", "pattern": "src/foo.py"}],
        },
    )
    assert r.status_code == 200, r.text
    r = await client.get("/claims?active_only=true", headers=h)
    body = r.json()
    assert body["claims"][0]["repo"] is None


@pytest.mark.asyncio
async def test_list_claims_filters_by_repo(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}

    for repo, pat in [
        ("amittell/coord", "src/a.py"),
        ("amittell/coord", "src/b.py"),
        ("amittell/bastionx", "services/x.py"),
    ]:
        r = await client.post(
            "/claims",
            headers=h,
            json={
                "engineer": "alice",
                "repo": repo,
                "claims": [{"type": "file", "pattern": pat}],
            },
        )
        assert r.status_code == 200, r.text

    r = await client.get(
        "/claims?active_only=true&repo=amittell/coord", headers=h
    )
    body = r.json()
    assert body["count"] == 2
    assert all(c["repo"] == "amittell/coord" for c in body["claims"])


@pytest.mark.asyncio
async def test_repos_endpoint_aggregates_per_repo_stats(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}

    for repo, eng, pat in [
        ("amittell/coord", "alice", "src/a.py"),
        ("amittell/coord", "bob", "src/b.py"),
        ("amittell/bastionx", "alice", "services/x.py"),
    ]:
        r = await client.post(
            "/claims",
            headers=h,
            json={
                "engineer": eng,
                "repo": repo,
                "claims": [{"type": "file", "pattern": pat}],
            },
        )
        assert r.status_code == 200, r.text

    r = await client.get("/repos", headers=h)
    assert r.status_code == 200
    body = r.json()
    by_name = {x["repo"]: x for x in body["repos"]}
    assert by_name["amittell/coord"]["claims_24h"] == 2
    assert by_name["amittell/coord"]["engineers_24h"] == 2
    assert by_name["amittell/coord"]["active_claims"] == 2
    assert by_name["amittell/bastionx"]["claims_24h"] == 1


@pytest.mark.asyncio
async def test_repos_endpoint_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/repos")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_claims_no_cross_repo_conflict(client: AsyncClient) -> None:
    """End-to-end: same pattern under different repos must not 409."""
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "module", "pattern": "client/js/**"}],
        },
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "alice",
            "repo": "amittell/astrowars",
            "claims": [{"type": "module", "pattern": "client/js/**"}],
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_conflicts_endpoint_filters_by_repo(client: AsyncClient) -> None:
    """GET /conflicts?repo=X must only consider claims from repo X."""
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "module", "pattern": "client/js/**"}],
        },
    )
    assert r.status_code == 200, r.text

    # Cross-repo: clean.
    r = await client.get(
        "/conflicts?pattern=client/js/foo.ts&engineer=alice&repo=amittell/astrowars",
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_conflicts"] is False

    # Same-repo: still flagged.
    r = await client.get(
        "/conflicts?pattern=client/js/foo.ts&engineer=alice&repo=amittell/coord",
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_conflicts"] is True


@pytest.mark.asyncio
async def test_create_claims_self_excludes_within_session(client: AsyncClient) -> None:
    """End-to-end: a subagent with the same session_id but a different
    engineer name is not blocked by the prior subagent's overlapping claim."""
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "codex-server",
            "repo": "amittell/astrowars",
            "session_id": "sess-xyz",
            "claims": [{"type": "module", "pattern": "server/**"}],
        },
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "codex-shared",
            "repo": "amittell/astrowars",
            "session_id": "sess-xyz",
            "claims": [{"type": "module", "pattern": "server/**"}],
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_conflicts_endpoint_honors_session_id(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "codex-foo",
            "repo": "amittell/astrowars",
            "session_id": "sess-1",
            "claims": [{"type": "module", "pattern": "server/**"}],
        },
    )
    assert r.status_code == 200, r.text

    # Same session, different engineer name: clean.
    r = await client.get(
        "/conflicts?pattern=server/x.js&engineer=codex-bar"
        "&repo=amittell/astrowars&session_id=sess-1",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["has_conflicts"] is False

    # Different session: adversarial.
    r = await client.get(
        "/conflicts?pattern=server/x.js&engineer=codex-bar"
        "&repo=amittell/astrowars&session_id=sess-2",
        headers=h,
    )
    assert r.json()["has_conflicts"] is True


@pytest.mark.asyncio
async def test_release_session_endpoint_releases_all_session_claims(
    client: AsyncClient,
) -> None:
    h = {"Authorization": "Bearer test-token"}

    for engineer, pat in [
        ("codex-a", "src/a.py"),
        ("codex-b", "src/b.py"),
    ]:
        r = await client.post(
            "/claims",
            headers=h,
            json={
                "engineer": engineer,
                "repo": "amittell/coord",
                "session_id": "release-me",
                "claims": [{"type": "file", "pattern": pat}],
            },
        )
        assert r.status_code == 200, r.text

    r = await client.post(
        "/sessions/release-me/release", headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 2

    # Active claims for the session are now empty.
    r = await client.get("/claims?active_only=true", headers=h)
    body = r.json()
    in_session = [c for c in body["claims"] if c.get("session_id") == "release-me"]
    assert in_session == []


@pytest.mark.asyncio
async def test_release_session_endpoint_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/sessions/anything/release")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pending_requests_endpoint_returns_inbox(client: AsyncClient) -> None:
    """GET /sessions/{id}/pending_requests returns recent conflict-log
    entries against claims that session holds, so an active holder can
    poll for 'has anyone been blocked on my scope?'"""
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "alice",
            "repo": "amittell/coord",
            "session_id": "holder-1",
            "claims": [{"type": "module", "pattern": "server/**"}],
        },
    )
    assert r.status_code == 200, r.text

    # Foreign session attempts overlapping pattern; gets blocked.
    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "bob",
            "repo": "amittell/coord",
            "session_id": "requester-1",
            "claims": [{"type": "module", "pattern": "server/x.js"}],
        },
    )
    assert r.status_code == 409, r.text

    # Holder polls its inbox.
    r = await client.get("/sessions/holder-1/pending_requests", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] >= 1
    one = body["pending"][0]
    assert one["attempted_by"] == "bob"
    assert one["attempted_pattern"] == "server/x.js"
    assert one["attempted_session_id"] == "requester-1"


@pytest.mark.asyncio
async def test_pending_requests_endpoint_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/sessions/whatever/pending_requests")
    assert r.status_code == 401


# --- Release-request endpoints (v0.9.0) -------------------------------------


@pytest.mark.asyncio
async def test_file_request_endpoint_creates_request_and_shortens_ttl(
    client: AsyncClient,
) -> None:
    """End-to-end: holder creates a claim, requester files a request
    against it, response includes the request row and the claim's
    expires_at has been pulled forward."""
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "alice",
            "repo": "amittell/coord",
            "session_id": "holder-sess",
            "claims": [{"type": "file", "pattern": "src/foo.py"}],
        },
    )
    assert r.status_code == 200
    cid = r.json()["claim_ids"][0]

    r = await client.post(
        "/requests",
        headers=h,
        json={
            "claim_id": cid,
            "requester": "bob",
            "session_id": "requester-sess",
            "reason": "hot-fix",
            "urgency": "high",
            "wait_seconds": 0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "pending"
    assert body["claim_id"] == cid
    assert body["urgency"] == "high"

    # Claim TTL has been shortened.
    r = await client.get("/claims?active_only=true", headers=h)
    claims = r.json()["claims"]
    claim = next(c for c in claims if c["id"] == cid)
    assert claim["expires_at"] is not None


@pytest.mark.asyncio
async def test_file_request_returns_404_for_unknown_claim(
    client: AsyncClient,
) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/requests",
        headers=h,
        json={
            "claim_id": "00000000-0000-0000-0000-000000000000",
            "requester": "bob",
            "wait_seconds": 0,
        },
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_respond_endpoint_approve_releases_claim(
    client: AsyncClient,
) -> None:
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "alice",
            "claims": [{"type": "file", "pattern": "src/foo.py"}],
        },
    )
    cid = r.json()["claim_ids"][0]
    r = await client.post(
        "/requests",
        headers=h,
        json={
            "claim_id": cid,
            "requester": "bob",
            "wait_seconds": 0,
        },
    )
    rid = r.json()["id"]

    r = await client.post(
        f"/requests/{rid}/respond",
        headers=h,
        json={
            "decision": "approved",
            "engineer": "alice",
            "note": "ok",
        },
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "approved"

    # Claim is no longer active.
    r = await client.get("/claims?active_only=true", headers=h)
    assert all(c["id"] != cid for c in r.json()["claims"])


@pytest.mark.asyncio
async def test_respond_endpoint_rejects_invalid_decision(
    client: AsyncClient,
) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/requests/anything/respond",
        headers=h,
        json={"decision": "maybe"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_request_events_returns_audit_timeline(
    client: AsyncClient,
) -> None:
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={"engineer": "alice", "claims": [{"type": "file", "pattern": "src/x.py"}]},
    )
    cid = r.json()["claim_ids"][0]
    r = await client.post(
        "/requests",
        headers=h,
        json={"claim_id": cid, "requester": "bob", "wait_seconds": 0},
    )
    rid = r.json()["id"]
    await client.post(
        f"/requests/{rid}/respond",
        headers=h,
        json={"decision": "approved", "engineer": "alice"},
    )

    r = await client.get(f"/requests/{rid}/events", headers=h)
    assert r.status_code == 200
    body = r.json()
    types = [e["event_type"] for e in body["events"]]
    # filed and responded must both be present in chronological order.
    assert "filed" in types
    assert "responded" in types
    assert types.index("filed") < types.index("responded")


@pytest.mark.asyncio
async def test_list_requests_filters_by_requester_and_decision(
    client: AsyncClient,
) -> None:
    h = {"Authorization": "Bearer test-token"}

    # Two claims, two requests, two requesters.
    cids = []
    for pat in ("src/a.py", "src/b.py"):
        r = await client.post(
            "/claims",
            headers=h,
            json={"engineer": "alice", "claims": [{"type": "file", "pattern": pat}]},
        )
        cids.append(r.json()["claim_ids"][0])

    for cid, requester in zip(cids, ("bob", "carol")):
        await client.post(
            "/requests",
            headers=h,
            json={"claim_id": cid, "requester": requester, "wait_seconds": 0},
        )

    r = await client.get("/requests?requester=bob", headers=h)
    body = r.json()
    assert body["count"] == 1
    assert body["requests"][0]["requester_engineer"] == "bob"


@pytest.mark.asyncio
async def test_request_endpoints_require_auth(client: AsyncClient) -> None:
    r = await client.post("/requests", json={"claim_id": "x", "requester": "y"})
    assert r.status_code == 401
    r = await client.post("/requests/x/respond", json={"decision": "approved"})
    assert r.status_code == 401
    r = await client.get("/requests")
    assert r.status_code == 401
    r = await client.get("/requests/x")
    assert r.status_code == 401
    r = await client.get("/requests/x/events")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_access_log_records_non_2xx_status(
    client: AsyncClient, access_log_records: list[logging.LogRecord]
) -> None:
    # Auth-gated endpoint without a bearer token -> 401. Middleware
    # must still emit an access log line for the failed request.
    r = await client.get("/claims")
    assert r.status_code == 401

    access_records = [
        rec for rec in access_log_records if rec.name == "coordination.access"
    ]
    assert len(access_records) == 1
    rec = access_records[0]
    assert getattr(rec, "status", None) == 401
    assert getattr(rec, "method", None) == "GET"
    assert getattr(rec, "path", None) == "/claims"
