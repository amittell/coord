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
  [--expires-in 30d] [--repo owner/name] [--local-db]``
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

``rotate`` and ``revoke`` accept either a full token id or an
unambiguous prefix of at least 8 characters -- exactly what
``coord tokens list`` prints in its TOKEN ID column, so the
documented list-copy-revoke flow works without ``--json``.

All four subcommands take ``--database-path`` to target an explicit
SQLite file; ``list``/``rotate``/``revoke`` otherwise use
``COORD_DATABASE_PATH`` / the settings default. In a remote-mode
repo, ``create``/``rotate``/``revoke`` refuse to touch a local
SQLite DB unless the operator signals intent with ``--local-db``,
``--database-path``, or ``COORD_DATABASE_PATH`` -- the remote
service would never see the change.

Output shape mirrors ``coord engineers``: a human-readable table
by default, ``--json`` for unattended runs.

Exit codes follow the project convention:
    0 -- success
    1 -- operator error (bad flag combo, unknown or ambiguous token
         id, refused implicit local-DB operation in a remote-mode
         repo)
    2 -- database / environment error (``rotate``/``revoke`` pointed
         at a missing SQLite DB file). ``list`` treats a missing DB
         as "no tokens issued" without creating an empty file; on
         the Postgres backend there is no local file to check.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
import sys
from pathlib import Path

from coordination.cli_shared import (
    find_repo_root,
    format_repo_scoped_token_create_command,
    parse_duration,
)
from coordination.config import get_settings
from coordination.db import Database, _postgres_url_selected
from coordination.repo_config import RepoConfig
from coordination.repo_id import InvalidRepoId, normalize_repo_id

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


def _db_path(args: argparse.Namespace | None = None) -> Path:
    override = getattr(args, "database_path", None) if args is not None else None
    return Path(override) if override else Path(get_settings().database_path)


def _display_db_path(path: Path) -> Path:
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _load_remote_mode_context() -> tuple[Path, RepoConfig] | None:
    repo_root = find_repo_root()
    if repo_root is None:
        return None
    config_path = repo_root / ".coordination" / "config.toml"
    if not config_path.exists():
        return None
    try:
        config = RepoConfig.load(config_path)
    except Exception:  # noqa: BLE001 - token ops should not die on bad repo config
        return None
    if config.mode != "remote":
        return None
    return repo_root, config


def _local_db_is_explicit(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "local_db", False)
        or getattr(args, "database_path", None)
        or os.environ.get("COORD_DATABASE_PATH")
    )


def _remote_server_create_example(engineer: str, config: RepoConfig) -> str:
    cmd = format_repo_scoped_token_create_command(engineer, config.repo_id)
    return f"kubectl -n coord exec deploy/coord -- {cmd}"


def _refuse_implicit_remote_mode_local_create(args: argparse.Namespace) -> bool:
    """Return True when ``tokens create`` should be blocked.

    In a remote-mode application repo, ``coord tokens create`` can only write a
    local SQLite database. That is useful for an operator intentionally
    pointing at a server-side DB, but disastrous when someone thinks the remote
    service just minted a token. Require an explicit local-DB signal in that
    context.
    """
    if _local_db_is_explicit(args):
        return False
    context = _load_remote_mode_context()
    if context is None:
        return False
    repo_root, config = context
    db_path = _display_db_path(_db_path(args))
    env_path = repo_root / config.local_env_file
    print(
        "Refusing to create a local SQLite token from a remote-mode repo.",
        file=sys.stderr,
    )
    print(
        f"This command would write to {db_path}, but {config.service_url} "
        "will not know that token.",
        file=sys.stderr,
    )
    print(
        "Create the token on the coord server/service instead, for example:",
        file=sys.stderr,
    )
    print(f"  {_remote_server_create_example(args.engineer, config)}", file=sys.stderr)
    print(
        f"Then paste the token into {env_path} as COORD_AUTH_TOKEN=...",
        file=sys.stderr,
    )
    print(
        "Pass --local-db, --database-path, or COORD_DATABASE_PATH only when "
        "you intentionally want a local-only SQLite token.",
        file=sys.stderr,
    )
    return True


