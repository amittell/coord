"""Tests for the v0.29.5 in-dashboard token management UI + CSRF.

Two surfaces under test:

1. CSRF protection (double-submit cookie pattern): GET /dashboard
   seeds the ``coord_csrf`` cookie, every state-changing form embeds
   the matching hidden ``csrf_token`` field, and the token endpoints
   403 on a missing or mismatched pair.

2. The token endpoints themselves: POST /dashboard/tokens/create and
   POST /dashboard/tokens/revoke, with the per-engineer scoping rules
   (self-service mints only for yourself, capped at your own token's
   expiry; revokes only your own rows) versus the operator view
   (shared-token session: mint for anyone, revoke anything).

Fixture conventions mirror tests/test_dashboard_login.py: env via
monkeypatch, deps.get_service.cache_clear(), ASGITransport client.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination.main import app

SHARED_TOKEN = "shared-test-token"

_RAW_TOKEN_RE = re.compile(r"coordt_[0-9a-f]{64}")
_CSRF_VALUE_RE = re.compile(r"coord_csrf=([0-9a-f]{64})")


def _sha256(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


@pytest.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED_TOKEN)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture()
async def insecure_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """Insecure no-auth deployment: every request authenticates as the
    anonymous outcome (ok, auth_kind None). Token management must be
    locked out -- there is no identity to bind tokens to."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("COORD_ALLOW_INSECURE_NO_AUTH", "true")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _svc():
    from coordination.deps import get_service

    return get_service()


async def _mint_engineer_token(
    engineer: str, *, expires_at: datetime | None = None, repo: str | None = None
) -> tuple[str, str]:
    """Insert a per-engineer token row directly; returns (raw, id)."""
    raw = "coordt_" + secrets.token_hex(32)
    token_id = await _svc().db.create_engineer_token(
        engineer, _sha256(raw), expires_at=expires_at, repo=repo
    )
    return raw, token_id


