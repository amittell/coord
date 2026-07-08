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
from datetime import UTC, datetime, timedelta
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
        detail = shared.json()["detail"]
        assert "Per-engineer token required" in detail
        assert "coord tokens create \"<engineer>\" --repo <owner/name>" in detail
        assert "coord server/service" in detail
        assert "local SQLite token" in detail

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


async def test_expired_token_401s_with_expiry_hint(
    client: AsyncClient,
) -> None:
    """v0.29.4: an expired per-engineer token must 401 with a detail
    that names the expiry timestamp and points at the operator
    commands -- NOT the bare "Invalid bearer token" a missing or
    revoked token gets."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "f" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main",
        _sha256(raw),
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    r = await client.get(
        "/claims", headers={"Authorization": f"Bearer {raw}"}
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "expired" in detail
    assert "2026-01-01T00:00:00Z" in detail
    assert "coord tokens" in detail


async def test_rotated_token_inside_grace_still_authenticates(
    client: AsyncClient,
) -> None:
    """Rotation hands out a successor but keeps the old token alive
    until grace_until so in-flight agents finish their session
    without a mid-task 401."""
    from coordination.deps import get_service

    svc = get_service()
    old_raw = "coordt_" + "1" * 64
    new_raw = "coordt_" + "2" * 64
    token_id = await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(old_raw)
    )
    rotated = await svc.db.rotate_engineer_token(
        token_id,
        _sha256(new_raw),
        grace_until=datetime.now(UTC) + timedelta(hours=1),
    )
    assert rotated["ok"], rotated

    old = await client.get(
        "/claims", headers={"Authorization": f"Bearer {old_raw}"}
    )
    assert old.status_code == 200, old.text

    new = await client.get(
        "/claims", headers={"Authorization": f"Bearer {new_raw}"}
    )
    assert new.status_code == 200, new.text


async def test_rotated_token_after_grace_401s_with_rotation_hint(
    client: AsyncClient,
) -> None:
    """Once the grace window closes the old token is dead, and the
    401 detail must say WHY (rotation) and point at the replacement
    path -- without leaking any token ids."""
    from coordination.deps import get_service

    svc = get_service()
    old_raw = "coordt_" + "3" * 64
    new_raw = "coordt_" + "4" * 64
    token_id = await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(old_raw)
    )
    rotated = await svc.db.rotate_engineer_token(
        token_id,
        _sha256(new_raw),
        grace_until=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert rotated["ok"], rotated

    r = await client.get(
        "/claims", headers={"Authorization": f"Bearer {old_raw}"}
    )
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "rotated" in detail
    assert "replacement" in detail
    assert "coord tokens create" in detail
    # No token ids in the hint.
    assert token_id not in detail
    assert rotated["new_token_id"] not in detail

    # The successor authenticates fine after the window closes.
    new = await client.get(
        "/claims", headers={"Authorization": f"Bearer {new_raw}"}
    )
    assert new.status_code == 200, new.text


async def test_per_engineer_only_mode_no_shared_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.29.4: COORD_REQUIRE_PER_ENGINEER_TOKEN=true with NO
    COORD_AUTH_TOKEN is a legal deployment (per-engineer-only mode).
    Per-engineer tokens authenticate; anything else 401s with the
    migration hint -- crucially NOT the 500 the pre-v0.29.4 code
    raised whenever auth_token was unset."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("COORD_ALLOW_INSECURE_NO_AUTH", raising=False)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_REQUIRE_PER_ENGINEER_TOKEN", "true")
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        raw = "coordt_" + "5" * 64
        await deps.get_service().db.create_engineer_token(
            "alex/claude/main", _sha256(raw)
        )

        per = await ac.get(
            "/claims", headers={"Authorization": f"Bearer {raw}"}
        )
        assert per.status_code == 200, per.text

        garbage = await ac.get(
            "/claims", headers={"Authorization": "Bearer not-a-token"}
        )
        assert garbage.status_code == 401, garbage.text
        assert "Per-engineer token required" in garbage.json()["detail"]


async def test_no_auth_at_all_still_500s(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misconfiguration contract unchanged: no shared token, no
    require flag, no insecure opt-in means there is no way to
    authenticate anything, and that stays a loud 500."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("COORD_REQUIRE_PER_ENGINEER_TOKEN", raising=False)
    monkeypatch.delenv("COORD_ALLOW_INSECURE_NO_AUTH", raising=False)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get(
            "/claims", headers={"Authorization": "Bearer anything"}
        )
        assert r.status_code == 500
        assert "Server misconfigured" in r.json()["detail"]


async def test_activity_capture_records_source_ip_and_count(
    client: AsyncClient,
) -> None:
    """v0.29.4 activity capture: each authenticated request bumps
    request_count and records the proxy-derived source IP. The first
    hop of X-Forwarded-For is the client; CF-Connecting-IP, when
    present, is the stronger signal and wins."""
    from coordination.deps import get_service

    svc = get_service()
    # Disable touch coalescing for this instance: the test drives two
    # back-to-back requests and asserts both are recorded, which needs
    # write-every-touch (coalescing would defer the second touch's
    # count/IP until the next flushed write).
    svc.db.TOKEN_TOUCH_MIN_INTERVAL_SEC = 0.0
    raw = "coordt_" + "6" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw), description="activity"
    )

    r = await client.get(
        "/claims",
        headers={
            "Authorization": f"Bearer {raw}",
            "X-Forwarded-For": "203.0.113.9, 10.0.0.1",
        },
    )
    assert r.status_code == 200, r.text

    row = await svc.db.resolve_engineer_token(_sha256(raw))
    assert row is not None
    assert row["request_count"] >= 1
    assert row["last_source_ip"] == "203.0.113.9"

    # CF-Connecting-IP beats X-Forwarded-For when both arrive.
    r = await client.get(
        "/claims",
        headers={
            "Authorization": f"Bearer {raw}",
            "CF-Connecting-IP": "198.51.100.4",
            "X-Forwarded-For": "203.0.113.9, 10.0.0.1",
        },
    )
    assert r.status_code == 200, r.text

    row = await svc.db.resolve_engineer_token(_sha256(raw))
    assert row is not None
    assert row["request_count"] >= 2
    assert row["last_source_ip"] == "198.51.100.4"
