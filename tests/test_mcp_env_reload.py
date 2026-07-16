"""Hot reload of ``.coordination/local.env`` in the MCP wrapper (v0.47).

MCP servers are long-lived; before this, a token rotation or repo-scope
change did nothing until every agent session restarted (live incident
2026-07-15: a repo-scope rename left running sessions claiming into the old
scope). The wrapper now re-stats the file whenever it is about to use its
config and re-applies it on change -- unless the new version contains a
line that is not blank/comment/KEY=VALUE, in which case the last-good
config is kept.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordination import mcp_server


@pytest.fixture(autouse=True)
def _fresh_reload_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        mcp_server,
        "_env_reload",
        {"path": None, "stamp": None, "applied": {}, "bad_stamp": None},
    )
    for key in mcp_server._LOCAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _seed(repo_root: Path, body: str) -> Path:
    coord_dir = repo_root / ".coordination"
    coord_dir.mkdir(parents=True, exist_ok=True)
    env_file = coord_dir / "local.env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def _bump_mtime(path: Path) -> None:
    """Guarantee a stamp change even on coarse-mtime filesystems."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_reload_applies_changed_token_and_repo(tmp_path: Path) -> None:
    env_file = _seed(
        tmp_path, "COORD_AUTH_TOKEN=old-token\nCOORD_REPO_ID=old/repo\n"
    )
    mcp_server._load_local_env(start=tmp_path)
    assert os.environ["COORD_AUTH_TOKEN"] == "old-token"

    env_file.write_text(
        "COORD_AUTH_TOKEN=new-token\nCOORD_REPO_ID=new/repo\n", encoding="utf-8"
    )
    _bump_mtime(env_file)

    # _repo_id/_headers/_base_url all trigger the reload; use the public seams.
    assert mcp_server._repo_id() == "new/repo"
    assert mcp_server._headers()["Authorization"] == "Bearer new-token"


def test_unchanged_file_is_not_reparsed(tmp_path: Path, monkeypatch) -> None:
    _seed(tmp_path, "COORD_AUTH_TOKEN=tok\n")
    mcp_server._load_local_env(start=tmp_path)

    def boom(_text):  # parse must not run when the stamp is unchanged
        raise AssertionError("parse_env called for unchanged file")

    monkeypatch.setattr("coordination.envfile.parse_env", boom)
    assert mcp_server._headers()["Authorization"] == "Bearer tok"


def test_syntax_error_keeps_last_good_config(tmp_path: Path, capsys) -> None:
    env_file = _seed(tmp_path, "COORD_AUTH_TOKEN=good-token\n")
    mcp_server._load_local_env(start=tmp_path)

    env_file.write_text(
        "COORD_AUTH_TOKEN=new-token\nthis is not an assignment\n",
        encoding="utf-8",
    )
    _bump_mtime(env_file)

    assert mcp_server._headers()["Authorization"] == "Bearer good-token"
    err = capsys.readouterr().err
    assert "NOT reloading" in err
    assert "not an assignment" in err

    # Warn once per bad version, not per call.
    capsys.readouterr()
    mcp_server._headers()
    assert "NOT reloading" not in capsys.readouterr().err


def test_fixing_a_bad_version_recovers(tmp_path: Path) -> None:
    env_file = _seed(tmp_path, "COORD_AUTH_TOKEN=good-token\n")
    mcp_server._load_local_env(start=tmp_path)

    env_file.write_text("BROKEN LINE\n", encoding="utf-8")
    _bump_mtime(env_file)
    assert mcp_server._headers()["Authorization"] == "Bearer good-token"

    env_file.write_text("COORD_AUTH_TOKEN=fixed-token\n", encoding="utf-8")
    _bump_mtime(env_file)
    assert mcp_server._headers()["Authorization"] == "Bearer fixed-token"


def test_explicit_env_still_wins_on_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real value from the shell/.mcp.json must never be clobbered by a
    file reload -- same ownership rule as startup."""
    env_file = _seed(tmp_path, "COORD_AUTH_TOKEN=file-token\n")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "explicit-token")
    mcp_server._load_local_env(start=tmp_path)
    assert os.environ["COORD_AUTH_TOKEN"] == "explicit-token"

    env_file.write_text("COORD_AUTH_TOKEN=file-token-2\n", encoding="utf-8")
    _bump_mtime(env_file)
    assert mcp_server._headers()["Authorization"] == "Bearer explicit-token"


def test_file_owned_value_is_updatable_even_though_set(tmp_path: Path) -> None:
    """The value in os.environ came from the file itself; the reload must
    treat it as ours-to-update, not as an explicit override."""
    env_file = _seed(tmp_path, "COORD_REPO_ID=alexm-was-here/one\n")
    mcp_server._load_local_env(start=tmp_path)
    assert mcp_server._repo_id() == "alexm-was-here/one"

    env_file.write_text("COORD_REPO_ID=alexm-was-here/two\n", encoding="utf-8")
    _bump_mtime(env_file)
    assert mcp_server._repo_id() == "alexm-was-here/two"


def test_vanished_file_keeps_last_good(tmp_path: Path) -> None:
    env_file = _seed(tmp_path, "COORD_AUTH_TOKEN=tok\n")
    mcp_server._load_local_env(start=tmp_path)
    env_file.unlink()
    assert mcp_server._headers()["Authorization"] == "Bearer tok"


def test_invalid_env_lines_definition() -> None:
    good = "# c\n\nexport COORD_AUTH_TOKEN=x\nKEY='quoted'\n"
    assert mcp_server._invalid_env_lines(good) == []
    assert mcp_server._invalid_env_lines("just words\n") == ["just words"]
    assert mcp_server._invalid_env_lines("=nokey\n") == ["=nokey"]
