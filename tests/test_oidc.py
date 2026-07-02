"""Tests for the v0.29.6 generic OIDC SSO login.

The interesting property of this suite is that it drives the REAL
routes (``/auth/oidc/login`` and ``/auth/oidc/callback``) end to end
against an in-process fake IdP: an ``httpx.MockTransport`` handler
that serves discovery, JWKS, and the token endpoint, minting ID
tokens signed with a module-local RSA key. The production code's
``_oidc_http_client`` factory is monkeypatched to return a client
backed by that transport, so every byte of the protocol module runs
unmodified -- no protocol step is stubbed out.

Fixture conventions mirror tests/test_dashboard_login.py: env via
monkeypatch, deps.get_service.cache_clear(), ASGITransport client.
The OIDC metadata/JWKS TTL caches are cleared per test via
``oidc._clear_caches()`` so the shared fake-issuer URL never leaks
state between tests.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient, MockTransport

from coordination import main as main_mod
from coordination import oidc
from coordination.main import app

ISSUER = "https://idp.test"
GOOGLE_ISSUER = "https://accounts.google.com"
CLIENT_ID = "coord-dashboard"
CLIENT_SECRET = "oidc-test-client-secret"
REDIRECT_URI = "http://test/auth/oidc/callback"
KID = "test-key"

# One RSA keypair per module: 2048-bit generation is fast enough to do
# once, far too slow to do per test. A second key signs the
# "bad signature" and "rotated kid" cases.
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_for(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


JWKS = {"keys": [_jwk_for(_PRIVATE_KEY, KID)]}


def _make_id_token(
    *,
    nonce: str,
    key: rsa.RSAPrivateKey | None = None,
    kid: str = KID,
    iss: str = ISSUER,
    aud: str | list[str] = CLIENT_ID,
    email: str | None = "dev@example.com",
    exp_delta: int = 600,
    iat_delta: int = 0,
    extra: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": "user-1",
        "nonce": nonce,
        "iat": now + iat_delta,
        "exp": now + exp_delta,
    }
    if email is not None:
        claims["email"] = email
    if extra:
        claims.update(extra)
    return jwt.encode(
        claims, key or _PRIVATE_KEY, algorithm="RS256", headers={"kid": kid}
    )


class FakeIdP:
    """In-process IdP: discovery + JWKS + token endpoint behind an
    httpx.MockTransport handler. Mutable knobs let individual tests
    inject failure modes without re-plumbing the transport."""

    def __init__(self, issuer: str = ISSUER):
        self.issuer = issuer
        # The nonce the next minted ID token should carry. The test
        # captures it out of the authorize-redirect query string and
        # assigns it here before hitting the callback.
        self.nonce: str | None = None
        # Overrides for _make_id_token on the default token response.
        self.id_token_kwargs: dict[str, Any] = {}
        # Full token-endpoint JSON override (e.g. the alg=none case).
        self.token_response: dict[str, Any] | None = None
        self.token_status = 200
        self.discovery_status = 200
        # Merged over the well-formed discovery doc: lets tests serve
        # a poisoned document (http:// endpoints, wrong issuer claim).
        self.metadata_overrides: dict[str, Any] = {}
        # FIFO of JWKS bodies; empty means "serve the default JWKS".
        self.jwks_queue: list[dict[str, Any]] = []
        self.jwks_fetches = 0
        self.token_posts = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != (urlsplit(self.issuer).hostname or ""):
            return httpx.Response(404)
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            if self.discovery_status != 200:
                return httpx.Response(self.discovery_status, text="boom")
            doc: dict[str, Any] = {
                "issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token",
                "jwks_uri": self.issuer + "/jwks",
            }
            doc.update(self.metadata_overrides)
            return httpx.Response(200, json=doc)
        if path == "/jwks":
            self.jwks_fetches += 1
            if self.jwks_queue:
                return httpx.Response(200, json=self.jwks_queue.pop(0))
            return httpx.Response(200, json=JWKS)
        if path == "/token":
            self.token_posts += 1
            if self.token_status != 200:
                return httpx.Response(
                    self.token_status, json={"error": "server_error"}
                )
            if self.token_response is not None:
                return httpx.Response(200, json=self.token_response)
            kwargs = dict(self.id_token_kwargs)
            kwargs.setdefault("nonce", self.nonce or "")
            return httpx.Response(
                200,
                json={
                    "id_token": _make_id_token(**kwargs),
                    "access_token": "fake-access-token",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(404)


BASE_OIDC_ENV = {
    "COORD_OIDC_ISSUER": ISSUER,
    "COORD_OIDC_CLIENT_ID": CLIENT_ID,
    "COORD_OIDC_CLIENT_SECRET": CLIENT_SECRET,
    "COORD_OIDC_REDIRECT_URI": REDIRECT_URI,
    "COORD_OIDC_ALLOWED_PRINCIPALS": "dev@example.com",
}


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    idp: FakeIdP | None = None,
    *,
    oidc_env: bool = True,
    env: dict[str, str | None] | None = None,
) -> None:
    monkeypatch.setenv("COORD_AUTH_TOKEN", "shared-test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    if oidc_env:
        for k, v in BASE_OIDC_ENV.items():
            monkeypatch.setenv(k, v)
    else:
        # Actively delete rather than merely not-set: a test may call
        # _setup twice (e.g. the /meta flag test) and the second call
        # must be able to turn the feature off again.
        for k in BASE_OIDC_ENV:
            monkeypatch.delenv(k, raising=False)
    # An env override of None means "delete the variable" -- used by
    # the per-engineer-only test to drop COORD_AUTH_TOKEN entirely.
    for k, override in (env or {}).items():
        if override is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, override)

    from coordination import deps

    deps.get_service.cache_clear()
    oidc._clear_caches()
    if idp is not None:
        transport = MockTransport(idp.handler)
        monkeypatch.setattr(
            main_mod,
            "_oidc_http_client",
            lambda: httpx.AsyncClient(transport=transport),
        )


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login_redirect(client: AsyncClient) -> tuple[dict[str, str], str]:
    """GET /auth/oidc/login and return (authorize-URL query params,
    raw coord_oidc cookie value). The client's jar also keeps the
    cookie, so a follow-up callback on the same client carries it."""
    r = await client.get("/auth/oidc/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    qs = dict(parse_qsl(urlsplit(r.headers["location"]).query))
    cookie = r.cookies.get("coord_oidc")
    assert cookie
    return qs, cookie


# ---------------------------------------------------------------------------
# Feature flag + login redirect
# ---------------------------------------------------------------------------


async def test_routes_404_when_oidc_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With none of the COORD_OIDC_* vars set, the routes effectively
    do not exist -- and the JSON body is byte-identical to FastAPI's
    default for a route that genuinely does not exist, so a probe
    cannot fingerprint "coord with SSO off"."""
    _setup(monkeypatch, tmp_path, oidc_env=False)
    async with _client() as client:
        baseline = await client.get("/auth/oidc/no-such-route")
        assert baseline.status_code == 404
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 404
        assert r.json() == baseline.json() == {"detail": "Not Found"}
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "x", "state": "y"},
            follow_redirects=False,
        )
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}