async def test_scoped_session_cannot_mint_unscoped_token(
    client: AsyncClient,
) -> None:
    """#30 slice 2/3 escalation guard: a repo-scoped session must not be able
    to mint itself an unscoped (operator) token. The minted token inherits
    the session token's repo, never NULL."""
    raw, _ = await _mint_engineer_token("e/claude/main", repo="amittell/repo-a")
    cookies = await _login(client, raw)
    r = await client.post(
        "/dashboard/tokens/create",
        data={"description": "child-token", "csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
    )
    assert r.status_code == 200, r.text
    rows = await _token_rows()
    child = next(row for row in rows if row.get("description") == "child-token")
    assert child["repo"] == "amittell/repo-a", child


async def _login(client: AsyncClient, token: str) -> dict[str, str]:
    """POST the login form and capture the session + csrf cookie pair.
    The client jar is cleared afterwards so each test passes cookies
    explicitly per request (deterministic, no jar accumulation)."""
    r = await client.post(
        "/dashboard/login", data={"token": token}, follow_redirects=False
    )
    assert r.status_code == 303, r.text
    cookies = {
        "coord_session": r.cookies["coord_session"],
        "coord_csrf": r.cookies["coord_csrf"],
    }
    client.cookies.clear()
    return cookies


async def _token_rows() -> list[dict]:
    return await _svc().db.list_engineer_tokens(include_revoked=True)


# ---------------------------------------------------------------------------
# CSRF cookie issuance + form embedding
# ---------------------------------------------------------------------------


async def test_csrf_cookie_issued_on_unauthenticated_dashboard_get(
    client: AsyncClient,
) -> None:
    """The very first GET /dashboard (login page) must seed the
    coord_csrf cookie so the forms are armed before any login."""
    r = await client.get("/dashboard")
    assert r.status_code == 200
    set_cookie = ";".join(r.headers.get_list("set-cookie"))
    match = _CSRF_VALUE_RE.search(set_cookie)
    assert match, f"expected coord_csrf cookie, got: {set_cookie!r}"
    assert "HttpOnly" in set_cookie

    # A follow-up GET carrying the well-shaped cookie must NOT re-mint.
    r2 = await client.get("/dashboard")
    assert "coord_csrf=" not in ";".join(r2.headers.get_list("set-cookie"))


async def test_csrf_hidden_input_present_in_rendered_forms(
    client: AsyncClient,
) -> None:
    """The authenticated dashboard embeds the cookie's csrf value as a
    hidden field in the logout, create, and revoke forms."""
    raw, _ = await _mint_engineer_token("alex/claude/main")
    cookies = await _login(client, raw)

    r = await client.get("/dashboard", cookies=cookies)
    assert r.status_code == 200
    assert 'name="csrf_token"' in r.text
    assert cookies["coord_csrf"] in r.text


# ---------------------------------------------------------------------------
# POST /dashboard/tokens/create
# ---------------------------------------------------------------------------


async def test_create_with_missing_csrf_field_is_rejected(
    client: AsyncClient,
) -> None:
    raw, _ = await _mint_engineer_token("alex/claude/main")
    cookies = await _login(client, raw)
    before = len(await _token_rows())

    r = await client.post(
        "/dashboard/tokens/create",
        data={"engineer": "alex/claude/main"},
        cookies=cookies,
    )
    assert r.status_code == 403
    assert "CSRF validation failed" in r.text
    assert len(await _token_rows()) == before


async def test_create_with_mismatched_csrf_is_rejected(
    client: AsyncClient,
) -> None:
    raw, _ = await _mint_engineer_token("alex/claude/main")
    cookies = await _login(client, raw)
    before = len(await _token_rows())

    r = await client.post(
        "/dashboard/tokens/create",
        data={"engineer": "alex/claude/main", "csrf_token": "f" * 64},
        cookies=cookies,
    )
    assert r.status_code == 403
    assert len(await _token_rows()) == before


async def test_operator_create_returns_one_time_page(
    client: AsyncClient,
) -> None:
    """Shared-token (operator) session mints for an arbitrary engineer
    and gets the raw token back exactly once, never cacheable, never
    re-renderable on the dashboard itself."""
    cookies = await _login(client, SHARED_TOKEN)

    r = await client.post(
        "/dashboard/tokens/create",
        data={
            "engineer": "dana/claude/feature",
            "description": "build box",
            "csrf_token": cookies["coord_csrf"],
        },
        cookies=cookies,
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["pragma"] == "no-cache"
    assert "shown exactly once" in r.text
    match = _RAW_TOKEN_RE.search(r.text)
    assert match, "one-time page must contain the raw coordt_ token"
    raw_minted = match.group(0)

    rows = await _token_rows()
    assert any(
        row["engineer"] == "dana/claude/feature"
        and row["description"] == "build box"
        for row in rows
    )

    # The dashboard reload must not contain the raw value -- only the
    # sha256 hash was stored, so it cannot be re-rendered.
    r2 = await client.get("/dashboard", cookies=cookies)
    assert r2.status_code == 200
    assert raw_minted not in r2.text

    # The minted token actually authenticates.
    r3 = await client.get(
        "/claims", headers={"Authorization": f"Bearer {raw_minted}"}
    )
    assert r3.status_code == 200


async def test_per_engineer_create_forces_own_engineer(
    client: AsyncClient,
) -> None:
    """A per-engineer session cannot mint for someone else: the
    submitted engineer field is ignored, not validated-and-honoured."""
    raw, _ = await _mint_engineer_token("alex/claude/main")
    cookies = await _login(client, raw)

    r = await client.post(
        "/dashboard/tokens/create",
        data={
            "engineer": "someone-else/claude/main",
            "csrf_token": cookies["coord_csrf"],
        },
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "shown exactly once" in r.text

    rows = await _token_rows()
    assert not any(
        row["engineer"] == "someone-else/claude/main" for row in rows
    )
    own = [r2 for r2 in rows if r2["engineer"] == "alex/claude/main"]
    assert len(own) == 2  # the session token + the freshly minted one


async def test_shared_session_create_requires_engineer(
    client: AsyncClient,
) -> None:
    cookies = await _login(client, SHARED_TOKEN)
    before = len(await _token_rows())

    r = await client.post(
        "/dashboard/tokens/create",
        data={"csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "Engineer is required" in r.text
    assert len(await _token_rows()) == before


async def test_expiring_session_must_set_expires_in(
    client: AsyncClient,
) -> None:
    """Self-service expiry policy, half one: a per-engineer session
    whose own token expires cannot mint a never-expiring token."""
    cap = datetime.now(UTC) + timedelta(days=7)
    raw, _ = await _mint_engineer_token("alex/claude/main", expires_at=cap)
    cookies = await _login(client, raw)
    before = len(await _token_rows())

    r = await client.post(
        "/dashboard/tokens/create",
        data={"csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "must set" in r.text
    assert len(await _token_rows()) == before


async def test_expiring_session_cannot_outlive_own_token(
    client: AsyncClient,
) -> None:
    """Self-service expiry policy, half two: the new token's expiry
    must land at or before the session token's own expiry."""
    cap = datetime.now(UTC) + timedelta(days=7)
    raw, _ = await _mint_engineer_token("alex/claude/main", expires_at=cap)
    cookies = await _login(client, raw)
    before = len(await _token_rows())

    r = await client.post(
        "/dashboard/tokens/create",
        data={"expires_in": "30d", "csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "cannot outlive" in r.text
    assert len(await _token_rows()) == before


async def test_expiring_session_create_within_cap_succeeds(
    client: AsyncClient,
) -> None:
    cap = datetime.now(UTC) + timedelta(days=7)
    raw, _ = await _mint_engineer_token("alex/claude/main", expires_at=cap)
    cookies = await _login(client, raw)

    r = await client.post(
        "/dashboard/tokens/create",
        data={"expires_in": "1d", "csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "shown exactly once" in r.text

    # Both rows (the session token and the minted one) must carry an
    # expiry at or before the session token's cap.
    rows = await _token_rows()
    own = [r2 for r2 in rows if r2["engineer"] == "alex/claude/main"]
    assert len(own) == 2
    for row in own:
        assert row["expires_at"] is not None
        exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        assert exp <= cap


async def test_operator_create_with_bad_duration_shows_error(
    client: AsyncClient,
) -> None:
    cookies = await _login(client, SHARED_TOKEN)
    before = len(await _token_rows())

    r = await client.post(
        "/dashboard/tokens/create",
        data={
            "engineer": "dana/claude/feature",
            "expires_in": "banana",
            "csrf_token": cookies["coord_csrf"],
        },
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "Invalid duration" in r.text
    assert len(await _token_rows()) == before


async def test_operator_create_without_expiry_is_uncapped(
    client: AsyncClient,
) -> None:
    """Shared/operator sessions have no self-service cap: minting a
    never-expiring token is allowed even though the dashboard session
    itself is cookie-lifetime-bound."""
    cookies = await _login(client, SHARED_TOKEN)

    r = await client.post(
        "/dashboard/tokens/create",
        data={
            "engineer": "dana/claude/feature",
            "csrf_token": cookies["coord_csrf"],
        },
        cookies=cookies,
    )
    assert r.status_code == 200
    assert "shown exactly once" in r.text
    rows = await _token_rows()
    minted = [r2 for r2 in rows if r2["engineer"] == "dana/claude/feature"]
    assert len(minted) == 1
    assert minted[0]["expires_at"] is None


# ---------------------------------------------------------------------------
# Token panel scoping
# ---------------------------------------------------------------------------


async def test_per_engineer_panel_lists_only_own_tokens(
    client: AsyncClient,
) -> None:
    raw_a, id_a = await _mint_engineer_token("alex/claude/main")
    _, id_b = await _mint_engineer_token("blair/claude/main")
    cookies = await _login(client, raw_a)

    r = await client.get("/dashboard", cookies=cookies)
    assert r.status_code == 200
    assert "<h2>engineer tokens</h2>" in r.text
    # Full id ships in the row's title attribute.
    assert id_a in r.text
    assert id_b not in r.text


async def test_operator_panel_lists_all_tokens_including_revoked(
    client: AsyncClient,
) -> None:
    _, id_a = await _mint_engineer_token("alex/claude/main")
    _, id_b = await _mint_engineer_token("blair/claude/main")
    await _svc().db.revoke_engineer_token(id_b)
    cookies = await _login(client, SHARED_TOKEN)

    r = await client.get("/dashboard", cookies=cookies)
    assert r.status_code == 200
    assert "<h2>engineer tokens</h2>" in r.text
    assert id_a in r.text
    assert id_b in r.text
    assert "tok-revoked" in r.text
    # Operator view carries the engineer column.
    assert "alex/claude/main" in r.text
    assert "blair/claude/main" in r.text


# ---------------------------------------------------------------------------
# POST /dashboard/tokens/revoke
# ---------------------------------------------------------------------------


async def test_per_engineer_revokes_own_token(client: AsyncClient) -> None:
    raw, _ = await _mint_engineer_token("alex/claude/main")
    _, target_id = await _mint_engineer_token("alex/claude/main")
    cookies = await _login(client, raw)

    r = await client.post(
        "/dashboard/tokens/revoke",
        data={"token_id": target_id, "csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    row = await _svc().db.get_engineer_token_by_id(target_id)
    assert row is not None and row["revoked_at"] is not None


async def test_per_engineer_cannot_revoke_foreign_token(
    client: AsyncClient,
) -> None:
    raw, _ = await _mint_engineer_token("alex/claude/main")
    _, foreign_id = await _mint_engineer_token("blair/claude/main")
    cookies = await _login(client, raw)

    r = await client.post(
        "/dashboard/tokens/revoke",
        data={"token_id": foreign_id, "csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "another engineer" in r.text
    row = await _svc().db.get_engineer_token_by_id(foreign_id)
    assert row is not None and row["revoked_at"] is None


async def test_revoke_with_missing_csrf_is_rejected(
    client: AsyncClient,
) -> None:
    raw, _ = await _mint_engineer_token("alex/claude/main")
    _, target_id = await _mint_engineer_token("alex/claude/main")
    cookies = await _login(client, raw)

    r = await client.post(
        "/dashboard/tokens/revoke",
        data={"token_id": target_id},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 403
    row = await _svc().db.get_engineer_token_by_id(target_id)
    assert row is not None and row["revoked_at"] is None


async def test_operator_revokes_expired_and_rotating_tokens(
    client: AsyncClient,
) -> None:
    """Revocation applies to any non-revoked row, including ones that
    no longer authenticate (expired, rotation grace) -- a revoke is an
    audit-trail statement, not just an auth cutoff."""
    db = _svc().db
    _, expired_id = await _mint_engineer_token(
        "alex/claude/main",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _, rotated_id = await _mint_engineer_token("blair/claude/main")
    result = await db.rotate_engineer_token(
        rotated_id,
        _sha256("coordt_" + secrets.token_hex(32)),
        grace_until=datetime.now(UTC) + timedelta(hours=24),
    )
    assert result["ok"]
    cookies = await _login(client, SHARED_TOKEN)

    for token_id in (expired_id, rotated_id):
        r = await client.post(
            "/dashboard/tokens/revoke",
            data={"token_id": token_id, "csrf_token": cookies["coord_csrf"]},
            cookies=cookies,
            follow_redirects=False,
        )
        assert r.status_code == 303
        row = await db.get_engineer_token_by_id(token_id)
        assert row is not None and row["revoked_at"] is not None


async def test_revoke_missing_or_already_revoked_is_idempotent(
    client: AsyncClient,
) -> None:
    raw, _ = await _mint_engineer_token("alex/claude/main")
    _, target_id = await _mint_engineer_token("alex/claude/main")
    await _svc().db.revoke_engineer_token(target_id)
    cookies = await _login(client, raw)

    # Already revoked (own row): PRG back to the dashboard, no error.
    r = await client.post(
        "/dashboard/tokens/revoke",
        data={"token_id": target_id, "csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Unknown id: same idempotent 303.
    r = await client.post(
        "/dashboard/tokens/revoke",
        data={"token_id": "no-such-id", "csrf_token": cookies["coord_csrf"]},
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# Insecure no-auth mode
# ---------------------------------------------------------------------------


async def test_insecure_mode_renders_dashboard_without_token_panel(
    insecure_client: AsyncClient,
) -> None:
    r = await insecure_client.get("/dashboard")
    assert r.status_code == 200
    # The real dashboard, not the login form...
    assert 'action="/dashboard/login"' not in r.text
    assert "active claims" in r.text
    # ...but no token panel: there is no identity to scope it to.
    assert "<h2>engineer tokens</h2>" not in r.text
    assert "/dashboard/tokens/create" not in r.text


async def test_insecure_mode_cannot_manage_tokens(
    insecure_client: AsyncClient,
) -> None:
    csrf = "a" * 64
    r = await insecure_client.post(
        "/dashboard/tokens/create",
        data={"engineer": "anyone", "csrf_token": csrf},
        cookies={"coord_csrf": csrf},
    )
    assert r.status_code == 403
    assert "cannot manage tokens" in r.text
    assert len(await _token_rows()) == 0
