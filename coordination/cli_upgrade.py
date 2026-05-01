"""Refresh managed coordination artefacts in an already-initialised repo.

This is the upgrade path teams should run after pulling a new version of
the `coord` package. It rewrites the pre-push hook (managed script and
`.git/hooks/pre-push` shim), the tool-specific MCP config, the CLAUDE.md
/ AGENTS.md / cursor managed block, and the URL fields of `local.env`.
It deliberately leaves these alone:

- `.coordination/config.toml` -- captures the user's tool/mode/service_url
  picks; we read from it but never overwrite it.
- `.coordination/owners.yaml` -- hand-tuned by the team after init.
- `COORD_AUTH_TOKEN` inside `local.env` -- preserved verbatim.

Reusing this command after every `git pull` of the `coord` package means
hook-level fixes propagate to every repo without an `init --force` (which
would also reset config.toml) or manual asset copying.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coordination.assets import AGENTS_SNIPPET, CLAUDE_SNIPPET, CURSOR_RULE
from coordination.cli_init import (
    _install_hook,
    _resolve_root,
    _update_codex_config,
    _update_mcp_json,
)
from coordination.cli_shared import ensure_managed_block
from coordination.repo_config import RepoConfig


def _read_existing_token(local_env: Path) -> str:
    if not local_env.exists():
        return ""
    for line in local_env.read_text(encoding="utf-8").splitlines():
        if line.startswith("COORD_AUTH_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


def _rewrite_local_env(
    local_env: Path,
    service_url: str,
    token: str,
    repo_id: str | None = None,
) -> None:
    local_env.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"COORD_API_URL={service_url}\n"
        f"COORD_SERVICE_URL={service_url}\n"
        f"COORD_AUTH_TOKEN={token}\n"
    )
    if repo_id:
        body += f"COORD_REPO_ID={repo_id}\n"
    local_env.write_text(body, encoding="utf-8")


def run_upgrade(args) -> int:
    repo_root = _resolve_root(getattr(args, "root", None))
    if repo_root is None:
        if getattr(args, "root", None) is None:
            print("Not inside a git repository.", file=sys.stderr)
        return 1

    config_path = repo_root / ".coordination" / "config.toml"
    if not config_path.exists():
        print(
            f"Repo not initialized: missing {config_path.relative_to(repo_root)}. "
            "Run 'coord init' first.",
            file=sys.stderr,
        )
        return 1

    config = RepoConfig.load(config_path)
    local_env = repo_root / ".coordination" / "local.env"
    token = _read_existing_token(local_env)
    # Backfill: pre-v0.3 configs have no repo_id. Re-detect from git remote
    # so existing repos pick up a repo identifier on their next upgrade.
    from coordination.cli_init import _detect_repo_id

    repo_id = config.repo_id or _detect_repo_id(repo_root)

    written: list[str] = []

    _rewrite_local_env(local_env, config.service_url, token, repo_id=repo_id)
    written.append(".coordination/local.env (URL refreshed, token preserved)")

    if config.tool == "claude":
        _update_mcp_json(
            repo_root / ".mcp.json", config.service_url, token, repo_id=repo_id
        )
        ensure_managed_block(repo_root / "CLAUDE.md", CLAUDE_SNIPPET)
        written.extend([".mcp.json", "CLAUDE.md (managed block)"])
    elif config.tool == "codex":
        _update_codex_config(repo_root / ".codex" / "config.toml")
        ensure_managed_block(repo_root / "AGENTS.md", AGENTS_SNIPPET)
        written.extend([".codex/config.toml", "AGENTS.md (managed block)"])
    else:
        _update_mcp_json(
            repo_root / ".cursor" / "mcp.json",
            config.service_url,
            token,
            repo_id=repo_id,
        )
        ensure_managed_block(
            repo_root / ".cursor" / "rules" / "coordination.mdc", CURSOR_RULE
        )
        written.extend([".cursor/mcp.json", ".cursor/rules/coordination.mdc"])

    # Always force-refresh the hook shim. The user expects 'upgrade' to
    # propagate hook fixes; init's force-gate doesn't apply here.
    _install_hook(repo_root, force=True)
    written.append(".coordination/hooks/pre-push")
    written.append(".git/hooks/pre-push (shim)")

    print(f"Upgraded coordination artefacts in {repo_root}")
    print(f"Tool: {config.tool}    Mode: {config.mode}    Service: {config.service_url}")
    print("")
    print("Refreshed:")
    for path in written:
        print(f"  {path}")
    print("")
    print("Preserved:")
    print("  .coordination/config.toml")
    print("  .coordination/owners.yaml")
    print("  COORD_AUTH_TOKEN")
    return 0
