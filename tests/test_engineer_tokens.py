"""Tests for the v0.29 per-engineer bearer token model.

The implementation lives in coordination/db.py: the v14 migration
creates the ``engineer_tokens`` table, and five Database methods
(create / lookup / touch / list / revoke) provide the lifecycle.

These tests pin three contracts that the higher-level auth path
in coordination/main.py depends on:

1. The raw token is never stored; only its sha256 hex digest is.
   ``lookup_engineer_token`` matches by hash; the row never carries
   the raw token.
2. ``revoked_at`` short-circuits ``lookup_engineer_token`` even if
   the token_sha256 still matches verbatim. Revocation is the only
   way to stop a leaked token from authenticating.
3. ``touch_engineer_token`` is best-effort: revoked tokens are
   silently skipped, missing tokens are silently skipped, and the
   call never raises into the request path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coordination.db import Database


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def test_create_lookup_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "a" * 64
    token_id = await db.create_engineer_token(
        "alex/claude/main",
        _sha256(raw),
        description="laptop",
    )
    assert token_id  # uuid-shaped string

    hit = await db.lookup_engineer_token(_sha256(raw))
    assert hit is not None
    assert hit["engineer"] == "alex/claude/main"
    assert hit["description"] == "laptop"
    # The raw token must never come back out of the DB.
    assert "token_sha256" not in hit
    assert raw not in str(hit)


async def test_lookup_miss_returns_none(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    hit = await db.lookup_engineer_token(_sha256("never-issued"))
    assert hit is None


async def test_revoked_token_does_not_authenticate(tmp_path: Path) -> None:
    """Revocation is the only kill switch -- the row stays for audit
    but lookup must return None so the auth middleware 401s the
    bearer."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "b" * 64
    token_id = await db.create_engineer_token(
        "alex/claude/main", _sha256(raw)
    )

    assert await db.lookup_engineer_token(_sha256(raw)) is not None
    revoked = await db.revoke_engineer_token(token_id)
    assert revoked is True
    assert await db.lookup_engineer_token(_sha256(raw)) is None

    # Re-revoking is a no-op (returns False) so the caller can treat
    # revoke as idempotent without first checking state.
    assert await db.revoke_engineer_token(token_id) is False


async def test_list_excludes_revoked_by_default(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    raw_live = "coordt_" + "c" * 64
    raw_dead = "coordt_" + "d" * 64

    live_id = await db.create_engineer_token(
        "alex/claude/main", _sha256(raw_live), description="live"
    )
    dead_id = await db.create_engineer_token(
        "alex/claude/main", _sha256(raw_dead), description="dead"
    )
    await db.revoke_engineer_token(dead_id)

    default = await db.list_engineer_tokens()
    assert [r["id"] for r in default] == [live_id]

    with_revoked = await db.list_engineer_tokens(include_revoked=True)
    ids = {r["id"] for r in with_revoked}
    assert ids == {live_id, dead_id}


async def test_list_filters_by_engineer(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    await db.create_engineer_token("alex/claude/main", _sha256("a"))
    await db.create_engineer_token("alex/codex/main", _sha256("b"))
    await db.create_engineer_token("dana/claude/main", _sha256("c"))

    alex_only = await db.list_engineer_tokens(engineer="alex/claude/main")
    assert len(alex_only) == 1
    assert alex_only[0]["engineer"] == "alex/claude/main"


async def test_touch_bumps_last_used_at(tmp_path: Path) -> None:
    """``last_used_at`` lets the operator spot stale tokens in
    ``coord tokens list``. It must be NULL at creation and get a
    timestamp after a successful auth touch."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "e" * 64
    await db.create_engineer_token("alex/claude/main", _sha256(raw))

    fresh = await db.lookup_engineer_token(_sha256(raw))
    assert fresh is not None
    assert fresh["last_used_at"] is None

    pinned = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)
    await db.touch_engineer_token(_sha256(raw), now=pinned)

    touched = await db.lookup_engineer_token(_sha256(raw))
    assert touched is not None
    assert touched["last_used_at"] is not None
    assert "2026-06-06T12:00:00Z" == touched["last_used_at"]


async def test_touch_revoked_token_is_silent_noop(tmp_path: Path) -> None:
    """``touch`` must never raise into the request path: a revoked or
    missing token should just be skipped silently. This pins the
    contract so the auth middleware can call touch without try/except
    around it."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "f" * 64
    tok = await db.create_engineer_token("alex/claude/main", _sha256(raw))
    await db.revoke_engineer_token(tok)

    # Both calls must not raise.
    await db.touch_engineer_token(_sha256(raw))
    await db.touch_engineer_token(_sha256("nonexistent"))


async def test_unique_token_hash_constraint(tmp_path: Path) -> None:
    """The unique index on ``token_sha256`` is what makes the lookup
    O(1) and stops the same token being credited to two engineers.
    Inserting a duplicate hash must error."""
    import aiosqlite

    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "g" * 64
    await db.create_engineer_token("alex/claude/main", _sha256(raw))

    try:
        await db.create_engineer_token("dana/claude/main", _sha256(raw))
    except aiosqlite.IntegrityError:
        return
    raise AssertionError("duplicate token_sha256 should have raised IntegrityError")


async def test_audit_row_survives_revocation(tmp_path: Path) -> None:
    """A revoked token's row stays in the table so operators can
    answer ``which tokens did this engineer ever hold, when were they
    issued, when were they revoked?`` via ``list_engineer_tokens``."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "h" * 64
    tok = await db.create_engineer_token(
        "alex/claude/main", _sha256(raw), description="old laptop"
    )
    issued = datetime.now(UTC)
    await db.revoke_engineer_token(tok)

    rows = await db.list_engineer_tokens(include_revoked=True)
    assert len(rows) == 1
    audit = rows[0]
    assert audit["description"] == "old laptop"
    assert audit["created_at"] is not None
    assert audit["revoked_at"] is not None
    # Both timestamps land in the same ISO-8601 'Z' UTC shape used
    # elsewhere in the schema.
    assert audit["revoked_at"].endswith("Z")
    assert (
        datetime.fromisoformat(audit["revoked_at"].replace("Z", "+00:00"))
        - issued
    ) < timedelta(seconds=2)