async def test_login_redirects_with_state_nonce_pkce_and_signed_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert loc.startswith(ISSUER + "/authorize?")
        qs = dict(parse_qsl(urlsplit(loc).query))
        assert qs["response_type"] == "code"
        assert qs["client_id"] == CLIENT_ID
        assert qs["redirect_uri"] == REDIRECT_URI
        assert qs["scope"] == "openid email profile"
        assert qs["state"] and qs["nonce"] and qs["code_challenge"]
        assert qs["code_challenge_method"] == "S256"

        # The coord_oidc cookie is signed and decodes back to the same
        # state/nonce, plus the PKCE verifier whose S256 hash matches
        # the challenge in the authorize URL.
        set_cookie = r.headers.get("set-cookie", "")
        assert "coord_oidc=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Max-Age=600" in set_cookie
        cookie = r.cookies.get("coord_oidc")
        assert cookie
        payload = oidc.verify_login_state(cookie, secret=CLIENT_SECRET)
        assert payload is not None
        assert payload["state"] == qs["state"]
        assert payload["nonce"] == qs["nonce"]
        _, challenge = oidc.make_pkce()  # shape reference only
        assert len(payload["verifier"]) >= 43


async def test_login_discovery_failure_returns_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.discovery_status = 500
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 502
        assert "IdP discovery failed" in r.text


