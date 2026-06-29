"""Tests for the ``coord mcp install`` command.

Each test monkeypatches ``HOME`` to a tmp directory so the tool config
files (``~/.claude.json``, Claude Desktop's per-platform config,
``~/.codex/config.toml``, ``~/.cursor/mcp.json``) land under tmp and are
inspectable. A seeded ``.coordination/local.env`` inside a tmp git work
tree supplies the connection settings, and commands are driven through
``coordination.cli.build_parser`` so the parser surface (flag names,
defaults, choices) is part of what these tests pin down.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from coordination import cli_mcp
from coordination.cli import build_parser


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


def _seed_local_env(repo_root: Path, *, token: str = "tok-abc123") -> None:
    """Write a minimal but complete .coordination/local.env."""
    coord_dir = repo_root / ".coordination"
    coord_dir.mkdir(parents=True, exist_ok=True)
    (coord_dir / "local.env").write_text(
        "COORD_API_URL=https://coord.example.com\n"
        "COORD_SERVICE_URL=https://coord.example.com\n"
        f"COORD_AUTH_TOKEN={token}\n"
        "COORD_REPO_ID=example-org/example-repo\n",
        encoding="utf-8",
    )


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """A tmp git work tree with a seeded local.env."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    _seed_local_env(root)
    return root


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME (and Path.home()) at a fresh tmp directory.

    ``Path.home()`` consults ``HOME`` on POSIX; we also point ``APPDATA``
    under the same tree so the Windows branch of the desktop-config path
    is covered on any platform.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))
    return fake_home


def _run(repo_root: Path, *extra: str) -> int:
    """Invoke ``coord mcp install --root <repo_root> <extra...>``."""
    parser = build_parser()
    args = parser.parse_args(["mcp", "install", "--root", str(repo_root), *extra])
    return args.func(args)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# per-tool: create from scratch
# ---------------------------------------------------------------------------


def test_claude_code_created_from_scratch(repo_root: Path, home: Path) -> None:
    rc = _run(repo_root, "--tool", "claude-code")
    assert rc == 0
    cfg = _read_json(home / ".claude.json")
    server = cfg["mcpServers"]["coord"]
    assert server["command"] == "coord-mcp"
    assert server["args"] == []
    assert server["env"] == {
        "COORD_API_URL": "https://coord.example.com",
        "COORD_AUTH_TOKEN": "tok-abc123",
        "COORD_REPO_ID": "example-org/example-repo",
    }


def test_cursor_created_from_scratch(repo_root: Path, home: Path) -> None:
    rc = _run(repo_root, "--tool", "cursor")
    assert rc == 0
    cfg = _read_json(home / ".cursor" / "mcp.json")
    server = cfg["mcpServers"]["coord"]
    assert server["command"] == "coord-mcp"
    assert server["env"]["COORD_AUTH_TOKEN"] == "tok-abc123"


def test_claude_desktop_created_from_scratch(repo_root: Path, home: Path) -> None:
    rc = _run(repo_root, "--tool", "claude-desktop")
    assert rc == 0
    path = cli_mcp._claude_desktop_config_path()
    assert path.exists()
    cfg = _read_json(path)
    assert cfg["mcpServers"]["coord"]["command"] == "coord-mcp"


def test_codex_created_from_scratch_parses_as_toml(
    repo_root: Path, home: Path
) -> None:
    rc = _run(repo_root, "--tool", "codex")
    assert rc == 0
    path = home / ".codex" / "config.toml"
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    coord = parsed["mcp_servers"]["coord"]
    assert coord["command"] == "coord-mcp"
    assert coord["args"] == []
    assert coord["env"] == {
        "COORD_API_URL": "https://coord.example.com",
        "COORD_AUTH_TOKEN": "tok-abc123",
        "COORD_REPO_ID": "example-org/example-repo",
    }


# ---------------------------------------------------------------------------
# idempotency: re-run updates in place, no duplicate
# ---------------------------------------------------------------------------


def test_claude_code_idempotent_rerun(repo_root: Path, home: Path) -> None:
    assert _run(repo_root, "--tool", "claude-code") == 0
    first = (home / ".claude.json").read_text(encoding="utf-8")
    assert _run(repo_root, "--tool", "claude-code") == 0
    second = (home / ".claude.json").read_text(encoding="utf-8")
    assert first == second
    cfg = _read_json(home / ".claude.json")
    # Exactly one coord server, on the canonical key.
    coord_keys = [
        name
        for name, value in cfg["mcpServers"].items()
        if name == "coord" or (isinstance(value, dict) and "coord-mcp" in value.get("command", ""))
    ]
    assert coord_keys == ["coord"]


def test_codex_idempotent_rerun(repo_root: Path, home: Path) -> None:
    assert _run(repo_root, "--tool", "codex") == 0
    path = home / ".codex" / "config.toml"
    first = path.read_text(encoding="utf-8")
    assert _run(repo_root, "--tool", "codex") == 0
    second = path.read_text(encoding="utf-8")
    assert first == second
    # The header appears exactly once -- no duplicated table.
    assert second.count("[mcp_servers.coord]") == 1


def test_coexisting_coord_mcp_under_other_key_is_collapsed(
    repo_root: Path, home: Path
) -> None:
    """A pre-existing coord server stored under a non-coord key (but with a
    coord-mcp command) is collapsed onto the single ``coord`` key."""
    path = home / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "coordination": {"command": "/usr/local/bin/coord-mcp", "args": []}
                }
            }
        ),
        encoding="utf-8",
    )
    assert _run(repo_root, "--tool", "claude-code") == 0
    cfg = _read_json(path)
    assert list(cfg["mcpServers"].keys()) == ["coord"]


