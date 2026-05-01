"""Tests for repo_id detection and persistence in RepoConfig + cli_init."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


from coordination.cli_init import _detect_repo_id
from coordination.repo_config import RepoConfig


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _setup_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "test", cwd=path)


def test_detect_repo_id_from_https_origin(tmp_path: Path) -> None:
    _setup_repo(tmp_path)
    _git("remote", "add", "origin", "https://github.com/amittell/coord.git", cwd=tmp_path)
    assert _detect_repo_id(tmp_path) == "amittell/coord"


def test_detect_repo_id_from_ssh_origin(tmp_path: Path) -> None:
    _setup_repo(tmp_path)
    _git("remote", "add", "origin", "git@github.com:amittell/bastionx.git", cwd=tmp_path)
    assert _detect_repo_id(tmp_path) == "amittell/bastionx"


def test_detect_repo_id_strips_dot_git_suffix(tmp_path: Path) -> None:
    _setup_repo(tmp_path)
    _git("remote", "add", "origin", "https://github.com/foo/bar.git", cwd=tmp_path)
    assert _detect_repo_id(tmp_path) == "foo/bar"


def test_detect_repo_id_falls_back_to_directory_basename(tmp_path: Path) -> None:
    """Repo with no git remote falls back to the directory basename."""
    repo = tmp_path / "my-cool-project"
    _setup_repo(repo)
    # No remote configured at all.
    assert _detect_repo_id(repo) == "my-cool-project"


def test_detect_repo_id_handles_non_git_directory(tmp_path: Path) -> None:
    """Non-git directory falls back to basename without crashing."""
    repo = tmp_path / "plain-dir"
    repo.mkdir()
    assert _detect_repo_id(repo) == "plain-dir"


def test_repoconfig_round_trips_repo_id(tmp_path: Path) -> None:
    cfg = RepoConfig(
        version=1,
        tool="claude",
        mode="remote",
        service_url="http://coord.example",
        ownership_file=".coordination/owners.yaml",
        repo_id="amittell/coord",
    )
    path = tmp_path / "config.toml"
    path.write_text(cfg.to_toml(), encoding="utf-8")
    parsed = RepoConfig.load(path)
    assert parsed.repo_id == "amittell/coord"


def test_repoconfig_load_with_no_repo_id_returns_none() -> None:
    """Pre-v0.3 config files have no repo_id key; loading must not crash."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(
            'version = 1\n'
            'tool = "claude"\n'
            'mode = "remote"\n'
            'service_url = "http://x"\n'
            'ownership_file = ".coordination/owners.yaml"\n'
        )
        f.flush()
        cfg = RepoConfig.load(Path(f.name))
    assert cfg.repo_id is None
