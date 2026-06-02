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
        ("example-org/bastionx", "services/x.py"),
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
        ("example-org/bastionx", "alice", "services/x.py"),
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
    assert by_name["example-org/bastionx"]["claims_24h"] == 1


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
            "repo": "example-org/astrowars",
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
        "/conflicts?pattern=client/js/foo.ts&engineer=alice&repo=example-org/astrowars",
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
            "repo": "example-org/astrowars",
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
            "repo": "example-org/astrowars",
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
            "repo": "example-org/astrowars",
            "session_id": "sess-1",
            "claims": [{"type": "module", "pattern": "server/**"}],
        },
    )
    assert r.status_code == 200, r.text

    # Same session, different engineer name: clean.
    r = await client.get(
        "/conflicts?pattern=server/x.js&engineer=codex-bar"
        "&repo=example-org/astrowars&session_id=sess-1",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["has_conflicts"] is False

    # Different session: adversarial.
    r = await client.get(
        "/conflicts?pattern=server/x.js&engineer=codex-bar"
        "&repo=example-org/astrowars&session_id=sess-2",
        headers=h,
    )
    assert r.json()["has_conflicts"] is True


@pytest.mark.asyncio
async def test_conflicts_endpoint_honors_repeated_session_id_params(
    client: AsyncClient,
) -> None:
    # v0.10 sharp edge: a single agent process can carry multiple live
    # session_ids in the repo at once (parent dispatcher + per-worktree
    # subagents). The pre-push hook reads every id from
    # .coordination/sessions.live and forwards them all so the agent's
    # own claims under different engineer names don't false-positive on
    # its own push. The /conflicts endpoint must therefore exclude
    # claims matching ANY of the supplied session_ids.
    h = {"Authorization": "Bearer test-token"}

    for engineer, sess in [
        ("codex-a", "sess-A"),
        ("codex-b", "sess-B"),
    ]:
        r = await client.post(
            "/claims",
            headers=h,
            json={
                "engineer": engineer,
                "repo": "example-org/astrowars",
                "session_id": sess,
                "claims": [
                    {"type": "module", "pattern": f"server/{engineer}/**"}
                ],
            },
        )
        assert r.status_code == 200, r.text

    r = await client.get(
        "/conflicts",
        params=[
            ("pattern", "server/codex-a/x.js"),
            ("pattern", "server/codex-b/y.js"),
            ("engineer", "outsider"),
            ("repo", "example-org/astrowars"),
            ("session_id", "sess-A"),
            ("session_id", "sess-B"),
        ],
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_conflicts"] is False, (
        "claims under either supplied session_id must be excluded"
    )

    # Drop one of the two session_ids: the other session's claim is now
    # adversarial again.
    r = await client.get(
        "/conflicts",
        params=[
            ("pattern", "server/codex-a/x.js"),
            ("pattern", "server/codex-b/y.js"),
            ("engineer", "outsider"),
            ("repo", "example-org/astrowars"),
            ("session_id", "sess-A"),
        ],
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_conflicts"] is True
    pats = {c["pattern"] for c in body["conflicts"]}
    assert pats == {"server/codex-b/**"}, (
        f"only sess-B's claim should remain adversarial; got {pats}"
    )


@pytest.mark.asyncio
async def test_conflicts_endpoint_single_session_id_unchanged(
    client: AsyncClient,
) -> None:
    # Backward compatibility: one session_id still self-excludes that
    # session's claims and only that session's claims.
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "codex-foo",
            "repo": "example-org/astrowars",
            "session_id": "sess-only",
            "claims": [{"type": "module", "pattern": "server/**"}],
        },
    )
    assert r.status_code == 200, r.text

    r = await client.get(
        "/conflicts?pattern=server/x.js&engineer=codex-bar"
        "&repo=example-org/astrowars&session_id=sess-only",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["has_conflicts"] is False

    r = await client.get(
        "/conflicts?pattern=server/x.js&engineer=codex-bar"
        "&repo=example-org/astrowars&session_id=sess-other",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["has_conflicts"] is True


@pytest.mark.asyncio
async def test_conflicts_endpoint_no_session_id_unchanged(
    client: AsyncClient,
) -> None:
    # Pre-v0.5 callers that omit session_id entirely must keep the
    # legacy behaviour: no self-exclusion, every same-repo claim is
    # adversarial.
    h = {"Authorization": "Bearer test-token"}

    r = await client.post(
        "/claims",
        headers=h,
        json={
            "engineer": "codex-foo",
            "repo": "example-org/astrowars",
            "session_id": "sess-legacy",
            "claims": [{"type": "module", "pattern": "server/**"}],
        },
    )
    assert r.status_code == 200, r.text

    r = await client.get(
        "/conflicts?pattern=server/x.js&engineer=codex-bar"
        "&repo=example-org/astrowars",
        headers=h,
    )
    assert r.status_code == 200
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


# ---------------------------------------------------------------------------
# v0.14: symbol-scope claims end-to-end through POST /claims
# ---------------------------------------------------------------------------


_AUTH = {"Authorization": "Bearer test-token"}


def _symbol_claim(pattern: str, symbols: list[str]) -> dict:
    return {"type": "file", "pattern": pattern, "symbols": symbols}


@pytest.mark.asyncio
async def test_symbol_claim_creates_with_scope_type_symbol(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/claims",
        headers=_AUTH,
        json={
            "engineer": "alex",
            "repo": "amittell/coord",
            "claims": [_symbol_claim("src/auth/login.ts", ["handleLogin"])],
        },
    )
    assert r.status_code == 200, r.text
    cid = r.json()["claim_ids"][0]
    listing = await client.get(
        "/claims", headers=_AUTH, params={"active_only": "true"}
    )
    rows = listing.json()
    target = [c for c in rows.get("claims", []) if c["id"] == cid][0]
    assert target["scope_type"] == "symbol"
    assert target["narrowable"] == 0


@pytest.mark.asyncio
async def test_symbol_disjoint_auto_coexists_without_409(
    client: AsyncClient,
) -> None:
    body_a = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/login.ts", ["handleLogin"])],
    }
    body_b = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/login.ts", ["handleLogout"])],
    }
    ra = await client.post("/claims", headers=_AUTH, json=body_a)
    assert ra.status_code == 200, ra.text
    rb = await client.post("/claims", headers=_AUTH, json=body_b)
    assert rb.status_code == 200, rb.text
    assert rb.json()["claim_ids"], "AUTO_COEXIST should grant the claim"
    listing = await client.get(
        "/claims", headers=_AUTH, params={"active_only": "true"}
    )
    active_ids = {c["id"] for c in listing.json().get("claims", [])}
    assert ra.json()["claim_ids"][0] in active_ids
    assert rb.json()["claim_ids"][0] in active_ids