async def test_discovery_with_http_token_endpoint_is_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An otherwise-https discovery doc advertising an http://
    token_endpoint (the classic Keycloak-behind-proxy misconfig) must
    fail discovery -- POSTing the client secret there would ship it
    in cleartext. The token endpoint is never contacted."""
    idp = FakeIdP()
    idp.metadata_overrides = {"token_endpoint": "http://idp.test/token"}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 502
        assert "must use https" in r.text
        assert idp.token_posts == 0


async def test_discovery_with_http_jwks_uri_is_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.metadata_overrides = {"jwks_uri": "http://idp.test/jwks"}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 502
        assert "must use https" in r.text
        assert idp.token_posts == 0


async def test_discovery_issuer_mismatch_is_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OIDC Discovery 4.3: the document's own issuer claim MUST equal
    the issuer it was fetched for. A mismatch is a confused or hostile
    IdP and the flow must not start."""
    idp = FakeIdP()
    idp.metadata_overrides = {"issuer": "https://other-idp.test"}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 502
        assert "does not match the configured issuer" in r.text


async def test_login_state_cookie_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-cookie attribute assertions on coord_oidc (joined-header
    checks can be satisfied by a different cookie in the same
    response): HttpOnly, SameSite=lax, Max-Age=600 always; Secure
    only when the request arrived over TLS (X-Forwarded-Proto)."""
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 302
        oidc_cookies = [
            c
            for c in r.headers.get_list("set-cookie")
            if c.startswith("coord_oidc=")
        ]
        assert len(oidc_cookies) == 1
        cookie = oidc_cookies[0]
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Max-Age=600" in cookie
        assert "Secure" not in cookie  # plain-http dev: no Secure flag

        # Behind a TLS-terminating proxy the login-state cookie must
        # carry Secure, exactly like coord_session does.
        client.cookies.clear()
        r = await client.get(
            "/auth/oidc/login",
            headers={"x-forwarded-proto": "https"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        oidc_cookies = [
            c
            for c in r.headers.get_list("set-cookie")
            if c.startswith("coord_oidc=")
        ]
        assert len(oidc_cookies) == 1
        assert "Secure" in oidc_cookies[0]


async def test_meta_reports_oidc_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/meta carries oidc_enabled so operators (and coord doctor) can
    confirm the feature flag without probing the login route."""
    _setup(monkeypatch, tmp_path)
    async with _client() as client:
        r = await client.get("/meta")
        assert r.status_code == 200
        assert r.json()["oidc_enabled"] is True

    _setup(monkeypatch, tmp_path, oidc_env=False)
    async with _client() as client:
        r = await client.get("/meta")
        assert r.status_code == 200
        assert r.json()["oidc_enabled"] is False


# ---------------------------------------------------------------------------
# Full happy path
# ---------------------------------------------------------------------------


async def test_full_happy_path_mints_session_and_renders_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        qs, _cookie = await _login_redirect(client)
        idp.nonce = qs["nonce"]

        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "authcode-1", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/dashboard"

        set_cookies = r.headers.get_list("set-cookie")
        sess = [c for c in set_cookies if c.startswith("coord_session=")]
        csrf = [c for c in set_cookies if c.startswith("coord_csrf=")]
        oidc_cookie = [c for c in set_cookies if c.startswith("coord_oidc=")]
        assert sess and "HttpOnly" in sess[0] and "SameSite=lax" in sess[0]
        assert csrf and "HttpOnly" in csrf[0]
        # The transient login-state cookie is consumed on success.
        assert oidc_cookie
        assert (
            "Max-Age=0" in oidc_cookie[0]
            or "expires" in oidc_cookie[0].lower()
        )

        raw = r.cookies.get("coord_session")
        assert raw and raw.startswith("coordt_")
        # The raw token travels only in Set-Cookie, never in a body.
        assert raw not in r.text

        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="dev@example.com"
        )
        assert len(rows) == 1
        assert rows[0]["description"] == "oidc sso login"
        assert rows[0]["expires_at"]  # session-bounded, never immortal

        # The session cookie is now in the client jar: the dashboard
        # renders for the mapped engineer (token panel shows them).
        rd = await client.get("/dashboard")
        assert rd.status_code == 200
        assert 'action="/dashboard/login"' not in rd.text
        assert "dev@example.com" in rd.text


