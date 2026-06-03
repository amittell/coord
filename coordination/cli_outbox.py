"""``coord outbox`` operator commands for the v0.27 webhook outbox.

The v0.27 release shipped the ``webhook_outbox`` table and a background
delivery loop (see ``service.deliver_pending_webhooks``). v0.27.1 layers
the operator UX on top: stats, tail, retry, and purge subcommands that
run against the local database without going through the HTTP API.

The CLI is intentionally sync. The single async helper we lean on
(``Database.webhook_delivery_stats``) is wrapped in ``asyncio.run`` so
the surrounding command stays a plain procedure call -- ``coord outbox``
is invoked from shells and is not in any hot path where event-loop
overhead would matter.

Database access for the read-only inspection paths (``tail``) and the
small mutators (``retry``, ``purge``) goes through ``sqlite3`` directly.
That keeps the CLI module dependency-free relative to the async stack
and keeps the SQL it runs visible at the call site for an operator
reading the source.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from coordination.config import get_settings
from coordination.db import Database, _utcnow


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _format_age_seconds(seconds: int) -> str:
    """Format a non-negative duration in seconds as a compact human string.

    Examples: ``42s``, ``5m``, ``5m12s``, ``2h``, ``2h30m``, ``3d``,
    ``3d4h``. Negative inputs are clamped to ``0s`` so a clock that
    briefly moves backwards (NTP slew) cannot produce a negative age.
    """
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s}s" if s else f"{m}m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h{m}m" if m else f"{h}h"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d}d{h}h" if h else f"{d}d"


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse a Coord-style ISO 8601 ``...Z`` timestamp.

    Returns ``None`` on falsy input or anything unparseable; callers
    treat ``None`` as 'unknown age' rather than failing the row.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _db_path() -> Path:
    """Resolve the local database path from ``Settings``.

    Mirrors the resolution every other coord subprocess does:
    ``COORD_DATABASE_PATH`` -> ``Settings.database_path`` default. The CLI
    never invents its own path; if the env var is set, the user's choice
    wins.
    """
    return Path(get_settings().database_path)


def _ensure_db(path: Path) -> None:
    """Refuse to operate on a database file that does not exist.

    Exits with code 2 (database error) so the operator gets a clear
    message instead of an opaque ``OperationalError: unable to open
    database file`` from sqlite3 on the next read.
    """
    if not path.exists():
        print(
            f"Database not found at {path}. Set COORD_DATABASE_PATH or run "
            "'coord start' to create one.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _connect(path: Path) -> sqlite3.Connection:
    """Open a sync sqlite3 connection with ``Row`` access for ergonomic reads."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# coord outbox stats
# ---------------------------------------------------------------------------


def _run_stats(args: argparse.Namespace) -> int:
    """Print per-event-type delivery counts for the last ``--hours``.

    Reuses ``Database.webhook_delivery_stats`` so the numbers stay in
    lockstep with the dashboard panel that uses the same helper.
    """
    path = _db_path()
    _ensure_db(path)
    hours = int(args.hours)
    if hours <= 0:
        print("--hours must be positive", file=sys.stderr)
        return 1
    try:
        stats = asyncio.run(
            Database(path).webhook_delivery_stats(window_hours=hours)
        )
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"window_hours": hours, "stats": stats}, sort_keys=True))
        return 0

    if not stats:
        print(f"No webhook events in the last {hours}h.")
        return 0

    print(f"Webhook delivery (last {hours}h)")
    header = (
        f"{'event_type':<30}  {'delivered':>9}  {'failed':>6}  "
        f"{'pending':>7}  {'exhausted':>9}"
    )
    print(header)
    for event_type in sorted(stats):
        counts = stats[event_type]
        print(
            f"{event_type:<30}  "
            f"{counts.get('delivered', 0):>9}  "
            f"{counts.get('failed', 0):>6}  "
            f"{counts.get('pending', 0):>7}  "
            f"{counts.get('exhausted', 0):>9}"
        )
    return 0


# ---------------------------------------------------------------------------
# coord outbox tail
# ---------------------------------------------------------------------------


