from __future__ import annotations

import argparse

from coordination import BANNER, __version__
from coordination.cli_doctor import run_doctor
from coordination.cli_engineers import add_engineers_subparser
from coordination.cli_init import run_init
from coordination.cli_mcp import add_mcp_subparser
from coordination.cli_ops import run_claims, run_release, run_status, run_stop
from coordination.cli_outbox import add_outbox_subparser
from coordination.cli_tokens import add_tokens_subparser
from coordination.cli_start import run_start
from coordination.cli_update_notice import maybe_emit_update_notice
from coordination.cli_upgrade import run_upgrade
from coordination.main import run as run_api


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coord",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Multi-agent coordination service.\n"
            "\n"
            "Common workflow:\n"
            "  coord start       Run the service locally (preferred entry point)\n"
            "  coord stop        Stop a background service started by 'coord start'\n"
            "  coord init        Wire the current repo for Claude Code, Codex, or Cursor\n"
            "  coord mcp install Register the coord MCP server into your AI coding tools\n"
            "  coord upgrade     Refresh managed artefacts after pulling a new coord version\n"
            "  coord doctor      Verify repo wiring and service connectivity\n"
            "  coord status      Print health of the configured service\n"
            "  coord claims      List active claims\n"
            "  coord release     Release a claim by id\n"
            "  coord outbox      Inspect / retry / purge the webhook outbox\n"
            "  coord engineers   Per-engineer housekeeping (list / release stale engineers)\n"
            "\n"
            "Advanced entry points:\n"
            "  coord-api         Raw uvicorn runner for the FastAPI app (coordination.main:app)\n"
            "  coord-mcp         MCP stdio bridge to the HTTP API (used by editors/CLIs)\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=(
            f"{BANNER}\n"
            f"  multi-agent coordination service\n"
            f"  coord {__version__}"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser(
        "start",
        help="Start a local coordination service",
        description="Start the coordination HTTP API on this machine.",
    )
    start.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 to listen on all interfaces)",
    )
    start.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    start.add_argument(
        "--background",
        action="store_true",
        help="Run in the background and return once /readyz responds",
    )
    start.add_argument(
        "--open-dashboard",
        action="store_true",
        help="Open the dashboard URL in the default browser after startup",
    )
    start.add_argument(
        "--json",
        action="store_true",
        help="Print startup state as a single JSON object instead of human-readable lines",
    )
    start.set_defaults(func=run_start)

    init = sub.add_parser(
        "init",
        help="Initialize coordination in the current repo",
        description=(
            "Wire the current repository so agent tools can talk to a coordination service. "
            "Run this inside the application repo you want to coordinate (not inside the "
            "coordination service repo)."
        ),
    )
    init.add_argument(
        "--tool",
        choices=["claude", "codex", "cursor"],
        help="Primary coding tool to wire up (prompts if omitted and --yes is not set)",
    )
    init.add_argument(
        "--mode",
        choices=["local", "remote"],
        help="local: talk to http://127.0.0.1:8080; remote: use --service-url",
    )
    init.add_argument(
        "--service-url",
        help="Base URL of the coordination service (required for --mode remote)",
    )
    init.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive prompts and accept defaults",
    )
    init.add_argument(
        "--no-hook",
        action="store_true",
        help="Skip installing the pre-push git hook",
    )
    init.add_argument(
        "--no-owners",
        action="store_true",
        help="Skip writing the starter .coordination/owners.yaml",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing managed files (owners.yaml, pre-push hook shim)",
    )
    init.add_argument(
        "--root",
        help=(
            "Base path where .coordination/ is written (absolute, or relative to cwd). "
            "Must exist and live inside a git work tree. Defaults to the enclosing "
            "git repo root. Useful for monorepos where each service should own its "
            "own coordination config."
        ),
    )
    init.set_defaults(func=run_init)

    upgrade = sub.add_parser(
        "upgrade",
        help="Refresh managed coordination artefacts in this repo",
        description=(
            "Re-render the pre-push hook, MCP config, and managed CLAUDE.md / "
            "AGENTS.md / cursor block from the latest coord package, preserving "
            ".coordination/config.toml, .coordination/owners.yaml, and the "
            "existing COORD_AUTH_TOKEN. Run this in each repo after pulling a "
            "new version of the coord package so hook-level fixes propagate."
        ),
    )
    upgrade.add_argument(
        "--root",
        help=(
            "Base path where .coordination/ lives (absolute or relative to cwd). "
            "Defaults to the enclosing git repo root."
        ),
    )
    upgrade.set_defaults(func=run_upgrade)

    doctor = sub.add_parser(
        "doctor",
        help="Check repo and service health",
        description=(
            "Run a series of checks against the current repo's coordination wiring and "
            "verify the configured service is reachable with a working auth token."
        ),
    )
    doctor.set_defaults(func=run_doctor)

    stop = sub.add_parser(
        "stop",
        help="Stop a background coordination service",
        description=(
            "Stop the background coordination service spawned by 'coord start --background'. "
            "Reads the PID file under ~/.coord/ and sends SIGTERM (then SIGKILL after 5s)."
        ),
    )
    stop.set_defaults(func=run_stop)

    status = sub.add_parser(
        "status",
        help="Show service health and active claim count",
        description=(
            "Resolve the configured service (repo .coordination/ or env vars) and print a "
            "compact health summary including /readyz, /meta, and the active claim count."
        ),
    )
    status.set_defaults(func=run_status)

    claims = sub.add_parser(
        "claims",
        help="List active claims",
        description=(
            "List claims from the configured service. Scopes to the local repo "
            "(config.toml repo_id / COORD_REPO_ID) by default; use --repo <id> to "
            "target another repo or --all-repos for the operator view across every "
            "repo. Filter by --engineer, include expired claims with --all, or emit "
            "raw JSON with --json."
        ),
    )
    claims.add_argument("--engineer", help="Only show claims for this engineer id")
    claims.add_argument(
        "--all",
        action="store_true",
        help="Include expired claims (active_only=false)",
    )
    # --repo and --all-repos are logically exclusive: one narrows to a single
    # repo, the other widens to every repo. Enforce it in argparse so the CLI
    # fails fast with a clear error instead of silently preferring one.
    repo_scope = claims.add_mutually_exclusive_group()
    repo_scope.add_argument(
        "--repo",
        help="Scope to a specific repo id (overrides the local repo default)",
    )
    repo_scope.add_argument(
        "--all-repos",
        action="store_true",
        help="Show claims across every repo (operator view; skips the local-repo scope)",
    )
    claims.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON instead of a human-readable list",
    )
    claims.set_defaults(func=run_claims)

    release = sub.add_parser(
        "release",
        help="Release a claim by id",
        description=(
            "Release a claim by id. --engineer is required to prevent accidental cross-"
            "engineer releases."
        ),
    )
    release.add_argument("claim_id", help="Claim id to release")
    release.add_argument(
        "--engineer",
        help="Engineer id that owns the claim (required)",
    )
    release.set_defaults(func=run_release)

    add_outbox_subparser(sub)
    add_engineers_subparser(sub)
    add_tokens_subparser(sub)
    add_mcp_subparser(sub)

    internal = sub.add_parser("_serve")
    internal.set_defaults(func=lambda _: _run_internal_server())
    return parser


def _run_internal_server() -> int:
    run_api()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = args.func(args)
    # Run after the user's command finishes so a slow check can't make
    # the command itself feel slow. The notice is throttled and silent
    # on failure -- see coordination.cli_update_notice for skip rules.
    try:
        maybe_emit_update_notice(client_version=__version__, subcommand=args.command)
    except Exception:
        # Belt and braces: maybe_emit_update_notice already swallows
        # everything, but a never-crash-the-CLI guard is cheap.
        pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
