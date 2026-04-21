from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination.db import Database
from coordination.main import app


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
