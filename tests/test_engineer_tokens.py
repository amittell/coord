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


# --- v0.29.4: expiry ---


async def test_expired_token_does_not_authenticate(tmp_path: Path) -> None:
    """``expires_at`` in the past makes ``lookup_engineer_token``
    return None, exactly like a revoked token. The valid-only lookup
    contract is what the auth path leans on."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "i" * 64
    past = datetime.now(UTC) - timedelta(hours=1)
    await db.create_engineer_token(
        "alex/claude/main", _sha256(raw), expires_at=past
    )

    assert await db.lookup_engineer_token(_sha256(raw)) is None

    resolved = await db.resolve_engineer_token(_sha256(raw))
    assert resolved is not None
    assert resolved["status"] == "expired"


async def test_future_expiry_still_authenticates(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "j" * 64
    future = datetime.now(UTC) + timedelta(days=30)
    await db.create_engineer_token(
        "alex/claude/main", _sha256(raw), expires_at=future
    )

    hit = await db.lookup_engineer_token(_sha256(raw))
    assert hit is not None
    assert hit["status"] == "ok"
    assert hit["expires_at"].endswith("Z")


async def test_no_expiry_means_immortal(tmp_path: Path) -> None:
    """Legacy rows (and tokens created without --expires-in) have
    expires_at NULL and never expire."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "k" * 64
    await db.create_engineer_token("alex/claude/main", _sha256(raw))

    resolved = await db.resolve_engineer_token(_sha256(raw))
    assert resolved is not None
    assert resolved["status"] == "ok"
    assert resolved["expires_at"] is None


async def test_resolve_returns_none_for_revoked_and_missing(tmp_path: Path) -> None:
    """Revoked and missing are indistinguishable through resolve: both
    mean "not a credential" and must not leak metadata to whoever is
    holding the dead token."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "l" * 64
    tok = await db.create_engineer_token("alex/claude/main", _sha256(raw))
    await db.revoke_engineer_token(tok)

    assert await db.resolve_engineer_token(_sha256(raw)) is None
    assert await db.resolve_engineer_token(_sha256("never-issued")) is None


# --- v0.29.4: activity tracking ---


async def test_touch_records_activity_metadata(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "m" * 64
    await db.create_engineer_token("alex/claude/main", _sha256(raw))

    await db.touch_engineer_token(
        _sha256(raw), source_ip="203.0.113.7", user_agent="coord-mcp/0.29"
    )
    await db.touch_engineer_token(
        _sha256(raw), source_ip="203.0.113.8", user_agent="coord-mcp/0.29"
    )

    row = await db.lookup_engineer_token(_sha256(raw))
    assert row is not None
    assert row["request_count"] == 2
    assert row["last_source_ip"] == "203.0.113.8"

    listed = await db.list_engineer_tokens()
    assert listed[0]["last_user_agent"] == "coord-mcp/0.29"


async def test_touch_without_metadata_keeps_last_seen(tmp_path: Path) -> None:
    """A request that arrives without IP/UA (e.g. direct connection
    with no proxy headers) must not blank out the last-seen values."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "n" * 64
    await db.create_engineer_token("alex/claude/main", _sha256(raw))

    await db.touch_engineer_token(
        _sha256(raw), source_ip="203.0.113.7", user_agent="coord-mcp/0.29"
    )
    await db.touch_engineer_token(_sha256(raw))

    row = await db.lookup_engineer_token(_sha256(raw))
    assert row is not None
    assert row["request_count"] == 2
    assert row["last_source_ip"] == "203.0.113.7"


async def test_touch_truncates_oversized_metadata(tmp_path: Path) -> None:
    """IP and UA are untrusted proxy metadata; the db layer caps them
    so a hostile client cannot bloat the table."""
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "o" * 64
    await db.create_engineer_token("alex/claude/main", _sha256(raw))

    await db.touch_engineer_token(
        _sha256(raw), source_ip="x" * 1000, user_agent="y" * 5000
    )

    row = await db.lookup_engineer_token(_sha256(raw))
    assert row is not None
    assert len(row["last_source_ip"]) == 128
    listed = await db.list_engineer_tokens()
    assert len(listed[0]["last_user_agent"]) == 512


# --- v0.29.4: rotation ---


