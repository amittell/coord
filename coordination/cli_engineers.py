"""``coord engineers`` operator commands for v0.28 stale-engineer housekeeping.

v0.28 surfaces engineers whose most-recent ``last_activity`` exceeds a
configurable threshold (``Settings.stale_engineer_days``) so an operator
can spot abandoned worktrees that never released their claims. The CLI
is the actuation surface for that signal: ``coord engineers stale``
lists the engineers, and ``--release`` drops every active claim they
own.

The module mirrors the ``cli_outbox`` shape (v0.27.1): a sync top-level
that wraps the ``Database`` helpers it needs (``list_stale_engineers`` for
the listing, ``release_active_claims_for_engineers`` for the release write
path) in ``asyncio.run``. The CLI is invoked from shells, not from a hot
loop, so the per-call event-loop setup is irrelevant -- and routing both
paths through ``Database`` honours the backend selected by
``COORD_DATABASE_URL`` (SQLite by default, PostgresStore for a
``postgresql://`` DSN) rather than a raw ``sqlite3`` connection.

Exit codes follow the project convention:
    0 -- success (including empty results)
    1 -- operator error (bad flag combo, declined confirmation)
    2 -- database / environment error (missing DB file, sqlite3 failure)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from coordination.config import get_settings
from coordination.db import Database, _utcnow


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _format_age_days(seconds: int) -> str:
    """Format a non-negative duration as a compact age string.

    Stale engineers are measured in days but a few-hours-old entry may
    appear when an operator dials ``--days`` low for a forensic sweep,
    so we still format short windows usefully.
    """
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse a Coord-style ISO 8601 ``...Z`` timestamp."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _db_path() -> Path:
    """Resolve the local database path from ``Settings``.

    Mirrors what every other coord subprocess does: honour
    ``COORD_DATABASE_PATH`` via ``Settings.database_path``. The CLI
    never invents its own path.
    """
    return Path(get_settings().database_path)


def _postgres_selected() -> bool:
    """True when ``COORD_DATABASE_URL`` selects the Postgres backend.

    The missing-file guard below is a SQLite-only safety net (the PG
    backend keeps its data in a schema, not the ``database_path`` file),
    so it is skipped in PG mode.
    """
    url = os.environ.get("COORD_DATABASE_URL", "")
    return url.startswith("postgresql://") or url.startswith("postgres://")


def _ensure_db(path: Path) -> None:
    """Refuse to operate on a missing SQLite database file.

    Exits with code 2 so the operator sees a clear message instead of an
    opaque ``unable to open database file`` on the next read. Inert under
    Postgres, where the data lives in a PG schema rather than the
    ``database_path`` file.
    """
    if _postgres_selected():
        return
    if not path.exists():
        print(
            f"Database not found at {path}. Set COORD_DATABASE_PATH or run "
            "'coord start' to create one.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _confirm(prompt: str) -> bool:
    """Ask the operator for confirmation on stdin.

    Returns True only when the operator types something starting with
    ``y`` (case-insensitive). Anything else -- including EOF (no
    interactive stdin) -- counts as a decline so the destructive path
    cannot run unattended without ``--yes``.
    """
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer.startswith("y")


# ---------------------------------------------------------------------------
# coord engineers stale
# ---------------------------------------------------------------------------


def _resolve_days(args: argparse.Namespace) -> int | None:
    """Resolve the ``--days`` window.

    Falls back to ``Settings.stale_engineer_days`` when the flag is
    omitted. Returns None when the resulting value is non-positive so
    the caller can short-circuit and exit cleanly: a non-positive
    threshold means "housekeeping disabled" per the settings contract.
    """
    days_raw = getattr(args, "days", None)
    if days_raw is None:
        days = int(get_settings().stale_engineer_days)
    else:
        try:
            days = int(days_raw)
        except (TypeError, ValueError):
            print("--days must be a positive integer", file=sys.stderr)
            return None
    if days <= 0:
        return None
    return days


def _run_stale(args: argparse.Namespace) -> int:
    """List engineers whose most recent ``last_activity`` is older than
    ``--days`` days ago. Optionally release every active claim each
    listed engineer still owns.
    """
    path = _db_path()
    _ensure_db(path)
    days = _resolve_days(args)
    if days is None:
        # _resolve_days already printed the error for the bad-int case.
        # The "disabled" case is not an error: emit a friendly note and
        # exit 0 so a cron wrapper doesn't go red on a configured-off
        # housekeeping window.
        if getattr(args, "days", None) is None:
            print(
                "Stale-engineer housekeeping is disabled "
                "(stale_engineer_days <= 0)."
            )
            return 0
        return 1

    now = datetime.now(UTC)
    try:
        engineers = asyncio.run(
            Database(path).list_stale_engineers(days=days, now=now)
        )
    except Exception as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload: list[dict[str, object]] = []
        for e in engineers:
            la = _parse_iso(str(e.get("last_activity") or ""))
            age = int((now - la).total_seconds()) if la else None
            payload.append(
                {
                    "engineer": e["engineer"],
                    "last_activity": e["last_activity"],
                    "last_activity_age_seconds": age,
                    "active_claim_count": e["active_claim_count"],
                    "repos": e["repos"],
                }
            )
        result: dict[str, object] = {"days": days, "engineers": payload}
        # --release on top of --json emits ONE combined JSON object (never
        # human text mixed into the stream). JSON/automation mode has no
        # interactive prompt, so a release requires --yes explicitly.
        if args.release:
            if not args.yes:
                print(
                    json.dumps(
                        {"error": "release in --json mode requires --yes"},
                        sort_keys=True,
                    )
                )
                return 1
            now_iso = _utcnow()
            try:
                released_per_engineer = asyncio.run(
                    Database(path).release_active_claims_for_engineers(
                        [str(e["engineer"]) for e in engineers], now_iso=now_iso
                    )
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {"error": f"database error: {exc}"}, sort_keys=True
                    )
                )
                return 2
            result["release"] = {
                "released_total": sum(released_per_engineer.values()),
                "released_per_engineer": released_per_engineer,
            }
        print(json.dumps(result, sort_keys=True))
        return 0

    if not engineers:
        print(f"No stale engineers (threshold: {days}d).")
        return 0

    print(f"Stale engineers (threshold: {days}d)")
    header = f"{'engineer':<24}  {'age':>6}  {'claims':>6}  repos"
    print(header)
    for e in engineers:
        la = _parse_iso(str(e.get("last_activity") or ""))
        age_str = (
            _format_age_days(int((now - la).total_seconds()))
            if la
            else "?"
        )
        repos_str = ",".join(e["repos"]) if e["repos"] else "-"
        print(
            f"{str(e['engineer']):<24}  "
            f"{age_str:>6}  "
            f"{int(e['active_claim_count']):>6}  "
            f"{repos_str}"
        )

    if args.release:
        return _release_engineers(path, engineers, assume_yes=args.yes)
    return 0


def _release_engineers(
    path: Path,
    engineers: list[dict[str, Any]],
    *,
    assume_yes: bool,
) -> int:
    """Drop every active claim for each engineer in ``engineers``.

    ``assume_yes`` skips the confirmation prompt; when False, the
    operator must type ``y`` on stdin or the call is a no-op (return
    code 1, "operator declined"). We deliberately do not key the
    prompt off ``stdin.isatty()`` -- a script that wants to bypass the
    prompt should pass ``--yes``; everything else gets the safety
    interlock.
    """
    if not engineers:
        return 0
    if not assume_yes:
        names = ", ".join(str(e["engineer"]) for e in engineers)
        total_claims = sum(int(e["active_claim_count"]) for e in engineers)
        prompt = (
            f"Release {total_claims} active claim(s) "
            f"across {len(engineers)} engineer(s) [{names}]? [y/N] "
        )
        if not _confirm(prompt):
            print("Aborted; no claims released.")
            return 1

    now_iso = _utcnow()
    try:
        released_per_engineer = asyncio.run(
            Database(path).release_active_claims_for_engineers(
                [str(e["engineer"]) for e in engineers], now_iso=now_iso
            )
        )
    except Exception as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    total = sum(released_per_engineer.values())
    print(f"Released {total} claim(s) across {len(engineers)} engineer(s).")
    for engineer, n in released_per_engineer.items():
        print(f"  {engineer}: {n}")
    return 0


# ---------------------------------------------------------------------------
# dispatch + parser registration
# ---------------------------------------------------------------------------


def run_engineers(args: argparse.Namespace) -> int:
    """Top-level dispatch for ``coord engineers``.

    Only ``stale`` ships in v0.28; the subcommand layout leaves room
    for follow-on tooling (``list``, ``audit``, etc.) without
    reshaping the surface.
    """
    action = getattr(args, "engineers_action", None)
    if action == "stale":
        return _run_stale(args)
    print("Unknown engineers subcommand", file=sys.stderr)
    return 1


def add_engineers_subparser(sub: argparse._SubParsersAction) -> None:
    """Wire ``coord engineers`` and its nested ``stale`` subcommand.

    Kept as a registration helper so ``cli.build_parser`` stays the
    single place subcommand registration lives. The cli_engineers
    module owns the flag surface, the cli module owns ordering and
    the help banner.
    """
    engineers = sub.add_parser(
        "engineers",
        help="Inspect and manage per-engineer claim state",
        description=(
            "Operator commands for per-engineer claim housekeeping. "
            "v0.28 ships the 'stale' subcommand for finding (and "
            "optionally releasing) claims held by engineers whose "
            "sessions have gone silent."
        ),
    )
    nested = engineers.add_subparsers(dest="engineers_action", required=True)

    stale = nested.add_parser(
        "stale",
        help="List engineers whose most recent activity is older than --days",
        description=(
            "List engineers whose most recent active-claim last_activity "
            "is older than --days days (default: Settings.stale_engineer_days). "
            "Use --release to drop every active claim those engineers still "
            "own; --yes skips the confirmation prompt for unattended runs."
        ),
    )
    stale.add_argument(
        "--days",
        type=int,
        default=None,
        help=(
            "Threshold in days. Defaults to Settings.stale_engineer_days "
            "(0 disables housekeeping)."
        ),
    )
    stale.add_argument(
        "--release",
        action="store_true",
        help="Release every active claim for each listed engineer",
    )
    stale.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt for --release",
    )
    stale.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a human-readable table",
    )
    stale.set_defaults(func=run_engineers)

    engineers.set_defaults(func=run_engineers)
