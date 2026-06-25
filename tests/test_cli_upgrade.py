from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from coordination import cli_init, cli_upgrade


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def _seed_initialised_repo(
    repo: Path,
    tool: str = "claude",
    mode: str = "remote",
    service_url: str = "http://coord.team.local",
    token: str = "preserved-token",
    owners_yaml: str = "areas:\n  src:\n    paths: [\"src/**\"]\n    owners: [team]\n",
) -> None:
    _git_init(repo)
    coord = repo / ".coordination"
    coord.mkdir()
    (coord / "config.toml").write_text(
        f'version = 1\n'
        f'tool = "{tool}"\n'
        f'mode = "{mode}"\n'
        f'service_url = "{service_url}"\n'
        f'ownership_file = ".coordination/owners.yaml"\n'
        f'local_env_file = ".coordination/local.env"\n',
        encoding="utf-8",
    )
    (coord / "local.env").write_text(
        f"COORD_API_URL=http://stale.example\n"
        f"COORD_SERVICE_URL=http://stale.example\n"
        f"COORD_AUTH_TOKEN={token}\n",
        encoding="utf-8",
    )
    (coord / "owners.yaml").write_text(owners_yaml, encoding="utf-8")
    (coord / "hooks").mkdir()
    (coord / "hooks" / "pre-push").write_text(
        "#!/usr/bin/env bash\n# OBSOLETE PLACEHOLDER\nexit 0\n",
        encoding="utf-8",
    )
    (repo / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "hooks" / "pre-push").write_text(
        "#!/usr/bin/env bash\nexec \"$(git rev-parse --show-toplevel)/.coordination/hooks/pre-push\" \"$@\"\n",
        encoding="utf-8",
    )


def _make_args(repo: Path, **overrides):
    class Args:
        pass

    a = Args()
    a.root = str(repo)
    for k, v in overrides.items():
        setattr(a, k, v)
    return a


def test_upgrade_fails_when_repo_not_initialized(tmp_path: Path, capsys) -> None:
    _git_init(tmp_path)
    rc = cli_upgrade.run_upgrade(_make_args(tmp_path))
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "not initialized" in err or "config.toml" in err


