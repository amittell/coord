"""``coord tokens`` operator commands for v0.29 per-engineer bearer tokens.

The shared ``COORD_AUTH_TOKEN`` works for everyone with the secret,
which is fine for a single-operator setup but breaks down as soon as
multiple agents (each on different machines) need distinguishable
credentials or staged revocation. v0.29 introduces per-engineer
tokens stored as sha256 hashes in the ``engineer_tokens`` table
(schema migration v14). This module is the operator surface for
managing those tokens from the command line.

Subcommands:

* ``coord tokens create <engineer> [--description "..."]
  [--expires-in 30d]``
  Mint a fresh ``coordt_`` + 64-hex token, hash it with sha256, and
  insert the hash into ``engineer_tokens``. The raw token is printed
  exactly once -- there is no way to recover it later. The operator
  drops it into ``.coordination/local.env`` on the engineer's
  machine. ``--expires-in`` (v0.29.4) sets an expiry as a duration
  (m/h/d/w); absent means the token never expires.

* ``coord tokens list [--engineer X] [--include-revoked]``
  Print the table of issued tokens (id, engineer, created_at,
  last_used_at, status, expiry, request activity, description).
  The raw token and its hash are never returned -- the audit view
  is metadata only.

* ``coord tokens rotate <token-id> [--grace 24h] [--expires-in 30d]``
  v0.29.4: mint a successor token for the same engineer and put the
  old token into a grace window (default 24h; ``--grace 0h`` cuts it
  off immediately). The old token keeps authenticating until the
  window closes, so the engineer can swap credentials without a
  hard outage. ``--expires-in`` applies to the successor only.

* ``coord tokens revoke <token-id>``
  Mark a token as revoked. The row stays for audit; lookup returns
  None so the bearer stops authenticating immediately on the next
  request.

Output shape mirrors ``coord engineers``: a human-readable table
by default, ``--json`` for unattended runs.

Exit codes follow the project convention:
    0 -- success
    1 -- operator error (bad flag combo, unknown token id)
    2 -- database / environment error (missing DB file)
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import sys
from pathlib import Path

from coordination.cli_shared import parse_duration
from coordination.config import get_settings
from coordination.db import Database

# Generation, hashing, and status derivation moved to
# ``coordination.tokens`` in v0.29.5 so the dashboard token panel can
# share them without importing argparse machinery. TOKEN_PREFIX is
# re-exported here because callers (and tests) treat cli_tokens as
# the operator-facing namespace.
from coordination.tokens import (
    TOKEN_PREFIX,
    derive_token_status,
    generate_raw_token,
    sha256_token,
)

__all__ = ["TOKEN_PREFIX", "add_tokens_subparser", "run_tokens"]


def _db_path() -> Path:
    return Path(get_settings().database_path)


def _iso_z(dt: datetime) -> str:
    """Render a datetime in the project's canonical timestamp form:
    second precision, UTC, ``Z`` suffix -- the same shape the db layer
    writes, so CLI output and stored rows compare byte-for-byte."""
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _create(args: argparse.Namespace) -> int:
    expires_at: datetime | None = None
    if args.expires_in:
        # Parse before touching the database so a typo'd duration
        # never leaves a half-configured token behind.
        try:
            expires_at = datetime.now(UTC) + parse_duration(args.expires_in)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    db = Database(_db_path())
    raw = generate_raw_token()
    token_id = await db.create_engineer_token(
        args.engineer,
        sha256_token(raw),
        description=args.description,
        expires_at=expires_at,
        repo=args.repo,
    )
    expires_str = _iso_z(expires_at) if expires_at else None
    if args.json:
        out = {
            "id": token_id,
            "engineer": args.engineer,
            "description": args.description,
            "expires_at": expires_str,
            "repo": args.repo,
            "token": raw,
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"Token id:     {token_id}")
        print(f"Engineer:     {args.engineer}")
        if args.description:
            print(f"Description:  {args.description}")
        if expires_str:
            print(f"Expires:      {expires_str}")
        print(f"Repo scope:   {args.repo or 'all repos (unscoped)'}")
        print("")
        print(f"  {raw}")
        print("")
        print("Paste the line above into the engineer's")
        print(".coordination/local.env as COORD_AUTH_TOKEN=... .")
        print("This token will NOT be shown again -- coord stores")
        print("only the sha256 hash. If lost, revoke and reissue.")
    return 0


async def _rotate(args: argparse.Namespace) -> int:
    # Both durations are validated before any database work; --grace
    # allows zero (immediate cutoff) but --expires-in does not (a
    # successor that is born expired is operator error).
    try:
        grace_delta = parse_duration(args.grace, allow_zero=True)
        expires_delta = (
            parse_duration(args.expires_in) if args.expires_in else None
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    now = datetime.now(UTC)
    expires_at = now + expires_delta if expires_delta else None
    db = Database(_db_path())
    raw = generate_raw_token()
    result = await db.rotate_engineer_token(
        args.token_id,
        sha256_token(raw),
        grace_until=now + grace_delta,
        expires_at=expires_at,
        now=now,
    )
    if not result["ok"]:
        # Each refusal names the next move so the operator never has
        # to dig into the rotation rules to recover.
        messages = {
            "not_found": f"No token with id {args.token_id}.",
            "revoked": (
                f"Token {args.token_id} is revoked; "
                "create a fresh token instead."
            ),
            "expired": (
                f"Token {args.token_id} has expired; "
                "create a fresh token instead."
            ),
            "already_rotated": (
                f"Token {args.token_id} was already rotated; rotate its "
                "successor instead (coord tokens list shows the chain "
                "via rotated_from)."
            ),
        }
        print(messages[result["error"]], file=sys.stderr)
        return 1
    expires_str = _iso_z(expires_at) if expires_at else None
    if args.json:
        out = {
            "token": raw,
            "token_id": result["new_token_id"],
            "engineer": result["engineer"],
            "rotated_from": args.token_id,
            "grace_until": result["grace_until"],
            "expires_at": expires_str,
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"Token id:     {result['new_token_id']}")
        print(f"Engineer:     {result['engineer']}")
        print(f"Rotated from: {args.token_id}")
        if expires_str:
            print(f"Expires:      {expires_str}")
        print(f"Old token stops working at {result['grace_until']}.")
        print("")
        print(f"  {raw}")
        print("")
        print("Paste the line above into the engineer's")
        print(".coordination/local.env as COORD_AUTH_TOKEN=... .")
        print("This token will NOT be shown again -- coord stores")
        print("only the sha256 hash. If lost, revoke and reissue.")
    return 0


async def _list(args: argparse.Namespace) -> int:
    db = Database(_db_path())
    rows = await db.list_engineer_tokens(
        engineer=args.engineer,
        include_revoked=args.include_revoked,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        scope = (
            f" for engineer {args.engineer!r}" if args.engineer else ""
        )
        print(f"No tokens issued{scope}.")
        return 0

    # Compact table: id (truncated), engineer, lifecycle status,
    # expiry, last-used activity, source IP, description. The status
    # word is derived here (the db stores the raw timestamps) so the
    # table answers "can this token authenticate right now?" without
    # the operator doing date math.
    now = datetime.now(UTC)
    print(
        f"{'TOKEN ID':<10} {'ENGINEER':<30} {'STATUS':<14} "
        f"{'EXPIRES':<22} {'LAST USED':<22} {'REQS':>6} "
        f"{'LAST IP':<16} DESCRIPTION"
    )
    for r in rows:
        tid = r["id"][:8]
        eng = (r["engineer"] or "")[:30]
        status = derive_token_status(r, now=now)
        expires = (r.get("expires_at") or "-")[:22]
        last = (r["last_used_at"] or "-")[:22]
        reqs = int(r.get("request_count") or 0)
        ip = (r.get("last_source_ip") or "-")[:16]
        desc = r["description"] or ""
        print(
            f"{tid:<10} {eng:<30} {status:<14} "
            f"{expires:<22} {last:<22} {reqs:>6} "
            f"{ip:<16} {desc}"
        )
    return 0


async def _revoke(args: argparse.Namespace) -> int:
    db = Database(_db_path())
    revoked = await db.revoke_engineer_token(args.token_id)
    if revoked:
        print(f"Revoked {args.token_id}.")
        return 0
    # Idempotent: not an error if it was already revoked, but a
    # genuinely unknown id should be loud so the operator notices
    # a typo before assuming the token is dead.
    rows = await db.list_engineer_tokens(include_revoked=True)
    if any(r["id"] == args.token_id for r in rows):
        print(f"{args.token_id} was already revoked.")
        return 0
    print(f"Unknown token id: {args.token_id}", file=sys.stderr)
    return 1


def run_tokens(args: argparse.Namespace) -> int:
    action = getattr(args, "tokens_action", None)
    if action == "create":
        return asyncio.run(_create(args))
    if action == "list":
        return asyncio.run(_list(args))
    if action == "rotate":
        return asyncio.run(_rotate(args))
    if action == "revoke":
        return asyncio.run(_revoke(args))
    print(
        "Use 'coord tokens create / list / rotate / revoke'. See "
        "'coord tokens -h' for the full surface.",
        file=sys.stderr,
    )
    return 1


def add_tokens_subparser(sub: argparse._SubParsersAction) -> None:
    """Wire ``coord tokens`` and its nested actions.

    Mirrors ``cli_engineers``: top-level ``tokens`` parser plus three
    nested actions. The ``cli`` module stays the single registration
    site so ``coord -h`` lists the commands in a stable order.
    """
    tokens = sub.add_parser(
        "tokens",
        help="Manage per-engineer bearer tokens (v0.29+)",
        description=(
            "Per-engineer bearer tokens replace the legacy single "
            "shared COORD_AUTH_TOKEN once every engineer has been "
            "migrated. Until then both auth paths coexist; set "
            "COORD_REQUIRE_PER_ENGINEER_TOKEN=true to reject the "
            "shared token cluster-wide."
        ),
    )
    nested = tokens.add_subparsers(dest="tokens_action", required=True)

    create = nested.add_parser(
        "create",
        help="Issue a new per-engineer bearer token",
        description=(
            "Mint a fresh coordt_... bearer token bound to the given "
            "engineer. The raw token is printed exactly once; only "
            "its sha256 hash is stored. Paste the output into the "
            "engineer's .coordination/local.env as "
            "COORD_AUTH_TOKEN=... ."
        ),
    )
    create.add_argument(
        "engineer",
        help="Engineer id (e.g. alex/claude/main) the token is issued to",
    )
    create.add_argument(
        "--description",
        help="Optional human label (e.g. 'work laptop')",
    )
    create.add_argument(
        "--expires-in",
        metavar="DURATION",
        help=(
            "Optional expiry as <number><unit> with unit m/h/d/w "
            "(e.g. 30d, 12h). Omit for a token that never expires"
        ),
    )
    create.add_argument(
        "--repo",
        metavar="REPO_ID",
        help=(
            "Bind the token to a repo id (e.g. amittell/coord) so the server "
            "enforces repo scope from auth. Omit for an unscoped (operator) "
            "token that sees all repos"
        ),
    )
    create.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable text block",
    )
    create.set_defaults(func=run_tokens)

    listc = nested.add_parser(
        "list",
        help="List issued tokens (audit view; no raw values)",
        description=(
            "Print the metadata for issued tokens: id, engineer, "
            "lifecycle status (active / rotating / grace-elapsed / "
            "expired / revoked), expiry, request activity, "
            "description. The raw token and its hash are never "
            "surfaced by this command -- there is no recovery path "
            "for a lost token, only revoke + reissue."
        ),
    )
    listc.add_argument(
        "--engineer",
        help="Only show tokens for this engineer id",
    )
    listc.add_argument(
        "--include-revoked",
        action="store_true",
        help="Include revoked tokens in the output (default: live only)",
    )
    listc.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable table",
    )
    listc.set_defaults(func=run_tokens)

    rotate = nested.add_parser(
        "rotate",
        help="Rotate a token: mint a successor, grace-expire the old one",
        description=(
            "Mint a successor coordt_... token for the same engineer "
            "and put the old token into a grace window (default 24h). "
            "The old token keeps authenticating until the window "
            "closes, so the engineer can swap credentials without an "
            "outage; --grace 0h cuts it off immediately. --expires-in "
            "sets the successor's expiry and never touches the old "
            "token. The new raw token is printed exactly once."
        ),
    )
    rotate.add_argument(
        "token_id",
        help="Token id from 'coord tokens list'",
    )
    rotate.add_argument(
        "--grace",
        default="24h",
        metavar="DURATION",
        help=(
            "How long the old token keeps working, as <number><unit> "
            "with unit m/h/d/w (default: 24h; 0h cuts over immediately)"
        ),
    )
    rotate.add_argument(
        "--expires-in",
        metavar="DURATION",
        help=(
            "Optional expiry for the SUCCESSOR token as <number><unit> "
            "with unit m/h/d/w. Omit for a successor that never expires"
        ),
    )
    rotate.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable text block",
    )
    rotate.set_defaults(func=run_tokens)

    revoke = nested.add_parser(
        "revoke",
        help="Revoke a token by id (idempotent)",
        description=(
            "Mark a token as revoked. The row stays for audit; "
            "lookup_engineer_token returns None so the bearer "
            "stops authenticating on the next request. Re-running "
            "revoke on an already-revoked token is a no-op."
        ),
    )
    revoke.add_argument(
        "token_id",
        help="Token id from 'coord tokens list'",
    )
    revoke.set_defaults(func=run_tokens)

    tokens.set_defaults(func=run_tokens)
