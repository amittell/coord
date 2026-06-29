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
import os
import sys
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
    # Path.home() consults USERPROFILE on Windows, not HOME, so redirect it
    # too -- otherwise the config paths resolve to the real home there.
    monkeypatch.setenv("USERPROFILE", str(fake_home))
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


# ---------------------------------------------------------------------------
# Codex TOML: non-canonical existing coord tables are still collapsed, and
# anything the surgery cannot safely converge fails closed (never corrupts)
# ---------------------------------------------------------------------------


def test_codex_collapses_quoted_coord_table(repo_root: Path, home: Path) -> None:
    """An existing coord table written with a quoted key
    (``[mcp_servers."coord"]``) is recognised, removed, and replaced by the
    single canonical table -- not left in place beside a duplicate."""
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'model = "gpt-5"\n'
        "\n"
        '[mcp_servers."coord"]\n'
        'command = "coord-mcp"\n'
        "args = []\n"
        "\n"
        '[mcp_servers."coord".env]\n'
        'COORD_AUTH_TOKEN = "stale-token"\n',
        encoding="utf-8",
    )
    assert _run(repo_root, "--tool", "codex") == 0
    text = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)  # must still be valid TOML
    # Exactly one coord table, carrying the fresh token; model preserved.
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["coord"]["env"]["COORD_AUTH_TOKEN"] == "tok-abc123"
    assert '[mcp_servers."coord"]' not in text
    assert text.count("[mcp_servers.coord]") == 1


def test_codex_collapses_coord_table_with_trailing_comment(
    repo_root: Path, home: Path
) -> None:
    """A coord header carrying a trailing comment
    (``[mcp_servers.coord]  # ...``) is recognised and collapsed."""
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "[mcp_servers.coord]  # installed by hand last week\n"
        'command = "coord-mcp"\n'
        "args = []\n"
        "\n"
        "[mcp_servers.coord.env]\n"
        'COORD_AUTH_TOKEN = "stale-token"\n',
        encoding="utf-8",
    )
    assert _run(repo_root, "--tool", "codex") == 0
    text = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["coord"]["env"]["COORD_AUTH_TOKEN"] == "tok-abc123"
    # Exactly the two canonical coord table headers survive -- the old
    # commented header did not leak a duplicate.
    coord_headers = [
        line.rstrip()
        for line in text.splitlines()
        if (m := cli_mcp._TABLE_HEADER_RE.match(line))
        and cli_mcp._is_coord_table_header(m.group(1), {"coord"})
    ]
    assert coord_headers == ["[mcp_servers.coord]", "[mcp_servers.coord.env]"]


def test_codex_refuses_when_surgery_cannot_converge(
    repo_root: Path, home: Path, capsys
) -> None:
    """A coord entry written as a top-level dotted key (which the block
    surgery cannot remove) would collide with the appended table. Rather
    than emit invalid/duplicate TOML, the writer refuses and leaves the
    original file untouched."""
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    original = 'mcp_servers.coord.command = "coord-mcp"\n'
    path.write_text(original, encoding="utf-8")
    rc = _run(repo_root, "--tool", "codex")
    assert rc == 1
    # File left byte-for-byte untouched.
    assert path.read_text(encoding="utf-8") == original
    err = capsys.readouterr().err
    assert "codex: error" in err


def test_codex_refuses_coord_under_noncanonical_key(
    repo_root: Path, home: Path, capsys
) -> None:
    """A coord server stored under a non-coord key via dotted keys (which the
    block surgery cannot remove) would survive beside the appended canonical
    table -- two coord servers. The writer detects this and refuses, leaving
    the original file untouched."""
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    original = 'mcp_servers.legacy.command = "coord-mcp"\n'
    path.write_text(original, encoding="utf-8")
    rc = _run(repo_root, "--tool", "codex")
    assert rc == 1
    assert path.read_text(encoding="utf-8") == original
    err = capsys.readouterr().err
    assert "another key" in err and "legacy" in err


def test_invalid_codex_toml_not_clobbered(
    repo_root: Path, home: Path, capsys
) -> None:
    """An existing config that is not valid TOML is reported, never
    overwritten."""
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("this is = = not toml\n", encoding="utf-8")
    rc = _run(repo_root, "--tool", "codex")
    assert rc == 1
    assert path.read_text(encoding="utf-8") == "this is = = not toml\n"
    err = capsys.readouterr().err
    assert "not valid TOML" in err


# ---------------------------------------------------------------------------
# auto-detect with nothing installed must error, not no-op to success
# ---------------------------------------------------------------------------


def test_autodetect_no_tools_errors(
    repo_root: Path, home: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """With no --tool/--all and no detectable tool (no CLI on PATH, no config
    dirs), the command exits non-zero with an actionable message rather than
    silently succeeding."""
    monkeypatch.setattr(cli_mcp.shutil, "which", lambda _name: None)
    # Fresh home fixture has no tool config dirs/files, so nothing detects.
    rc = _run(repo_root)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No supported AI coding tools detected" in err
    assert "--tool" in err and "--all" in err
    assert not (home / ".claude.json").exists()


# ---------------------------------------------------------------------------
# malformed JSON shapes are refused, not silently clobbered
# ---------------------------------------------------------------------------


def test_json_non_object_mcpservers_refused(
    repo_root: Path, home: Path, capsys
) -> None:
    """An existing config whose ``mcpServers`` is not an object would be
    silently discarded by a naive writer; instead it is refused so the user
    can inspect their unexpected config."""
    path = home / ".claude.json"
    original = json.dumps({"mcpServers": "oops-not-an-object"})
    path.write_text(original, encoding="utf-8")
    rc = _run(repo_root, "--tool", "claude-code")
    assert rc == 1
    assert path.read_text(encoding="utf-8") == original
    err = capsys.readouterr().err
    assert "non-object" in err


# ---------------------------------------------------------------------------
# coord-mcp detection is exact-basename, not substring
# ---------------------------------------------------------------------------


def test_unrelated_substring_command_preserved(repo_root: Path, home: Path) -> None:
    """A server whose command merely CONTAINS 'coord-mcp' as a substring
    (e.g. 'my-coord-mcp-helper') is unrelated and must be preserved, not
    collapsed onto the coord key."""
    path = home / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "helper": {"command": "my-coord-mcp-helper", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    assert _run(repo_root, "--tool", "claude-code") == 0
    cfg = _read_json(path)
    # The unrelated helper survives; coord is added alongside it.
    assert cfg["mcpServers"]["helper"] == {
        "command": "my-coord-mcp-helper",
        "args": [],
    }
    assert cfg["mcpServers"]["coord"]["command"] == "coord-mcp"


# ---------------------------------------------------------------------------
# token-bearing config files are written owner-only (0600)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="POSIX file modes only",
)
def test_created_config_is_owner_only(repo_root: Path, home: Path) -> None:
    """A freshly written config embeds COORD_AUTH_TOKEN, so it must land
    0600 rather than at the process umask default."""
    assert _run(repo_root, "--tool", "claude-code") == 0
    mode = os.stat(home / ".claude.json").st_mode & 0o777
    assert mode == 0o600
    # Codex's TOML config carries the token too.
    assert _run(repo_root, "--tool", "codex") == 0
    mode_toml = os.stat(home / ".codex" / "config.toml").st_mode & 0o777
    assert mode_toml == 0o600