def test_upgrade_refreshes_pre_push_hook_content(tmp_path: Path) -> None:
    from coordination.assets import PRE_PUSH_SCRIPT

    _seed_initialised_repo(tmp_path)
    rc = cli_upgrade.run_upgrade(_make_args(tmp_path))
    assert rc == 0
    hook = (tmp_path / ".coordination" / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert hook == PRE_PUSH_SCRIPT
    assert "OBSOLETE PLACEHOLDER" not in hook
    # The .git/hooks/pre-push shim must also migrate from the old
    # show-toplevel form to the worktree-invariant git-common-dir form
    # (issue #28), so already-deployed buggy shims self-heal on upgrade.
    shim = (tmp_path / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert "git rev-parse --git-common-dir" in shim
    assert "rev-parse --show-toplevel)" not in shim


def test_upgrade_runs_from_a_linked_worktree(tmp_path: Path) -> None:
    """coord upgrade must work when run from a LINKED git worktree, where
    ``<root>/.git`` is a file (a gitdir pointer), not a directory.
    _install_hook previously assumed ``<root>/.git/hooks/`` and crashed with
    NotADirectoryError there. The shim must land in the shared common hooks
    dir instead (the v0.35.2 fix)."""
    main = tmp_path / "main"
    main.mkdir()
    _seed_initialised_repo(main)
    # Commit so a worktree can be added and so the linked checkout carries
    # the tracked .coordination/ config -- the scenario where upgrade runs.
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init coord")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", str(linked))
    assert (linked / ".git").is_file()  # gitdir pointer, not a directory

    rc = cli_upgrade.run_upgrade(_make_args(linked))
    assert rc == 0

    # The shim landed in the SHARED common hooks dir (main/.git/hooks),
    # worktree-aware, not under a (nonexistent) linked/.git/hooks.
    shim = (main / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert "git rev-parse --git-common-dir" in shim
    assert not (linked / ".git" / "hooks").exists()


def test_upgrade_preserves_owners_yaml(tmp_path: Path) -> None:
    custom_owners = (
        "# Hand-tuned by the team\n"
        "areas:\n"
        "  services:\n"
        "    paths: [\"services/**\"]\n"
        "    owners: [agent-a]\n"
    )
    _seed_initialised_repo(tmp_path, owners_yaml=custom_owners)
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    after = (tmp_path / ".coordination" / "owners.yaml").read_text(encoding="utf-8")
    assert after == custom_owners


def test_upgrade_preserves_auth_token_in_local_env(tmp_path: Path) -> None:
    _seed_initialised_repo(tmp_path, token="keep-this-token")
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    env = (tmp_path / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert "COORD_AUTH_TOKEN=keep-this-token" in env


def test_upgrade_refreshes_url_in_local_env(tmp_path: Path) -> None:
    # Stale URLs in local.env should be replaced with whatever config.toml says.
    _seed_initialised_repo(tmp_path, service_url="http://coord.new.example")
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    env = (tmp_path / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert "COORD_API_URL=http://coord.new.example" in env
    assert "COORD_SERVICE_URL=http://coord.new.example" in env
    assert "stale.example" not in env


def test_upgrade_refreshes_mcp_json_env_block(tmp_path: Path) -> None:
    """``coord upgrade`` rewrites the tracked .mcp.json template with
    the documented placeholder env block, leaving any foreign
    ``mcpServers`` entries untouched. The real bearer token and
    service URL live exclusively in .coordination/local.env, which
    the MCP wrapper resolves at startup. See
    coordination/cli_init.py:PLACEHOLDER_* for the constants and
    tests/test_deploy_overlay.py for the public-fork leak guard."""
    from coordination.cli_init import (
        PLACEHOLDER_API_URL,
        PLACEHOLDER_AUTH_TOKEN,
        PLACEHOLDER_REPO_ID,
    )

    _seed_initialised_repo(
        tmp_path,
        tool="claude",
        service_url="http://coord.new.example",
        token="t-123",
    )
    # Simulate an existing .mcp.json with an unrelated server preserved alongside.
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({
        "mcpServers": {
            "coord": {"command": "coord-mcp", "args": [], "env": {"COORD_API_URL": "http://stale", "COORD_AUTH_TOKEN": "old"}},
            "other": {"command": "other-mcp", "args": []},
        },
    }, indent=2) + "\n", encoding="utf-8")
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    data = json.loads(mcp.read_text(encoding="utf-8"))
    env = data["mcpServers"]["coord"]["env"]
    assert env["COORD_API_URL"] == PLACEHOLDER_API_URL
    assert env["COORD_AUTH_TOKEN"] == PLACEHOLDER_AUTH_TOKEN
    assert env["COORD_REPO_ID"] == PLACEHOLDER_REPO_ID
    # Foreign server entries must be left alone.
    assert data["mcpServers"]["other"]["command"] == "other-mcp"


def test_upgrade_never_writes_real_token_into_tracked_mcp_json(tmp_path: Path) -> None:
    """Regression for the v0.28.1/v0.28.2 leak: upgrade used to read the
    real bearer token out of local.env and write it straight into the
    tracked .mcp.json template, which then tripped the
    test_deploy_overlay.py placeholder guard on the next CI run. Even
    when local.env carries a real 64-hex-char token, the tracked
    template must end up with the documented `set-me` placeholder."""
    real_token = "a" * 64  # shape-matches the COORD_AUTH_TOKEN format
    _seed_initialised_repo(
        tmp_path,
        tool="claude",
        service_url="https://prod-coord.example.com",
        token=real_token,
    )

    cli_upgrade.run_upgrade(_make_args(tmp_path))

    # The tracked template must have placeholders, never the real values.
    mcp = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    env = mcp["mcpServers"]["coord"]["env"]
    assert env["COORD_AUTH_TOKEN"] == "set-me", env
    assert env["COORD_API_URL"] == "http://127.0.0.1:8080", env
    assert real_token not in (tmp_path / ".mcp.json").read_text(encoding="utf-8")

    # ...but local.env must still carry the real token + URL so the
    # MCP wrapper can override the placeholders at startup.
    local_env = (tmp_path / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert f"COORD_AUTH_TOKEN={real_token}" in local_env
    assert "COORD_API_URL=https://prod-coord.example.com" in local_env


def test_upgrade_never_writes_real_token_into_codex_config(tmp_path: Path) -> None:
    """Same regression as above, but for the Codex-flavoured tracked
    template (.codex/config.toml)."""
    real_token = "b" * 64
    _seed_initialised_repo(
        tmp_path,
        tool="codex",
        service_url="https://prod-coord.example.com",
        token=real_token,
    )

    cli_upgrade.run_upgrade(_make_args(tmp_path))

    cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'COORD_AUTH_TOKEN = "set-me"' in cfg
    assert 'COORD_API_URL = "http://127.0.0.1:8080"' in cfg
    assert real_token not in cfg
    assert "prod-coord.example.com" not in cfg

    local_env = (tmp_path / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert f"COORD_AUTH_TOKEN={real_token}" in local_env
    assert "COORD_API_URL=https://prod-coord.example.com" in local_env


def test_upgrade_preserves_claude_md_content_outside_managed_block(tmp_path: Path) -> None:
    _seed_initialised_repo(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "# Project notes\n\n"
        "Important context for this repo.\n\n"
        "<!-- coord:begin -->\n"
        "# stale managed content\n"
        "<!-- coord:end -->\n\n"
        "More project notes below.\n",
        encoding="utf-8",
    )
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    after = claude_md.read_text(encoding="utf-8")
    assert "Important context for this repo." in after
    assert "More project notes below." in after
    # Stale managed content must have been replaced by the latest snippet.
    assert "stale managed content" not in after
    assert "Coordination protocol" in after


def test_upgrade_migrates_gitignore_markers_to_hash_style(tmp_path: Path) -> None:
    """Repos initialised by pre-v0.6.1 coord have HTML-comment markers
    in `.gitignore` (not valid gitignore comment syntax). Running
    `coord upgrade` must migrate the block to `# coord:begin` /
    `# coord:end` markers in place, without losing or duplicating the
    entry."""
    _seed_initialised_repo(tmp_path, tool="claude")
    # Overwrite the seeded .gitignore (if any) with the legacy HTML
    # marker style we want to migrate from.
    (tmp_path / ".gitignore").write_text(
        "node_modules/\n\n"
        "<!-- coord:begin -->\n"
        ".coordination/local.env\n"
        "<!-- coord:end -->\n",
        encoding="utf-8",
    )

    cli_upgrade.run_upgrade(_make_args(tmp_path))

    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "# coord:begin" in text
    assert "# coord:end" in text
    assert "<!-- coord:begin -->" not in text
    assert "<!-- coord:end -->" not in text
    # v0.8.1 widened the entry from .coordination/local.env to the
    # whole directory; the migration rewrites the entry too.
    assert "/.coordination/" in text
    assert text.count("/.coordination/") == 1
    assert "node_modules/" in text


def test_upgrade_codex_refreshes_codex_config(tmp_path: Path) -> None:
    _seed_initialised_repo(tmp_path, tool="codex")
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    codex_cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert '[mcp_servers.coord]' in codex_cfg
    assert 'command = "coord-mcp"' in codex_cfg


def test_upgrade_codex_writes_env_block_so_mcp_can_reach_service(
    tmp_path: Path,
) -> None:
    """Regression: codex spawns coord-mcp without sourcing .coordination/local.env,
    so the MCP child has no COORD_API_URL and silently dials 127.0.0.1:8080 with
    "All connection attempts failed". The codex MCP config MUST embed an [env]
    block, the same way Claude's .mcp.json does. As of the v0.28.3 hotfix the
    embedded block carries the documented placeholder values; the MCP wrapper
    resolves them against ``.coordination/local.env`` at startup."""
    _seed_initialised_repo(
        tmp_path, tool="codex", service_url="http://coord.example.lan",
        token="prod-token-xyz",
    )
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    codex_cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.coord.env]" in codex_cfg
    assert 'COORD_API_URL = "http://127.0.0.1:8080"' in codex_cfg
    assert 'COORD_AUTH_TOKEN = "set-me"' in codex_cfg
    assert "prod-token-xyz" not in codex_cfg
    assert "coord.example.lan" not in codex_cfg


def test_upgrade_refreshes_all_tool_configs_present_on_disk(
    tmp_path: Path,
) -> None:
    """Multi-tool repos: a project that wired both claude and codex (by
    running ``coord init`` twice) must have BOTH ``.mcp.json`` and
    ``.codex/config.toml`` refreshed by a single ``coord upgrade``.

    The pre-fix behaviour read ``tool = "codex"`` from
    ``.coordination/config.toml`` and silently skipped ``.mcp.json``,
    leaving stale URLs/tokens in the claude config every time the user
    rotated the cluster or bumped a managed snippet."""
    _seed_initialised_repo(
        tmp_path, tool="codex", service_url="http://coord.fresh.lan",
        token="rotated-token-123",
    )

    # Pre-existing claude artefact from an earlier ``coord init --tool claude``
    # run. The seeded URL/token here are intentionally stale; upgrade should
    # bring them in line with the current local.env state.
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "coord": {
                        "command": "coord-mcp",
                        "args": [],
                        "env": {
                            "COORD_API_URL": "http://stale.example",
                            "COORD_AUTH_TOKEN": "stale-token",
                        },
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (tmp_path / "CLAUDE.md").write_text(
        "# Project notes\n\n<!-- coord:begin -->\nstale managed content\n<!-- coord:end -->\n",
        encoding="utf-8",
    )

    cli_upgrade.run_upgrade(_make_args(tmp_path))

    # Both tool configs now hold the documented placeholder env block.
    # The real URL and token live exclusively in .coordination/local.env
    # which the MCP wrapper resolves at startup. The stale strings from
    # the pre-existing claude artefact must be gone -- not replaced by
    # the real token (that would leak it back into a tracked file).
    codex_cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'COORD_API_URL = "http://127.0.0.1:8080"' in codex_cfg
    assert 'COORD_AUTH_TOKEN = "set-me"' in codex_cfg
    assert "rotated-token-123" not in codex_cfg

    mcp_data = json.loads(mcp.read_text(encoding="utf-8"))
    env = mcp_data["mcpServers"]["coord"]["env"]
    assert env["COORD_API_URL"] == "http://127.0.0.1:8080"
    assert env["COORD_AUTH_TOKEN"] == "set-me"
    assert "stale-token" not in mcp.read_text(encoding="utf-8")
    assert "rotated-token-123" not in mcp.read_text(encoding="utf-8")

    # CLAUDE.md managed block was rewritten too (no stale content).
    assert "stale" not in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Coordination protocol" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    # And the real, rotated token must still flow into local.env so the
    # MCP wrapper can resolve the placeholder at startup.
    local_env = (tmp_path / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert "COORD_AUTH_TOKEN=rotated-token-123" in local_env
    assert "COORD_API_URL=http://coord.fresh.lan" in local_env


def test_upgrade_does_not_create_tool_configs_that_were_never_initialised(
    tmp_path: Path,
) -> None:
    """Upgrade refreshes what's there. It must not silently bring up a
    claude config (or AGENTS.md / CLAUDE.md) just because the package
    knows how to. Otherwise running upgrade on a codex-only repo would
    spam unwanted artefacts."""
    _seed_initialised_repo(tmp_path, tool="codex")

    cli_upgrade.run_upgrade(_make_args(tmp_path))

    assert not (tmp_path / ".mcp.json").exists(), (
        "upgrade must not synthesize a claude config when the user never "
        "ran `coord init --tool claude`"
    )
    assert not (tmp_path / "CLAUDE.md").exists(), (
        "CLAUDE.md should only get a managed block when claude is actually wired"
    )
    assert not (tmp_path / ".cursor").exists(), (
        "cursor config must not be synthesized either"
    )


def test_upgrade_codex_embeds_placeholder_repo_id(tmp_path: Path) -> None:
    """The codex env block carries a placeholder COORD_REPO_ID so the
    MCP wrapper has a key to override at startup from local.env. The
    real repo_id is detected at init time and written into local.env;
    the tracked template must NEVER carry the real value (which would
    leak the customer/organisation identifier into a public fork)."""
    _seed_initialised_repo(tmp_path, tool="codex")
    # Add a real-looking origin so _detect_repo_id resolves; the real
    # repo_id must NOT end up in the tracked template.
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example-org/widgets.git"],
        cwd=tmp_path, check=True,
    )
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    codex_cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'COORD_REPO_ID = "example-org/example-repo"' in codex_cfg
    assert "widgets" not in codex_cfg


def test_upgrade_skips_owners_yaml_even_if_force_flag_unused(tmp_path: Path) -> None:
    # The whole point of upgrade is that owners.yaml is never touched, so
    # there should be no --force flag plumbed through.
    custom = "areas:\n  x:\n    paths: ['x/**']\n    owners: [keep-me]\n"
    _seed_initialised_repo(tmp_path, owners_yaml=custom)
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    after = (tmp_path / ".coordination" / "owners.yaml").read_text(encoding="utf-8")
    assert after == custom


def test_upgrade_adds_mcp_json_to_prettierignore_when_prettier_present(
    tmp_path: Path,
) -> None:
    """A Prettier-using repo gets coord's generated .mcp.json exempted
    from format checks on upgrade -- the fix for onboarding reddening a
    repo's `prettier --check` CI."""
    _seed_initialised_repo(tmp_path)
    (tmp_path / ".prettierrc").write_text("{}\n", encoding="utf-8")
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    pi = tmp_path / ".prettierignore"
    assert pi.exists()
    assert ".mcp.json" in pi.read_text(encoding="utf-8")


def test_upgrade_no_prettierignore_when_repo_has_no_prettier(
    tmp_path: Path,
) -> None:
    """A repo with no Prettier config must not get a stray
    .prettierignore created by upgrade."""
    _seed_initialised_repo(tmp_path)
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    assert not (tmp_path / ".prettierignore").exists()


def test_warn_tracked_wiring_fires_off_default_branch(tmp_path: Path, capsys) -> None:
    """On a feature branch, the warning names the tracked wiring files
    (so they can be staged alone) and excludes gitignored .coordination/
    entries. This is the guard against coord wiring leaking into a PR."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feature")

    cli_init._warn_tracked_wiring_commit_risk(
        tmp_path,
        [".mcp.json", "CLAUDE.md (managed block)", ".coordination/config.toml"],
    )
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert ".mcp.json" in err
    assert "CLAUDE.md" in err
    # gitignored / local paths are excluded from the warning
    assert ".coordination/config.toml" not in err
    assert "feature" in err and "main" in err


def test_warn_tracked_wiring_silent_on_default_branch_clean_index(
    tmp_path: Path, capsys
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    cli_init._warn_tracked_wiring_commit_risk(tmp_path, [".mcp.json"])
    assert "WARNING" not in capsys.readouterr().err


def test_warn_tracked_wiring_fires_with_staged_changes(
    tmp_path: Path, capsys
) -> None:
    """Even on the default branch, a non-empty index means a commit
    would bundle coord's wiring with unrelated staged work."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "unrelated.txt").write_text("work in progress\n", encoding="utf-8")
    _git(tmp_path, "add", "unrelated.txt")

    cli_init._warn_tracked_wiring_commit_risk(tmp_path, [".mcp.json"])
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "staged" in err.lower()


def test_warn_tracked_wiring_silent_when_only_gitignored(
    tmp_path: Path, capsys
) -> None:
    """If coord only touched .coordination/ (all gitignored), there is
    nothing committable to warn about even on a feature branch."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feature")

    cli_init._warn_tracked_wiring_commit_risk(
        tmp_path, [".coordination/config.toml", ".git/hooks/pre-push (shim)"]
    )
    assert "WARNING" not in capsys.readouterr().err


def test_upgrade_gitignores_machine_configs(tmp_path: Path) -> None:
    """v0.32: upgrade adds coord's generated machine configs to the
    managed .gitignore block alongside /.coordination/."""
    _seed_initialised_repo(tmp_path)
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "/.coordination/" in gi
    assert ".mcp.json" in gi
    assert ".cursor/mcp.json" in gi
    assert ".codex/config.toml" in gi


def test_upgrade_untracks_already_tracked_mcp_json(tmp_path: Path) -> None:
    """A .mcp.json committed by an older coord version must be untracked
    on upgrade (git rm --cached) -- gitignore alone does not untrack --
    while the file stays on disk for local use."""
    _seed_initialised_repo(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    _git(tmp_path, "add", ".mcp.json")
    _git(tmp_path, "commit", "-qm", "older coord: track .mcp.json")
    # sanity: it is tracked before upgrade
    pre = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "--error-unmatch", ".mcp.json"],
        capture_output=True,
    )
    assert pre.returncode == 0

    cli_upgrade.run_upgrade(_make_args(tmp_path))

    post = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "--error-unmatch", ".mcp.json"],
        capture_output=True,
    )
    assert post.returncode != 0, "expected .mcp.json to be untracked"
    assert (tmp_path / ".mcp.json").exists(), "file must remain on disk"


def test_warn_skips_gitignored_machine_config(tmp_path: Path, capsys) -> None:
    """Once .mcp.json is gitignored, the commit-risk warning must not
    flag it (git will not commit an ignored file), but still flags the
    tracked protocol docs."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / ".gitignore").write_text(".mcp.json\n", encoding="utf-8")

    cli_init._warn_tracked_wiring_commit_risk(
        tmp_path, [".mcp.json", "CLAUDE.md (managed block)"]
    )
    err = capsys.readouterr().err
    assert ".mcp.json" not in err
    assert "CLAUDE.md" in err
