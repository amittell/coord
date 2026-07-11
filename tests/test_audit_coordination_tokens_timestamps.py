"""Audit-fix coverage for naive-timestamp handling in token expiry
checks (v0.45 audit).

``tokens._elapsed`` and its twin ``db._ts_elapsed`` promised to fail
closed on bad timestamps, but a timezone-NAIVE value (a hand-edited or
legacy row like ``2026-01-01T00:00:00``) parses fine and then the
naive-vs-aware comparison raised TypeError -- crashing ``coord tokens
list`` / the dashboard panel via ``derive_token_status``, and turning
every request with such a token into a 500 via
``resolve_engineer_token``. Naive values are now read as UTC; garbage
still counts as elapsed (fail closed).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from conftest import seam_connection
from coordination.db import Database, _ts_elapsed
from coordination.tokens import (
    _elapsed,
    derive_token_status,
    generate_raw_token,
    sha256_token,
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


# --- tokens._elapsed ---------------------------------------------------------


def test_elapsed_naive_past_timestamp_is_elapsed() -> None:
    assert _elapsed("2026-01-01T00:00:00", NOW) is True


def test_elapsed_naive_future_timestamp_is_not_elapsed() -> None:
    assert _elapsed("2999-01-01T00:00:00", NOW) is False


def test_elapsed_garbage_still_fails_closed() -> None:
    assert _elapsed("not-a-timestamp", NOW) is True


def test_elapsed_empty_means_no_deadline() -> None:
    assert _elapsed(None, NOW) is False
    assert _elapsed("", NOW) is False


# --- db._ts_elapsed (the request-auth-path twin) -----------------------------


def test_ts_elapsed_naive_past_timestamp_is_elapsed() -> None:
    assert _ts_elapsed("2026-01-01T00:00:00", NOW) is True


def test_ts_elapsed_naive_future_timestamp_is_not_elapsed() -> None:
    assert _ts_elapsed("2999-01-01T00:00:00", NOW) is False


def test_ts_elapsed_garbage_still_fails_closed() -> None:
    assert _ts_elapsed("garbage", NOW) is True


# --- derive_token_status must not crash on naive rows ------------------------


def test_derive_status_naive_expires_at_reads_as_utc() -> None:
    row = {"revoked_at": None, "expires_at": "2026-01-01T00:00:00"}
    assert derive_token_status(row, now=NOW) == "expired"
    row["expires_at"] = "2999-01-01T00:00:00"
    assert derive_token_status(row, now=NOW) == "active"


def test_derive_status_naive_grace_window_reads_as_utc() -> None:
    row = {
        "revoked_at": None,
        "expires_at": None,
        "rotation_grace_until": "2026-01-01T00:00:00",
    }
    assert derive_token_status(row, now=NOW) == "grace-elapsed"
    row["rotation_grace_until"] = "2999-01-01T00:00:00"
    assert derive_token_status(row, now=NOW) == "rotating"


# --- resolve_engineer_token: corrupt expiry is a 401, not a 500 --------------


async def _corrupt_expiry(db: Database, token_id: str, value: str) -> None:
    async with seam_connection(db) as conn:
        await conn.execute(
            "UPDATE engineer_tokens SET expires_at = ? WHERE id = ?",
            (value, token_id),
        )


def test_resolve_token_with_naive_expiry_does_not_raise(
    tmp_path: Path,
) -> None:
    """The request auth path: a naive expires_at must resolve (as UTC)
    rather than raising TypeError into a 500."""

    async def _go() -> None:
        db_path = tmp_path / "db.sqlite"
        db = Database(db_path)
        raw = generate_raw_token()
        token_id = await db.create_engineer_token(
            "alex/claude/main", sha256_token(raw)
        )
        # Future naive timestamp: still authenticates, read as UTC.
        await _corrupt_expiry(db, token_id, "2999-01-01T00:00:00")
        resolved = await db.resolve_engineer_token(sha256_token(raw))
        assert resolved is not None
        assert resolved["status"] == "ok"
        # Past naive timestamp: expired, not a crash.
        await _corrupt_expiry(db, token_id, "2000-01-01T00:00:00")
        resolved = await db.resolve_engineer_token(sha256_token(raw))
        assert resolved is not None
        assert resolved["status"] == "expired"

    asyncio.run(_go())
