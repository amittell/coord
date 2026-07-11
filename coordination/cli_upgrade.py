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
    _untrack_machine_configs,
    _update_codex_config,
    _update_mcp_json,
    _warn_tracked_wiring_commit_risk,
    _MACHINE_CONFIGS,
)
from coordination.cli_shared import (
    ensure_gitignore_entries,
    ensure_managed_block,
    ensure_prettierignore_entries,
)
from coordination.envfile import read_env_file, update_env_file
from coordination.repo_config import RepoConfig


def _read_existing_token(local_env: Path) -> str:
    # Shared parser (coordination.envfile): tolerates a leading `export `,
    # indentation and quotes, and applies last-assignment-wins -- exactly
    # how the MCP wrapper, coord doctor, and the pre-push hook's inert-data
    # loader read this file. The old first-match ``startswith`` reader
    # blanked `export`-prefixed tokens (rewriting the file with an empty
    # COORD_AUTH_TOKEN) and silently regressed a rotated token appended
    # below a stale one.
    return read_env_file(local_env).get("COORD_AUTH_TOKEN", "")


def _rewrite_local_env(
    local_env: Path,
    service_url: str,
    token: str,
    repo_id: str | None = None,
) -> None:
    """Refresh the coord-managed keys of local.env in place.

    Unmanaged keys (COORD_USER, COORD_BRANCH, COORD_REPO_ROOT, and every
    other key the MCP wrapper bootstraps from this file -- see
    ``coordination.mcp_server._LOCAL_ENV_KEYS``) plus comments and blank
    lines are preserved verbatim; only the URL/token/repo-id lines coord
    owns are rewritten.
    """
    updates = {
        "COORD_API_URL": service_url,
        "COORD_SERVICE_URL": service_url,
        "COORD_AUTH_TOKEN": token,
    }
    if repo_id:
        updates["COORD_REPO_ID"] = repo_id
    update_env_file(local_env, updates)


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
    if local_env.exists() and not token:
        print(
            "Warning: no COORD_AUTH_TOKEN found in .coordination/local.env; "
            "the refreshed file keeps it empty. Paste a token there if "
            "clients should authenticate.",
            file=sys.stderr,
        )
    # Backfill: pre-v0.3 configs have no repo_id. Re-detect from git remote
    # so existing repos pick up a repo identifier on their next upgrade.
    from coordination.cli_init import _detect_repo_id

    repo_id = config.repo_id or _detect_repo_id(repo_root)

    written: list[str] = []

    _rewrite_local_env(local_env, config.service_url, token, repo_id=repo_id)
    written.append(".coordination/local.env (URL refreshed, token preserved)")

    # Multi-tool aware: refresh every tool config that already exists on
    # disk, regardless of which tool is recorded in config.toml. The
    # `tool = "..."` field captures the most recent `coord init --tool X`
    # invocation, but a repo can have several tools wired (running init
    # twice with different --tool values is supported and additive). The
    # pre-fix behaviour single-dispatched on config.tool and silently
    # skipped the others, so a token rotation or URL change refreshed
    # one tool's config and left the rest stale.
    #
    # We also refresh the tool named by config.tool even when its config
    # file is missing -- this is the recovery case where a user has
    # accidentally deleted .mcp.json / .codex/config.toml etc and runs
    # coord upgrade to put it back. Without this fallback, upgrade would
    # silently no-op when given a half-erased repo.
    # The tracked-template writers below intentionally do NOT receive the
    # real service_url or token; they always emit placeholder values that
    # the MCP wrapper resolves against .coordination/local.env at startup.
    # Writing the real token here would commit a credential to the repo
    # if .mcp.json is tracked, which it is by design (it's the public-safe
    # template). See coordination/cli_init.py:PLACEHOLDER_* and
    # tests/test_deploy_overlay.py for the leak guard. The unused `token`
    # and `repo_id` locals in this function are still required by
    # _rewrite_local_env above so the real config keeps flowing into
    # the gitignored local.env file.
    # Coord-generated machine config files (JSON) that a Prettier-using
    # repo would otherwise format-check and fail on. Collected across
    # every wired tool and exempted via .prettierignore once below. This
    # is the fix for the class of CI breakage where an onboarded repo's
    # `prettier --check` rejected coord's 2-space .mcp.json.
    prettier_targets: list[str] = []
    wiring_failed = False
    mcp_json = repo_root / ".mcp.json"
    if mcp_json.exists() or config.tool == "claude":
        if _update_mcp_json(mcp_json):
            written.append(".mcp.json")
            prettier_targets.append(".mcp.json")
        else:
            wiring_failed = True
        ensure_managed_block(repo_root / "CLAUDE.md", CLAUDE_SNIPPET)
        written.append("CLAUDE.md (managed block)")

    codex_cfg = repo_root / ".codex" / "config.toml"
    if codex_cfg.exists() or config.tool == "codex":
        _update_codex_config(codex_cfg)
        ensure_managed_block(repo_root / "AGENTS.md", AGENTS_SNIPPET)
        written.extend([".codex/config.toml", "AGENTS.md (managed block)"])

    cursor_cfg = repo_root / ".cursor" / "mcp.json"
    if cursor_cfg.exists() or config.tool == "cursor":
        if _update_mcp_json(cursor_cfg):
            written.append(".cursor/mcp.json")
            prettier_targets.append(".cursor/mcp.json")
        else:
            wiring_failed = True
        ensure_managed_block(
            repo_root / ".cursor" / "rules" / "coordination.mdc", CURSOR_RULE
        )
        written.append(".cursor/rules/coordination.mdc")

    if ensure_prettierignore_entries(repo_root, prettier_targets):
        written.append(".prettierignore (managed block)")

    # Always force-refresh the hook shim. The user expects 'upgrade' to
    # propagate hook fixes; init's force-gate doesn't apply here.
    _install_hook(repo_root, force=True)
    written.append(".coordination/hooks/pre-push")
    written.append(".git/hooks/pre-push (shim)")

    # Refresh the .gitignore managed block so marker-style and entry
    # changes from the coord package make their way into existing repos.
    # Pre-v0.6.1 wrote HTML-comment markers (invalid gitignore syntax);
    # pre-v0.8.1 wrote only the narrow .coordination/local.env rule
    # which left the rest of the dir vulnerable to `git stash -u`
    # cycles dropping the hook implementation. The wide rule plus the
    # in-place marker migration makes the upgrade idempotent.
    ensure_gitignore_entries(repo_root, ["/.coordination/", *_MACHINE_CONFIGS])
    written.append(".gitignore (managed block)")

    # v0.32: machine configs (.mcp.json etc.) are no longer tracked.
    # Untrack any that an older coord version committed so the gitignore
    # rule takes effect; the files stay on disk for local use.
    for rel in _untrack_machine_configs(repo_root):
        written.append(f"{rel} (untracked from git)")

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
    _warn_tracked_wiring_commit_risk(repo_root, written)
    if wiring_failed:
        print(
            "One or more MCP configs could not be refreshed (see warnings "
            "above). Fix or remove the file(s) and re-run 'coord upgrade'.",
            file=sys.stderr,
        )
        return 1
    return 0
