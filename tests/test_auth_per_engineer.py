"""End-to-end auth tests for v0.29 per-engineer bearer tokens.

These cover the four contracts the dashboard login + agent
bearer flow depends on:

1. A per-engineer token issued via ``Database.create_engineer_token``
   authenticates the same HTTP routes that the shared token does.
2. A revoked per-engineer token 401s on next use.
3. Setting ``COORD_REQUIRE_PER_ENGINEER_TOKEN=true`` makes the
   shared token stop working without breaking per-engineer tokens.
4. A ``coord_session`` cookie carrying a valid token authenticates
   the same as ``Authorization: Bearer ...``. This is what makes
   the dashboard login form work.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination.main import app


def _sha256(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


@pytest.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "shared-test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_per_engineer_token_authenticates(client: AsyncClient) -> None:
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "a" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw), description="laptop"
    )

    r = await client.get(
        "/claims", headers={"Authorization": f"Bearer {raw}"}
    )
    assert r.status_code == 200, r.text


async def test_shared_token_still_works_by_default(client: AsyncClient) -> None:
    """Default deployment: per-engineer tokens are additive. Existing
    clients with the shared token keep working untouched after the
    v14 migration."""
    r = await client.get(
        "/claims", headers={"Authorization": "Bearer shared-test-token"}
    )
    assert r.status_code == 200, r.text


async def test_revoked_per_engineer_token_401s(client: AsyncClient) -> None:
    """Revocation is the kill switch -- the row stays in
    engineer_tokens for audit but lookup_engineer_token returns
    None, so the auth path falls through to invalid-token 401."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "b" * 64
    token_id = await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw)
    )

    ok = await client.get(
        "/claims", headers={"Authorization": f"Bearer {raw}"}
    )
    assert ok.status_code == 200

    await svc.db.revoke_engineer_token(token_id)

    dead = await client.get(
        "/claims", headers={"Authorization": f"Bearer {raw}"}
    )
    assert dead.status_code == 401
    assert "Invalid" in dead.json()["detail"]


async def test_require_per_engineer_token_rejects_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """COORD_REQUIRE_PER_ENGINEER_TOKEN=true: the migration kill
    switch. Shared token MUST 401 with a helpful hint, but a
    per-engineer token continues to work."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "shared-test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_REQUIRE_PER_ENGINEER_TOKEN", "true")
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Pre-seed a per-engineer token.
        raw = "coordt_" + "c" * 64
        await deps.get_service().db.create_engineer_token(
            "alex/claude/main", _sha256(raw)
        )

        # Shared token: 401 with the migration hint.
        shared = await ac.get(
            "/claims", headers={"Authorization": "Bearer shared-test-token"}
        )
        assert shared.status_code == 401
        assert "Per-engineer token required" in shared.json()["detail"]
        assert "coord tokens create" in shared.json()["detail"]

        # Per-engineer token: still 200.
        per = await ac.get(
            "/claims", headers={"Authorization": f"Bearer {raw}"}
        )
        assert per.status_code == 200, per.text


async def test_cookie_session_authenticates(client: AsyncClient) -> None:
    """The dashboard login form sets ``coord_session=<token>`` as an
    HTTP-only cookie. Subsequent requests carrying the cookie but no
    Authorization header must authenticate identically -- this is
    the contract that lets a real browser navigate the dashboard
    without typing the token on every request."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "d" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw), description="browser"
    )

    r = await client.get("/claims", cookies={"coord_session": raw})
    assert r.status_code == 200, r.text


async def test_header_beats_cookie_when_both_present(
    client: AsyncClient,
) -> None:
    """An explicit Authorization header always wins over a stale
    cookie so an operator can debug with curl against the same
    browser session without clearing cookies first."""
    from coordination.deps import get_service

    svc = get_service()
    raw_good = "coordt_" + "e" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw_good)
    )

    r = await client.get(
        "/claims",
        headers={"Authorization": f"Bearer {raw_good}"},
        cookies={"coord_session": "stale-revoked-cookie-value"},
    )
    assert r.status_code == 200, r.text


async def test_missing_both_header_and_cookie_401s(
    client: AsyncClient,
) -> None:
    r = await client.get("/claims")
    assert r.status_code == 401
    assert "Missing" in r.json()["detail"]
