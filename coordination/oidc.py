"""Generic OIDC authorization-code + PKCE protocol helpers (v0.29.6).

Pure protocol module: every network call goes through an injected
``httpx.AsyncClient`` so tests can drive the whole flow against an
in-process ``MockTransport`` fake IdP, and so the routes in
``coordination.main`` own the client lifecycle (one short-lived client
per request, no shared connection pool to leak).

The module deliberately knows nothing about FastAPI, cookies, or the
database. The split is:

* here: discovery, JWKS, PKCE, the authorize URL, the code exchange,
  ID-token validation, claim->engineer mapping, and the signed
  transient login-state blob that survives the redirect round-trip;
* ``coordination.main``: the two routes, cookie handling, and the
  HTML error pages.

Error taxonomy (callers map these onto HTTP statuses):

* :class:`OIDCProtocolError` -- the IdP or the network misbehaved
  (discovery failed, token endpoint 500, malformed JSON). 502.
* :class:`OIDCValidationError` -- the ID token failed validation
  (bad signature, wrong aud/iss, expired, nonce mismatch). 401.
* :class:`OIDCClaimError` -- the token validated but its claims do
  not map to a permitted engineer (missing claim, unverified email,
  not on the allowlist, malformed value). 403.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from coordination.config import Settings


class OIDCError(Exception):
    """Base class for everything this module raises on purpose."""


class OIDCProtocolError(OIDCError):
    """The IdP, the network, or discovery misbehaved (-> HTTP 502)."""


class OIDCValidationError(OIDCError):
    """The ID token failed cryptographic/claims validation (-> 401)."""


class OIDCClaimError(OIDCError):
    """Token claims do not map to a permitted engineer (-> 403)."""


# Issuers where literally anyone on the internet can hold a valid
# account. An empty allowlist against one of these means "every Google
# user may log into my coord dashboard", which is never what an
# operator intends -- the login route refuses to start the flow until
# COORD_OIDC_ALLOWED_PRINCIPALS is set or the operator explicitly
# opts in with COORD_OIDC_ALLOW_ANY_PRINCIPAL=true. Tenant-scoped
# issuers (Okta, Entra, Keycloak) are not listed: their user
# population IS the allowlist.
KNOWN_PUBLIC_ISSUER_HOSTS = frozenset({"accounts.google.com"})

# Algorithms we will verify ID tokens with. RS256 is what every major
# IdP signs with by default; ES256 covers the ECDSA holdouts. Anything
# else -- most importantly ``none`` -- is rejected before any
# signature work happens, so an attacker-crafted header can never
# steer us into skipping verification.
ALLOWED_ID_TOKEN_ALGS = ("RS256", "ES256")

# Metadata and JWKS change rarely; a short TTL cache spares the IdP a
# discovery round-trip per login without holding stale keys for long.
# Keyed on issuer / jwks_uri, valued (monotonic_deadline, payload).
_CACHE_TTL_SEC = 300.0
_METADATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _clear_caches() -> None:
    """Test hook: drop both TTL caches so each test sees a cold IdP."""
    _METADATA_CACHE.clear()
    _JWKS_CACHE.clear()


def _require_https_url(url: str, what: str) -> None:
    """Refuse any OIDC URL that is not https; the one exception is
    localhost / 127.0.0.1 so a dev stack (or this test suite's fake
    IdP) can run without certificates. Applies to the configured
    issuer AND to every endpoint a discovery document hands back --
    a Keycloak misconfigured behind its proxy will happily advertise
    http:// endpoints, and following them would ship the client
    secret and authorization codes in cleartext."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1"):
        return
    raise OIDCProtocolError(
        f"{what} must use https (got {url!r}); plain http is "
        "only allowed for localhost/127.0.0.1 development setups."
    )


def _require_acceptable_issuer(issuer: str) -> None:
    _require_https_url(issuer, "OIDC issuer")


def principal_policy_error(settings: Settings) -> str | None:
    """The public-issuer safety gate, evaluated at /auth/oidc/login.

    Returns a human-readable refusal when the configuration would let
    any account on a known public issuer log in: oidc enabled, empty
    allowlist, no explicit any-principal opt-in, and the issuer host
    is on :data:`KNOWN_PUBLIC_ISSUER_HOSTS`. Returns None when the
    configuration is acceptable (including the empty-allowlist case
    against a tenant-scoped IdP, where the tenant boundary is the
    access control)."""
    if not settings.oidc_enabled:
        return None
    if settings.oidc_allow_any_principal:
        return None
    if settings.oidc_allowed_principal_set:
        return None
    host = (urlsplit(settings.oidc_issuer).hostname or "").lower()
    if host in KNOWN_PUBLIC_ISSUER_HOSTS:
        return (
            f"The configured OIDC issuer ({host}) is a public identity "
            "provider: without an allowlist, any account there could "
            "log in. Set COORD_OIDC_ALLOWED_PRINCIPALS to the permitted "
            "principals, or set COORD_OIDC_ALLOW_ANY_PRINCIPAL=true to "
            "explicitly accept any principal."
        )
    return None


