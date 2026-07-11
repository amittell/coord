"""Pure helpers for v0.29 per-engineer bearer tokens.

Shared by the operator CLI (``coordination.cli_tokens``) and the
dashboard token-management surface (v0.29.5). Deliberately free of
FastAPI, argparse, and database imports so either caller can use the
helpers without dragging in the other's dependency graph.

Three concerns live here:

* raw-token generation (``generate_raw_token``) -- the only place the
  ``coordt_`` wire format is defined;
* hashing (``sha256_token``) -- tokens are stored as sha256 hex
  digests, never raw;
* lifecycle-status derivation (``derive_token_status``) -- the
  one-word status the CLI table and the dashboard panel both render,
  computed from a token row's raw timestamps.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any

# Tokens are sha256-hashed before storage; the raw form below is
# what the operator copies into local.env. Prefix mimics the GitHub
# PAT convention (``ghp_...``) so a leaked token in CI logs or
# clipboards is immediately recognisable as a coord credential and
# can be grepped for during incident response.
TOKEN_PREFIX = "coordt_"


def generate_raw_token() -> str:
    """A new token is ``coordt_`` + 64 hex chars (~256 bits of
    entropy). ``secrets.token_hex(32)`` is the standard library's
    safe random source; we never use ``random`` for credentials.
    """
    return TOKEN_PREFIX + secrets.token_hex(32)


def sha256_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _elapsed(ts: Any, now: datetime) -> bool:
    """True when a stored ``...Z`` timestamp is at or before ``now``.
    Empty / NULL means "no deadline", which never elapses. A naive
    timestamp (no timezone -- a hand-edited or legacy row) is read as
    UTC instead of raising the naive-vs-aware TypeError, which would
    crash status derivation rather than failing closed. An
    unparseable value counts as elapsed -- fail closed, because a
    corrupt expiry must not turn into an immortal token."""
    if not ts:
        return False
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= now


def derive_token_status(
    row: dict[str, Any], *, now: datetime | None = None
) -> str:
    """Derive the one-word lifecycle status for a token row. Revocation
    dominates (a revoked token is dead no matter what else says), then
    expiry, then the rotation grace window -- ``rotating`` while the
    old token still authenticates, ``grace-elapsed`` once the window
    has closed."""
    ref = now or datetime.now(UTC)
    if row.get("revoked_at"):
        return "revoked"
    if _elapsed(row.get("expires_at"), ref):
        return "expired"
    if row.get("rotation_grace_until"):
        if _elapsed(row.get("rotation_grace_until"), ref):
            return "grace-elapsed"
        return "rotating"
    return "active"
