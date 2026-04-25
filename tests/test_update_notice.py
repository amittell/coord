from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from coordination import cli_update_notice


def _seed_repo(tmp_path: Path, service_url: str = "http://coord.example") -> Path:
    coord = tmp_path / ".coordination"
    coord.mkdir()
    (coord / "config.toml").write_text(
        "version = 1\n"
        'tool = "claude"\n'
        'mode = "remote"\n'
        f'service_url = "{service_url}"\n'
        'ownership_file = ".coordination/owners.yaml"\n'
        'local_env_file = ".coordination/local.env"\n',
        encoding="utf-8",
    )
    (tmp_path / ".git").mkdir()
    return tmp_path


def _mock_meta(version: str | None):
    def get(url, **kw):
        body = {} if version is None else {"version": version}
        return httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=body)
        )).get(url, **kw)

    return get


@pytest.fixture()
def home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COORD_HOME", str(home / ".coord"))
    return home


def test_notice_fires_when_server_is_newer(monkeypatch, tmp_path, home_dir, capsys):
    monkeypatch.chdir(_seed_repo(tmp_path))
    monkeypatch.setattr(cli_update_notice.httpx, "get", _mock_meta("0.5.0"))
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand="claims")
    err = capsys.readouterr().err
    assert "0.5.0" in err
    assert "0.1.0" in err
    assert "coord upgrade" in err or "git pull" in err


def test_notice_silent_when_versions_match(monkeypatch, tmp_path, home_dir, capsys):
    monkeypatch.chdir(_seed_repo(tmp_path))
    monkeypatch.setattr(cli_update_notice.httpx, "get", _mock_meta("0.1.0"))
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand="claims")
    assert capsys.readouterr().err == ""


def test_notice_silent_when_client_is_newer(monkeypatch, tmp_path, home_dir, capsys):
    monkeypatch.chdir(_seed_repo(tmp_path))
    monkeypatch.setattr(cli_update_notice.httpx, "get", _mock_meta("0.1.0"))
    cli_update_notice.maybe_emit_update_notice(client_version="0.5.0", subcommand="claims")
    # Don't pester contributors whose local CLI is ahead of the cluster.
    assert capsys.readouterr().err == ""


def test_notice_skipped_when_env_var_set(monkeypatch, tmp_path, home_dir, capsys):
    monkeypatch.chdir(_seed_repo(tmp_path))
    monkeypatch.setenv("COORD_NO_UPDATE_CHECK", "1")
    called = {"n": 0}

    def get(*a, **kw):
        called["n"] += 1
        return httpx.Response(200, json={"version": "0.5.0"})

    monkeypatch.setattr(cli_update_notice.httpx, "get", get)
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand="claims")
    # No banner AND no network call -- the env var must short-circuit early.
    assert capsys.readouterr().err == ""
    assert called["n"] == 0


def test_notice_skipped_when_cache_is_fresh(monkeypatch, tmp_path, home_dir, capsys):
    monkeypatch.chdir(_seed_repo(tmp_path))
    cache = home_dir / ".coord" / "last_update_check"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("")
    # Touch to "1 hour ago" -- well within the 24h refresh window.
    one_hour_ago = time.time() - 3600
    import os
    os.utime(cache, (one_hour_ago, one_hour_ago))

    called = {"n": 0}

    def get(*a, **kw):
        called["n"] += 1
        return httpx.Response(200, json={"version": "0.5.0"})

    monkeypatch.setattr(cli_update_notice.httpx, "get", get)
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand="claims")
    assert called["n"] == 0
    assert capsys.readouterr().err == ""


def test_notice_runs_when_cache_is_stale(monkeypatch, tmp_path, home_dir, capsys):
    monkeypatch.chdir(_seed_repo(tmp_path))
    cache = home_dir / ".coord" / "last_update_check"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("")
    # Touch to ~25 hours ago -- past the refresh window.
    long_ago = time.time() - 25 * 3600
    import os
    os.utime(cache, (long_ago, long_ago))

    monkeypatch.setattr(cli_update_notice.httpx, "get", _mock_meta("0.5.0"))
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand="claims")
    assert "0.5.0" in capsys.readouterr().err
    # Cache must be touched so we don't re-run within 24h.
    assert (time.time() - cache.stat().st_mtime) < 60


def test_notice_silent_on_network_error(monkeypatch, tmp_path, home_dir, capsys):
    monkeypatch.chdir(_seed_repo(tmp_path))

    def raising(*a, **kw):
        raise httpx.ConnectError("dead")

    monkeypatch.setattr(cli_update_notice.httpx, "get", raising)
    # Must not raise -- update check is best-effort.
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand="claims")
    assert capsys.readouterr().err == ""


def test_notice_skipped_when_not_in_repo(monkeypatch, tmp_path, home_dir, capsys):
    # No .coordination/config.toml under cwd -- check has no service URL to hit.
    monkeypatch.chdir(tmp_path)
    called = {"n": 0}

    def get(*a, **kw):
        called["n"] += 1
        return httpx.Response(200, json={"version": "0.5.0"})

    monkeypatch.setattr(cli_update_notice.httpx, "get", get)
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand="claims")
    assert called["n"] == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("cmd", ["init", "start", "_serve", "doctor"])
def test_notice_skipped_for_specific_subcommands(monkeypatch, tmp_path, home_dir, capsys, cmd):
    monkeypatch.chdir(_seed_repo(tmp_path))
    called = {"n": 0}

    def get(*a, **kw):
        called["n"] += 1
        return httpx.Response(200, json={"version": "0.5.0"})

    monkeypatch.setattr(cli_update_notice.httpx, "get", get)
    cli_update_notice.maybe_emit_update_notice(client_version="0.1.0", subcommand=cmd)
    assert called["n"] == 0
    assert capsys.readouterr().err == ""