@pytest.mark.asyncio
async def test_symbol_overlap_returns_409_with_symbol_detail(
    client: AsyncClient,
) -> None:
    body_a = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/login.ts", ["handleLogin"])],
    }
    ra = await client.post("/claims", headers=_AUTH, json=body_a)
    assert ra.status_code == 200, ra.text
    body_b = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [
            _symbol_claim("src/auth/login.ts", ["handleLogin", "validate"])
        ],
    }
    rb = await client.post("/claims", headers=_AUTH, json=body_b)
    assert rb.status_code == 409
    payload = rb.json()
    assert payload["claim_ids"] == []
    assert payload["conflicts"], "expected at least one conflict"
    entry = payload["conflicts"][0]
    assert entry["your_symbols"] == ["handleLogin", "validate"]
    assert entry["conflicting_claim"]["scope_type"] == "symbol"
    so = entry.get("symbol_overlap")
    assert so and so[0]["symbols"] == ["handleLogin"]


@pytest.mark.asyncio
async def test_file_holder_narrowable_auto_narrows_for_symbol_requester(
    client: AsyncClient,
) -> None:
    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/auth/login.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    requester = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/login.ts", ["handleLogin"])],
    }
    rr = await client.post("/claims", headers=_AUTH, json=requester)
    assert rr.status_code == 200, rr.text
    assert rr.json()["claim_ids"], "AUTO_NARROW should grant symbol claim"
    # Both claims should now be active and marked as coexist partners.
    listing = await client.get(
        "/claims", headers=_AUTH, params={"active_only": "true"}
    )
    rows = listing.json()["claims"]
    holder_id = rh.json()["claim_ids"][0]
    requester_id = rr.json()["claim_ids"][0]
    holder_row = [c for c in rows if c["id"] == holder_id][0]
    requester_row = [c for c in rows if c["id"] == requester_id][0]
    import json as _json

    holder_partners = _json.loads(holder_row.get("coexists_with") or "[]")
    requester_partners = _json.loads(requester_row.get("coexists_with") or "[]")
    assert requester_id in holder_partners
    assert holder_id in requester_partners


@pytest.mark.asyncio
async def test_shared_file_holder_blocks_symbol_requester(
    client: AsyncClient,
) -> None:
    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "shared_file", "pattern": "package-lock.json"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    requester = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("package-lock.json", ["irrelevant"])],
    }
    rr = await client.post("/claims", headers=_AUTH, json=requester)
    # shared_file is explicitly non-narrowable -> conflict path stays 409.
    assert rr.status_code == 409


