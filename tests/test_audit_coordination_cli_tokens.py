"""Audit-fix coverage for the ``coord tokens`` CLI (v0.45 audit).

Pins the fixes for three findings against ``coordination/cli_tokens.py``:

1. ``coord tokens list`` truncates token ids to 8 characters, so
   rotate/revoke now accept an unambiguous id prefix (>= 8 chars) --
   the documented list-copy-revoke flow works without ``--json``.
   Ambiguous prefixes are a loud operator error, never a guess.
2. ``list``/``rotate``/``revoke`` take ``--database-path`` so they can
   target the same DB a ``create --database-path`` wrote, and the
   remote-mode refusal now covers the mutating subcommands too.
3. The documented exit-code-2 contract is real: ``rotate``/``revoke``
   against a missing SQLite DB file exit 2 instead of silently
   materialising an empty database; ``list`` reports the empty state
   without creating the file.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from conftest import seam_connection
from coordination import cli
from coordination.db import Database


def _run(argv: list[str], db_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_AUTH_TOKEN", "smoke")
    return cli.main(argv)


def _create_token(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    engineer: str = "alex/claude/main",
) -> str:
    rc = _run(["tokens", "create", engineer, "--json"], db_path, monkeypatch)
    assert rc == 0
    return str(json.loads(capsys.readouterr().out)["id"])


def _rewrite_token_id(db_path: Path, old_id: str, new_id: str) -> None:
    """Force a token id through ``conftest.seam_connection`` (the store's
    own ``_connect`` seam, so the write lands in whichever backend the
    suite runs) -- the only way to manufacture two tokens sharing an
    8-char id prefix, since uuid4 collisions cannot be provoked."""

    async def _go() -> None:
        async with seam_connection(Database(db_path)) as conn:
            await conn.execute(
                "UPDATE engineer_tokens SET id = ? WHERE id = ?",
                (new_id, old_id),
            )

    asyncio.run(_go())


def _seed_remote_mode_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    coord = repo / ".coordination"
    coord.mkdir()
    (coord / "config.toml").write_text(
        "version = 1\n"
        'tool = "claude"\n'
        'mode = "remote"\n'
        'service_url = "http://coord.kebabrack.lan"\n'
        'ownership_file = ".coordination/owners.yaml"\n'
        'local_env_file = ".coordination/local.env"\n'
        'repo_id = "amittell/requesthub"\n',
        encoding="utf-8",
    )


# --- id-prefix resolution (list output round-trips into rotate/revoke) -----


def test_list_table_prefix_round_trips_into_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The exact 8-char value the list table prints must be a valid
    revoke target -- this is the documented operator flow."""
    db_path = tmp_path / "db.sqlite"
    token_id = _create_token(db_path, monkeypatch, capsys)

    rc = _run(["tokens", "list"], db_path, monkeypatch)
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    row = next(line for line in lines[1:] if line.strip())
    printed_id = row.split()[0]
    assert printed_id == token_id[:8]

    rc = _run(["tokens", "revoke", printed_id], db_path, monkeypatch)
    assert rc == 0
    assert f"Revoked {token_id}." in capsys.readouterr().out


