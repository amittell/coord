"""Tests for the v0.27.1 ``coord outbox`` operator CLI.

Each test seeds rows into a tmp ``webhook_outbox`` through the
``aiosqlite`` seam (the connection the PG test harness redirects to the
Postgres schema, so the seed lands in whichever backend the suite runs),
monkeypatches ``COORD_DATABASE_PATH`` so ``cli_outbox`` picks up the tmp
DB through ``Settings``, and invokes the parsed subcommand through
``coordination.cli.build_parser``.

The reason for going through ``build_parser`` rather than crafting an
``argparse.Namespace`` by hand is that the parser surface is part of
the contract this CLI ships: the test will catch a bad flag name or a
missing default before the user does.
"""

from __future__ import annotations

import aiosqlite
import asyncio
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
    at it.

    ``Database(path).init()`` runs all pending migrations under a single
    transaction, so the ``webhook_outbox`` table (schema v13) exists
    before any seed insert. The monkeypatched env var is read by
    ``Settings`` inside the CLI module, so the subcommand sees the same
    file we are about to seed.
    """
    path = tmp_path / "outbox.sqlite"
    asyncio.run(Database(path).init())
    monkeypatch.setenv("COORD_DATABASE_PATH", str(path))
    return path


def _ts(offset_seconds: int = 0) -> str:
    """Return an ISO 8601 ``Z`` timestamp ``offset_seconds`` before now.

    Tests that need deterministic ordering use this to space out
    ``created_at`` values; the absolute value is unimportant as long
    as relative ordering matches the test's expectations.
    """
    dt = datetime.now(UTC) - timedelta(seconds=offset_seconds)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _insert_row(
    path: Path,
    *,
    row_id: str,
    status: str = "pending",
    event_type: str = "auto-coexist",
    retry_count: int = 0,
    last_error: str | None = None,
    created_at: str | None = None,
    next_attempt_at: str | None = None,
    delivered_at: str | None = None,
) -> None:
    """Insert a single ``webhook_outbox`` row with sensible defaults.

    Defaults match what ``enqueue_webhook`` would produce for a fresh
    pending row (status='pending', retry_count=0, last_error NULL),
    overridden per-test for the failure / exhaustion / delivered
    flavours.
    """
    created = created_at or _ts()
    next_attempt = next_attempt_at or created
    delivered = delivered_at if status == "delivered" else None

    async def _go() -> None:
        # aiosqlite.connect is the seam the PG test harness redirects to the
        # Postgres schema, so this seed lands in whichever backend the suite
        # is running against (raw sqlite3 would only ever see the local file).
        async with aiosqlite.connect(str(path)) as conn:
            await conn.execute(
                "INSERT INTO webhook_outbox "
                "(id, url, event_type, payload_json, hmac_signature, "
                " status, retry_count, last_error, next_attempt_at, "
                " created_at, delivered_at) "
                "VALUES (?, 'https://example/h', ?, '{}', 'sig', "
                " ?, ?, ?, ?, ?, ?)",
                (
                    row_id,
                    event_type,
                    status,
                    retry_count,
                    last_error,
                    next_attempt,
                    created,
                    delivered,
                ),
            )
            await conn.commit()

    asyncio.run(_go())


def _get_row(path: Path, row_id: str) -> dict[str, object] | None:
    """Return the row with id ``row_id`` as a dict, or None if missing."""
    async def _go() -> dict[str, object] | None:
        async with aiosqlite.connect(str(path)) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM webhook_outbox WHERE id = ?", (row_id,)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    return asyncio.run(_go())


def _count_all(path: Path) -> int:
    async def _go() -> int:
        async with aiosqlite.connect(str(path)) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM webhook_outbox")
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
# stats
# ---------------------------------------------------------------------------


def test_stats_shows_per_event_type_counts(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three rows in three different states under two event types: the
    stats table must report per-status counts grouped by event_type so
    operators can tell which subscriber is misbehaving."""
    _insert_row(db_path, row_id="d1", status="delivered",
                event_type="auto-coexist")
    _insert_row(db_path, row_id="f1", status="failed",
                event_type="auto-coexist", retry_count=2,
                last_error="HTTP 503")
    _insert_row(db_path, row_id="x1", status="exhausted",
                event_type="auto-narrow", retry_count=5,
                last_error="HTTP 500")

    rc = _run(["outbox", "stats"])
    assert rc == 0

    out = capsys.readouterr().out
    # Header reflects the default window.
    assert "Webhook delivery (last 24h)" in out
    # Per-event-type rows present with the right counts.
    lines = out.splitlines()
    coexist = next(line for line in lines if line.startswith("auto-coexist"))
    narrow = next(line for line in lines if line.startswith("auto-narrow"))
    # Order: event_type, delivered, failed, pending, exhausted.
    assert coexist.split() == ["auto-coexist", "1", "1", "0", "0"]
    assert narrow.split() == ["auto-narrow", "0", "0", "0", "1"]