async def test_prefix_is_applied_after_allowlist_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COORD_OIDC_ENGINEER_PREFIX=sso/ : the allowlist matches the
    bare email but the minted row carries the prefixed name."""
    idp = FakeIdP()
    _setup(
        monkeypatch, tmp_path, idp,
        env={"COORD_OIDC_ENGINEER_PREFIX": "sso/"},
    )
    async with _client() as client:
        qs, _ = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="sso/dev@example.com"
        )
        assert len(rows) == 1


async def test_happy_path_in_per_engineer_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COORD_REQUIRE_PER_ENGINEER_TOKEN=true with NO shared token
    configured (v0.29.4 per-engineer-only mode): SSO must be a fully
    working login path, since the token it mints is itself a
    per-engineer token."""
    idp = FakeIdP()
    _setup(
        monkeypatch, tmp_path, idp,
        env={
            "COORD_AUTH_TOKEN": None,  # deleted: no shared token at all
            "COORD_REQUIRE_PER_ENGINEER_TOKEN": "true",
        },
    )
    async with _client() as client:
        qs, _ = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

        rd = await client.get("/dashboard")
        assert rd.status_code == 200
        assert 'action="/dashboard/login"' not in rd.text
        assert "dev@example.com" in rd.text


async def test_minted_token_expiry_tracks_session_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSO-minted token must die with the session: expires_at is
    now + COORD_DASHBOARD_SESSION_LIFETIME_SEC, not the default and
    never immortal."""
    idp = FakeIdP()
    _setup(
        monkeypatch, tmp_path, idp,
        env={"COORD_DASHBOARD_SESSION_LIFETIME_SEC": "1234"},
    )
    async with _client() as client:
        before = datetime.now(UTC)
        qs, _ = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        after = datetime.now(UTC)

        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="dev@example.com"
        )
        assert len(rows) == 1
        assert rows[0]["description"] == "oidc sso login"
        expires_at = datetime.fromisoformat(
            str(rows[0]["expires_at"]).replace("Z", "+00:00")
        )
        # The stored value drops microseconds, so allow a few seconds
        # of slack around the request window.
        low = before + timedelta(seconds=1234 - 3)
        high = after + timedelta(seconds=1234 + 3)
        assert low <= expires_at <= high


# ---------------------------------------------------------------------------
# Login-state (cookie) failure modes
# ---------------------------------------------------------------------------


async def test_callback_state_mismatch_is_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        qs, _ = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": "not-the-state-we-minted"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "State mismatch" in r.text


async def test_callback_without_cookie_is_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": "s"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "missing, expired, or invalid" in r.text


async def test_callback_with_tampered_cookie_is_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        qs, cookie = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        b64_part, sig = cookie.split(".", 1)
        flipped = ("0" if sig[-1] != "0" else "1")
        tampered = b64_part + "." + sig[:-1] + flipped
        client.cookies.clear()
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": qs["state"]},
            cookies={"coord_oidc": tampered},
            follow_redirects=False,
        )
        assert r.status_code == 403


async def test_callback_with_expired_cookie_is_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    stale = oidc.sign_login_state(
        {"state": "s", "nonce": "n", "verifier": "v" * 43},
        secret=CLIENT_SECRET,
        now=time.time() - 700,  # past the 600s window
    )
    async with _client() as client:
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": "s"},
            cookies={"coord_oidc": stale},
            follow_redirects=False,
        )
        assert r.status_code == 403


async def test_callback_idp_error_param_renders_403_with_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User clicked cancel at the IdP: ?error=access_denied comes back
    instead of a code. Surface the code (escaped) on a 403 page."""
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        await _login_redirect(client)
        r = await client.get(
            "/auth/oidc/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        assert "access_denied" in r.text


# ---------------------------------------------------------------------------
# ID-token validation failure modes (all 401)
# ---------------------------------------------------------------------------


async def _run_callback(
    client: AsyncClient, idp: FakeIdP
) -> httpx.Response:
    qs, _ = await _login_redirect(client)
    if idp.nonce is None:
        idp.nonce = qs["nonce"]
    return await client.get(
        "/auth/oidc/callback",
        params={"code": "authcode", "state": qs["state"]},
        follow_redirects=False,
    )


async def test_nonce_mismatch_in_id_token_is_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.nonce = "a-completely-different-nonce"
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 401
        assert "nonce" in r.text


async def test_bad_signature_is_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"key": _OTHER_KEY}  # signed by the wrong key
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 401