def test_rotate_accepts_unambiguous_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    token_id = _create_token(db_path, monkeypatch, capsys)

    rc = _run(
        ["tokens", "rotate", token_id[:8], "--json"], db_path, monkeypatch
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # The prefix resolved to the stored id; the chain records the full id.
    assert payload["rotated_from"] == token_id


def test_full_id_still_matches_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    token_id = _create_token(db_path, monkeypatch, capsys)
    rc = _run(["tokens", "revoke", token_id], db_path, monkeypatch)
    assert rc == 0
    assert f"Revoked {token_id}." in capsys.readouterr().out


def test_ambiguous_prefix_is_operator_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two tokens sharing the printed 8-char prefix: revoke must refuse
    with both candidates named rather than guessing (revoking someone
    else's credential would be far worse than an error)."""
    db_path = tmp_path / "db.sqlite"
    first = _create_token(db_path, monkeypatch, capsys, "alex/claude/one")
    second = _create_token(db_path, monkeypatch, capsys, "alex/claude/two")
    collided = first[:8] + "-collided-sibling"
    _rewrite_token_id(db_path, second, collided)

    rc = _run(["tokens", "revoke", first[:8]], db_path, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert first in err
    assert collided in err

    # Neither token was touched: both still list as live.
    rc = _run(["tokens", "list", "--json"], db_path, monkeypatch)
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["id"] for r in rows} == {first, collided}


def test_prefix_shorter_than_eight_chars_is_not_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    token_id = _create_token(db_path, monkeypatch, capsys)
    short = token_id[:7]

    rc = _run(["tokens", "revoke", short], db_path, monkeypatch)
    assert rc == 1
    assert f"Unknown token id: {short}" in capsys.readouterr().err


def test_unknown_prefix_falls_through_to_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _create_token(db_path, monkeypatch, capsys)

    rc = _run(["tokens", "rotate", "ffffffff"], db_path, monkeypatch)
    assert rc == 1
    assert "No token with id ffffffff." in capsys.readouterr().err


# --- --database-path on list/rotate/revoke ----------------------------------


def test_list_database_path_targets_the_created_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A token minted with ``create --database-path`` must be visible to
    ``list --database-path`` even when COORD_DATABASE_PATH points
    elsewhere -- the asymmetry the audit flagged."""
    target = tmp_path / "server.sqlite"
    decoy = tmp_path / "decoy.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "smoke")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(decoy))

    rc = cli.main(
        [
            "tokens",
            "create",
            "alex/claude/main",
            "--database-path",
            str(target),
            "--json",
        ]
    )
    assert rc == 0
    token_id = json.loads(capsys.readouterr().out)["id"]

    rc = cli.main(
        ["tokens", "list", "--database-path", str(target), "--json"]
    )
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in rows] == [token_id]


def test_rotate_and_revoke_accept_database_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "server.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "smoke")
    monkeypatch.delenv("COORD_DATABASE_PATH", raising=False)

    rc = cli.main(
        [
            "tokens",
            "create",
            "alex/claude/main",
            "--database-path",
            str(target),
            "--json",
        ]
    )
    assert rc == 0
    token_id = json.loads(capsys.readouterr().out)["id"]

    rc = cli.main(
        [
            "tokens",
            "rotate",
            token_id,
            "--database-path",
            str(target),
            "--json",
        ]
    )
    assert rc == 0
    successor = json.loads(capsys.readouterr().out)["token_id"]

    rc = cli.main(
        ["tokens", "revoke", successor, "--database-path", str(target)]
    )
    assert rc == 0
    assert f"Revoked {successor}." in capsys.readouterr().out


# --- exit-code-2 contract (missing SQLite DB file) ---------------------------


@pytest.mark.sqlite_only
def test_rotate_missing_db_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # sqlite_only: the file-existence check is inert by design on the
    # Postgres backend, where no local DB file ever exists.
    db_path = tmp_path / "nope" / "db.sqlite"
    rc = _run(["tokens", "rotate", "deadbeef"], db_path, monkeypatch)
    assert rc == 2
    assert "Token database not found" in capsys.readouterr().err
    assert not db_path.exists()


@pytest.mark.sqlite_only
def test_revoke_missing_db_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "nope" / "db.sqlite"
    rc = _run(["tokens", "revoke", "deadbeef"], db_path, monkeypatch)
    assert rc == 2
    assert "Token database not found" in capsys.readouterr().err
    assert not db_path.exists()


@pytest.mark.sqlite_only
def test_list_missing_db_reports_empty_without_creating_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fresh-install list stays friendly (rc=0, "No tokens issued.") but
    must no longer leave an empty coordination.db behind at whatever
    possibly-wrong path it was aimed at."""
    db_path = tmp_path / "db.sqlite"
    rc = _run(["tokens", "list"], db_path, monkeypatch)
    assert rc == 0
    assert "No tokens issued" in capsys.readouterr().out
    assert not db_path.exists()

    rc = _run(["tokens", "list", "--json"], db_path, monkeypatch)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not db_path.exists()


# --- remote-mode refusal extends to rotate/revoke ----------------------------


def test_rotate_refuses_implicit_local_db_in_remote_mode_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _seed_remote_mode_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("COORD_DATABASE_PATH", raising=False)

    rc = cli.main(["tokens", "rotate", "deadbeef-dead-beef"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Refusing to rotate a local SQLite token" in err
    assert "http://coord.kebabrack.lan" in err
    assert "coord tokens rotate deadbeef-dead-beef" in err
    assert "--local-db" in err


def test_revoke_refuses_implicit_local_db_in_remote_mode_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _seed_remote_mode_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("COORD_DATABASE_PATH", raising=False)

    rc = cli.main(["tokens", "revoke", "deadbeef-dead-beef"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Refusing to revoke a local SQLite token" in err
    assert "coord tokens revoke deadbeef-dead-beef" in err
