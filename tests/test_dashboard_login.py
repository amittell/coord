"""Tests for the v0.29 dashboard login form + cookie session.

The browser path is what was missing pre-v0.29: hitting
``/dashboard`` without auth used to return the raw JSON
``{"detail":"Missing bearer token"}``. Now it returns an HTML
login page, the form posts to ``/dashboard/login``, and a
successful submission sets the ``coord_session`` cookie which the
auth path treats identically to ``Authorization: Bearer ...``.

These tests pin the behaviour real browsers depend on:

1. GET /dashboard unauthenticated -> HTML login page (HTTP 200).
2. POST /dashboard/login with a valid token -> 303 + Set-Cookie.
3. POST /dashboard/login with an invalid token -> login page +
   inline error banner; no cookie set.
4. GET /dashboard with a valid cookie -> dashboard HTML.
5. GET /dashboard with a stale cookie -> login page with the
   stale-token error message.
6. POST /dashboard/logout -> 303 to login form + cleared cookie.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
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


async def test_dashboard_unauth_serves_login_form(client: AsyncClient) -> None:
    """Pre-v0.29 the dashboard returned the raw JSON 401. Browsers
    just saw the text ``{"detail":"Missing bearer token"}``. The fix
    is an HTML login page on HTTP 200 with the form."""
    r = await client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<form" in r.text
    assert "/dashboard/login" in r.text
    assert 'name="token"' in r.text
    # No JSON 401 leaked into the body.
    assert "Missing bearer token" not in r.text


async def test_login_post_with_valid_token_sets_cookie(
    client: AsyncClient,
) -> None:
    """A successful login returns 303 (PRG pattern) with the session
    cookie set. The cookie value is the raw token; the auth path
    resolves it through the same per-engineer / shared / require
    flag pipeline as a header-bearer."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "a" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw), description="laptop"
    )

    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    set_cookie = r.headers.get("set-cookie", "")
    assert "coord_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie


async def test_login_post_with_shared_token_works_by_default(
    client: AsyncClient,
) -> None:
    """Back-compat: the existing shared COORD_AUTH_TOKEN still logs
    in via the form by default. Flipping
    COORD_REQUIRE_PER_ENGINEER_TOKEN=true is the only way to make
    the shared token stop working at this layer too."""
    r = await client.post(
        "/dashboard/login",
        data={"token": "shared-test-token"},
        follow_redirects=False,
    )
    assert r.status_code == 303


