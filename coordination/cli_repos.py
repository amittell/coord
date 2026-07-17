"""``coord repos`` -- the repos registry (v20).

A repo used to "exist" only as a free string on claims and tokens, so a
typo'd ``--repo`` minted a scoped token whose claims landed under a phantom
scope nobody watches. The registry makes repo onboarding explicit:
``coord init`` registers the repo it wires, ``coord repos register`` is the
manual path, and ``coord tokens create --repo`` refuses unregistered ids
unless ``--register`` is passed.

Mirrors ``cli_tokens``: local-DB operations intended to run where the
service's database lives (the server pod in hosted mode).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from coordination.cli_tokens import (
    _database,
    _db_path,
    _refuse_implicit_remote_mode_local_mutation,
)
from coordination.repo_id import InvalidRepoId, normalize_repo_id


async def _register(args: argparse.Namespace) -> int:
    try:
        repo = normalize_repo_id(args.repo_id)
    except InvalidRepoId as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if _refuse_implicit_remote_mode_local_mutation(args, "repos register"):
        return 1
    db = _database(_db_path(args))
    created = await db.register_repo(repo, registered_by=args.by)
    print(f"{'Registered' if created else 'Already registered:'} {repo}")
    return 0


async def _list(args: argparse.Namespace) -> int:
    db = _database(_db_path(args))
    rows = await db.list_registered_repos()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No repos registered.")
        return 0
    width = max(len(r["repo_id"]) for r in rows)
    for r in rows:
        by = r.get("registered_by") or "-"
        print(f"{r['repo_id']:<{width}}  {r['registered_at']}  by {by}")
    return 0


def run_repos(args: argparse.Namespace) -> int:
    action = getattr(args, "repos_action", None)
    if action == "register":
        return asyncio.run(_register(args))
    if action == "list":
        return asyncio.run(_list(args))
    print("usage: coord repos {register,list} ...", file=sys.stderr)
    return 2


def add_repos_subparser(sub: argparse._SubParsersAction) -> None:
    repos = sub.add_parser(
        "repos",
        help="Manage the repos registry (scoped-token typo protection, v20)",
        description=(
            "Register the repo ids this coord service coordinates. "
            "Scoped-token minting refuses unregistered ids so a typo'd "
            "--repo cannot strand claims under a phantom scope."
        ),
    )
    nested = repos.add_subparsers(dest="repos_action")

    register = nested.add_parser(
        "register",
        help="Register a repo id",
    )
    register.add_argument("repo_id", help="Repo id, e.g. amittell/coord")
    register.add_argument(
        "--by",
        help="Operator/engineer recorded as the registrar",
    )
    register.add_argument(
        "--database-path",
        metavar="PATH",
        help="Explicit SQLite database path (see coord tokens --database-path)",
    )
    register.add_argument(
        "--local-db",
        action="store_true",
        help="Allow the local-DB operation from a remote-mode repo",
    )
    register.set_defaults(func=run_repos)

    listp = nested.add_parser(
        "list",
        help="List registered repos",
    )
    listp.add_argument(
        "--database-path",
        metavar="PATH",
        help="Explicit SQLite database path",
    )
    listp.add_argument(
        "--local-db",
        action="store_true",
        help="Allow the local-DB operation from a remote-mode repo",
    )
    listp.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON",
    )
    listp.set_defaults(func=run_repos)