def _run_tail(args: argparse.Namespace) -> int:
    """Show the N most recent outbox rows, oldest-first within the slice.

    'Oldest first within the slice' means we fetch the N newest rows
    (ORDER BY created_at DESC LIMIT N) then reverse so the bottom of
    the printout is the most recent row. This matches ``tail``'s
    convention and puts the latest action at the operator's cursor.
    """
    path = _db_path()
    _ensure_db(path)
    n = int(args.n)
    if n <= 0:
        print("-n must be positive", file=sys.stderr)
        return 1

    try:
        with _connect(path) as conn:
            cur = conn.execute(
                "SELECT id, status, event_type, created_at, last_error, "
                "retry_count, next_attempt_at FROM webhook_outbox "
                "ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
                (n,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    # ``rows`` is newest-first because that's the only ORDER BY a
    # LIMIT can use. Reverse for oldest-first display within the slice.
    rows.reverse()
    now = datetime.now(UTC)

    if args.json:
        payload = []
        for r in rows:
            created = _parse_iso(r.get("created_at"))
            age = int((now - created).total_seconds()) if created else None
            payload.append(
                {
                    "id": r["id"],
                    "status": r["status"],
                    "event_type": r["event_type"],
                    "created_at": r["created_at"],
                    "event_age_seconds": age,
                    "retry_count": r["retry_count"],
                    "next_attempt_at": r["next_attempt_at"],
                    "last_error": r["last_error"],
                }
            )
        print(json.dumps(payload, sort_keys=True))
        return 0

    if not rows:
        print("No webhook outbox rows.")
        return 0

    for r in rows:
        created = _parse_iso(r.get("created_at"))
        age_str = (
            _format_age_seconds(int((now - created).total_seconds()))
            if created
            else "?"
        )
        last_error = r.get("last_error") or ""
        if last_error and len(last_error) > 60:
            last_error = last_error[:57] + "..."
        err_part = f"  err={last_error}" if last_error else ""
        print(
            f"{r['created_at']}  {str(r['status']):<10}  "
            f"{str(r['event_type']):<24}  age={age_str}{err_part}"
        )
    return 0


# ---------------------------------------------------------------------------
# coord outbox retry
# ---------------------------------------------------------------------------


def _retry_statuses(args: argparse.Namespace) -> list[str] | None:
    """Resolve which statuses ``retry`` should act on.

    Returns ``None`` on conflicting flags so the caller can exit with
    code 1 (operator error). ``--all`` covers the two failure states
    (failed + exhausted); pending rows already have a scheduled attempt
    so 'retrying' them is a no-op concept and we leave them alone.
    """
    selected = [name for name, flag in (
        ("failed", args.failed),
        ("exhausted", args.exhausted),
        ("all", args.all),
    ) if flag]
    if len(selected) > 1:
        print(
            "--failed, --exhausted, and --all are mutually exclusive",
            file=sys.stderr,
        )
        return None
    if args.all:
        return ["failed", "exhausted"]
    if args.exhausted:
        return ["exhausted"]
    return ["failed"]


def _run_retry(args: argparse.Namespace) -> int:
    """Reset ``retry_count`` and ``next_attempt_at`` for the selected rows.

    Flips ``status`` back to ``pending`` so the delivery loop picks
    them up on its next sweep. ``last_error`` is cleared too: leaving
    a stale error on a row we just reset would confuse the next
    operator who runs ``coord outbox tail`` looking for current
    failures.
    """
    path = _db_path()
    _ensure_db(path)
    statuses = _retry_statuses(args)
    if statuses is None:
        return 1

    placeholders = ",".join(["?"] * len(statuses))
    now = _utcnow()
    try:
        with _connect(path) as conn:
            cur = conn.execute(
                f"SELECT COUNT(*) FROM webhook_outbox WHERE status IN ({placeholders})",
                statuses,
            )
            count = int(cur.fetchone()[0])
            if args.dry_run:
                print(
                    f"DRY RUN: would reset {count} row(s) "
                    f"(statuses={','.join(statuses)})."
                )
                return 0
            if count:
                conn.execute(
                    f"UPDATE webhook_outbox SET status='pending', "
                    f"retry_count=0, next_attempt_at=?, last_error=NULL "
                    f"WHERE status IN ({placeholders})",
                    [now, *statuses],
                )
                conn.commit()
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    print(f"Reset {count} row(s) (statuses={','.join(statuses)}).")
    return 0


# ---------------------------------------------------------------------------
# coord outbox purge
# ---------------------------------------------------------------------------


def _purge_statuses(args: argparse.Namespace) -> list[str] | None:
    """Resolve which statuses ``purge`` should DELETE.

    Mutually exclusive: ``--delivered`` (the default), ``--exhausted``,
    or ``--all-terminal`` (both). Pending and failed rows are
    deliberately not purgeable -- they are still live state and
    deleting them would lose work.
    """
    selected = [name for name, flag in (
        ("delivered", args.delivered),
        ("exhausted", args.exhausted),
        ("all-terminal", args.all_terminal),
    ) if flag]
    if len(selected) > 1:
        print(
            "--delivered, --exhausted, and --all-terminal are mutually exclusive",
            file=sys.stderr,
        )
        return None
    if args.all_terminal:
        return ["delivered", "exhausted"]
    if args.exhausted:
        return ["exhausted"]
    return ["delivered"]


def _run_purge(args: argparse.Namespace) -> int:
    """DELETE outbox rows in the selected terminal status set."""
    path = _db_path()
    _ensure_db(path)
    statuses = _purge_statuses(args)
    if statuses is None:
        return 1

    placeholders = ",".join(["?"] * len(statuses))
    try:
        with _connect(path) as conn:
            cur = conn.execute(
                f"SELECT COUNT(*) FROM webhook_outbox WHERE status IN ({placeholders})",
                statuses,
            )
            count = int(cur.fetchone()[0])
            if args.dry_run:
                print(
                    f"DRY RUN: would remove {count} row(s) "
                    f"(statuses={','.join(statuses)})."
                )
                return 0
            if count:
                conn.execute(
                    f"DELETE FROM webhook_outbox WHERE status IN ({placeholders})",
                    statuses,
                )
                conn.commit()
    except sqlite3.Error as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2

    print(f"Removed {count} row(s) (statuses={','.join(statuses)}).")
    return 0


# ---------------------------------------------------------------------------
# dispatch + parser registration
# ---------------------------------------------------------------------------


def run_outbox(args: argparse.Namespace) -> int:
    """Top-level dispatch for ``coord outbox``.

    The action is set by the nested subparser; if it is missing the
    argparse layer has already errored out, so we treat the empty case
    as an internal misconfiguration rather than a user-facing bad
    invocation.
    """
    action = getattr(args, "outbox_action", None)
    if action == "stats":
        return _run_stats(args)
    if action == "tail":
        return _run_tail(args)
    if action == "retry":
        return _run_retry(args)
    if action == "purge":
        return _run_purge(args)
    print("Unknown outbox subcommand", file=sys.stderr)
    return 1


def add_outbox_subparser(sub: argparse._SubParsersAction) -> None:
    """Wire ``coord outbox`` and its four nested subcommands onto ``sub``.

    Kept as a registration helper so ``cli.build_parser`` stays the
    single place subcommand registration lives. The cli_outbox module
    owns the flag surface, the cli module owns ordering and the help
    banner.
    """
    outbox = sub.add_parser(
        "outbox",
        help="Inspect and manage the webhook delivery outbox",
        description=(
            "Operator commands for the v0.27 webhook delivery outbox. All "
            "subcommands run against the local database (COORD_DATABASE_PATH) "
            "and do not require a running coord service."
        ),
    )
    nested = outbox.add_subparsers(dest="outbox_action", required=True)

    stats = nested.add_parser(
        "stats",
        help="Per-event-type counts (delivered/failed/pending/exhausted)",
        description=(
            "Per-event-type delivery counts for a rolling window keyed off "
            "the row's created_at timestamp."
        ),
    )
    stats.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Rolling window size in hours (default: 24)",
    )
    stats.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table",
    )
    stats.set_defaults(func=run_outbox)

    tail = nested.add_parser(
        "tail",
        help="Show the most recent outbox rows",
        description=(
            "Show the N most recent outbox rows, oldest-first within the "
            "slice so the latest action lands at the bottom of the printout."
        ),
    )
    tail.add_argument(
        "-n",
        type=int,
        default=20,
        help="Number of rows to show (default: 20)",
    )
    tail.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of one row per line",
    )
    tail.set_defaults(func=run_outbox)

    retry = nested.add_parser(
        "retry",
        help="Reset retry_count + next_attempt_at on selected rows",
        description=(
            "Reset retry_count to 0 and next_attempt_at to now for selected "
            "rows so the delivery loop picks them up on its next sweep. "
            "Defaults to --failed."
        ),
    )
    retry.add_argument(
        "--failed",
        action="store_true",
        help="Reset rows in the 'failed' state (default)",
    )
    retry.add_argument(
        "--exhausted",
        action="store_true",
        help="Reset rows in the 'exhausted' state",
    )
    retry.add_argument(
        "--all",
        action="store_true",
        help="Reset both 'failed' and 'exhausted' rows",
    )
    retry.add_argument(
        "--dry-run",
        action="store_true",
        help="Print how many rows would be reset without writing anything",
    )
    retry.set_defaults(func=run_outbox)

    purge = nested.add_parser(
        "purge",
        help="DELETE rows in terminal states",
        description=(
            "DELETE outbox rows in terminal status. Defaults to --delivered. "
            "Use --all-terminal to also remove --exhausted rows in one pass."
        ),
    )
    purge.add_argument(
        "--delivered",
        action="store_true",
        help="Delete rows in the 'delivered' state (default)",
    )
    purge.add_argument(
        "--exhausted",
        action="store_true",
        help="Delete rows in the 'exhausted' state",
    )
    purge.add_argument(
        "--all-terminal",
        dest="all_terminal",
        action="store_true",
        help="Delete both 'delivered' and 'exhausted' rows in one pass",
    )
    purge.add_argument(
        "--dry-run",
        action="store_true",
        help="Print how many rows would be removed without writing anything",
    )
    purge.set_defaults(func=run_outbox)

    outbox.set_defaults(func=run_outbox)