# ---------------------------------------------------------------------------
# preservation of unrelated servers
# ---------------------------------------------------------------------------


def test_unrelated_json_server_preserved(repo_root: Path, home: Path) -> None:
    path = home / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "someOtherKey": {"keep": "me"},
                "mcpServers": {
                    "github": {"command": "gh-mcp", "args": ["--foo"]},
                },
            }
        ),
        encoding="utf-8",
    )
    assert _run(repo_root, "--tool", "claude-code") == 0
    cfg = _read_json(path)
    assert cfg["someOtherKey"] == {"keep": "me"}
    assert cfg["mcpServers"]["github"] == {"command": "gh-mcp", "args": ["--foo"]}
    assert cfg["mcpServers"]["coord"]["command"] == "coord-mcp"


def test_unrelated_codex_table_preserved(repo_root: Path, home: Path) -> None:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "model = \"gpt-5\"\n"
        "\n"
        "[mcp_servers.github]\n"
        'command = "gh-mcp"\n'
        "args = []\n"
        "\n"
        "[mcp_servers.github.env]\n"
        'GH_TOKEN = "secret"\n',
        encoding="utf-8",
    )
    assert _run(repo_root, "--tool", "codex") == 0
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["github"]["command"] == "gh-mcp"
    assert parsed["mcp_servers"]["github"]["env"] == {"GH_TOKEN": "secret"}
    assert parsed["mcp_servers"]["coord"]["command"] == "coord-mcp"


# ---------------------------------------------------------------------------
# auto-detect
# ---------------------------------------------------------------------------


def test_autodetect_installs_only_present_tools(
    repo_root: Path, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """With no --tool/--all, only detected tools are wired.

    We make detection deterministic: no tool CLI is on PATH, and we create
    only the cursor config dir so cursor is the sole detected tool.
    """
    monkeypatch.setattr(cli_mcp.shutil, "which", lambda _name: None)
    (home / ".cursor").mkdir()

    assert _run(repo_root) == 0
    # Cursor was detected and written; the others were not.
    assert (home / ".cursor" / "mcp.json").exists()
    assert not (home / ".claude.json").exists()
    assert not (home / ".codex" / "config.toml").exists()
    assert not cli_mcp._claude_desktop_config_path().exists()
    out = capsys.readouterr().out
    assert "Detected: cursor" in out
    assert "claude-code" in out  # listed among skipped


def test_autodetect_claude_code_via_cli_on_path(
    repo_root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude-code is detected when the ``claude`` CLI is on PATH even with
    no ~/.claude.json present."""
    monkeypatch.setattr(
        cli_mcp.shutil,
        "which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    assert _run(repo_root) == 0
    assert (home / ".claude.json").exists()


# ---------------------------------------------------------------------------
# dry-run writes nothing
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(repo_root: Path, home: Path, capsys) -> None:
    rc = _run(repo_root, "--all", "--dry-run")
    assert rc == 0
    assert not (home / ".claude.json").exists()
    assert not (home / ".cursor" / "mcp.json").exists()
    assert not (home / ".codex" / "config.toml").exists()
    assert not cli_mcp._claude_desktop_config_path().exists()
    out = capsys.readouterr().out
    assert "DRY RUN: would create" in out


# ---------------------------------------------------------------------------
# missing / blank local.env exits non-zero
# ---------------------------------------------------------------------------


def test_missing_local_env_exits_nonzero(
    tmp_path: Path, home: Path, capsys
) -> None:
    root = tmp_path / "norepo"
    (root / ".git").mkdir(parents=True)
    # No .coordination/local.env at all.
    rc = _run(root, "--tool", "claude-code")
    assert rc == 1
    assert not (home / ".claude.json").exists()
    err = capsys.readouterr().err
    assert "coord init" in err


def test_blank_token_local_env_exits_nonzero(
    tmp_path: Path, home: Path, capsys
) -> None:
    root = tmp_path / "blanktoken"
    coord_dir = root / ".coordination"
    coord_dir.mkdir(parents=True)
    (root / ".git").mkdir()
    (coord_dir / "local.env").write_text(
        "COORD_API_URL=https://coord.example.com\nCOORD_AUTH_TOKEN=\n",
        encoding="utf-8",
    )
    rc = _run(root, "--tool", "claude-code")
    assert rc == 1
    err = capsys.readouterr().err
    assert "coord init" in err


# ---------------------------------------------------------------------------
# invalid existing config is reported, not clobbered
# ---------------------------------------------------------------------------


def test_invalid_json_config_not_clobbered(
    repo_root: Path, home: Path, capsys
) -> None:
    path = home / ".claude.json"
    path.write_text("{ this is not json", encoding="utf-8")
    rc = _run(repo_root, "--tool", "claude-code")
    assert rc == 1
    # Original content untouched.
    assert path.read_text(encoding="utf-8") == "{ this is not json"
    err = capsys.readouterr().err
    assert "not valid JSON" in err


def test_optional_env_keys_omitted_when_absent(
    tmp_path: Path, home: Path
) -> None:
    """Only COORD_AUTH_TOKEN is present -> the embedded env carries just the
    token (no empty COORD_API_URL / COORD_REPO_ID placeholders)."""
    root = tmp_path / "minimal"
    coord_dir = root / ".coordination"
    coord_dir.mkdir(parents=True)
    (root / ".git").mkdir()
    (coord_dir / "local.env").write_text(
        "COORD_AUTH_TOKEN=only-token\n", encoding="utf-8"
    )
    assert _run(root, "--tool", "claude-code") == 0
    cfg = _read_json(home / ".claude.json")
    assert cfg["mcpServers"]["coord"]["env"] == {"COORD_AUTH_TOKEN": "only-token"}
