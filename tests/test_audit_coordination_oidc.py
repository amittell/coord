"""Audit-fix coverage for the OIDC protocol module (v0.45 audit).

Pins two findings against ``coordination/oidc.py``:

1. Allowlist case-folding: ``Settings.oidc_allowed_principal_set``
   lowercases every entry, but non-email claim values used to be
   compared raw -- an IdP emitting a mixed-case principal (e.g.
   ``preferred_username: "AMittell"``) could never pass the allowlist.
   The membership check now folds the claim value; the engineer name
   keeps the IdP's original casing for non-email claims, and email
   handling (already lowercased) is unchanged.
2. Trailing-slash issuer tolerance: discovery compares issuers as
   locations (``rstrip("/")``), but the ID-token ``iss`` check used
   PyJWT's exact string compare -- a configured issuer with a trailing
   slash passed discovery and then failed every callback. The iss
   check now uses the same trailing-slash tolerance, and everything
   else (wrong issuer, missing iss) still rejects.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import MockTransport

from coordination import oidc

ISSUER = "https://idp.test"
CLIENT_ID = "coord-dashboard"
JWKS_URI = "https://idp.test/jwks"
KID = "audit-key"
NONCE = "audit-nonce"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk() -> dict[str, Any]:
    jwk = json.loads(
        jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key())
    )
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return jwk


def _jwks_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == JWKS_URI
        return httpx.Response(200, json={"keys": [_jwk()]})

    return httpx.AsyncClient(transport=MockTransport(handler))


def _id_token(*, iss: str | None = ISSUER) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "aud": CLIENT_ID,
        "sub": "user-1",
        "nonce": NONCE,
        "iat": now,
        "exp": now + 600,
    }
    if iss is not None:
        claims["iss"] = iss
    return jwt.encode(
        claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": KID}
    )


async def _validate(token: str, *, configured_issuer: str) -> dict[str, Any]:
    oidc._clear_caches()
    async with _jwks_client() as client:
        return await oidc.validate_id_token(
            client,
            id_token=token,
            issuer=configured_issuer,
            client_id=CLIENT_ID,
            nonce=NONCE,
            jwks_uri=JWKS_URI,
        )


# --- trailing-slash issuer tolerance ----------------------------------------


async def test_configured_trailing_slash_issuer_validates() -> None:
    """COORD_OIDC_ISSUER with a trailing slash the IdP does not emit:
    discovery tolerates it, so the callback must too."""
    claims = await _validate(
        _id_token(iss=ISSUER), configured_issuer=ISSUER + "/"
    )
    assert claims["sub"] == "user-1"


async def test_token_trailing_slash_iss_validates() -> None:
    claims = await _validate(
        _id_token(iss=ISSUER + "/"), configured_issuer=ISSUER
    )
    assert claims["sub"] == "user-1"


async def test_exact_issuer_match_still_validates() -> None:
    claims = await _validate(_id_token(iss=ISSUER), configured_issuer=ISSUER)
    assert claims["sub"] == "user-1"


async def test_wrong_issuer_still_rejected() -> None:
    with pytest.raises(oidc.OIDCValidationError, match="does not match"):
        await _validate(
            _id_token(iss="https://evil.test"), configured_issuer=ISSUER
        )


async def test_missing_iss_claim_rejected() -> None:
    with pytest.raises(oidc.OIDCValidationError):
        await _validate(_id_token(iss=None), configured_issuer=ISSUER)


# --- allowlist case-folding for non-email principals -------------------------


def _map(
    claims: dict[str, Any],
    *,
    claim_name: str = "preferred_username",
    allowed: frozenset[str],
) -> str:
    return oidc.map_claim_to_engineer(
        claims,
        claim_name=claim_name,
        allowed=allowed,
        prefix="sso/",
        allow_any=False,
        issuer=ISSUER,
    )


def test_mixed_case_principal_passes_lowercased_allowlist() -> None:
    """An IdP emitting "AMittell" against the parse-time-lowercased
    allowlist entry "amittell": previously permanently 403'd, now
    matched case-insensitively. The engineer name keeps the IdP's
    casing for non-email claims."""
    engineer = _map(
        {"preferred_username": "AMittell"},
        allowed=frozenset({"amittell"}),
    )
    assert engineer == "sso/AMittell"


def test_exact_lowercase_principal_still_matches() -> None:
    engineer = _map(
        {"preferred_username": "amittell"},
        allowed=frozenset({"amittell"}),
    )
    assert engineer == "sso/amittell"


def test_unlisted_principal_still_rejected() -> None:
    with pytest.raises(oidc.OIDCClaimError, match="not on the"):
        _map(
            {"preferred_username": "Intruder"},
            allowed=frozenset({"amittell"}),
        )


def test_email_claim_lowercases_value_and_matches() -> None:
    """Email handling is unchanged: the value itself is lowercased, so
    both the allowlist check and the engineer name use lower case."""
    engineer = oidc.map_claim_to_engineer(
        {"email": "Dev@Example.COM"},
        claim_name="email",
        allowed=frozenset({"dev@example.com"}),
        prefix="sso/",
        allow_any=False,
        issuer=ISSUER,
    )
    assert engineer == "sso/dev@example.com"