async def fetch_metadata(
    client: httpx.AsyncClient, issuer: str
) -> dict[str, Any]:
    """Fetch (and TTL-cache) the issuer's discovery document.

    Validates that the three endpoints the flow actually uses are
    present, so a misconfigured issuer fails here with a clear message
    instead of a KeyError three calls later."""
    _require_acceptable_issuer(issuer)
    cached = _METADATA_CACHE.get(issuer)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise OIDCProtocolError(
            f"Could not reach the IdP discovery endpoint: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise OIDCProtocolError(
            f"IdP discovery endpoint answered HTTP {resp.status_code}."
        )
    try:
        metadata = resp.json()
    except ValueError as exc:
        raise OIDCProtocolError(
            "IdP discovery endpoint returned malformed JSON."
        ) from exc
    if not isinstance(metadata, dict):
        raise OIDCProtocolError(
            "IdP discovery document is not a JSON object."
        )
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = metadata.get(field)
        if not value:
            raise OIDCProtocolError(
                f"IdP discovery document is missing {field!r}."
            )
        # The discovery doc is attacker-shaped input as far as this
        # client is concerned: an http:// token_endpoint (the classic
        # Keycloak-behind-proxy misconfig) would have us POST the
        # client secret in cleartext, so every endpoint gets the same
        # https check the issuer itself does -- before the cache
        # write, so a bad doc never gets served from cache either.
        _require_https_url(str(value), f"IdP {field}")
    # OIDC Discovery 4.3: the document's own issuer claim MUST match
    # the issuer it was fetched for; a mismatch means a confused (or
    # hostile) IdP and every downstream iss check would be validating
    # against the wrong value. Trailing slashes are the one tolerated
    # difference -- issuer URLs are compared as locations, not bytes.
    doc_issuer = metadata.get("issuer")
    if not isinstance(doc_issuer, str) or (
        doc_issuer.rstrip("/") != issuer.rstrip("/")
    ):
        raise OIDCProtocolError(
            f"IdP discovery document reports issuer {doc_issuer!r}, "
            f"which does not match the configured issuer {issuer!r}."
        )
    _METADATA_CACHE[issuer] = (time.monotonic() + _CACHE_TTL_SEC, metadata)
    return metadata


async def fetch_jwks(
    client: httpx.AsyncClient, jwks_uri: str, *, force: bool = False
) -> dict[str, Any]:
    """Fetch (and TTL-cache) the IdP's JWKS. ``force=True`` bypasses
    the cache read (but still refreshes it) -- used exactly once per
    validation when a kid is not found, to pick up key rotation."""
    if not force:
        cached = _JWKS_CACHE.get(jwks_uri)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    try:
        resp = await client.get(jwks_uri)
    except httpx.HTTPError as exc:
        raise OIDCProtocolError(
            f"Could not reach the IdP JWKS endpoint: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise OIDCProtocolError(
            f"IdP JWKS endpoint answered HTTP {resp.status_code}."
        )
    try:
        jwks = resp.json()
    except ValueError as exc:
        raise OIDCProtocolError(
            "IdP JWKS endpoint returned malformed JSON."
        ) from exc
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise OIDCProtocolError(
            "IdP JWKS document has no 'keys' array."
        )
    _JWKS_CACHE[jwks_uri] = (time.monotonic() + _CACHE_TTL_SEC, jwks)
    return jwks


async def _find_jwk(
    client: httpx.AsyncClient, jwks_uri: str, kid: str
) -> dict[str, Any]:
    """Look up a signing key by kid, with exactly one forced refetch
    on miss. The miss-then-refetch handles the standard key-rotation
    race: the IdP starts signing with a fresh key while our cached
    JWKS still holds only the old one."""
    jwks = await fetch_jwks(client, jwks_uri)
    for key in jwks["keys"]:
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    jwks = await fetch_jwks(client, jwks_uri, force=True)
    for key in jwks["keys"]:
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    raise OIDCValidationError(
        "ID token is signed with a key the IdP's JWKS does not publish."
    )