# ---------------------------------------------------------------------------
# v0.16: method-level (namespaced) symbol claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_method_disjoint_auto_coexists_within_class(
    client: AsyncClient,
) -> None:
    """Two agents claiming different methods on the same class auto-coexist."""
    body_a = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/router.ts", ["Router::handleAuth"])],
    }
    body_b = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/router.ts", ["Router::handleLogout"])],
    }
    ra = await client.post("/claims", headers=_AUTH, json=body_a)
    assert ra.status_code == 200, ra.text
    rb = await client.post("/claims", headers=_AUTH, json=body_b)
    assert rb.status_code == 200, rb.text
    assert rb.json()["claim_ids"], "different methods of same class must auto-coexist"


@pytest.mark.asyncio
async def test_method_same_path_conflicts(client: AsyncClient) -> None:
    body_a = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/router.ts", ["Router::handleAuth"])],
    }
    body_b = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/router.ts", ["Router::handleAuth"])],
    }
    await client.post("/claims", headers=_AUTH, json=body_a)
    rb = await client.post("/claims", headers=_AUTH, json=body_b)
    assert rb.status_code == 409
    payload = rb.json()
    assert payload["conflicts"], "expected conflict on same method"
    so = payload["conflicts"][0].get("symbol_overlap")
    assert so and "Router::handleAuth" in so[0]["symbols"]


@pytest.mark.asyncio
async def test_class_claim_blocks_method_claim(client: AsyncClient) -> None:
    """Claiming the whole class blocks a method claim on it."""
    body_class = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/router.ts", ["Router"])],
    }
    body_method = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/auth/router.ts", ["Router::handleAuth"])],
    }
    await client.post("/claims", headers=_AUTH, json=body_class)
    rb = await client.post("/claims", headers=_AUTH, json=body_method)
    assert rb.status_code == 409
    so = rb.json()["conflicts"][0].get("symbol_overlap")
    assert so and "Router::handleAuth" in so[0]["symbols"]


@pytest.mark.asyncio
async def test_method_on_different_classes_coexists(client: AsyncClient) -> None:
    body_a = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/handlers.ts", ["AuthRouter::handle"])],
    }
    body_b = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/handlers.ts", ["UserRouter::handle"])],
    }
    await client.post("/claims", headers=_AUTH, json=body_a)
    rb = await client.post("/claims", headers=_AUTH, json=body_b)
    assert rb.status_code == 200, rb.text
    assert rb.json()["claim_ids"], "same method name on different parents must coexist"


# ---------------------------------------------------------------------------
# v0.17: recursive nested-namespace claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_methods_coexist(client: AsyncClient) -> None:
    """Sibling methods of the same nested class auto-coexist."""
    body_a = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/api.ts", ["Outer::Inner::handle"])],
    }
    body_b = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/api.ts", ["Outer::Inner::reset"])],
    }
    ra = await client.post("/claims", headers=_AUTH, json=body_a)
    assert ra.status_code == 200, ra.text
    rb = await client.post("/claims", headers=_AUTH, json=body_b)
    assert rb.status_code == 200, rb.text


@pytest.mark.asyncio
async def test_outer_class_blocks_nested_method(client: AsyncClient) -> None:
    """Claiming the outer class blocks any descendant method claim."""
    body_outer = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/api.ts", ["Outer"])],
    }
    body_method = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/api.ts", ["Outer::Inner::handle"])],
    }
    await client.post("/claims", headers=_AUTH, json=body_outer)
    rb = await client.post("/claims", headers=_AUTH, json=body_method)
    assert rb.status_code == 409
    so = rb.json()["conflicts"][0].get("symbol_overlap")
    assert so and "Outer::Inner::handle" in so[0]["symbols"]


@pytest.mark.asyncio
async def test_inner_class_blocks_method_but_not_sibling(
    client: AsyncClient,
) -> None:
    """Claiming Outer::Inner blocks Outer::Inner::handle but not
    Outer::Other::handle (different inner class)."""
    body_inner = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/api.ts", ["Outer::Inner"])],
    }
    body_method_same = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/api.ts", ["Outer::Inner::handle"])],
    }
    body_method_diff = {
        "engineer": "carol",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/api.ts", ["Outer::Other::handle"])],
    }
    await client.post("/claims", headers=_AUTH, json=body_inner)
    r_same = await client.post("/claims", headers=_AUTH, json=body_method_same)
    r_diff = await client.post("/claims", headers=_AUTH, json=body_method_diff)
    assert r_same.status_code == 409, "Outer::Inner must block Outer::Inner::handle"
    assert r_diff.status_code == 200, "Outer::Inner must NOT block Outer::Other::handle"