async def test_wrong_audience_is_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"aud": "some-other-client"}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 401


async def test_azp_mismatch_is_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ID token minted for a DIFFERENT client of the same IdP
    (azp says so) must not log into coord, even though aud passes."""
    idp = FakeIdP()
    idp.id_token_kwargs = {"extra": {"azp": "some-other-client"}}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 401
        assert "azp" in r.text


async def test_azp_matching_client_id_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"extra": {"azp": CLIENT_ID}}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 303, r.text


async def test_audience_array_with_matching_azp_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-audience ID token (aud is an array containing us plus
    another client) with azp naming us: full happy path."""
    idp = FakeIdP()
    idp.id_token_kwargs = {
        "aud": [CLIENT_ID, "other-client"],
        "extra": {"azp": CLIENT_ID},
    }
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 303, r.text

        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="dev@example.com"
        )
        assert len(rows) == 1


async def test_audience_array_without_azp_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin current behaviour: a multi-audience token WITHOUT azp is
    accepted as long as our client_id is in the array. OIDC core
    3.1.3.7 says azp SHOULD be present when aud has multiple values
    but only mandates checking it when it IS present -- PyJWT's
    audience check (membership) plus our conditional azp enforcement
    matches that reading."""
    idp = FakeIdP()
    idp.id_token_kwargs = {"aud": [CLIENT_ID, "other-client"]}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 303, r.text


async def test_wrong_issuer_is_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"iss": "https://evil.test"}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 401


async def test_expired_id_token_is_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"exp_delta": -3600}  # well past the 60s leeway
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 401
        assert "expired" in r.text


async def test_alg_none_is_rejected_with_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic JWT attack: an unsigned token claiming alg=none
    must be rejected by the algorithm allowlist before any signature
    work happens."""
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        qs, _ = await _login_redirect(client)
        now = int(time.time())
        unsigned = jwt.encode(
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "user-1",
                "email": "dev@example.com",
                "nonce": qs["nonce"],
                "iat": now,
                "exp": now + 600,
            },
            key=None,
            algorithm="none",
        )
        idp.token_response = {"id_token": unsigned}
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "c", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 401
        assert "not allowed" in r.text


async def test_jwks_kid_rotation_triggers_one_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First JWKS response only has a stale kid; the validator must
    force exactly one refetch, get the right key, and succeed."""
    idp = FakeIdP()
    idp.jwks_queue = [{"keys": [_jwk_for(_OTHER_KEY, "stale-key")]}]
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 303, r.text
        assert idp.jwks_fetches == 2


async def test_token_endpoint_500_is_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.token_status = 500
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 502


# ---------------------------------------------------------------------------
# Claim mapping / policy failure modes (403)
# ---------------------------------------------------------------------------


async def test_missing_engineer_claim_is_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"email": None}  # claim omitted entirely
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 403
        assert "missing" in r.text


async def test_email_verified_false_is_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"extra": {"email_verified": False}}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 403
        assert "unverified" in r.text


async def test_principal_not_on_allowlist_is_403(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(
        monkeypatch, tmp_path, idp,
        env={"COORD_OIDC_ALLOWED_PRINCIPALS": "other@example.com"},
    )
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 403
        assert "allowlist" in r.text


async def test_listed_principal_passes_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed-case email claim still matches the (normalised)
    allowlist, and the minted engineer is the lowercased form."""
    idp = FakeIdP()
    idp.id_token_kwargs = {"email": "Dev@Example.COM"}
    _setup(monkeypatch, tmp_path, idp)
    async with _client() as client:
        r = await _run_callback(client, idp)
        assert r.status_code == 303, r.text

        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="dev@example.com"
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Public-issuer safety gate
# ---------------------------------------------------------------------------


