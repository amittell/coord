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