def _refuse_implicit_remote_mode_local_mutation(
    args: argparse.Namespace, action: str
) -> bool:
    """Return True when ``tokens rotate``/``tokens revoke`` should be blocked.

    Same trap as :func:`_refuse_implicit_remote_mode_local_create`: in a
    remote-mode application repo these commands can only touch a local
    SQLite database, so a token minted on the coord service is never
    affected -- the operator believes the credential is rotated or dead
    while the service keeps honouring it. Require the same explicit
    local-DB signal before mutating anything.
    """
    if _local_db_is_explicit(args):
        return False
    context = _load_remote_mode_context()
    if context is None:
        return False
    _repo_root, config = context
    db_path = _display_db_path(_db_path(args))
    print(
        f"Refusing to {action} a local SQLite token from a remote-mode repo.",
        file=sys.stderr,
    )
    print(
        f"This command would only touch {db_path}; tokens issued by "
        f"{config.service_url} live in the service's database.",
        file=sys.stderr,
    )
    print(
        f"Run '{action}' on the coord server/service instead, for example:",
        file=sys.stderr,
    )
    print(
        "  kubectl -n coord exec deploy/coord -- "
        f"coord tokens {action} {args.token_id}",
        file=sys.stderr,
    )
    print(
        "Pass --local-db, --database-path, or COORD_DATABASE_PATH only when "
        "you intentionally target a local-only SQLite token.",
        file=sys.stderr,
    )
    return True


def _sqlite_db_missing(path: Path) -> bool:
    """True when the SQLite backend is selected and ``path`` does not
    exist yet. On the Postgres backend (``COORD_DATABASE_URL`` set to a
    postgresql:// DSN) there is no local file by design, so the check is
    inert there. Opening a Database against a missing file would
    silently materialise an empty SQLite DB -- and then report "no
    tokens" / "unknown id" against the wrong store."""
    return _postgres_url_selected() is None and not path.exists()


def _missing_db_error(path: Path) -> None:
    print(
        f"Token database not found: {_display_db_path(path)}",
        file=sys.stderr,
    )
    print(
        "Pass --database-path (or set COORD_DATABASE_PATH) to point at "
        "the coord database; 'coord tokens create' initialises a new one.",
        file=sys.stderr,
    )


# ``coord tokens list`` truncates ids to this many characters in its
# TOKEN ID column; rotate/revoke accept any unambiguous prefix at least
# this long so the printed value round-trips into the mutation commands.
_MIN_ID_PREFIX = 8


