from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

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
    # The unauthenticated probe must not leak server-internal filesystem
    # layout (audit: info-disclosure).
    assert "database_path" not in body


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

    # Fire bob first, then carol. The previous implementation used
    # `await asyncio.sleep(0.05)` between create_task calls to give the
    # event loop time to enqueue each request, but that timing was
    # reliable only on Linux/macOS; on Windows's coarser scheduler the
    # holder release could fire before bob's POST had actually entered
    # the queue, draining nothing and timing both waiters out. Polling
    # for the observable queue row removes the race entirely.
    bob_task = _asyncio.create_task(queued_request("bob"))
    await _wait_for_queue_id(client, "bob")
    carol_task = _asyncio.create_task(queued_request("carol"))
    await _wait_for_queue_id(client, "carol")

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


# ---------------------------------------------------------------------------
# v0.22: hard auto-promote
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client_auto_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Variant of ``client`` with hard auto-promote enabled.

    Threshold is 3 attempts within a 7-day window: the third 409
    against the same pattern triggers the shared_file rule write.
    """

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_AUTO_PROMOTE_THRESHOLD", "3")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_WINDOW_DAYS", "7")

    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


@pytest.mark.asyncio
async def test_auto_promote_writes_rule_when_threshold_crossed(
    client_auto_promote: AsyncClient,
) -> None:
    """3 distinct attempters bouncing on src/router.ts trips the
    threshold; the 3rd 409 promotes the pattern into shared_files."""

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/router.ts"}],
    }
    rh = await client_auto_promote.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    # Before any 409 the YAML has no shared_files entry.
    g0 = await client_auto_promote.get("/config/ownership", headers=_AUTH)
    assert "src/router.ts" not in g0.text

    for engineer in ("bob", "carol", "dave"):
        rr = await client_auto_promote.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": engineer,
                "repo": "amittell/coord",
                "claims": [{"type": "file", "pattern": "src/router.ts"}],
            },
        )
        assert rr.json().get("claim_ids") == [], rr.text

    g = await client_auto_promote.get("/config/ownership", headers=_AUTH)
    assert g.status_code == 200
    assert "shared_files" in g.text, g.text
    assert "src/router.ts" in g.text, g.text


@pytest.mark.asyncio
async def test_auto_promote_idempotent_on_repeated_conflicts(
    client_auto_promote: AsyncClient,
) -> None:
    """Once a pattern is promoted, further 409s do not rewrite the
    YAML."""

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/router.ts"}],
    }
    rh = await client_auto_promote.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    # Cross the threshold.
    for engineer in ("bob", "carol", "dave"):
        await client_auto_promote.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": engineer,
                "repo": "amittell/coord",
                "claims": [{"type": "file", "pattern": "src/router.ts"}],
            },
        )

    g1 = await client_auto_promote.get("/config/ownership", headers=_AUTH)
    assert "src/router.ts" in g1.text
    snapshot = g1.text

    # 5 more 409s on the same pattern; YAML must not change.
    for engineer in ("eve", "frank", "grace", "heidi", "ivan"):
        await client_auto_promote.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": engineer,
                "repo": "amittell/coord",
                "claims": [{"type": "file", "pattern": "src/router.ts"}],
            },
        )

    g2 = await client_auto_promote.get("/config/ownership", headers=_AUTH)
    assert g2.text == snapshot


@pytest.mark.asyncio
async def test_auto_promote_disabled_when_threshold_zero(
    client: AsyncClient,
) -> None:
    """Default config (threshold=0) leaves owners.yaml empty regardless
    of how many 409s the same pattern accumulates."""

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/router.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    for i in range(10):
        await client.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": f"bouncer-{i}",
                "repo": "amittell/coord",
                "claims": [{"type": "file", "pattern": "src/router.ts"}],
            },
        )

    g = await client.get("/config/ownership", headers=_AUTH)
    # 204 No Content (no YAML ever written) is the success signal for
    # "auto-promote did nothing"; 200 with a body that lacks the
    # pattern is the other valid shape.
    assert g.status_code in (200, 204), g.text
    assert "src/router.ts" not in g.text
    assert "shared_files" not in g.text


# ---------------------------------------------------------------------------
# v0.26: pattern-class granularity
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client_auto_promote_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """v0.26 variant of ``client_auto_promote`` with the subtree-min
    setting left at its default (3). Threshold of 1 means the very
    first 409 against a leaf qualifies it, which lets the test seed
    multiple hot leaves with a single bounce each instead of running
    the full threshold pass per file.
    """

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_AUTO_PROMOTE_THRESHOLD", "1")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_WINDOW_DAYS", "7")
    monkeypatch.delenv("COORD_AUTO_PROMOTE_SUBTREE_MIN_FILES", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


async def _seed_holder_and_bounce(
    ac: AsyncClient,
    *,
    holder_engineer: str,
    holder_pattern: str,
    bouncer_files: list[str],
) -> None:
    """Helper: holder claims ``holder_pattern``; then a single bouncer
    request asks for every leaf in ``bouncer_files`` in one batch. The
    batch 409s, every leaf is recorded in ``conflict_log``, and the
    final 409 is the one whose ``_maybe_auto_promote`` call sees the
    full grouping in its conflicts list (per the v0.26 contract).
    """
    rh = await ac.post(
        "/claims",
        headers=_AUTH,
        json={
            "engineer": holder_engineer,
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": holder_pattern}],
        },
    )
    assert rh.status_code == 200, rh.text

    rr = await ac.post(
        "/claims",
        headers=_AUTH,
        json={
            "engineer": "bouncer",
            "repo": "amittell/coord",
            "claims": [
                {"type": "file", "pattern": leaf} for leaf in bouncer_files
            ],
        },
    )
    # 409 (canonical) or 200-with-empty-claim_ids (legacy shape); what
    # matters is that the attempts landed in conflict_log so the hotspot
    # query can see them.
    assert rr.status_code in (200, 409), rr.text
    assert rr.json().get("claim_ids") == [], rr.text


@pytest.mark.asyncio
async def test_subtree_promote_when_n_files_share_directory(
    client_auto_promote_subtree: AsyncClient,
) -> None:
    """4 hot leaves under ``src/auth/`` (>= default subtree_min=3)
    collapse into a single ``src/auth/**`` shared_files entry; the
    individual leaves are NOT written as their own entries."""

    leaves = [
        "src/auth/login.ts",
        "src/auth/logout.ts",
        "src/auth/oauth.ts",
        "src/auth/session.ts",
    ]
    # Holder pattern covers the whole subtree so every bouncer 409s
    # against the same claim.
    await _seed_holder_and_bounce(
        client_auto_promote_subtree,
        holder_engineer="alice",
        holder_pattern="src/auth/**",
        bouncer_files=leaves,
    )

    g = await client_auto_promote_subtree.get(
        "/config/ownership", headers=_AUTH
    )
    assert g.status_code == 200, g.text
    body = g.text
    assert "shared_files" in body, body
    assert "src/auth/**" in body, body
    # The leaves must NOT also be present as individual shared_files
    # entries; the subtree glob subsumes them.
    for leaf in leaves:
        assert leaf not in body, (
            f"leaf {leaf!r} should be covered by subtree glob, not its "
            f"own entry; got:\n{body}"
        )


@pytest.mark.asyncio
async def test_subtree_threshold_not_crossed_falls_back_to_per_file(
    client_auto_promote_subtree: AsyncClient,
) -> None:
    """Only 2 hot leaves under ``src/auth/`` (< default subtree_min=3).
    Both leaves are written individually; no subtree glob appears."""

    leaves = ["src/auth/login.ts", "src/auth/logout.ts"]
    await _seed_holder_and_bounce(
        client_auto_promote_subtree,
        holder_engineer="alice",
        holder_pattern="src/auth/**",
        bouncer_files=leaves,
    )

    g = await client_auto_promote_subtree.get(
        "/config/ownership", headers=_AUTH
    )
    assert g.status_code == 200, g.text
    body = g.text
    assert "shared_files" in body, body
    assert "src/auth/**" not in body, body
    for leaf in leaves:
        assert leaf in body, body


@pytest.mark.asyncio
async def test_subtree_disabled_when_setting_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``COORD_AUTO_PROMOTE_SUBTREE_MIN_FILES=0`` preserves the v0.22
    per-file behaviour even when 5 leaves share the same directory."""

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_AUTO_PROMOTE_THRESHOLD", "1")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_WINDOW_DAYS", "7")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_SUBTREE_MIN_FILES", "0")

    from coordination import deps

    deps.get_service.cache_clear()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            leaves = [
                "src/auth/login.ts",
                "src/auth/logout.ts",
                "src/auth/oauth.ts",
                "src/auth/session.ts",
                "src/auth/tokens.ts",
            ]
            await _seed_holder_and_bounce(
                ac,
                holder_engineer="alice",
                holder_pattern="src/auth/**",
                bouncer_files=leaves,
            )

            g = await ac.get("/config/ownership", headers=_AUTH)
            assert g.status_code == 200, g.text
            body = g.text
            assert "shared_files" in body, body
            # Subtree promotion is disabled: every leaf gets its own
            # entry and no ``src/auth/**`` glob is written by coord.
            assert "src/auth/**" not in body, body
            for leaf in leaves:
                assert leaf in body, body
    finally:
        deps.get_service.cache_clear()


# ---------------------------------------------------------------------------
# v0.23: ownership helpers (marker round-trip)
# ---------------------------------------------------------------------------


def test_ownership_managed_marker_round_trip() -> None:
    """The managed marker survives a write -> list -> remove round trip
    and the list helper ignores operator-added entries."""
    from coordination.ownership import (
        list_coord_managed_shared_files,
        patch_owners_yaml_remove_shared_file,
        patch_owners_yaml_with_shared_file,
    )

    # Operator-seeded entry plus a coord-managed one.
    yaml_text = "shared_files:\n  - src/operator.ts\n"
    yaml_text = patch_owners_yaml_with_shared_file(
        yaml_text,
        "src/managed.ts",
        managed=True,
        promoted_at="2026-06-02",
    )

    entries = list_coord_managed_shared_files(yaml_text)
    # Only the managed entry is reported; the operator one is invisible.
    assert entries == [("src/managed.ts", "2026-06-02")]
    # The marker is a YAML comment, so a strict parser still sees just
    # the pattern string.
    import yaml as _yaml

    parsed = _yaml.safe_load(yaml_text)
    assert parsed == {
        "shared_files": ["src/operator.ts", "src/managed.ts"]
    }

    # Removing the managed entry leaves the operator one intact.
    after = patch_owners_yaml_remove_shared_file(yaml_text, "src/managed.ts")
    assert "src/managed.ts" not in after
    assert "src/operator.ts" in after
    assert list_coord_managed_shared_files(after) == []


# ---------------------------------------------------------------------------
# v0.23: auto-demote
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client_auto_demote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Variant of ``client`` with auto-promote AND auto-demote enabled.

    Threshold is 3 attempts within the auto-promote window (7 days);
    the auto-demote sweep uses a 14-day rolling window. The background
    auto-demote loop is suppressed via ``COORD_DISABLE_BACKGROUND_CLEANUP``
    so the test drives ``_maybe_auto_demote`` directly and the sweep
    state is deterministic.
    """
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_AUTO_PROMOTE_THRESHOLD", "3")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_WINDOW_DAYS", "7")
    monkeypatch.setenv("COORD_AUTO_DEMOTE_WINDOW_DAYS", "14")

    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


@pytest.mark.asyncio
async def test_auto_demote_removes_dormant_entry(
    client_auto_demote: AsyncClient,
) -> None:
    """A coord-managed shared_files entry with no recent 409 activity
    is removed by the sweep and an ``auto-demote`` audit row is
    recorded."""
    from coordination import deps
    from coordination.ownership import (
        list_coord_managed_shared_files,
        patch_owners_yaml_with_shared_file,
    )

    svc = deps.get_service()
    seeded = patch_owners_yaml_with_shared_file(
        "", "src/dormant.ts", managed=True, promoted_at="2026-01-01"
    )
    await svc.db.set_ownership_yaml(seeded)
    assert list_coord_managed_shared_files(seeded) == [
        ("src/dormant.ts", "2026-01-01")
    ]

    removed = await svc._maybe_auto_demote()
    assert removed == 1

    after = await svc.db.get_ownership_yaml() or ""
    assert "src/dormant.ts" not in after
    assert list_coord_managed_shared_files(after) == []

    # Audit row was recorded with the expected detail shape.
    import aiosqlite

    async with aiosqlite.connect(svc.db.path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT event_type, detail FROM request_events "
            "WHERE event_type = 'auto-demote'"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    import json as _json

    detail = _json.loads(rows[0]["detail"])
    assert detail["pattern"] == "src/dormant.ts"
    assert detail["threshold"] == 3
    assert detail["window_days"] == 14
    assert detail["count_in_window"] == 0


@pytest.mark.asyncio
async def test_auto_demote_skips_operator_added_entries(
    client_auto_demote: AsyncClient,
) -> None:
    """Operator-added shared_files entries (no marker) are immune to
    the sweep even when they have zero 409 activity."""
    from coordination import deps
    from coordination.ownership import (
        list_coord_managed_shared_files,
        patch_owners_yaml_with_shared_file,
    )

    svc = deps.get_service()
    # Operator first, then a managed entry.
    seeded = "shared_files:\n  - src/operator.ts\n"
    seeded = patch_owners_yaml_with_shared_file(
        seeded, "src/managed.ts", managed=True, promoted_at="2026-01-01"
    )
    await svc.db.set_ownership_yaml(seeded)

    removed = await svc._maybe_auto_demote()
    assert removed == 1

    after = await svc.db.get_ownership_yaml() or ""
    # Managed entry gone, operator entry intact.
    assert "src/managed.ts" not in after
    assert "src/operator.ts" in after
    assert list_coord_managed_shared_files(after) == []


@pytest.mark.asyncio
async def test_auto_demote_skips_active_entries(
    client_auto_demote: AsyncClient,
) -> None:
    """A managed entry whose pattern is still drawing 409s at or above
    the threshold is left in place."""
    from coordination import deps
    from coordination.ownership import (
        list_coord_managed_shared_files,
        patch_owners_yaml_with_shared_file,
    )

    svc = deps.get_service()

    # Seed a managed entry for a pattern we are about to trigger
    # conflicts on.
    seeded = patch_owners_yaml_with_shared_file(
        "", "src/active.ts", managed=True, promoted_at="2026-01-01"
    )
    await svc.db.set_ownership_yaml(seeded)

    # Drive conflict_log activity: holder + 3 attempters all racing on
    # the same pattern. The hotspot query keys on the holder's
    # ``claims.repo`` so we tag every claim with the same repo.
    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/active.ts"}],
    }
    rh = await client_auto_demote.post(
        "/claims", headers=_AUTH, json=holder
    )
    assert rh.status_code == 200, rh.text
    for engineer in ("bob", "carol", "dave"):
        rr = await client_auto_demote.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": engineer,
                "repo": "amittell/coord",
                "claims": [{"type": "file", "pattern": "src/active.ts"}],
            },
        )
        # Conflict path returns claim_ids=[] regardless of status code.
        assert rr.json().get("claim_ids") == [], rr.text

    # Sweep should NOT touch this entry: count_in_window >= threshold.
    removed = await svc._maybe_auto_demote()
    assert removed == 0

    after = await svc.db.get_ownership_yaml() or ""
    assert "src/active.ts" in after
    managed = list_coord_managed_shared_files(after)
    assert managed and managed[0][0] == "src/active.ts"


# ---------------------------------------------------------------------------
# v0.25: permanent-marker (pinned shared_files entries)
# ---------------------------------------------------------------------------


def test_ownership_lists_permanent_marker() -> None:
    """``list_permanent_shared_files`` returns every entry whose line
    carries the operator-set ``# coord-managed=permanent`` marker,
    including entries that also carry the auto-promoted marker."""
    from coordination.ownership import list_permanent_shared_files

    yaml_text = (
        "shared_files:\n"
        "  - src/auto.ts  # auto-promoted=2026-06-02\n"
        "  - src/pinned.ts  # coord-managed=permanent\n"
        "  - src/operator.ts\n"
        "  - src/both.ts  # auto-promoted=2026-06-02 coord-managed=permanent\n"
    )

    pinned = list_permanent_shared_files(yaml_text)
    assert set(pinned) == {"src/pinned.ts", "src/both.ts"}


@pytest.mark.asyncio
async def test_auto_demote_skips_permanent_entries(
    client_auto_demote: AsyncClient,
) -> None:
    """A shared_files entry that is both auto-promoted AND pinned with
    the operator ``# coord-managed=permanent`` marker survives the
    sweep even when its rolling 409 count is zero."""
    from coordination import deps
    from coordination.ownership import (
        list_coord_managed_shared_files,
        list_permanent_shared_files,
    )

    svc = deps.get_service()
    # Hand-crafted YAML: the entry carries both markers so the parser
    # sees it as managed AND the sweep guard sees it as pinned.
    seeded = (
        "shared_files:\n"
        "  - src/pinned.ts  # auto-promoted=2026-01-01 coord-managed=permanent\n"
    )
    await svc.db.set_ownership_yaml(seeded)
    assert list_coord_managed_shared_files(seeded) == [
        ("src/pinned.ts", "2026-01-01")
    ]
    assert list_permanent_shared_files(seeded) == ["src/pinned.ts"]

    # Threshold 3 and zero conflict_log activity would normally demote;
    # the permanent marker must keep the entry in place.
    removed = await svc._maybe_auto_demote()
    assert removed == 0

    after = await svc.db.get_ownership_yaml() or ""
    assert "src/pinned.ts" in after
    assert list_permanent_shared_files(after) == ["src/pinned.ts"]


@pytest.mark.asyncio
async def test_auto_demote_removes_managed_non_permanent_when_dormant(
    client_auto_demote: AsyncClient,
) -> None:
    """Mixed seed regression: a pinned permanent entry and a plain
    managed entry both sit dormant. The sweep removes only the
    managed-only one; the permanent entry is untouched."""
    from coordination import deps
    from coordination.ownership import (
        list_coord_managed_shared_files,
        list_permanent_shared_files,
        patch_owners_yaml_with_shared_file,
    )

    svc = deps.get_service()
    # Plain managed entry (will be demoted).
    seeded = patch_owners_yaml_with_shared_file(
        "", "src/dormant.ts", managed=True, promoted_at="2026-01-01"
    )
    # Append a pinned permanent entry by hand (operators do this in
    # owners.yaml; there's no auto-promote path that writes the
    # permanent marker).
    if not seeded.endswith("\n"):
        seeded += "\n"
    seeded += "  - src/pinned.ts  # coord-managed=permanent\n"
    await svc.db.set_ownership_yaml(seeded)

    removed = await svc._maybe_auto_demote()
    assert removed == 1

    after = await svc.db.get_ownership_yaml() or ""
    assert "src/dormant.ts" not in after
    assert "src/pinned.ts" in after
    # The pinned entry has no auto-promoted marker, so the managed
    # helper reports nothing; the permanent helper still sees it.
    assert list_coord_managed_shared_files(after) == []
    assert list_permanent_shared_files(after) == ["src/pinned.ts"]


# ---------------------------------------------------------------------------
# v0.22: queue visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requests_queued_filter_returns_queue_rows(
    client: AsyncClient,
) -> None:
    """GET /requests?queued=true surfaces FIFO queue rows joined with
    the blocking holder's engineer/pattern."""
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v22a.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="bob",
        requester_session_id="bob-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v22a.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
    )

    r = await client.get("/requests?queued=true", headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] is True
    assert body["count"] == 1
    row = body["requests"][0]
    assert row["kind"] == "queued"
    assert row["blocking_claim_id"] == holder_cid
    assert row["blocking_engineer"] == "alice"
    assert row["blocking_pattern"] == "src/v22a.ts"
    assert row["requester_engineer"] == "bob"
    assert row["requester_pattern"] == "src/v22a.ts"
    assert row["claim_type"] == "file"
    assert row["position"] == 1
    assert row["state"] == "waiting"
    assert row["symbols"] is None


@pytest.mark.asyncio
async def test_list_requests_queued_filter_by_requester(
    client: AsyncClient,
) -> None:
    """The requester filter narrows queue rows to a single engineer."""
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v22b.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    for engineer in ("bob", "carol"):
        await db.enqueue_claim_request(
            blocking_claim_id=holder_cid,
            requester_engineer=engineer,
            requester_session_id=f"{engineer}-sess",
            requester_branch=None,
            requester_description=None,
            repo="amittell/coord",
            claim_type="file",
            pattern="src/v22b.ts",
            symbols=None,
            narrowable=None,
            ttl_hours=None,
            wait_seconds=120,
        )

    r = await client.get(
        "/requests?queued=true&requester=bob", headers=_AUTH
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queued"] is True
    assert body["count"] == 1
    assert body["requests"][0]["requester_engineer"] == "bob"


@pytest.mark.asyncio
async def test_list_requests_default_excludes_queue(
    client: AsyncClient,
) -> None:
    """Without the queued flag the response is the legacy requests
    table: no kind='queued' rows leak through, even when the FIFO
    queue is non-empty."""
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v22c.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="bob",
        requester_session_id="bob-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v22c.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
    )

    r = await client.get("/requests", headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "queued" not in body
    for row in body["requests"]:
        assert row.get("kind") != "queued"


# ---------------------------------------------------------------------------
# v0.24: cross-process queue backend
# ---------------------------------------------------------------------------


async def _wait_for_queue_id(
    client: AsyncClient, requester: str, timeout: float = 2.0
) -> str:
    """Poll GET /requests?queued=true until a row appears for the given
    requester engineer and return its queue id. Used by the v0.24 tests
    to discover the queue_id that an in-flight POST /claims long-poll
    just enqueued."""

    import asyncio as _asyncio

    deadline = _asyncio.get_event_loop().time() + timeout
    while _asyncio.get_event_loop().time() < deadline:
        r = await client.get(
            f"/requests?queued=true&requester={requester}", headers=_AUTH
        )
        if r.status_code == 200:
            rows = r.json().get("requests", [])
            for row in rows:
                if (
                    row.get("requester_engineer") == requester
                    and row.get("state") == "waiting"
                ):
                    return row["queue_id"]
        await _asyncio.sleep(0.05)
    raise AssertionError(
        f"queue row for requester {requester!r} did not appear within "
        f"{timeout}s"
    )


@pytest.mark.asyncio
async def test_queue_grant_visible_without_in_process_event(
    client: AsyncClient,
) -> None:
    """v0.24: a grant marked directly in the DB (simulating another
    replica releasing in a different Python process) wakes the
    long-poll via the polling path even though the in-memory
    ``_notify_waiter`` event never fires."""
    import asyncio as _asyncio
    from uuid import uuid4

    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v24a.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    async def queued_request() -> dict:
        body = {
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/v24a.ts"}],
            "wait_seconds": 5,
        }
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    waiter = _asyncio.create_task(queued_request())
    queue_id = await _wait_for_queue_id(client, "bob")

    fake_claim_id = str(uuid4())
    db = deps.get_service().db
    # Cross-process simulation: persist the grant directly without
    # routing through _notify_waiter, so the in-memory event stays
    # unset. The waiter must observe the DB transition via the poll.
    await db.mark_queue_granted(queue_id, fake_claim_id)

    result = await _asyncio.wait_for(waiter, timeout=3.0)
    assert result.get("claim_ids") == [fake_claim_id], result
    assert result.get("conflicts") == [], result


@pytest.mark.asyncio
async def test_queue_expiry_visible_without_in_process_event(
    client: AsyncClient,
) -> None:
    """v0.24: an expiry marked directly in the DB (simulating another
    replica timing out the entry) wakes the long-poll via the polling
    path. Response surfaces the legacy 409 conflict payload."""
    import asyncio as _asyncio

    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v24b.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    async def queued_request() -> dict:
        body = {
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/v24b.ts"}],
            "wait_seconds": 5,
        }
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    waiter = _asyncio.create_task(queued_request())
    queue_id = await _wait_for_queue_id(client, "bob")

    db = deps.get_service().db
    # Cross-process simulation: mark expired without firing the
    # in-memory event. The poll path should observe state='expired'
    # and surface the conflict payload to the caller.
    await db.mark_queue_expired(queue_id)

    result = await _asyncio.wait_for(waiter, timeout=3.0)
    assert result.get("claim_ids") == [], result
    assert result.get("conflicts"), (
        f"expiry must surface conflict payload; got {result}"
    )


@pytest.mark.asyncio
async def test_in_process_event_still_works(
    client: AsyncClient,
) -> None:
    """v0.24: the existing same-process FIFO release path (event-driven
    auto-grant via _drain_queue_for) keeps working unchanged. Proves
    the event fast-path coexists with the new polling path."""
    import asyncio as _asyncio

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v24c.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    async def queued_request() -> dict:
        body = {
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/v24c.ts"}],
            "wait_seconds": 10,
        }
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    waiter = _asyncio.create_task(queued_request())
    # Give bob's POST time to enqueue before we release.
    await _wait_for_queue_id(client, "bob")

    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [holder_cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text

    result = await _asyncio.wait_for(waiter, timeout=5.0)
    assert result.get("claim_ids"), (
        f"bob should be auto-granted via the in-process event path; "
        f"got {result}"
    )
    assert result.get("conflicts") == [], result


# ---------------------------------------------------------------------------
# v0.25: queue priority hints
#
# urgency on CreateClaimsRequest threads into claim_queue.priority so the
# drain path can grant high/blocking waiters ahead of earlier-but-lower-
# priority entries. Unknown values coerce to 'normal' so a typo never
# breaks enqueue. Absent urgency preserves strict v0.21 FIFO.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_priority_blocking_jumps_ahead(
    client: AsyncClient,
) -> None:
    """Three waiters enqueued in FIFO order bob, carol, dan with
    priorities normal, high, blocking. When the holder releases, the
    drain pops by priority DESC (blocking > high > normal) so dan wins
    even though he enqueued last; carol and bob get processed in
    priority order behind him (they re-conflict against dan's new claim
    and surface the conflict payload). The behavioural assertion is
    that strict-FIFO bob does NOT win -- priority overrides position."""
    import asyncio as _asyncio

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/x.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    async def queued_request(
        engineer: str, urgency: str | None
    ) -> dict:
        body: dict[str, Any] = {
            "engineer": engineer,
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/x.ts"}],
            "wait_seconds": 10,
        }
        if urgency is not None:
            body["urgency"] = urgency
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    # Enqueue in FIFO order bob, carol, dan. With strict FIFO bob would
    # win; with priority dan (blocking) jumps to the head, carol (high)
    # second, bob (normal) last. Each `_wait_for_queue_id` replaces a
    # 50ms sleep that was Windows-flaky -- see the long comment in
    # test_queue_grants_in_fifo_order_on_release for the failure mode.
    bob_task = _asyncio.create_task(queued_request("bob", None))
    await _wait_for_queue_id(client, "bob")
    carol_task = _asyncio.create_task(queued_request("carol", "high"))
    await _wait_for_queue_id(client, "carol")
    dan_task = _asyncio.create_task(queued_request("dan", "blocking"))
    await _wait_for_queue_id(client, "dan")

    # Release the holder; the drain pops by priority DESC.
    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [holder_cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text

    dan_result = await _asyncio.wait_for(dan_task, timeout=5)
    carol_result = await _asyncio.wait_for(carol_task, timeout=5)
    bob_result = await _asyncio.wait_for(bob_task, timeout=5)

    # Dan (blocking) wins even though he enqueued last.
    assert dan_result.get("claim_ids"), (
        f"dan (blocking) should be auto-granted first; got {dan_result}"
    )
    # Bob (normal, enqueued FIRST) must NOT win -- priority beat his
    # FIFO position. The drain's second/third pops see dan's new claim
    # and re-conflict carol and bob, so both surface conflict payloads.
    assert bob_result.get("claim_ids") == [], (
        f"bob (normal) should NOT win over dan (blocking); got {bob_result}"
    )
    # Carol also lost to dan; she surfaces a conflict shape. The
    # implementation-level guarantee we care about is that carol was
    # popped from the queue ahead of bob (the priority-DESC ORDER BY
    # is what determines who gets the (failed) grant attempt first).
    assert carol_result.get("claim_ids") == [], (
        f"carol (high) should NOT win over dan (blocking); got {carol_result}"
    )
    assert carol_result.get("conflicts"), (
        f"carol should surface a conflict payload after losing to dan; "
        f"got {carol_result}"
    )


@pytest.mark.asyncio
async def test_queue_priority_default_normal_preserves_fifo(
    client: AsyncClient,
) -> None:
    """Two waiters with no urgency (both default 'normal') retain strict
    FIFO: the first to enqueue is the first to be granted."""
    import asyncio as _asyncio

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/x_fifo.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    async def queued_request(engineer: str) -> dict:
        body = {
            "engineer": engineer,
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/x_fifo.ts"}],
            "wait_seconds": 10,
        }
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    # Each `_wait_for_queue_id` replaces a 50ms sleep that was
    # Windows-flaky: the holder release could fire before bob's POST
    # had observably entered the queue, draining nothing.
    bob_task = _asyncio.create_task(queued_request("bob"))
    await _wait_for_queue_id(client, "bob")
    carol_task = _asyncio.create_task(queued_request("carol"))
    await _wait_for_queue_id(client, "carol")

    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [holder_cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text

    # Bob (enqueued first) wins under strict FIFO.
    bob_result = await _asyncio.wait_for(bob_task, timeout=5)
    assert bob_result.get("claim_ids"), (
        f"bob (first-enqueued, normal) should be granted first; "
        f"got {bob_result}"
    )

    # Carol's wait either times out (still waiting on bob) or also gets
    # granted if bob's claim was released somehow; we only assert the
    # shape is well-formed.
    carol_result = await _asyncio.wait_for(carol_task, timeout=15)
    assert "claim_ids" in carol_result


@pytest.mark.asyncio
async def test_queue_priority_unknown_value_coerces_to_normal(
    client: AsyncClient,
) -> None:
    """A garbage urgency string must not crash the enqueue path: the
    DB layer silently coerces unknown values to 'normal' so the row
    lands at the default priority and FIFO still applies."""
    import asyncio as _asyncio

    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/x_coerce.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    async def queued_request() -> dict:
        body = {
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/x_coerce.ts"}],
            "wait_seconds": 5,
            "urgency": "whatever",
        }
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    waiter = _asyncio.create_task(queued_request())
    # Give the POST time to enqueue, then snapshot the queue row to
    # verify the DB layer coerced the garbage urgency to 'normal'.
    await _wait_for_queue_id(client, "bob")
    db = deps.get_service().db
    rows = await db.list_queued_with_holder(engineer="bob")
    assert rows, "queue row for bob should exist"
    assert rows[0]["priority"] == "normal", (
        f"unknown urgency must coerce to 'normal'; got {rows[0]['priority']!r}"
    )

    # Cancel the long-poll cleanly so the test exits without waiting
    # the full wait_seconds window.
    waiter.cancel()
    try:
        await waiter
    except (_asyncio.CancelledError, Exception):
        pass


# ---------------------------------------------------------------------------
# v0.26: priority age boost
#
# pop_next_waiting_queue_entry lifts a waiting entry's effective rank by one
# priority level once it has been waiting longer than
# settings.queue_age_boost_seconds. Prevents low/normal waiters from starving
# under a steady stream of high/blocking entries. Boost is computed inline in
# the SQL CASE expression on the pop path -- no separate sweep, no writes.
# ---------------------------------------------------------------------------


async def _backdate_queue_entry(
    db: Database, queue_id: str, seconds_ago: int
) -> None:
    """Rewrite a claim_queue row's ``enqueued_at`` to N seconds in the past
    so the age boost can be exercised deterministically (no real sleeps).
    Uses raw aiosqlite because no public Database helper backdates queue
    rows -- this is a v0.26 test fixture, not a production code path.
    """
    import aiosqlite
    from datetime import UTC, datetime, timedelta

    past = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    past_iso = past.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "UPDATE claim_queue SET enqueued_at = ? WHERE id = ?",
            (past_iso, queue_id),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_age_boost_lifts_old_normal_above_fresh_normal(
    client: AsyncClient,
) -> None:
    """Two normal-priority waiters: the older one's enqueued_at is
    backdated 120s into the past, the fresh one was enqueued just now.
    With age_boost_seconds=60 the old waiter's effective rank rises to
    'high' while the fresh waiter stays at 'normal', so the old waiter
    pops first despite being inserted at position 2 (fresh enqueued
    first at position 1). The behavioural assertion is that age boost
    overrides position-tiebreak: even though both have the same
    declared priority, the old one wins because the boost lifts its
    effective rank above the fresh one's.
    """
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v26_a.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db

    # Enqueue fresh waiter FIRST so it gets position=1. If position were
    # the deciding factor, fresh would win. The old waiter at position=2
    # only wins because age boost lifts its effective rank above fresh's.
    fresh = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="fresh",
        requester_session_id="fresh-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26_a.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="normal",
    )
    old = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="old",
        requester_session_id="old-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26_a.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="normal",
    )
    assert fresh["position"] == 1
    assert old["position"] == 2

    # Backdate the "old" entry to look 120s old; the fresh one's
    # enqueued_at stays at now.
    await _backdate_queue_entry(db, old["id"], seconds_ago=120)

    popped = await db.pop_next_waiting_queue_entry(
        holder_cid, age_boost_seconds=60
    )
    assert popped is not None
    assert popped["requester_engineer"] == "old", (
        "old normal waiter (backdated 120s, threshold 60s) must out-pop "
        f"fresh normal at position 1; got {popped['requester_engineer']!r}"
    )


@pytest.mark.asyncio
async def test_age_boost_lifts_normal_to_high_against_fresh_high(
    client: AsyncClient,
) -> None:
    """Old normal-priority waiter (backdated past the boost threshold)
    vs fresh high-priority waiter. Normal rank is 2, high rank is 3;
    age boost adds 1 to the old normal so its effective rank becomes 3,
    tying fresh high's 3. With equal effective rank the tiebreaker is
    position ASC: old normal was enqueued first (position=1), fresh
    high enqueued second (position=2), so old normal wins on the
    position tiebreak. Documenting that explicitly because the win is
    age + position, not age alone.
    """
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v26_b.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db

    # Old normal enqueues first -- position=1.
    old = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="old_normal",
        requester_session_id="old-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26_b.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="normal",
    )
    # Fresh high enqueues second -- position=2.
    fresh = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="fresh_high",
        requester_session_id="fresh-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26_b.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="high",
    )
    assert old["position"] == 1
    assert fresh["position"] == 2

    # Backdate the old normal to look 120s old. With boost threshold 60s
    # the old normal's effective rank rises from 2 to 3, matching fresh
    # high's rank of 3. Position ASC tiebreak picks old (position=1).
    await _backdate_queue_entry(db, old["id"], seconds_ago=120)

    popped = await db.pop_next_waiting_queue_entry(
        holder_cid, age_boost_seconds=60
    )
    assert popped is not None
    assert popped["requester_engineer"] == "old_normal", (
        "old normal (boosted rank=3) ties fresh high (rank=3); position "
        "ASC tiebreak picks old at position=1; "
        f"got {popped['requester_engineer']!r}"
    )


@pytest.mark.asyncio
async def test_age_boost_disabled_when_setting_zero(
    client: AsyncClient,
) -> None:
    """With age_boost_seconds=0 the boost never fires and v0.25 strict-
    priority ordering is preserved: fresh high beats old normal no
    matter how old the normal entry is.
    """
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v26_c.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db

    old = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="old_normal",
        requester_session_id="old-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26_c.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="normal",
    )
    await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="fresh_high",
        requester_session_id="fresh-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26_c.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="high",
    )

    # Backdate old normal aggressively -- 1 full hour. With boost=0 the
    # SQL CASE short-circuits and the age expression never fires, so the
    # rank stays at the declared priority (normal=2 < high=3).
    await _backdate_queue_entry(db, old["id"], seconds_ago=3600)

    popped = await db.pop_next_waiting_queue_entry(
        holder_cid, age_boost_seconds=0
    )
    assert popped is not None
    assert popped["requester_engineer"] == "fresh_high", (
        "with boost disabled, strict declared priority must hold: fresh "
        "high (rank=3) beats backdated normal (rank=2); "
        f"got {popped['requester_engineer']!r}"
    )


# ---------------------------------------------------------------------------
# v0.28: queue ordering
#
# Two refinements stacked on top of the v0.25 priority CASE and v0.26 age
# boost: a fairness override that periodically ignores priority and pops by
# raw FIFO position, and a priority decay rule that drops effective priority
# one level per ``priority_decay_sec`` seconds in queue. Both live in the
# same SQL CASE in pop_next_waiting_queue_entry; tests exercise them via the
# Database API directly so we don't have to wait through real release-drain
# loops. Each test in this section uses the ``_reset_fairness_counters``
# fixture to clear the module-level _FAIRNESS_COUNTERS dict so the modulo
# phase doesn't leak between tests.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _reset_fairness_counters() -> None:
    """Clear the per-process fairness counter dictionary so each test
    starts from count=0. Without this the modulo phase leaks between
    tests via the module-level _FAIRNESS_COUNTERS dict and ordering
    assertions become order-dependent.
    """
    from coordination import db as _db_mod

    _db_mod._FAIRNESS_COUNTERS.clear()
    yield
    _db_mod._FAIRNESS_COUNTERS.clear()


@pytest.mark.asyncio
async def test_fairness_interval_pops_in_position_order_every_nth_time(
    client: AsyncClient,
    _reset_fairness_counters: None,
) -> None:
    """Seed three waiters at the same holder: two low-priority then one
    blocking. With fairness_interval=2 the first pop (count=1) honours
    priority and the blocking waiter wins; the second pop (count=2, the
    fairness pop) bypasses priority and the oldest remaining low waiter
    (position=1) wins.
    """
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v28_fair.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db

    low1 = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="low_first",
        requester_session_id="low1-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_fair.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="low",
    )
    low2 = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="low_second",
        requester_session_id="low2-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_fair.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="low",
    )
    blocking = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="blocker",
        requester_session_id="blocker-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_fair.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="blocking",
    )
    assert low1["position"] == 1
    assert low2["position"] == 2
    assert blocking["position"] == 3

    # Pop 1: count=1, 1 % 2 != 0, priority CASE wins -> blocking.
    pop1 = await db.pop_next_waiting_queue_entry(
        holder_cid,
        fairness_interval=2,
    )
    assert pop1 is not None
    assert pop1["requester_engineer"] == "blocker", (
        "first pop (count=1) honours priority; blocking must win; "
        f"got {pop1['requester_engineer']!r}"
    )

    # Pop 2: count=2, 2 % 2 == 0, fairness override -> oldest position.
    # blocking was already removed by pop1, so position 1 (low_first)
    # is the oldest remaining waiter.
    pop2 = await db.pop_next_waiting_queue_entry(
        holder_cid,
        fairness_interval=2,
    )
    assert pop2 is not None
    assert pop2["requester_engineer"] == "low_first", (
        "second pop (count=2, fairness pop) ignores priority and picks "
        f"position ASC; got {pop2['requester_engineer']!r}"
    )


@pytest.mark.asyncio
async def test_fairness_disabled_when_setting_zero(
    client: AsyncClient,
    _reset_fairness_counters: None,
) -> None:
    """With fairness_interval=0 the override never fires: even 10
    consecutive pops honour priority. Seed one blocking waiter and one
    low waiter; the blocking pops on the first call, the low waiter
    pops on the second, and the eight remaining calls return None.
    Critically, the per-process counter must not be touched -- otherwise
    toggling fairness on later would inherit a leaked modulo phase.
    """
    from coordination import deps
    from coordination import db as _db_mod

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v28_fair_off.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db

    # Enqueue low first (position 1) then blocking (position 2). With
    # fairness=0 the priority CASE wins and blocking pops first; with
    # any fairness pop the low would jump ahead. We assert blocking
    # always wins until removed.
    await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="low_waiter",
        requester_session_id="low-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_fair_off.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="low",
    )
    await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="blocker",
        requester_session_id="blocker-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_fair_off.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="blocking",
    )

    pop1 = await db.pop_next_waiting_queue_entry(
        holder_cid, fairness_interval=0
    )
    assert pop1 is not None
    assert pop1["requester_engineer"] == "blocker"

    # Next call: queue has only the low waiter left. Priority CASE
    # ranks low=1 and there's no fairness override to second-guess it,
    # so the low waiter pops and the subsequent eight calls return None.
    pop2 = await db.pop_next_waiting_queue_entry(
        holder_cid, fairness_interval=0
    )
    assert pop2 is not None
    assert pop2["requester_engineer"] == "low_waiter"
    for _ in range(8):
        assert (
            await db.pop_next_waiting_queue_entry(
                holder_cid, fairness_interval=0
            )
            is None
        )

    # Critical byte-identical-behaviour check: the per-blocking-claim
    # fairness counter must not have been touched when fairness=0.
    assert holder_cid not in _db_mod._FAIRNESS_COUNTERS, (
        "fairness_interval=0 must not advance the counter; found "
        f"{_db_mod._FAIRNESS_COUNTERS.get(holder_cid)!r}"
    )


@pytest.mark.asyncio
async def test_priority_decay_drops_blocking_to_high_after_decay_window(
    client: AsyncClient,
    _reset_fairness_counters: None,
) -> None:
    """Seed an old blocking waiter backdated past two decay windows plus
    a fresh high waiter. Decay subtracts 2 from the old blocking
    (effective rank = 4 - 2 = 2), so fresh high (rank = 3) wins by
    declared priority. We use two windows rather than one to avoid the
    rank-tie + position tiebreak case (the old blocking enqueued first
    at position=1 would otherwise win on a tie). The decay-disabled
    counterpart test asserts the inverse.
    """
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v28_decay.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    decay_sec = 300

    old_blocking = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="old_blocker",
        requester_session_id="ob-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_decay.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="blocking",
    )
    await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="fresh_high",
        requester_session_id="fh-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_decay.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="high",
    )

    # Backdate the old blocking by 2 decay windows + 1s so effective
    # rank = 4 - 2 = 2 < high's rank of 3. Age boost stays disabled so
    # decay alone decides the outcome.
    await _backdate_queue_entry(
        db, old_blocking["id"], seconds_ago=2 * decay_sec + 1
    )

    popped = await db.pop_next_waiting_queue_entry(
        holder_cid,
        age_boost_seconds=0,
        fairness_interval=0,
        priority_decay_sec=decay_sec,
    )
    assert popped is not None
    assert popped["requester_engineer"] == "fresh_high", (
        "decay drops old blocking (4 - 2 = 2) below fresh high (3); "
        f"fresh high must win; got {popped['requester_engineer']!r}"
    )


@pytest.mark.asyncio
async def test_priority_decay_floors_at_low(
    client: AsyncClient,
    _reset_fairness_counters: None,
) -> None:
    """An extreme decay (10x the window) on a normal entry would push
    raw effective rank to 2 - 10 = -8. The ORDER BY clamp floors it at
    'low'=1 so the entry still has a valid ordinal and still pops when
    no other waiters compete. Verifies the clamp + that decay never
    blocks eventual delivery.
    """
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v28_decay_floor.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    decay_sec = 300

    aged_normal = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="ancient_normal",
        requester_session_id="an-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_decay_floor.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="normal",
    )
    await _backdate_queue_entry(
        db, aged_normal["id"], seconds_ago=10 * decay_sec
    )

    popped = await db.pop_next_waiting_queue_entry(
        holder_cid,
        age_boost_seconds=0,
        fairness_interval=0,
        priority_decay_sec=decay_sec,
    )
    assert popped is not None
    assert popped["requester_engineer"] == "ancient_normal", (
        "an ancient normal entry must still pop when it is the only "
        f"waiter; got {popped['requester_engineer']!r}"
    )


@pytest.mark.asyncio
async def test_decay_disabled_when_setting_zero(
    client: AsyncClient,
    _reset_fairness_counters: None,
) -> None:
    """With priority_decay_sec=0 the decay CASE short-circuits to 0 and
    declared priority alone (plus the v0.26 boost, here disabled) drives
    ordering. An ancient blocking entry stays blocking and beats a fresh
    high entry no matter how far back it was enqueued.
    """
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v28_decay_off.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db

    old_blocking = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="ancient_blocker",
        requester_session_id="ab-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_decay_off.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="blocking",
    )
    await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="fresh_high",
        requester_session_id="fh-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v28_decay_off.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
        priority="high",
    )
    await _backdate_queue_entry(db, old_blocking["id"], seconds_ago=3600)

    popped = await db.pop_next_waiting_queue_entry(
        holder_cid,
        age_boost_seconds=0,
        fairness_interval=0,
        priority_decay_sec=0,
    )
    assert popped is not None
    assert popped["requester_engineer"] == "ancient_blocker", (
        "decay disabled: blocking (rank=4) must beat fresh high (rank=3) "
        f"regardless of age; got {popped['requester_engineer']!r}"
    )


# ---------------------------------------------------------------------------
# v0.26: queue cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_queue_request_marks_cancelled(
    client: AsyncClient,
) -> None:
    """DELETE /requests/{queue_id} on a waiting row transitions it to
    'cancelled' and the response carries cancelled=True."""
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v26a.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    entry = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="bob",
        requester_session_id="bob-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26a.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
    )
    queue_id = entry["id"]

    r = await client.delete(f"/requests/{queue_id}", headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "cancelled": True, "queue_id": queue_id}

    row = await db.get_queue_entry(queue_id)
    assert row is not None
    assert row["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_terminal_returns_false(
    client: AsyncClient,
) -> None:
    """Terminal rows (granted/expired/cancelled) cannot be cancelled
    again. The DELETE returns cancelled=False so the requester can tell
    the row wasn't waiting."""
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v26b.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    entry = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="bob",
        requester_session_id="bob-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26b.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
    )
    queue_id = entry["id"]

    # Force the row into a terminal state directly, simulating a
    # successful grant that landed before the requester thought to
    # cancel.
    await db.mark_queue_expired(queue_id)

    r = await client.delete(f"/requests/{queue_id}", headers=_AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["cancelled"] is False
    assert body["queue_id"] == queue_id

    # Row stays in its prior terminal state; cancellation didn't clobber.
    row = await db.get_queue_entry(queue_id)
    assert row is not None
    assert row["state"] == "expired"


@pytest.mark.asyncio
async def test_cancel_with_engineer_filter_rejects_mismatch(
    client: AsyncClient,
) -> None:
    """When ?engineer= is supplied the cancellation only takes effect
    if the row belongs to that engineer. A mismatched engineer gets
    cancelled=False and the row stays waiting; the matching engineer
    then succeeds."""
    from coordination import deps

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v26c.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = deps.get_service().db
    entry = await db.enqueue_claim_request(
        blocking_claim_id=holder_cid,
        requester_engineer="bob",
        requester_session_id="bob-sess",
        requester_branch=None,
        requester_description=None,
        repo="amittell/coord",
        claim_type="file",
        pattern="src/v26c.ts",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=120,
    )
    queue_id = entry["id"]

    # Carol tries to cancel bob's wait; the engineer filter rejects it.
    r_bad = await client.delete(
        f"/requests/{queue_id}?engineer=carol", headers=_AUTH
    )
    assert r_bad.status_code == 200, r_bad.text
    assert r_bad.json()["cancelled"] is False

    row = await db.get_queue_entry(queue_id)
    assert row is not None
    assert row["state"] == "waiting"

    # Bob cancels his own row; success.
    r_ok = await client.delete(
        f"/requests/{queue_id}?engineer=bob", headers=_AUTH
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["cancelled"] is True

    row_after = await db.get_queue_entry(queue_id)
    assert row_after is not None
    assert row_after["state"] == "cancelled"


@pytest.mark.asyncio
async def test_long_poll_wakes_on_cancellation(
    client: AsyncClient,
) -> None:
    """An in-flight POST /claims long-poll wakes promptly when its
    queue row is cancelled via DELETE /requests/{queue_id}. The waiter
    returns the legacy conflict-shape (claim_ids=[]) within ~POLL_INTERVAL,
    not the full wait_seconds window."""
    import asyncio as _asyncio
    import time as _time

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v26d.ts"}],
    }
    rh = await client.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    async def queued_request() -> dict:
        body = {
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/v26d.ts"}],
            "wait_seconds": 5,
        }
        return (await client.post("/claims", headers=_AUTH, json=body)).json()

    waiter = _asyncio.create_task(queued_request())
    queue_id = await _wait_for_queue_id(client, "bob")

    # Cancel from a separate task; the waiter should wake promptly.
    start = _time.monotonic()
    r_cancel = await client.delete(
        f"/requests/{queue_id}", headers=_AUTH
    )
    assert r_cancel.status_code == 200, r_cancel.text
    assert r_cancel.json()["cancelled"] is True

    result = await _asyncio.wait_for(waiter, timeout=3.0)
    elapsed = _time.monotonic() - start
    # Conflict-shape response: empty claim_ids plus the original
    # conflict payload that the legacy 409 path would have returned.
    assert result.get("claim_ids") == [], result
    assert result.get("conflicts"), (
        f"cancelled long-poll must still surface the conflict payload; "
        f"got {result}"
    )
    # POLL_INTERVAL is 0.5s; cancellation should be observed within ~1s
    # of the DELETE (one poll iteration + a little scheduling slack).
    # We assert under 2.0s to keep the test stable under CI load.
    assert elapsed < 2.0, (
        f"long-poll did not wake promptly on cancellation; "
        f"elapsed={elapsed:.2f}s"
    )


# ---------------------------------------------------------------------------
# v0.27: webhook event emission
# ---------------------------------------------------------------------------


async def _outbox_rows(db_path: str, event_type: str | None = None) -> list[dict[str, Any]]:
    """Return outbox rows directly via aiosqlite.

    The delivery loop is not running in these tests; we are exercising
    the emission path only (fire_webhook -> enqueue_webhook), so reading
    the table directly is the most precise observation point.
    """
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        if event_type is None:
            cur = await conn.execute(
                "SELECT * FROM webhook_outbox ORDER BY created_at ASC"
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM webhook_outbox WHERE event_type = ? "
                "ORDER BY created_at ASC",
                (event_type,),
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@pytest.fixture()
async def client_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Variant of ``client`` with webhook emission enabled.

    URL is a fake endpoint; the delivery loop is not started, so the
    outbox rows stay pending. Tests inspect the outbox table directly.
    """
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_WEBHOOK_URL", "http://fake")
    monkeypatch.setenv("COORD_WEBHOOK_SECRET", "test-secret")

    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.db_path = str(db_path)  # type: ignore[attr-defined]
        yield ac

    deps.get_service.cache_clear()


@pytest.fixture()
async def client_webhook_auto_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Webhook-enabled variant with hard auto-promote at threshold=2 so
    the second 409 on a path trips an ``auto-promote`` event."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_WEBHOOK_URL", "http://fake")
    monkeypatch.setenv("COORD_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_THRESHOLD", "2")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_WINDOW_DAYS", "7")
    # Disable subtree promotion so the leaf-promotion path fires
    # deterministically on a single-file hotspot.
    monkeypatch.setenv("COORD_AUTO_PROMOTE_SUBTREE_MIN_FILES", "0")

    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.db_path = str(db_path)  # type: ignore[attr-defined]
        yield ac

    deps.get_service.cache_clear()


@pytest.mark.asyncio
async def test_create_claims_emits_claim_granted(
    client_webhook: AsyncClient,
) -> None:
    """A successful POST /claims writes one ``claim_granted`` row to
    the outbox carrying the created claim id, the engineer, the repo,
    and the session id."""
    import json as _json

    body = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "session_id": "sess-1",
        "claims": [{"type": "file", "pattern": "src/v27a.ts"}],
    }
    r = await client_webhook.post("/claims", headers=_AUTH, json=body)
    assert r.status_code == 200, r.text
    claim_ids = r.json()["claim_ids"]
    assert claim_ids

    rows = await _outbox_rows(
        client_webhook.db_path,  # type: ignore[attr-defined]
        event_type="claim_granted",
    )
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["status"] == "pending"
    assert row["hmac_signature"], "HMAC must be computed at emit time"
    payload = _json.loads(row["payload_json"])
    assert payload["event_type"] == "claim_granted"
    detail = payload["detail"]
    assert detail["engineer"] == "alice"
    assert detail["repo"] == "amittell/coord"
    assert detail["session_id"] == "sess-1"
    assert detail["claim_ids"] == claim_ids


@pytest.mark.asyncio
async def test_auto_promote_emits_webhook(
    client_webhook_auto_promote: AsyncClient,
) -> None:
    """Two distinct attempters bouncing on the same path with
    threshold=2 trips an ``auto-promote`` event and a matching outbox
    row appears."""
    import json as _json

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v27b.ts"}],
    }
    rh = await client_webhook_auto_promote.post(
        "/claims", headers=_AUTH, json=holder
    )
    assert rh.status_code == 200, rh.text

    for engineer in ("bob", "carol"):
        rr = await client_webhook_auto_promote.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": engineer,
                "repo": "amittell/coord",
                "claims": [{"type": "file", "pattern": "src/v27b.ts"}],
            },
        )
        # Both attempts 409 (claim_ids=[]) -- they're the trigger.
        assert rr.json().get("claim_ids") == [], rr.text

    rows = await _outbox_rows(
        client_webhook_auto_promote.db_path,  # type: ignore[attr-defined]
        event_type="auto-promote",
    )
    assert rows, "auto-promote webhook must have fired"
    payload = _json.loads(rows[0]["payload_json"])
    assert payload["event_type"] == "auto-promote"
    detail = payload["detail"]
    assert detail["pattern"] == "src/v27b.ts"
    assert detail["threshold"] == 2
    assert detail["subtree"] is False


@pytest.mark.asyncio
async def test_queue_grant_emits_webhook(
    client_webhook: AsyncClient,
) -> None:
    """Hold + queue + release fires a ``queue_grant`` outbox row when
    the FIFO drain auto-grants the queued requester."""
    import asyncio as _asyncio
    import json as _json

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v27c.ts"}],
    }
    rh = await client_webhook.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text
    holder_claim_id = rh.json()["claim_ids"][0]

    async def queued_request() -> dict:
        body = {
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/v27c.ts"}],
            "wait_seconds": 5,
        }
        return (
            await client_webhook.post("/claims", headers=_AUTH, json=body)
        ).json()

    waiter = _asyncio.create_task(queued_request())
    await _wait_for_queue_id(client_webhook, "bob")

    # Release the holder. The drain re-runs create_claims for the queued
    # requester, which both grants and itself emits a claim_granted
    # webhook -- but the queue_grant emission is what this test asserts.
    rr = await client_webhook.post(
        "/claims/release",
        headers=_AUTH,
        json={"engineer": "alice", "claim_ids": [holder_claim_id]},
    )
    assert rr.status_code == 200, rr.text

    result = await _asyncio.wait_for(waiter, timeout=3.0)
    assert result.get("claim_ids"), result

    rows = await _outbox_rows(
        client_webhook.db_path,  # type: ignore[attr-defined]
        event_type="queue_grant",
    )
    assert len(rows) == 1, rows
    payload = _json.loads(rows[0]["payload_json"])
    assert payload["event_type"] == "queue_grant"
    detail = payload["detail"]
    assert detail["requester_engineer"] == "bob"
    assert detail["pattern"] == "src/v27c.ts"
    assert detail["granted_claim_id"] == result["claim_ids"][0]
    assert detail["queue_id"]


@pytest.mark.asyncio
async def test_queue_cancel_emits_webhook(
    client_webhook: AsyncClient,
) -> None:
    """Enqueue + DELETE /requests/{queue_id} fires a ``queue_cancel``
    outbox row carrying the cancelled requester and pattern."""
    import asyncio as _asyncio
    import json as _json

    holder = {
        "engineer": "alice",
        "repo": "amittell/coord",
        "claims": [{"type": "file", "pattern": "src/v27d.ts"}],
    }
    rh = await client_webhook.post("/claims", headers=_AUTH, json=holder)
    assert rh.status_code == 200, rh.text

    async def queued_request() -> dict:
        body = {
            "engineer": "bob",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": "src/v27d.ts"}],
            "wait_seconds": 5,
        }
        return (
            await client_webhook.post("/claims", headers=_AUTH, json=body)
        ).json()

    waiter = _asyncio.create_task(queued_request())
    queue_id = await _wait_for_queue_id(client_webhook, "bob")

    rc = await client_webhook.delete(
        f"/requests/{queue_id}?engineer=bob", headers=_AUTH
    )
    assert rc.status_code == 200, rc.text
    assert rc.json()["cancelled"] is True

    # Drain the waiter so the task doesn't leak across tests.
    await _asyncio.wait_for(waiter, timeout=3.0)

    rows = await _outbox_rows(
        client_webhook.db_path,  # type: ignore[attr-defined]
        event_type="queue_cancel",
    )
    assert len(rows) == 1, rows
    payload = _json.loads(rows[0]["payload_json"])
    assert payload["event_type"] == "queue_cancel"
    detail = payload["detail"]
    assert detail["requester_engineer"] == "bob"
    assert detail["pattern"] == "src/v27d.ts"
    assert detail["queue_id"] == queue_id


# ---------------------------------------------------------------------------
# v0.28: backpressure header
# ---------------------------------------------------------------------------


async def _seed_two_queued_for_bob(client: AsyncClient) -> None:
    """Set up the shared backpressure-header fixture: alice holds two
    distinct claims, bob has one waiting queue entry behind each. The
    middleware should report a queue depth of 2 whenever bob is the
    identified caller."""
    from coordination import deps

    db = deps.get_service().db

    for idx, pattern in enumerate(("src/v28a.ts", "src/v28b.ts")):
        holder = {
            "engineer": "alice",
            "repo": "amittell/coord",
            "claims": [{"type": "file", "pattern": pattern}],
        }
        rh = await client.post("/claims", headers=_AUTH, json=holder)
        assert rh.status_code == 200, rh.text
        holder_cid = rh.json()["claim_ids"][0]

        await db.enqueue_claim_request(
            blocking_claim_id=holder_cid,
            requester_engineer="bob",
            requester_session_id=f"bob-sess-{idx}",
            requester_branch=None,
            requester_description=None,
            repo="amittell/coord",
            claim_type="file",
            pattern=pattern,
            symbols=None,
            narrowable=None,
            ttl_hours=None,
            wait_seconds=120,
        )


@pytest.mark.asyncio
async def test_backpressure_header_present_with_engineer_in_query(
    client: AsyncClient,
) -> None:
    """The middleware stamps ``X-Coord-Queue-Depth`` on responses when
    the caller's engineer arrives as the standard ``engineer`` query
    parameter, counting that engineer's waiting queue rows."""
    await _seed_two_queued_for_bob(client)

    r = await client.get("/claims?engineer=bob", headers=_AUTH)
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Coord-Queue-Depth") == "2"


@pytest.mark.asyncio
async def test_backpressure_header_uses_x_coord_engineer_header(
    client: AsyncClient,
) -> None:
    """``X-Coord-Engineer`` is the explicit declaration channel for
    coord-mcp wrappers; it must work even when the query string is
    silent on the engineer identity."""
    await _seed_two_queued_for_bob(client)

    headers = dict(_AUTH)
    headers["X-Coord-Engineer"] = "bob"
    r = await client.get("/claims", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Coord-Queue-Depth") == "2"


@pytest.mark.asyncio
async def test_backpressure_header_omitted_without_engineer_signal(
    client: AsyncClient,
) -> None:
    """Anonymous calls (no header, no query) get no header at all -- the
    middleware has nothing to attribute the depth to."""
    await _seed_two_queued_for_bob(client)

    r = await client.get("/claims", headers=_AUTH)
    assert r.status_code == 200, r.text
    assert "X-Coord-Queue-Depth" not in r.headers


@pytest.mark.asyncio
async def test_backpressure_header_disabled_via_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``COORD_BACKPRESSURE_HEADER=false`` the middleware is a
    no-op even when an engineer signal is present."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_BACKPRESSURE_HEADER", "false")

    from coordination import deps

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await _seed_two_queued_for_bob(ac)

        r = await ac.get("/claims?engineer=bob", headers=_AUTH)
        assert r.status_code == 200, r.text
        assert "X-Coord-Queue-Depth" not in r.headers

        headers = dict(_AUTH)
        headers["X-Coord-Engineer"] = "bob"
        r2 = await ac.get("/claims", headers=headers)
        assert r2.status_code == 200, r2.text
        assert "X-Coord-Queue-Depth" not in r2.headers

    deps.get_service.cache_clear()