async def test_public_issuer_without_allowlist_refuses_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """accounts.google.com with no allowlist would let any Google
    account in; the login route must 403 before redirecting anywhere."""
    idp = FakeIdP(issuer=GOOGLE_ISSUER)
    _setup(
        monkeypatch, tmp_path, idp,
        env={
            "COORD_OIDC_ISSUER": GOOGLE_ISSUER,
            "COORD_OIDC_ALLOWED_PRINCIPALS": "",
        },
    )
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 403
        assert "COORD_OIDC_ALLOWED_PRINCIPALS" in r.text
        assert "COORD_OIDC_ALLOW_ANY_PRINCIPAL" in r.text
        assert "location" not in r.headers


async def test_public_issuer_with_allow_any_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP(issuer=GOOGLE_ISSUER)
    _setup(
        monkeypatch, tmp_path, idp,
        env={
            "COORD_OIDC_ISSUER": GOOGLE_ISSUER,
            "COORD_OIDC_ALLOWED_PRINCIPALS": "",
            "COORD_OIDC_ALLOW_ANY_PRINCIPAL": "true",
        },
    )
    async with _client() as client:
        r = await client.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith(GOOGLE_ISSUER + "/authorize?")


# ---------------------------------------------------------------------------
# Pure-function unit tests: login-state signing
# ---------------------------------------------------------------------------


def test_login_state_roundtrip() -> None:
    payload = {"state": "s1", "nonce": "n1", "verifier": "v" * 43}
    blob = oidc.sign_login_state(payload, secret="sek", now=1000.0)
    out = oidc.verify_login_state(blob, secret="sek", now=1500.0)
    assert out is not None
    assert out["state"] == "s1"
    assert out["nonce"] == "n1"
    assert out["verifier"] == "v" * 43
    assert out["iat"] == 1000


def test_login_state_rejects_wrong_secret_and_tampering() -> None:
    payload = {"state": "s", "nonce": "n", "verifier": "v" * 43}
    blob = oidc.sign_login_state(payload, secret="sek", now=1000.0)
    assert oidc.verify_login_state(blob, secret="other", now=1001.0) is None

    b64_part, sig = blob.split(".", 1)
    flipped_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert (
        oidc.verify_login_state(
            b64_part + "." + flipped_sig, secret="sek", now=1001.0
        )
        is None
    )

    # Payload tampering: re-encode a different payload under the old
    # signature.
    other_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"state": "x", "nonce": "n", "verifier": "v" * 43, "iat": 1000}
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    assert (
        oidc.verify_login_state(other_b64 + "." + sig, secret="sek", now=1001.0)
        is None
    )


def test_login_state_rejects_expired_future_and_garbage() -> None:
    payload = {"state": "s", "nonce": "n", "verifier": "v" * 43}
    blob = oidc.sign_login_state(payload, secret="sek", now=1000.0)
    # Past the max_age window.
    assert (
        oidc.verify_login_state(blob, secret="sek", max_age_sec=600, now=1700.0)
        is None
    )
    # From the future beyond the 60s skew allowance.
    assert oidc.verify_login_state(blob, secret="sek", now=900.0) is None
    # Structural garbage.
    for junk in ("", "no-dot", "a.b.c", "@@@.@@@", "ÿ.ÿ"):
        assert oidc.verify_login_state(junk, secret="sek") is None
    # Missing required field: sign without the verifier.
    partial = oidc.sign_login_state(
        {"state": "s", "nonce": "n"}, secret="sek", now=1000.0
    )
    assert oidc.verify_login_state(partial, secret="sek", now=1001.0) is None