async def _resolve_token_id(db: Database, token_id: str) -> str | None:
    """Resolve a full token id or an unambiguous id prefix.

    An exact match always wins. A prefix shorter than
    ``_MIN_ID_PREFIX`` or one matching nothing is returned unchanged so
    the caller's own not-found path names the operator's input. An
    ambiguous prefix prints the candidates and returns None (operator
    error, exit 1) -- guessing between colliding tokens would rotate or
    revoke someone else's credential."""
    rows = await db.list_engineer_tokens(include_revoked=True)
    ids = [r["id"] for r in rows]
    if token_id in ids or len(token_id) < _MIN_ID_PREFIX:
        return token_id
    matches = [i for i in ids if i.startswith(token_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(
            f"Token id prefix {token_id!r} is ambiguous; it matches:",
            file=sys.stderr,
        )
        for match in sorted(matches):
            print(f"  {match}", file=sys.stderr)
        print(
            "Use more characters, or the full id from "
            "'coord tokens list --json'.",
            file=sys.stderr,
        )
        return None
    return token_id


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
    # #61: validate/normalize the repo id up front so a malformed --repo
    # fails with a clear message instead of a deep traceback, and the stored
    # value is canonical.
    try:
        repo = normalize_repo_id(args.repo)
    except InvalidRepoId as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if _refuse_implicit_remote_mode_local_create(args):
        return 1
    db = Database(_db_path(args))
    raw = generate_raw_token()
    token_id = await db.create_engineer_token(
        args.engineer,
        sha256_token(raw),
        description=args.description,
        expires_at=expires_at,
        repo=repo,
    )
    expires_str = _iso_z(expires_at) if expires_at else None
    if args.json:
        out = {
            "id": token_id,
            "engineer": args.engineer,
            "description": args.description,
            "expires_at": expires_str,
            "repo": repo,
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
        print(f"Repo scope:   {repo or 'all repos (unscoped)'}")
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
    if _refuse_implicit_remote_mode_local_mutation(args, "rotate"):
        return 1
    db_path = _db_path(args)
    if _sqlite_db_missing(db_path):
        _missing_db_error(db_path)
        return 2
    now = datetime.now(UTC)
    expires_at = now + expires_delta if expires_delta else None
    db = Database(db_path)
    token_id = await _resolve_token_id(db, args.token_id)
    if token_id is None:
        return 1
    raw = generate_raw_token()
    result = await db.rotate_engineer_token(
        token_id,
        sha256_token(raw),
        grace_until=now + grace_delta,
        expires_at=expires_at,
        now=now,
    )
    if not result["ok"]:
        # Each refusal names the next move so the operator never has
        # to dig into the rotation rules to recover.
        messages = {
            "not_found": f"No token with id {token_id}.",
            "revoked": (
                f"Token {token_id} is revoked; "
                "create a fresh token instead."
            ),
            "expired": (
                f"Token {token_id} has expired; "
                "create a fresh token instead."
            ),
            "already_rotated": (
                f"Token {token_id} was already rotated; rotate its "
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
            "rotated_from": token_id,
            "grace_until": result["grace_until"],
            "expires_at": expires_str,
        }
        print(json.dumps(out, indent=2))
    else:
        print(f"Token id:     {result['new_token_id']}")
        print(f"Engineer:     {result['engineer']}")
        print(f"Rotated from: {token_id}")
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
    db_path = _db_path(args)
    if _sqlite_db_missing(db_path):
        # A fresh install legitimately has no DB yet; report the empty
        # state without materialising an empty SQLite file at whatever
        # (possibly wrong) path this command was aimed at.
        if args.json:
            print(json.dumps([], indent=2))
        else:
            scope = (
                f" for engineer {args.engineer!r}" if args.engineer else ""
            )
            print(f"No tokens issued{scope}.")
        return 0
    db = Database(db_path)
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
    if _refuse_implicit_remote_mode_local_mutation(args, "revoke"):
        return 1
    db_path = _db_path(args)
    if _sqlite_db_missing(db_path):
        _missing_db_error(db_path)
        return 2
    db = Database(db_path)
    token_id = await _resolve_token_id(db, args.token_id)
    if token_id is None:
        return 1
    revoked = await db.revoke_engineer_token(token_id)
    if revoked:
        print(f"Revoked {token_id}.")
        return 0
    # Idempotent: not an error if it was already revoked, but a
    # genuinely unknown id should be loud so the operator notices
    # a typo before assuming the token is dead.
    rows = await db.list_engineer_tokens(include_revoked=True)
    if any(r["id"] == token_id for r in rows):
        print(f"{token_id} was already revoked.")
        return 0
    print(f"Unknown token id: {token_id}", file=sys.stderr)
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
        "--database-path",
        metavar="PATH",
        help=(
            "Explicit SQLite database path to write. Implies an intentional "
            "local DB operation when run from a remote-mode repo"
        ),
    )
    create.add_argument(
        "--local-db",
        action="store_true",
        help=(
            "Allow local SQLite token creation even when the current repo is "
            "configured for a remote coord service"
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
        "--database-path",
        metavar="PATH",
        help=(
            "Explicit SQLite database path to read (e.g. the one a "
            "'coord tokens create --database-path' wrote). Defaults to "
            "COORD_DATABASE_PATH / the settings default"
        ),
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
        help=(
            "Token id from 'coord tokens list': the full id, or an "
            "unambiguous prefix of at least 8 characters (what the "
            "list table prints)"
        ),
    )
    rotate.add_argument(
        "--database-path",
        metavar="PATH",
        help=(
            "Explicit SQLite database path to write. Implies an intentional "
            "local DB operation when run from a remote-mode repo"
        ),
    )
    rotate.add_argument(
        "--local-db",
        action="store_true",
        help=(
            "Allow local SQLite token rotation even when the current repo is "
            "configured for a remote coord service"
        ),
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
        help=(
            "Token id from 'coord tokens list': the full id, or an "
            "unambiguous prefix of at least 8 characters (what the "
            "list table prints)"
        ),
    )
    revoke.add_argument(
        "--database-path",
        metavar="PATH",
        help=(
            "Explicit SQLite database path to write. Implies an intentional "
            "local DB operation when run from a remote-mode repo"
        ),
    )
    revoke.add_argument(
        "--local-db",
        action="store_true",
        help=(
            "Allow local SQLite token revocation even when the current repo "
            "is configured for a remote coord service"
        ),
    )
    revoke.set_defaults(func=run_tokens)

    tokens.set_defaults(func=run_tokens)
