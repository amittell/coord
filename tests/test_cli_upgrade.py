from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordination import cli_upgrade


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
    assert data["mcpServers"]["coord"]["env"]["COORD_API_URL"] == "http://coord.new.example"
    assert data["mcpServers"]["coord"]["env"]["COORD_AUTH_TOKEN"] == "t-123"
    # Foreign server entries must be left alone.
    assert data["mcpServers"]["other"]["command"] == "other-mcp"


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
    block, the same way Claude's .mcp.json does."""
    _seed_initialised_repo(
        tmp_path, tool="codex", service_url="http://coord.example.lan",
        token="prod-token-xyz",
    )
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    codex_cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.coord.env]" in codex_cfg
    assert 'COORD_API_URL = "http://coord.example.lan"' in codex_cfg
    assert 'COORD_AUTH_TOKEN = "prod-token-xyz"' in codex_cfg


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

    # Codex side stays correct.
    codex_cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'COORD_API_URL = "http://coord.fresh.lan"' in codex_cfg
    assert 'COORD_AUTH_TOKEN = "rotated-token-123"' in codex_cfg

    # Claude side ALSO refreshes even though config.toml says tool=codex.
    mcp_data = json.loads(mcp.read_text(encoding="utf-8"))
    env = mcp_data["mcpServers"]["coord"]["env"]
    assert env["COORD_API_URL"] == "http://coord.fresh.lan"
    assert env["COORD_AUTH_TOKEN"] == "rotated-token-123"

    # CLAUDE.md managed block was rewritten too (no stale content).
    assert "stale" not in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Coordination protocol" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


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


def test_upgrade_codex_embeds_repo_id_when_known(tmp_path: Path) -> None:
    """When the repo has a detectable origin, codex's env block should also
    carry COORD_REPO_ID so per-repo claim tagging works without the user
    manually sourcing local.env."""
    _seed_initialised_repo(tmp_path, tool="codex")
    # Add a fake origin so _detect_repo_id resolves.
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:example-org/widgets.git"],
        cwd=tmp_path, check=True,
    )
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    codex_cfg = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'COORD_REPO_ID = "example-org/widgets"' in codex_cfg


def test_upgrade_skips_owners_yaml_even_if_force_flag_unused(tmp_path: Path) -> None:
    # The whole point of upgrade is that owners.yaml is never touched, so
    # there should be no --force flag plumbed through.
    custom = "areas:\n  x:\n    paths: ['x/**']\n    owners: [keep-me]\n"
    _seed_initialised_repo(tmp_path, owners_yaml=custom)
    cli_upgrade.run_upgrade(_make_args(tmp_path))
    after = (tmp_path / ".coordination" / "owners.yaml").read_text(encoding="utf-8")
    assert after == custom
