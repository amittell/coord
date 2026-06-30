"""Tests for the v0.28 ``coord engineers`` operator CLI.

Each test seeds claims into a tmp database via ``insert_claims_batch``
plus an ``aiosqlite`` UPDATE to backdate ``last_activity`` (the helper
always stamps "now" when ``session_id`` is set; we need older values to
exercise the staleness threshold). The ``aiosqlite`` seam is the one the
PG test harness redirects to the Postgres schema, so the backdate lands
in whichever backend the suite runs. The tmp database path is plumbed
through ``COORD_DATABASE_PATH`` so ``cli_engineers`` resolves it via
``Settings`` like every other coord subprocess does.

We dispatch through ``coordination.cli.build_parser`` so the parser
surface itself is part of the contract under test.
"""

from __future__ import annotations

import aiosqlite
import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coordination.cli import build_parser
from coordination.db import Database


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a freshly-migrated tmp database and point ``COORD_DATABASE_PATH``
    at it so the CLI module picks it up via ``Settings``.
    """
    path = tmp_path / "engineers.sqlite"
    asyncio.run(Database(path).init())
    monkeypatch.setenv("COORD_DATABASE_PATH", str(path))
    return path


def _iso(dt: datetime) -> str:
    """Render a UTC datetime as a Coord-style ISO 8601 ``...Z`` string."""
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _seed_claim(
    path: Path,
    *,
    engineer: str,
    pattern: str,
    last_activity_days_ago: float,
    repo: str | None = "demo",
    claim_id: str | None = None,
) -> str:
    """Insert one active claim then backdate its ``last_activity``.

    We can't use ``insert_claims_batch`` alone because it stamps
    ``last_activity = now`` whenever a session_id is provided -- the
    test needs an explicit older timestamp. The simplest path is to
    insert then UPDATE; an empty session_id would skip the timestamp
    entirely (NULL), which the stale list deliberately filters out.
    """
    from uuid import uuid4

    cid = claim_id or str(uuid4())
    # Wide future expiry so the claim shows up as active.
    expires_at = _iso(datetime.now(UTC) + timedelta(days=14))
    asyncio.run(
        Database(path).insert_claims_batch(
            engineer=engineer,
            branch=None,
            description=None,
            items=[(cid, "file", pattern, "soft", expires_at)],
            repo=repo,
            session_id=f"session-{engineer}",
        )
    )
    backdated = _iso(
        datetime.now(UTC) - timedelta(days=last_activity_days_ago)
    )

    async def _backdate() -> None:
        # aiosqlite.connect is redirected to the Postgres schema by the PG
        # test harness, so the backdate lands in whichever backend runs.
        async with aiosqlite.connect(str(path)) as conn:
            await conn.execute(
                "UPDATE claims SET last_activity = ? WHERE id = ?",
                (backdated, cid),
            )
            await conn.commit()

    asyncio.run(_backdate())
    return cid


def _count_active(path: Path, engineer: str) -> int:
    """Count engineer's still-open claim rows (``released_at IS NULL``)."""
    async def _go() -> int:
        async with aiosqlite.connect(str(path)) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM claims "
                "WHERE engineer = ? AND released_at IS NULL",
                (engineer,),
            )
            row = await cur.fetchone()
            return int(row[0])

    return asyncio.run(_go())


def _run(argv: list[str]) -> int:
    """Parse ``argv`` and execute the matching subcommand.

    We dispatch through ``args.func(args)`` rather than ``cli.main``
    so the update-notice path stays out of the way.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


# ---------------------------------------------------------------------------
# stale: listing
# ---------------------------------------------------------------------------


def test_stale_engineers_lists_engineers_with_old_last_activity(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two engineers; one with a 20-day-old last_activity, one with a
    fresh one. ``--days 7`` must surface only the stale engineer; the
    fresh engineer must NOT appear in the listing."""
    _seed_claim(
        db_path,
        engineer="alice-stale",
        pattern="src/auth/login.ts",
        last_activity_days_ago=20.0,
    )
    _seed_claim(
        db_path,
        engineer="bob-fresh",
        pattern="src/ui/button.ts",
        last_activity_days_ago=0.5,
    )

    rc = _run(["engineers", "stale", "--days", "7"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "alice-stale" in out
    assert "bob-fresh" not in out
    # Header reflects the chosen threshold.
    assert "threshold: 7d" in out


# ---------------------------------------------------------------------------
# stale: --release with --yes
# ---------------------------------------------------------------------------


def test_stale_engineers_release_drops_claims(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale engineer with 3 active claims: ``--release --yes`` must
    close all three so a follow-up direct sqlite3 query sees zero
    unreleased rows for that engineer."""
    for i, pattern in enumerate(
        ["src/a.py", "src/b.py", "src/c.py"], start=1
    ):
        _seed_claim(
            db_path,
            engineer="carol-stale",
            pattern=pattern,
            last_activity_days_ago=12.0,
            claim_id=f"carol-{i}",
        )
    assert _count_active(db_path, "carol-stale") == 3

    rc = _run(
        ["engineers", "stale", "--days", "7", "--release", "--yes"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Released 3 claim(s)" in out
    assert "carol-stale: 3" in out
    assert _count_active(db_path, "carol-stale") == 0


# ---------------------------------------------------------------------------
# stale: confirmation prompt
# ---------------------------------------------------------------------------


def test_stale_engineers_release_requires_yes_or_prompt(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--yes`` the operator gets a confirmation prompt; an
    ``n`` answer (or anything not starting with ``y``) must abort the
    release path with no rows mutated and a non-zero exit code so an
    unattended cron wrapper notices the decline."""
    _seed_claim(
        db_path,
        engineer="dave-stale",
        pattern="src/x.py",
        last_activity_days_ago=14.0,
    )
    assert _count_active(db_path, "dave-stale") == 1

    # Feed "n\n" to stdin so the input() call returns "n".
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))

    rc = _run(["engineers", "stale", "--days", "7", "--release"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Aborted" in out
    # No mutation -- the claim is still open.
    assert _count_active(db_path, "dave-stale") == 1


# ---------------------------------------------------------------------------
# stale: --json shape
# ---------------------------------------------------------------------------


def test_stale_engineers_json_output(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` returns a parseable object with ``days`` and an
    ``engineers`` list. Each entry exposes engineer, last_activity, an
    age in seconds, the active claim count, and the repo list. The
    threshold echo lets a script verify which window the output was
    computed against."""
    _seed_claim(
        db_path,
        engineer="erin-stale",
        pattern="src/y.py",
        last_activity_days_ago=10.0,
        repo="repo-a",
    )
    _seed_claim(
        db_path,
        engineer="erin-stale",
        pattern="src/z.py",
        last_activity_days_ago=15.0,
        repo="repo-b",
    )

    rc = _run(["engineers", "stale", "--days", "7", "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["days"] == 7
    engineers = payload["engineers"]
    assert isinstance(engineers, list)
    assert len(engineers) == 1

    entry = engineers[0]
    assert entry["engineer"] == "erin-stale"
    assert entry["active_claim_count"] == 2
    assert isinstance(entry["last_activity"], str)
    # Age is in seconds and at least the older claim's window (15d).
    assert isinstance(entry["last_activity_age_seconds"], int)
    assert entry["last_activity_age_seconds"] >= 7 * 86400
    # Both repos are surfaced and order-stable (sorted in the helper).
    assert entry["repos"] == ["repo-a", "repo-b"]