async def test_rotate_happy_path(tmp_path: Path) -> None:
    """Rotation mints a successor (same engineer + description,
    rotated_from links back) and puts the old token into a grace
    window during which BOTH tokens authenticate."""
    db = Database(tmp_path / "tok.sqlite")
    old_raw = "coordt_" + "p" * 64
    new_raw = "coordt_" + "q" * 64
    old_id = await db.create_engineer_token(
        "alex/claude/main", _sha256(old_raw), description="laptop"
    )

    grace = datetime.now(UTC) + timedelta(hours=24)
    result = await db.rotate_engineer_token(
        old_id, _sha256(new_raw), grace_until=grace
    )
    assert result["ok"] is True
    assert result["engineer"] == "alex/claude/main"

    new_row = await db.lookup_engineer_token(_sha256(new_raw))
    assert new_row is not None
    assert new_row["status"] == "ok"
    assert new_row["rotated_from"] == old_id
    assert new_row["description"] == "laptop"

    # Old token still authenticates inside the grace window.
    old_row = await db.lookup_engineer_token(_sha256(old_raw))
    assert old_row is not None
    assert old_row["status"] == "ok"


async def test_rotated_token_dies_after_grace(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    old_raw = "coordt_" + "r" * 64
    new_raw = "coordt_" + "s" * 64
    old_id = await db.create_engineer_token("alex/claude/main", _sha256(old_raw))

    # Grace window already in the past: the old token is immediately dead.
    grace = datetime.now(UTC) - timedelta(seconds=1)
    result = await db.rotate_engineer_token(
        old_id, _sha256(new_raw), grace_until=grace
    )
    assert result["ok"] is True

    assert await db.lookup_engineer_token(_sha256(old_raw)) is None
    resolved = await db.resolve_engineer_token(_sha256(old_raw))
    assert resolved is not None
    assert resolved["status"] == "rotation_grace_elapsed"

    # The successor is unaffected.
    assert await db.lookup_engineer_token(_sha256(new_raw)) is not None


async def test_grace_boundary_is_inclusive(tmp_path: Path) -> None:
    """``_ts_elapsed`` uses ``<=``: at the exact instant
    ``rotation_grace_until == now`` the old token is already dead.
    Pins the boundary so a future ``<=`` -> ``<`` regression (which
    would extend validity past the advertised cutoff) fails loudly.
    One second earlier the token must still authenticate."""
    db = Database(tmp_path / "tok.sqlite")
    old_raw = "coordt_" + "v" * 64
    new_raw = "coordt_" + "w" * 64
    old_id = await db.create_engineer_token("alex/claude/main", _sha256(old_raw))

    cutoff = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)
    result = await db.rotate_engineer_token(
        old_id, _sha256(new_raw), grace_until=cutoff
    )
    assert result["ok"] is True

    inside = await db.resolve_engineer_token(
        _sha256(old_raw), now=cutoff - timedelta(seconds=1)
    )
    assert inside is not None
    assert inside["status"] == "ok"

    at_cutoff = await db.resolve_engineer_token(_sha256(old_raw), now=cutoff)
    assert at_cutoff is not None
    assert at_cutoff["status"] == "rotation_grace_elapsed"