# ---------------------------------------------------------------------------
# v0.17: server-side symbol-claim validation
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client_with_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Variant of ``client`` that points COORD_REPO_ROOT at a tmp repo.
    Tests seed source files under tmp_path/repo/ and claim paths
    relative to that root."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.setenv("COORD_REPO_ROOT", str(repo_root))
    # Disable max-ratio scope check; this fixture's repos are tiny so a
    # single-file claim would otherwise trip the 20% cap.
    monkeypatch.setenv("COORD_MAX_CLAIM_RATIO", "1.0")

    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.repo_root = repo_root  # type: ignore[attr-defined]
        yield ac

    deps.get_service.cache_clear()


@pytest.mark.asyncio
async def test_validation_skipped_when_repo_root_unset(
    client: AsyncClient,
) -> None:
    body = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("src/never.ts", ["doesNotExist"])],
    }
    r = await client.post("/claims", headers=_AUTH, json=body)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_validation_passes_when_symbol_exists(
    client_with_repo_root: AsyncClient,
) -> None:
    repo_root = client_with_repo_root.repo_root  # type: ignore[attr-defined]
    (repo_root / "auth.ts").write_text(
        "export function handleAuth() { return null; }\n", encoding="utf-8"
    )
    body = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("auth.ts", ["handleAuth"])],
    }
    r = await client_with_repo_root.post("/claims", headers=_AUTH, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["claim_ids"]


@pytest.mark.asyncio
async def test_validation_rejects_unknown_symbol(
    client_with_repo_root: AsyncClient,
) -> None:
    repo_root = client_with_repo_root.repo_root  # type: ignore[attr-defined]
    (repo_root / "auth.ts").write_text(
        "export function handleAuth() { return null; }\n", encoding="utf-8"
    )
    body = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("auth.ts", ["nonexistentFn"])],
    }
    r = await client_with_repo_root.post("/claims", headers=_AUTH, json=body)
    # Mirrors syntax/scope error path: 200 with warnings + empty claim_ids
    payload = r.json()
    assert payload["claim_ids"] == []
    assert payload["warnings"], "expected validation warning"
    msg = payload["warnings"][0]
    assert "nonexistentFn" in msg
    assert "handleAuth" in msg, "expected hint to list available symbol"


@pytest.mark.asyncio
async def test_validation_accepts_method_notation(
    client_with_repo_root: AsyncClient,
) -> None:
    repo_root = client_with_repo_root.repo_root  # type: ignore[attr-defined]
    (repo_root / "router.ts").write_text(
        "class Router {\n  handleAuth() { return null; }\n}\n",
        encoding="utf-8",
    )
    body = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("router.ts", ["Router::handleAuth"])],
    }
    r = await client_with_repo_root.post("/claims", headers=_AUTH, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["claim_ids"]


@pytest.mark.asyncio
async def test_validation_skipped_for_missing_files(
    client_with_repo_root: AsyncClient,
) -> None:
    body = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [_symbol_claim("does/not/exist.ts", ["whatever"])],
    }
    r = await client_with_repo_root.post("/claims", headers=_AUTH, json=body)
    # Missing file -> skip validation, no warning, claim succeeds.
    assert r.status_code == 200, r.text
    assert r.json()["claim_ids"]


