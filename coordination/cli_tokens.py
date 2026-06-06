"""``coord tokens`` operator commands for v0.29 per-engineer bearer tokens.

The shared ``COORD_AUTH_TOKEN`` works for everyone with the secret,
which is fine for a single-operator setup but breaks down as soon as
multiple agents (each on different machines) need distinguishable
credentials or staged revocation. v0.29 introduces per-engineer
tokens stored as sha256 hashes in the ``engineer_tokens`` table
(schema migration v14). This module is the operator surface for
managing those tokens from the command line.

Subcommands:

* ``coord tokens create <engineer> [--description "..."]``
  Mint a fresh ``coordt_`` + 64-hex token, hash it with sha256, and
  insert the hash into ``engineer_tokens``. The raw token is printed
  exactly once -- there is no way to recover it later. The operator
  drops it into ``.coordination/local.env`` on the engineer's
  machine.

* ``coord tokens list [--engineer X] [--include-revoked]``
  Print the table of issued tokens (id, engineer, created_at,
  last_used_at, revoked_at, description). The raw token and its
  hash are never returned -- the audit view is metadata only.

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
import hashlib
import json
import secrets
import sys
from pathlib import Path

from coordination.config import get_settings
from coordination.db import Database


# Tokens are sha256-hashed before storage; the raw form below is
# what the operator copies into local.env. Prefix mimics the GitHub
# PAT convention (``ghp_...``) so a leaked token in CI logs or
# clipboards is immediately recognisable as a coord credential and
# can be grepped for during incident response.
TOKEN_PREFIX = "coordt_"


def _db_path() -> Path:
    return Path(get_settings().database_path)


def _generate_raw_token() -> str:
    """A new token is ``coordt_`` + 64 hex chars (~256 bits of
    entropy). ``secrets.token_hex(32)`` is the standard library's
    safe random source; we never use ``random`` for credentials.
    """
    return TOKEN_PREFIX + secrets.token_hex(32)


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _create(args: argparse.Namespace) -> int:
    db = Database(_db_path())
    raw = _generate_raw_token()
    token_id = await db.create_engineer_token(
        args.engineer,
        _sha256_hex(raw),
        description=args.description,
    )
    if args.json:
        out = {
            "id": token_id,
            "engineer": args.engineer,
            "description": args.description,
            "token": raw,
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"Token id:     {token_id}")
        print(f"Engineer:     {args.engineer}")
        if args.description:
            print(f"Description:  {args.description}")
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

    # Compact table: id (truncated), engineer, last used, created,
    # status, description.
    print(
        f"{'TOKEN ID':<10} {'ENGINEER':<30} {'CREATED':<22} "
        f"{'LAST USED':<22} {'STATUS':<10} DESCRIPTION"
    )
    for r in rows:
        tid = r["id"][:8]
        eng = (r["engineer"] or "")[:30]
        created = (r["created_at"] or "-")[:22]
        last = (r["last_used_at"] or "-")[:22]
        status = "revoked" if r["revoked_at"] else "live"
        desc = r["description"] or ""
        print(
            f"{tid:<10} {eng:<30} {created:<22} "
            f"{last:<22} {status:<10} {desc}"
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
    if action == "revoke":
        return asyncio.run(_revoke(args))
    print(
        "Use 'coord tokens create / list / revoke'. See "
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
            "created_at, last_used_at, revoked_at, description. "
            "The raw token and its hash are never surfaced by "
            "this command -- there is no recovery path for a "
            "lost token, only revoke + reissue."
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