async def test_rotate_rejects_dead_or_already_rotated(tmp_path: Path) -> None:
    """A rotation must never revive a dead credential: revoked,
    expired, and already-rotated tokens all refuse to rotate with a
    distinct error. Rotating the current successor is the supported
    path (A -> B, then B -> C)."""
    db = Database(tmp_path / "tok.sqlite")
    grace = datetime.now(UTC) + timedelta(hours=1)

    missing = await db.rotate_engineer_token(
        "no-such-id", _sha256("w1"), grace_until=grace
    )
    assert missing == {"ok": False, "error": "not_found"}

    revoked_id = await db.create_engineer_token("alex/a", _sha256("w2"))
    await db.revoke_engineer_token(revoked_id)
    revoked = await db.rotate_engineer_token(
        revoked_id, _sha256("w3"), grace_until=grace
    )
    assert revoked == {"ok": False, "error": "revoked"}

    expired_id = await db.create_engineer_token(
        "alex/b", _sha256("w4"),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    expired = await db.rotate_engineer_token(
        expired_id, _sha256("w5"), grace_until=grace
    )
    assert expired == {"ok": False, "error": "expired"}

    chain_id = await db.create_engineer_token("alex/c", _sha256("w6"))
    first = await db.rotate_engineer_token(
        chain_id, _sha256("w7"), grace_until=grace
    )
    assert first["ok"] is True
    again = await db.rotate_engineer_token(
        chain_id, _sha256("w8"), grace_until=grace
    )
    assert again == {"ok": False, "error": "already_rotated"}

    # Rotating the successor extends the chain.
    succ = await db.rotate_engineer_token(
        first["new_token_id"], _sha256("w9"), grace_until=grace
    )
    assert succ["ok"] is True


async def test_rotate_is_atomic_on_insert_failure(tmp_path: Path) -> None:
    """If the successor insert fails (duplicate hash), the old token
    must come out untouched -- no half-applied rotation where the old
    token has a grace window but no successor exists."""
    import aiosqlite

    import pytest

    db = Database(tmp_path / "tok.sqlite")
    raw_a = "coordt_" + "t" * 64
    raw_b = "coordt_" + "u" * 64
    await db.create_engineer_token("alex/a", _sha256(raw_a))
    target_id = await db.create_engineer_token("alex/b", _sha256(raw_b))

    grace = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(aiosqlite.IntegrityError):
        # Successor hash collides with token A's hash.
        await db.rotate_engineer_token(
            target_id, _sha256(raw_a), grace_until=grace
        )

    row = await db.resolve_engineer_token(_sha256(raw_b))
    assert row is not None
    assert row["status"] == "ok"
    assert row["rotation_grace_until"] is None


# ---------------------------------------------------------------------------
# repo-bound tokens (issue #30 slice 2/3): a nullable ``repo`` binds a token
# to a repo so the server can enforce scope from auth. NULL = unscoped.
# ---------------------------------------------------------------------------


async def test_create_token_persists_repo(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "c" * 64
    await db.create_engineer_token(
        "alex/claude/main", _sha256(raw), repo="amittell/coord"
    )
    hit = await db.lookup_engineer_token(_sha256(raw))
    assert hit is not None
    assert hit["repo"] == "amittell/coord"
    resolved = await db.resolve_engineer_token(_sha256(raw))
    assert resolved is not None
    assert resolved["repo"] == "amittell/coord"


async def test_create_token_defaults_repo_to_null(tmp_path: Path) -> None:
    # An unscoped (operator / back-compat) token: repo is NULL.
    db = Database(tmp_path / "tok.sqlite")
    raw = "coordt_" + "d" * 64
    await db.create_engineer_token("alex/claude/main", _sha256(raw))
    hit = await db.lookup_engineer_token(_sha256(raw))
    assert hit is not None
    assert hit["repo"] is None


async def test_list_tokens_surfaces_repo(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    await db.create_engineer_token(
        "eng-a", _sha256("coordt_" + "e" * 64), repo="amittell/coord"
    )
    await db.create_engineer_token("eng-b", _sha256("coordt_" + "f" * 64))
    rows = await db.list_engineer_tokens()
    by_engineer = {r["engineer"]: r for r in rows}
    assert by_engineer["eng-a"]["repo"] == "amittell/coord"
    assert by_engineer["eng-b"]["repo"] is None


async def test_get_token_by_id_surfaces_repo(tmp_path: Path) -> None:
    db = Database(tmp_path / "tok.sqlite")
    tid = await db.create_engineer_token(
        "eng-a", _sha256("coordt_" + "1" * 64), repo="amittell/coord"
    )
    row = await db.get_engineer_token_by_id(tid)
    assert row is not None
    assert row["repo"] == "amittell/coord"


async def test_rotate_carries_repo_forward(tmp_path: Path) -> None:
    # Critical regression (flagged in review): a rotation must NOT silently
    # unscope its successor, or a scoped token becomes operator-equivalent
    # on rotation with no 401 to signal it.
    db = Database(tmp_path / "tok.sqlite")
    raw_old = "coordt_" + "2" * 64
    raw_new = "coordt_" + "3" * 64
    old_id = await db.create_engineer_token(
        "eng-a", _sha256(raw_old), repo="amittell/coord"
    )
    grace = datetime.now(UTC) + timedelta(hours=1)
    result = await db.rotate_engineer_token(
        old_id, _sha256(raw_new), grace_until=grace
    )
    assert result["ok"] is True
    successor = await db.resolve_engineer_token(_sha256(raw_new))
    assert successor is not None
    assert successor["status"] == "ok"
    assert successor["repo"] == "amittell/coord", (
        "rotation silently dropped the repo scope"
    )