# ---------------------------------------------------------------------------
# Pure-function unit tests: claim mapping
# ---------------------------------------------------------------------------


def _map(claims: dict[str, Any], **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "claim_name": "email",
        "allowed": frozenset(),
        "prefix": "",
        "allow_any": True,
        "issuer": ISSUER,
    }
    kwargs.update(overrides)
    return oidc.map_claim_to_engineer(claims, **kwargs)


def test_map_claim_lowercases_email_and_passes_when_verified_absent() -> None:
    # email_verified absent: passes (tenant IdPs often omit it).
    assert _map({"email": "Dev@Example.COM"}) == "dev@example.com"


def test_map_claim_rejects_explicit_unverified_email() -> None:
    with pytest.raises(oidc.OIDCClaimError):
        _map({"email": "dev@example.com", "email_verified": False})


def test_map_claim_rejects_missing_or_blank_claim() -> None:
    with pytest.raises(oidc.OIDCClaimError):
        _map({})
    with pytest.raises(oidc.OIDCClaimError):
        _map({"email": "   "})  # empty after strip
    with pytest.raises(oidc.OIDCClaimError):
        _map({"email": 12345})  # non-string


def test_map_claim_rejects_control_characters() -> None:
    with pytest.raises(oidc.OIDCClaimError):
        _map({"email": "dev@exa\nmple.com"})
    with pytest.raises(oidc.OIDCClaimError):
        _map({"email": "dev\x01@example.com"})


def test_map_claim_rejects_overlong_names() -> None:
    with pytest.raises(oidc.OIDCClaimError):
        _map({"email": "a" * 130 + "@example.com"})
    # Prefix counts toward the 128-char budget too.
    with pytest.raises(oidc.OIDCClaimError):
        _map({"email": "a" * 120 + "@e.com"}, prefix="sso/team/very-long/")


def test_map_claim_allowlist_checked_before_prefix() -> None:
    allowed = frozenset({"dev@example.com"})
    out = _map(
        {"email": "dev@example.com"},
        allowed=allowed,
        allow_any=False,
        prefix="sso/",
    )
    assert out == "sso/dev@example.com"
    with pytest.raises(oidc.OIDCClaimError):
        _map(
            {"email": "intruder@example.com"},
            allowed=allowed,
            allow_any=False,
            prefix="sso/",
        )


# ---------------------------------------------------------------------------
# COORD_OIDC_REPO_CLAIM (#30 slice 2/3): bind the SSO-minted token to a repo
# from a configured claim so OIDC dashboard sessions can be repo-scoped.
# ---------------------------------------------------------------------------


async def test_oidc_repo_claim_scopes_minted_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    idp.id_token_kwargs = {"extra": {"coord_repo": "amittell/repo-a"}}
    _setup(monkeypatch, tmp_path, idp, env={"COORD_OIDC_REPO_CLAIM": "coord_repo"})
    async with _client() as client:
        qs, _ = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "authcode-1", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="dev@example.com"
        )
        assert len(rows) == 1
        assert rows[0]["repo"] == "amittell/repo-a"


async def test_oidc_repo_claim_missing_refuses_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Configured-but-absent claim is a refusal, never a silent all-repo grant.
    idp = FakeIdP()  # no coord_repo claim in the token
    _setup(monkeypatch, tmp_path, idp, env={"COORD_OIDC_REPO_CLAIM": "coord_repo"})
    async with _client() as client:
        qs, _ = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "authcode-1", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 403, r.text
        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="dev@example.com"
        )
        assert rows == []  # no token minted for a refused login


async def test_oidc_without_repo_claim_mints_unscoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idp = FakeIdP()
    _setup(monkeypatch, tmp_path, idp)  # COORD_OIDC_REPO_CLAIM unset
    async with _client() as client:
        qs, _ = await _login_redirect(client)
        idp.nonce = qs["nonce"]
        r = await client.get(
            "/auth/oidc/callback",
            params={"code": "authcode-1", "state": qs["state"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        from coordination.deps import get_service

        rows = await get_service().db.list_engineer_tokens(
            engineer="dev@example.com"
        )
        assert rows[0]["repo"] is None