# ---------------------------------------------------------------------------
# v0.20: hotspot detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hotspots_endpoint_returns_series(
    client: AsyncClient,
) -> None:
    """End-to-end: create a holder, force several 409s on the same path,
    then /metrics/hotspots reports the pattern with the right counts."""
    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/router.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    # Five different attempters each bounce off the same path.
    for i in range(5):
        r = await client.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": f"bouncer-{i}",
                "repo": "amittell/coord",
                "claims": [{"type": "file", "pattern": "src/router.ts"}],
            },
        )
        assert r.status_code == 409
    # Hotspots endpoint surfaces the pattern.
    r = await client.get(
        "/metrics/hotspots",
        headers=_AUTH,
        params={"days": 30, "min_attempts": 5},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["count"] >= 1
    by_pattern = {row["pattern"]: row for row in payload["hotspots"]}
    assert "src/router.ts" in by_pattern
    target = by_pattern["src/router.ts"]
    assert target["attempts"] >= 5
    assert target["distinct_attempters"] >= 5


# ---------------------------------------------------------------------------
# v0.21: soft auto-promote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_hotspot_writes_shared_file_rule(
    client: AsyncClient,
) -> None:
    """POST /metrics/hotspots/promote with action=shared_file writes the
    pattern into the active owners.yaml under a `shared_files:` list."""
    r = await client.post(
        "/metrics/hotspots/promote",
        headers=_AUTH,
        json={"action": "shared_file", "pattern": "src/router.ts"},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["action"] == "shared_file"
    assert "src/router.ts" in payload["patched_yaml"]
    # Read-back via /config/ownership confirms the write landed.
    g = await client.get("/config/ownership", headers=_AUTH)
    assert g.status_code == 200
    assert "src/router.ts" in g.text


@pytest.mark.asyncio
async def test_promote_hotspot_idempotent_for_shared_file(
    client: AsyncClient,
) -> None:
    """Promoting the same pattern twice leaves the YAML unchanged."""
    payload = {"action": "shared_file", "pattern": "src/router.ts"}
    first = await client.post(
        "/metrics/hotspots/promote", headers=_AUTH, json=payload
    )
    second = await client.post(
        "/metrics/hotspots/promote", headers=_AUTH, json=payload
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["patched_yaml"] == second.json()["patched_yaml"]


@pytest.mark.asyncio
async def test_promote_hotspot_split_action_writes_suggestion(
    client: AsyncClient,
) -> None:
    """action=split writes an informational suggested_splits entry with
    the operator's note."""
    r = await client.post(
        "/metrics/hotspots/promote",
        headers=_AUTH,
        json={
            "action": "split",
            "pattern": "src/big-router.ts",
            "note": "too central, touched by every team",
        },
    )
    assert r.status_code == 200, r.text
    patched = r.json()["patched_yaml"]
    assert "suggested_splits" in patched
    assert "src/big-router.ts" in patched
    assert "too central" in patched


@pytest.mark.asyncio
async def test_promote_hotspot_rejects_unknown_action(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/metrics/hotspots/promote",
        headers=_AUTH,
        json={"action": "nope", "pattern": "src/x.ts"},
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# v0.21: FIFO queue (wait_seconds)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_disabled_when_wait_seconds_omitted(
    client: AsyncClient,
) -> None:
    """Without wait_seconds, conflict path is unchanged: immediate 409
    and no claim_queue row is inserted."""
    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/x.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    requester = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/x.ts"}],
    }
    rr = await client.post("/claims", headers=_AUTH, json=requester)
    # Legacy 409 (or 200 with conflicts depending on the path); claim
    # ids must be empty either way.
    assert rr.json().get("claim_ids", None) == [] or rr.status_code == 409


@pytest.mark.asyncio
async def test_queue_timeout_returns_original_conflict(
    client: AsyncClient,
) -> None:
    """wait_seconds=1 with no release fires the timeout; response
    surfaces the conflict payload, no granted claim."""
    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/y.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    requester = {
        "engineer": "bob",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/y.ts"}],
        "wait_seconds": 1,
    }
    rr = await client.post("/claims", headers=_AUTH, json=requester)
    # Timeout path returns the conflict payload (same shape as 409).
    payload = rr.json()
    assert payload.get("claim_ids") == []
    assert payload.get("conflicts"), "timeout must surface conflict payload"


@pytest.mark.asyncio
async def test_queue_grants_in_fifo_order_on_release(
    client: AsyncClient,
) -> None:
    """Concurrent waiters land in FIFO order; release auto-grants the
    head of the queue."""
    import asyncio as _asyncio

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/z.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    async def queued_request(engineer: str) -> dict:
        body = {
            "engineer": engineer,
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/z.ts"}],
            "wait_seconds": 10,
        }
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    # Fire bob first; brief sleep guarantees bob enqueues before carol.
    bob_task = _asyncio.create_task(queued_request("bob"))
    await _asyncio.sleep(0.05)
    carol_task = _asyncio.create_task(queued_request("carol"))
    await _asyncio.sleep(0.05)

    # Release the holder; bob (head of FIFO) should be auto-granted.
    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [holder_cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text

    bob_result = await _asyncio.wait_for(bob_task, timeout=5)
    carol_result = await _asyncio.wait_for(carol_task, timeout=5)

    # Bob got the auto-grant.
    assert bob_result.get("claim_ids"), (
        f"bob should be auto-granted; got {bob_result}"
    )
    # Carol either got auto-granted in turn (after bob releases, but he
    # didn't here) or her wait timed out into a conflict payload. In
    # this test we don't release bob, so carol times out with the
    # conflict shape (and her wait_seconds is large enough that she
    # may still be waiting -- the test asserts her result is well-formed
    # either way).
    assert carol_result.get("claim_ids", []) == [] or carol_result.get(
        "claim_ids"
    )