def make_pkce() -> tuple[str, str]:
    """Generate a PKCE (verifier, S256 challenge) pair.

    ``token_urlsafe(64)`` yields ~86 chars of the RFC 7636 unreserved
    alphabet; the slice keeps us inside the 43..128 length bounds even
    if the encoding ever pads longer."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    metadata: dict[str, Any],
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    """Assemble the IdP authorize redirect target. Plain urlencode of
    the standard code-flow parameters; the endpoint may already carry
    a query string (some IdPs version their endpoints that way), so
    join with '&' in that case."""
    endpoint = str(metadata["authorization_endpoint"])
    params = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    joiner = "&" if "?" in endpoint else "?"
    return f"{endpoint}{joiner}{params}"


async def exchange_code(
    client: httpx.AsyncClient,
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Redeem the authorization code at the token endpoint.

    Uses client_secret_post (the most widely supported client auth
    method) plus the PKCE code_verifier. Anything other than a 200
    with an id_token is a protocol error -- the IdP-side failure modes
    (expired code, verifier mismatch, bad client secret) all surface
    here and none of them are recoverable by the user retrying the
    same callback URL."""
    try:
        resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
        )
    except httpx.HTTPError as exc:
        raise OIDCProtocolError(
            f"Could not reach the IdP token endpoint: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise OIDCProtocolError(
            f"IdP token endpoint answered HTTP {resp.status_code}."
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OIDCProtocolError(
            "IdP token endpoint returned malformed JSON."
        ) from exc
    if not isinstance(payload, dict) or not payload.get("id_token"):
        raise OIDCProtocolError(
            "IdP token response did not include an id_token."
        )
    return payload


def _key_from_jwk(jwk: dict[str, Any], alg: str) -> Any:
    """Materialise a verification key object from a JWK dict for the
    already-allowlisted algorithm."""
    try:
        if alg == "RS256":
            return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    except (jwt.PyJWTError, ValueError) as exc:
        raise OIDCValidationError(
            "IdP signing key could not be parsed."
        ) from exc


async def validate_id_token(
    client: httpx.AsyncClient,
    *,
    id_token: str,
    issuer: str,
    client_id: str,
    nonce: str,
    jwks_uri: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Fully validate an ID token and return its claims.

    Order matters here:

    1. Read the unverified header for alg + kid and reject any alg
       outside the allowlist BEFORE doing signature work -- this is
       what kills ``alg: none`` and algorithm-confusion attacks.
    2. Resolve the signing key from the JWKS (one forced refetch on a
       kid miss, see :func:`_find_jwk`).
    3. ``jwt.decode`` verifies signature, audience and issuer.
       exp/iat presence is required but their time check is done by
       hand against ``now`` so tests can inject a clock (PyJWT has no
       clock parameter); leeway is 60s either way.
    4. Multi-audience hardening: when ``azp`` is present it must be
       our client_id (an ID token minted for a different client of
       the same IdP must not log into coord).
    5. The nonce claim must match the one we bound into the login
       state (constant-time compare).
    """
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise OIDCValidationError(
            "ID token header could not be parsed."
        ) from exc
    alg = header.get("alg")
    if alg not in ALLOWED_ID_TOKEN_ALGS:
        raise OIDCValidationError(
            f"ID token algorithm {alg!r} is not allowed (expected one "
            f"of {', '.join(ALLOWED_ID_TOKEN_ALGS)})."
        )
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise OIDCValidationError("ID token header is missing 'kid'.")

    key = _key_from_jwk(await _find_jwk(client, jwks_uri, kid), alg)

    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=[alg],
            audience=client_id,
            issuer=issuer,
            leeway=60,
            options={
                "require": ["exp", "iat"],
                # Time checks are done manually below so the test
                # suite can inject ``now``; everything else (signature,
                # aud, iss, required-claims presence) stays in PyJWT.
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise OIDCValidationError(
            f"ID token failed validation: {exc}"
        ) from exc

    ref = time.time() if now is None else now
    leeway = 60
    try:
        exp = float(claims["exp"])
        iat = float(claims["iat"])
    except (TypeError, ValueError) as exc:
        raise OIDCValidationError(
            "ID token exp/iat claims are not numeric."
        ) from exc
    if exp < ref - leeway:
        raise OIDCValidationError("ID token has expired.")
    if iat > ref + leeway:
        raise OIDCValidationError("ID token was issued in the future.")

    azp = claims.get("azp")
    if azp is not None and azp != client_id:
        raise OIDCValidationError(
            "ID token 'azp' does not match this client."
        )

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(
        token_nonce, nonce
    ):
        raise OIDCValidationError(
            "ID token nonce does not match this login attempt."
        )
    return claims


def map_claim_to_engineer(
    claims: dict[str, Any],
    *,
    claim_name: str,
    allowed: frozenset[str],
    prefix: str,
    allow_any: bool,
    issuer: str,
) -> str:
    """Map validated ID-token claims onto a coord engineer name, or
    raise :class:`OIDCClaimError` with a human-readable reason.

    The allowlist check runs against the bare claim value BEFORE the
    prefix is applied, so operators list real principals
    (``dev@example.com``) rather than coord-internal names
    (``sso/dev@example.com``)."""
    value = claims.get(claim_name)
    if not isinstance(value, str) or not value.strip():
        raise OIDCClaimError(
            f"ID token from {issuer} is missing the {claim_name!r} "
            "claim coord maps to an engineer name. Check "
            "COORD_OIDC_ENGINEER_CLAIM and the requested scopes."
        )
    value = value.strip()
    if claim_name == "email":
        value = value.lower()
        # Only an EXPLICIT ``email_verified: false`` rejects. Many
        # tenant-scoped IdPs (Entra, some Keycloak realms) simply do
        # not emit the claim even though every account's email is
        # provisioned by the org -- treating absence as failure would
        # lock those deployments out for no security gain.
        if claims.get("email_verified") is False:
            raise OIDCClaimError(
                "The identity provider reports this email address as "
                "unverified; coord will not map it to an engineer."
            )
    if not allow_any and allowed:
        # Email values are already lowercased above; other claim types
        # compare exactly against the (lowercase-normalised) allowlist.
        if value not in allowed:
            raise OIDCClaimError(
                f"Principal {value!r} is not on the "
                "COORD_OIDC_ALLOWED_PRINCIPALS allowlist."
            )
    engineer = (prefix + value).strip()
    if not engineer or len(engineer) > 128:
        raise OIDCClaimError(
            "Mapped engineer name must be 1-128 characters."
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in engineer):
        raise OIDCClaimError(
            "Mapped engineer name contains control characters."
        )
    return engineer


def _state_hmac_key(secret: str) -> bytes:
    """HMAC key for the login-state blob: sha256 of the OIDC client
    secret. Every replica of a deployment shares the client secret by
    definition, so the login-state cookie verifies on whichever
    replica receives the callback -- no server-side session store
    needed. Hashing (rather than using the secret raw) gives a
    fixed-length key and avoids reusing the literal secret bytes in a
    second cryptographic role."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def sign_login_state(
    payload: dict[str, Any], *, secret: str, now: float | None = None
) -> str:
    """Serialise + sign the transient login state (state, nonce, PKCE
    verifier) that must survive the redirect to the IdP and back.

    Format: ``base64url(json) + "." + hex(hmac_sha256(b64_segment))``.
    The payload is readable by the browser (it is the user's own
    state) -- the HMAC only guarantees the callback handler that the
    values are the ones THIS server minted, untampered."""
    data = dict(payload)
    data["iat"] = int(time.time() if now is None else now)
    b64 = base64.urlsafe_b64encode(
        json.dumps(data, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    sig = hmac.new(_state_hmac_key(secret), b64, hashlib.sha256).hexdigest()
    return b64.decode("ascii") + "." + sig


def verify_login_state(
    value: str,
    *,
    secret: str,
    max_age_sec: int = 600,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Verify and decode a :func:`sign_login_state` blob.

    Returns the payload dict, or None on ANY problem -- malformed
    structure, bad signature, missing/garbled iat, expired window, or
    a payload that lacks the three string fields the callback needs.
    Signature first (constant-time), then freshness: a forged blob
    never gets its contents inspected."""
    if not value or value.count(".") != 1:
        return None
    b64_part, sig_part = value.split(".", 1)
    try:
        expected = hmac.new(
            _state_hmac_key(secret), b64_part.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_part):
            return None
    except (TypeError, UnicodeEncodeError):
        # Non-ASCII bytes smuggled into the cookie: not something this
        # server ever minted, so it is malformed by definition.
        return None
    try:
        padded = b64_part + "=" * (-len(b64_part) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    iat = data.get("iat")
    if not isinstance(iat, (int, float)):
        return None
    ref = time.time() if now is None else now
    # 60s of forward skew tolerance mirrors the ID-token leeway; a
    # blob "from the future" beyond that is as suspicious as an
    # expired one.
    if iat > ref + 60 or ref - iat > max_age_sec:
        return None
    for field in ("state", "nonce", "verifier"):
        if not isinstance(data.get(field), str) or not data[field]:
            return None
    return data
