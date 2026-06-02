"""Tests for repo-aware claim persistence and aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite

from coordination.db import Database


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _insert(
    db: Database,
    *,
    engineer: str,
    pattern: str,
    repo: str | None,
    created_at: str,
    expires_at: str,
    released_at: str | None = None,
    branch: str | None = None,
) -> str:
    cid = str(uuid4())
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern, severity,
                created_at, expires_at, released_at, repo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                engineer,
                branch,
                None,
                "file",
                pattern,
                "soft",
                created_at,
                expires_at,
                released_at,
                repo,
            ),
        )
        await conn.commit()
    return cid


async def test_insert_claims_batch_persists_repo(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.init()

    exp = _iso(datetime.now(UTC) + timedelta(hours=4))
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description="repo-tagged",
        items=[("c1", "file", "src/foo.py", "soft", exp)],
        repo="amittell/coord",
    )

    rows = await db.list_recent_claims(10)
    assert len(rows) == 1
    assert rows[0]["repo"] == "amittell/coord"


async def test_insert_claims_batch_defaults_repo_to_null(tmp_path: Path) -> None:
    """Callers that don't pass repo (legacy code paths) must still work."""
    db = Database(tmp_path / "db.sqlite")
    await db.init()

    exp = _iso(datetime.now(UTC) + timedelta(hours=4))
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description="legacy-style",
        items=[("c1", "file", "src/foo.py", "soft", exp)],
    )

    rows = await db.list_recent_claims(10)
    assert len(rows) == 1
    assert rows[0]["repo"] is None


async def test_list_repos_aggregates_per_repo_stats(tmp_path: Path) -> None:
    """list_repos returns one row per distinct repo with claim/engineer
    aggregates within a configurable activity window."""
    db = Database(tmp_path / "db.sqlite")
    await db.init()

    now = datetime.now(UTC)
    fresh = _iso(now - timedelta(hours=2))
    expired = _iso(now - timedelta(hours=1))

    # bastionx: 3 claims, 2 engineers
    for eng, pat in [
        ("alice", "services/a.py"),
        ("alice", "services/b.py"),
        ("bob", "tests/x.py"),
    ]:
        await _insert(
            db,
            engineer=eng,
            pattern=pat,
            repo="example-org/bastionx",
            created_at=fresh,
            expires_at=expired,
        )

    # coord: 1 active claim
    await _insert(
        db,
        engineer="charlie",
        pattern="coordination/foo.py",
        repo="amittell/coord",
        created_at=fresh,
        expires_at=_iso(now + timedelta(hours=2)),
    )

    # legacy claim with no repo
    await _insert(
        db,
        engineer="dora",
        pattern="legacy/path.py",
        repo=None,
        created_at=fresh,
        expires_at=expired,
    )

    repos = await db.list_repos(window_hours=24, now=now)

    by_name = {r["repo"]: r for r in repos}
    assert "example-org/bastionx" in by_name
    assert "amittell/coord" in by_name

    bx = by_name["example-org/bastionx"]
    assert bx["claims_24h"] == 3
    assert bx["engineers_24h"] == 2

    coord = by_name["amittell/coord"]
    assert coord["claims_24h"] == 1
    assert coord["engineers_24h"] == 1


async def test_list_repos_excludes_old_activity_from_window(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.init()

    now = datetime.now(UTC)
    very_old = _iso(now - timedelta(days=3))

    await _insert(
        db,
        engineer="alice",
        pattern="src/old.py",
        repo="example-org/ancient",
        created_at=very_old,
        expires_at=very_old,
    )

    repos = await db.list_repos(window_hours=24, now=now)
    by_name = {r["repo"]: r for r in repos}
    # Old repo must still be listed (the user wants visibility) but with
    # zero counts inside the window.
    assert "example-org/ancient" in by_name
    assert by_name["example-org/ancient"]["claims_24h"] == 0


async def test_list_repos_reports_last_activity_timestamp(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.init()

    now = datetime.now(UTC)
    older = _iso(now - timedelta(hours=10))
    newer = _iso(now - timedelta(hours=1))
    expired = _iso(now - timedelta(minutes=5))

    await _insert(
        db,
        engineer="alice",
        pattern="src/a.py",
        repo="example-org/x",
        created_at=older,
        expires_at=expired,
    )
    await _insert(
        db,
        engineer="alice",
        pattern="src/b.py",
        repo="example-org/x",
        created_at=newer,
        expires_at=expired,
    )

    repos = await db.list_repos(window_hours=24, now=now)
    by_name = {r["repo"]: r for r in repos}
    assert by_name["example-org/x"]["last_activity"] == newer


async def test_list_repos_active_claim_count_excludes_released(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    await db.init()

    now = datetime.now(UTC)
    fresh = _iso(now - timedelta(minutes=5))
    expires = _iso(now + timedelta(hours=4))
    released = _iso(now)

    # Repo Y: one active, one released, one expired
    await _insert(
        db,
        engineer="alice",
        pattern="src/active.py",
        repo="example-org/y",
        created_at=fresh,
        expires_at=expires,
    )
    await _insert(
        db,
        engineer="bob",
        pattern="src/released.py",
        repo="example-org/y",
        created_at=fresh,
        expires_at=expires,
        released_at=released,
    )
    await _insert(
        db,
        engineer="bob",
        pattern="src/expired.py",
        repo="example-org/y",
        created_at=_iso(now - timedelta(hours=10)),
        expires_at=_iso(now - timedelta(hours=5)),
    )

    repos = await db.list_repos(window_hours=24, now=now)
    by_name = {r["repo"]: r for r in repos}
    assert by_name["example-org/y"]["active_claims"] == 1


async def test_list_repos_includes_auto_resolution_counts(tmp_path: Path) -> None:
    """v0.14.1: list_repos rows include ``auto_resolutions_24h`` with a
    breakdown of ``auto_coexist`` / ``auto_narrow`` audit events
    attributed to the repo. The attribution joins the audit event
    through ``detail.holder_claim_id`` -> ``claims.repo``.
    """
    db = Database(tmp_path / "db.sqlite")
    await db.init()

    now = datetime.now(UTC)
    fresh = _iso(now - timedelta(hours=2))
    expires = _iso(now + timedelta(hours=4))

    holder_id = await _insert(
        db,
        engineer="alice",
        pattern="src/auth.py",
        repo="example-org/coexist",
        created_at=fresh,
        expires_at=expires,
    )
    partner_id = await _insert(
        db,
        engineer="bob",
        pattern="src/auth.py",
        repo="example-org/coexist",
        created_at=fresh,
        expires_at=expires,
    )

    # Record an auto-coexist event between the two claims. The holder
    # claim id lives under detail.holder_claim_id; that's the field
    # count_auto_resolutions_since joins on to attribute the event to
    # the repo.
    await db.record_request_event(
        "auto-coexist",
        actor_engineer="bob",
        detail={
            "holder_claim_id": holder_id,
            "partner_claim_id": partner_id,
        },
    )

    repos = await db.list_repos(window_hours=24, now=now)
    by_name = {r["repo"]: r for r in repos}
    assert "example-org/coexist" in by_name
    counts = by_name["example-org/coexist"]["auto_resolutions_24h"]
    assert counts["auto_coexist"] == 1
    assert counts["auto_narrow"] == 0
