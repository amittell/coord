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
    # v0.29.5: logout now requires the double-submit CSRF pair
    # (coord_csrf cookie + matching csrf_token form field); the old
    # no-csrf logout is pinned as a 403 in
    # test_logout_without_csrf_is_rejected below.
    csrf = "c" * 64
    r = await client.post(
        "/dashboard/logout",
        data={"csrf_token": csrf},
        cookies={"coord_session": "anything", "coord_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/login"
    set_cookies = r.headers.get_list("set-cookie")
    # delete_cookie issues an empty-value Set-Cookie with a past
    # expiry -- for BOTH the session and the csrf cookie (v0.29.5).
    session_clears = [c for c in set_cookies if c.startswith("coord_session=")]
    csrf_clears = [c for c in set_cookies if c.startswith("coord_csrf=")]
    assert session_clears and csrf_clears
    for cleared in (*session_clears, *csrf_clears):
        assert "Max-Age=0" in cleared or "expires" in cleared.lower()


async def test_logout_without_csrf_is_rejected(
    client: AsyncClient,
) -> None:
    """A cross-site form can POST /dashboard/logout but cannot read
    the coord_csrf cookie to forge the matching field, so a missing
    or mismatched csrf_token must 403 and leave the cookies alone."""
    # Missing csrf_token field entirely.
    r = await client.post(
        "/dashboard/logout",
        cookies={"coord_session": "anything", "coord_csrf": "c" * 64},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "CSRF validation failed" in r.text
    assert r.headers.get("set-cookie") is None

    # Mismatched csrf_token value.
    r = await client.post(
        "/dashboard/logout",
        data={"csrf_token": "d" * 64},
        cookies={"coord_session": "anything", "coord_csrf": "c" * 64},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.headers.get("set-cookie") is None


async def test_login_success_sets_fresh_csrf_cookie(
    client: AsyncClient,
) -> None:
    """v0.29.5: a successful login rotates coord_csrf alongside
    coord_session so forms rendered for a previous session cannot
    submit into the new one."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "c" * 64
    await svc.db.create_engineer_token("alex/claude/main", _sha256(raw))

    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        follow_redirects=False,
    )
    assert r.status_code == 303
    set_cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith("coord_session=") for c in set_cookies)
    csrf_cookies = [c for c in set_cookies if c.startswith("coord_csrf=")]
    assert csrf_cookies
    assert "HttpOnly" in csrf_cookies[0]
    assert "SameSite=lax" in csrf_cookies[0]


async def test_login_with_cross_site_origin_is_rejected(
    client: AsyncClient,
) -> None:
    """The login POST is deliberately CSRF-exempt (curl scripting),
    but a browser always sends Origin on a cross-site form post --
    a present-but-mismatched Origin is rejected with the form's
    error banner and no cookie."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "d" * 64
    await svc.db.create_engineer_token("alex/claude/main", _sha256(raw))

    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Cross-site login submission rejected" in r.text
    assert "coord_session=" not in r.headers.get("set-cookie", "")


async def test_login_without_origin_header_still_works(
    client: AsyncClient,
) -> None:
    """curl never sends Origin; the soft guard must let it through
    (this is why the login POST is not behind the full CSRF check)."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "e" * 64
    await svc.db.create_engineer_token("alex/claude/main", _sha256(raw))

    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # A same-origin browser post (Origin matching Host) also passes.
    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"origin": "http://test"},
        follow_redirects=False,
    )
    assert r.status_code == 303


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
    # Assert per-cookie: httpx joins duplicate Set-Cookie headers with
    # ", ", so a joined-string check would pass as long as EITHER
    # cookie carried Secure. Both the session cookie and the v0.29.5
    # csrf cookie must carry it independently.
    set_cookies = proxied.headers.get_list("set-cookie")
    sess = [c for c in set_cookies if c.startswith("coord_session=")]
    assert sess and "Secure" in sess[0], (
        f"Secure flag should be set on coord_session when "
        f"X-Forwarded-Proto is https; got: {sess!r}"
    )
    csrf = [c for c in set_cookies if c.startswith("coord_csrf=")]
    assert csrf and "Secure" in csrf[0], (
        f"Secure flag should be set on coord_csrf when "
        f"X-Forwarded-Proto is https; got: {csrf!r}"
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
    for prefix in ("coord_session=", "coord_csrf="):
        match = [
            c for c in r.headers.get_list("set-cookie")
            if c.startswith(prefix)
        ]
        assert match and "Secure" in match[0], (
            f"CF-Visitor scheme=https must set Secure on {prefix}; "
            f"got: {match!r}"
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
        for prefix in ("coord_session=", "coord_csrf="):
            match = [
                c for c in r.headers.get_list("set-cookie")
                if c.startswith(prefix)
            ]
            assert match and "Secure" in match[0], (
                f"force-secure must set Secure on {prefix}; got: {match!r}"
            )


async def test_login_origin_guard_with_port_bearing_host(
    client: AsyncClient,
) -> None:
    """Dev runs on localhost:8000; the Origin guard compares the full
    netloc, so a matching port passes and a missing port is rejected.
    Pins the comparison against a future host-only or default-port
    normalisation regressing the common dev configuration."""
    from coordination.deps import get_service

    svc = get_service()
    raw = "coordt_" + "f" * 64
    await svc.db.create_engineer_token("alex/claude/main", _sha256(raw))

    # Origin and Host both carry the port: same-origin, accepted.
    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"origin": "http://test:8000", "host": "test:8000"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Origin lacks the port the Host carries: mismatch, rejected.
    r = await client.post(
        "/dashboard/login",
        data={"token": raw},
        headers={"origin": "http://test", "host": "test:8000"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "coord_session=" not in r.headers.get("set-cookie", "")


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