async def test_login_post_with_bad_token_renders_error_banner(
    client: AsyncClient,
) -> None:
    # v0.29.4: the form surfaces the auth pipeline's own detail
    # ("Invalid bearer token") instead of a dashboard-only string, so
    # the curl and browser paths report failures identically.
    r = await client.post(
        "/dashboard/login",
        data={"token": "not-a-real-token"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "<form" in r.text
    assert "Invalid bearer token" in r.text
    assert "coord_session=" not in r.headers.get("set-cookie", "")


async def test_login_post_with_expired_token_shows_expiry_hint(
    client: AsyncClient,
) -> None:
    """v0.29.4: an expired per-engineer token pasted into the form
    must re-render with the specific expiry hint -- the user learns
    they need a rotation, not that they fat-fingered the paste."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "7" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main",
        _sha256(raw),
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "<form" in r.text
    assert "Per-engineer token expired" in r.text
    assert "2026-01-01T00:00:00Z" in r.text
    assert "coord_session=" not in r.headers.get("set-cookie", "")


async def test_login_post_with_empty_token_renders_required_error(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/dashboard/login",
        data={"token": ""},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Token is required" in r.text


async def test_dashboard_with_valid_session_cookie_renders(
    client: AsyncClient,
) -> None:
    """The browser carries the session cookie on every dashboard
    request. The dashboard treats it identically to a header
    bearer and renders the real HTML."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "b" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw)
    )

    r = await client.get(
        "/dashboard", cookies={"coord_session": raw}
    )
    assert r.status_code == 200
    # The login form HTML carries `<form method="POST" action="/dashboard/login"`;
    # the real dashboard does not. So presence of the real dashboard
    # markers is the cleanest discriminator.
    assert "action=\"/dashboard/login\"" not in r.text


async def test_dashboard_with_stale_cookie_shows_error_banner(
    client: AsyncClient,
) -> None:
    """An old cookie -- e.g. after the token was revoked or rotated --
    must surface as the friendly stale-token banner, NOT as a JSON
    401. Otherwise the user just sees an opaque error page."""
    r = await client.get(
        "/dashboard", cookies={"coord_session": "stale-revoked"}
    )
    assert r.status_code == 200
    assert "<form" in r.text
    # v0.29.4: the GET path surfaces the auth pipeline's specific
    # detail, so an unknown/revoked token shows the invalid-bearer
    # message rather than a generic banner.
    assert "Invalid bearer token" in r.text


async def test_dashboard_get_with_expired_cookie_shows_expiry_hint(
    client: AsyncClient,
) -> None:
    """A coord_session cookie holding a token that has since expired
    is the most common way a user discovers expiry (login once, come
    back next month, refresh). The dashboard GET must show the same
    actionable expiry hint that the API 401 and the login form show,
    not a generic invalid-token banner."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "9" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main",
        _sha256(raw),
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    r = await client.get("/dashboard", cookies={"coord_session": raw})
    assert r.status_code == 200
    assert "<form" in r.text
    assert "Per-engineer token expired" in r.text
    assert "2026-01-01T00:00:00Z" in r.text


async def test_logout_clears_cookie_and_redirects(
    client: AsyncClient,
) -> None:
    r = await client.post(
        "/dashboard/logout",
        cookies={"coord_session": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"
    set_cookie = r.headers.get("set-cookie", "")
    # delete_cookie issues an empty-value Set-Cookie with a past expiry.
    assert "coord_session=" in set_cookie
    assert "Max-Age=0" in set_cookie or "expires" in set_cookie.lower()


async def test_secure_cookie_flag_honours_x_forwarded_proto(
    client: AsyncClient,
) -> None:
    """Behind Cloudflare or Traefik, TLS terminates at the edge and
    the origin sees plain HTTP. Without trusting
    ``X-Forwarded-Proto: https`` the Secure cookie flag would never
    be set in production, which leaves the cookie willing to travel
    over plaintext if a same-host plain-HTTP path ever existed. The
    fix is to honour the header; this test pins both halves of the
    contract -- header present, header absent.
    """
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "9" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw)
    )

    # Behind a TLS-terminating proxy (Cloudflare's edge): Secure must
    # be set even though the inner transport is HTTP.
    proxied = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    )
    assert proxied.status_code == 303
    cookie = proxied.headers.get("set-cookie", "")
    assert "coord_session=" in cookie
    assert "Secure" in cookie, (
        f"Secure flag should be set when X-Forwarded-Proto is https; "
        f"got Set-Cookie: {cookie!r}"
    )

    # Plain dev (no proxy): no Secure flag, otherwise localhost over
    # http would silently lose its session.
    direct = await client.post(
        "/dashboard/login",
        data={"token": raw},
        follow_redirects=False,
    )
    assert direct.status_code == 303
    cookie = direct.headers.get("set-cookie", "")
    assert "coord_session=" in cookie
    assert "Secure" not in cookie, (
        f"Secure flag must NOT be set on plain dev; "
        f"got Set-Cookie: {cookie!r}"
    )

    # Comma-separated chains (a downstream proxy appended): the
    # first hop is what matters per the X-Forwarded-Proto convention.
    chained = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"x-forwarded-proto": "https, http"},
        follow_redirects=False,
    )
    assert chained.status_code == 303
    assert "Secure" in chained.headers.get("set-cookie", "")


async def test_secure_cookie_flag_honours_cf_visitor(
    client: AsyncClient,
) -> None:
    """Cloudflare adds ``CF-Visitor: {"scheme":"https"}`` at the
    edge; cloudflared and Traefik pass it through untouched.
    Honour it as a fallback to ``X-Forwarded-Proto`` (which Traefik
    rewrites by default) so the Secure cookie flag still gets set
    in real Cloudflare-Tunnel deployments."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "8" * 64
    await svc.db.create_engineer_token(
        "alex/claude/main", _sha256(raw)
    )

    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"cf-visitor": '{"scheme":"https"}'},
        follow_redirects=False,
    )
    assert r.status_code == 303
    cookie = r.headers.get("set-cookie", "")
    assert "Secure" in cookie, (
        f"CF-Visitor scheme=https must set Secure; got Set-Cookie: {cookie!r}"
    )

    # Mangled JSON in CF-Visitor must not crash auth -- we treat it
    # as absent and fall through to the next signal.
    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"cf-visitor": "not-json"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Secure" not in r.headers.get("set-cookie", "")


async def test_dashboard_cookie_force_secure_operator_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch for proxy stacks that strip both
    X-Forwarded-Proto and CF-Visitor. Set
    COORD_DASHBOARD_COOKIE_FORCE_SECURE=true and the Secure flag
    is always written, regardless of headers."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "shared-test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.setenv("COORD_DASHBOARD_COOKIE_FORCE_SECURE", "true")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/dashboard/login",
            data={"token": "shared-test-token"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "Secure" in r.headers.get("set-cookie", "")


async def test_login_form_does_not_leak_token_back_into_html(
    client: AsyncClient,
) -> None:
    """Defense-in-depth: even on a bad-token submission, the
    rejected value must NOT be reflected back into the HTML
    response (that would be a stored XSS risk if the inline error
    message ever interpolated the raw input)."""
    payload = "fake-token-1234"
    r = await client.post(
        "/dashboard/login",
        data={"token": payload},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert payload not in r.text