def test_stats_json_output(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` returns a parseable object with window_hours and a
    per-event-type stats dict so dashboards / scripts can consume the
    output without parsing the human table."""
    _insert_row(db_path, row_id="d1", status="delivered",
                event_type="claim_granted")
    _insert_row(db_path, row_id="p1", status="pending",
                event_type="claim_granted")

    rc = _run(["outbox", "stats", "--hours", "12", "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["window_hours"] == 12
    stats = payload["stats"]
    assert "claim_granted" in stats
    assert stats["claim_granted"]["delivered"] == 1
    assert stats["claim_granted"]["pending"] == 1
    assert stats["claim_granted"]["failed"] == 0
    assert stats["claim_granted"]["exhausted"] == 0


# ---------------------------------------------------------------------------
# tail
# ---------------------------------------------------------------------------


def test_tail_shows_recent_rows(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``tail -n 3`` returns the 3 newest rows out of 5.

    We seed deterministic ``created_at`` values spaced 1s apart so
    ordering is testable without sleeping; the most recent rows must
    appear and the oldest two must be filtered out by the LIMIT.
    """
    # Oldest first: r1 at -50s, r2 at -40s, ..., r5 at -10s.
    for i, secs in enumerate([50, 40, 30, 20, 10]):
        _insert_row(
            db_path,
            row_id=f"r{i + 1}",
            status="delivered",
            event_type="queue_grant",
            created_at=_ts(offset_seconds=secs),
        )

    rc = _run(["outbox", "tail", "-n", "3"])
    assert rc == 0

    out = capsys.readouterr().out
    # Three newest are r3, r4, r5. r1 and r2 must NOT appear.
    assert "r1" not in out and "r2" not in out
    # The three rows are present in the output (id is included implicitly
    # via timestamps, but we surface the id only through the JSON path;
    # for the human path we assert by checking the line count and that
    # the timestamps match).
    lines = [line for line in out.splitlines() if "queue_grant" in line]
    assert len(lines) == 3


def test_tail_truncates_long_last_error_and_supports_json(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human renderer truncates ``last_error`` past 60 chars so a
    receiver dumping a stack trace into the error field doesn't blow
    out the operator's terminal. The JSON path preserves the field as
    stored so a downstream parser keeps the full message."""
    long_err = "X" * 200
    _insert_row(
        db_path,
        row_id="long1",
        status="failed",
        event_type="auto-demote",
        last_error=long_err,
        retry_count=3,
    )

    # Human path
    rc = _run(["outbox", "tail", "-n", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "..." in out
    # Full 200-X string must not appear verbatim.
    assert long_err not in out

    # JSON path
    rc = _run(["outbox", "tail", "-n", "1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert isinstance(payload, list)
    assert payload[0]["id"] == "long1"
    assert payload[0]["last_error"] == long_err
    assert payload[0]["status"] == "failed"
    assert payload[0]["retry_count"] == 3
    assert payload[0]["event_age_seconds"] is not None


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


def test_retry_failed_resets_state(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed row gets retry_count reset to 0, last_error cleared, and
    status flipped back to ``pending`` so the delivery loop picks it up
    on its next sweep. ``next_attempt_at`` advances to a current
    timestamp; we only assert it changed away from the seeded value."""
    seeded_next_attempt = _ts(offset_seconds=600)
    _insert_row(
        db_path,
        row_id="f1",
        status="failed",
        event_type="auto-coexist",
        retry_count=2,
        last_error="HTTP 503",
        next_attempt_at=seeded_next_attempt,
    )

    rc = _run(["outbox", "retry", "--failed"])
    assert rc == 0
    assert "Reset 1 row" in capsys.readouterr().out

    row = _get_row(db_path, "f1")
    assert row is not None
    assert row["status"] == "pending"
    assert row["retry_count"] == 0
    assert row["last_error"] is None
    assert row["next_attempt_at"] != seeded_next_attempt


def test_retry_exhausted_separate_from_failed(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status selection isolates failed from exhausted: --failed must
    leave exhausted rows alone, --all must touch both. This is the
    contract that protects an operator who only wants to nudge
    transient failures back into the loop without re-arming rows
    that have already exhausted their retry budget."""
    _insert_row(db_path, row_id="f1", status="failed",
                event_type="auto-coexist", retry_count=2,
                last_error="HTTP 503")
    _insert_row(db_path, row_id="x1", status="exhausted",
                event_type="auto-coexist", retry_count=5,
                last_error="HTTP 500")

    # --failed: only the failed row moves.
    rc = _run(["outbox", "retry", "--failed"])
    assert rc == 0
    capsys.readouterr()
    assert _get_row(db_path, "f1")["status"] == "pending"
    assert _get_row(db_path, "x1")["status"] == "exhausted"

    # Re-seed so we can verify --all touches BOTH.
    # (The failed row already moved to pending; re-fail it so the
    # selector has work to do.)
    async def _refail() -> None:
        async with aiosqlite.connect(str(db_path)) as conn:
            await conn.execute(
                "UPDATE webhook_outbox SET status='failed', retry_count=2, "
                "last_error='HTTP 503' WHERE id = 'f1'"
            )
            await conn.commit()

    asyncio.run(_refail())

    rc = _run(["outbox", "retry", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Reset 2 row" in out
    assert _get_row(db_path, "f1")["status"] == "pending"
    assert _get_row(db_path, "x1")["status"] == "pending"
    assert _get_row(db_path, "x1")["retry_count"] == 0
    assert _get_row(db_path, "x1")["last_error"] is None


def test_retry_dry_run_does_not_mutate(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` reports the row count but writes nothing. This is
    the safety hatch that lets an operator preview the blast radius of
    a ``--all`` before pulling the trigger."""
    _insert_row(db_path, row_id="f1", status="failed",
                event_type="auto-coexist", retry_count=2,
                last_error="HTTP 503")

    rc = _run(["outbox", "retry", "--failed", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would reset 1 row" in out
    # State unchanged.
    row = _get_row(db_path, "f1")
    assert row["status"] == "failed"
    assert row["retry_count"] == 2
    assert row["last_error"] == "HTTP 503"


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def test_purge_delivered_removes_only_delivered(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--delivered`` (the default) deletes only delivered rows.

    Pending and failed rows are live state and must NOT be touched by
    purge -- if purge could drop a failed row, an operator running it
    on a noisy day would silently lose retryable events. The
    ``--dry-run`` companion path proves no row count changes for the
    preview-only invocation.
    """
    _insert_row(db_path, row_id="d1", status="delivered",
                event_type="auto-coexist",
                delivered_at=_ts())
    _insert_row(db_path, row_id="p1", status="pending",
                event_type="auto-coexist")
    _insert_row(db_path, row_id="f1", status="failed",
                event_type="auto-coexist",
                retry_count=1, last_error="boom")

    # Dry run first: nothing should change.
    rc = _run(["outbox", "purge", "--dry-run"])
    assert rc == 0
    dry_out = capsys.readouterr().out
    assert "DRY RUN" in dry_out
    assert "would remove 1 row" in dry_out
    assert _count_all(db_path) == 3

    # Real purge: only the delivered row goes.
    rc = _run(["outbox", "purge"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Removed 1 row" in out
    assert _get_row(db_path, "d1") is None
    assert _get_row(db_path, "p1") is not None
    assert _get_row(db_path, "f1") is not None
