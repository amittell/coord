"""Tests for the ``coord tokens`` operator surface (v0.29).

The CLI is the only path operators use to mint, list, and revoke
per-engineer tokens, so these tests pin the contracts directly
against the registered argparse handler. The DB-layer tests in
``tests/test_engineer_tokens.py`` cover the storage primitives;
this file covers the CLI behaviour on top.

Three properties matter:

1. ``coord tokens create`` prints the raw token exactly once in a
   recognisable ``coordt_`` form. The DB never stores the raw
   value -- so if a test ever reads the DB back and finds the
   raw token, that is a real leak.
2. ``coord tokens list`` returns metadata only, never the raw
   token or its hash. Operators need to see "which tokens exist"
   without being able to recover them.
3. ``coord tokens revoke <id>`` flips the row's revoked_at and
   makes ``lookup_engineer_token`` return None -- which is what
   stops the bearer from authenticating on the next request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordination import cli, cli_tokens


def _run(argv: list[str], db_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    # The CLI reads COORD_AUTH_TOKEN at startup to verify config.
    monkeypatch.setenv("COORD_AUTH_TOKEN", "smoke")
    return cli.main(argv)


def test_create_prints_raw_token_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    rc = _run(
        ["tokens", "create", "alex/claude/main", "--description", "laptop"],
        db_path,
        monkeypatch,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "alex/claude/main" in out
    assert "laptop" in out
    # The token line is the bare value preceded by two spaces
    # (the cli pads it for readability). Find it and extract.
    raw_lines = [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(cli_tokens.TOKEN_PREFIX)
    ]
    assert len(raw_lines) == 1, f"expected one token line, got: {raw_lines}"
    raw = raw_lines[0]
    # coordt_ + 64 hex chars = 71 chars total.
    assert len(raw) == 7 + 64
    assert all(c in "0123456789abcdef" for c in raw.removeprefix("coordt_"))


def test_create_then_list_shows_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--description", "laptop"],
        db_path,
        monkeypatch,
    )
    capsys.readouterr()  # discard create output

    rc = _run(
        ["tokens", "list", "--json"], db_path, monkeypatch
    )
    assert rc == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert len(rows) == 1
    row = rows[0]
    assert row["engineer"] == "alex/claude/main"
    assert row["description"] == "laptop"
    # No raw token or hash in the metadata view.
    raw_keys = {k for k in row if "token" in k.lower() or "sha" in k.lower()}
    assert raw_keys == set(), f"metadata leaked sensitive fields: {raw_keys}"
    # No coordt_ string anywhere in the serialised output either.
    assert "coordt_" not in out


def test_list_filters_by_engineer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(["tokens", "create", "alex/claude/main"], db_path, monkeypatch)
    _run(["tokens", "create", "dana/claude/main"], db_path, monkeypatch)
    capsys.readouterr()

    rc = _run(
        ["tokens", "list", "--engineer", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["engineer"] == "alex/claude/main"


def test_revoke_kills_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI is synchronous (it wraps each subcommand in
    ``asyncio.run``), so this test reads the DB through sqlite3
    directly rather than spinning up another event loop -- which
    would conflict with the pytest-asyncio loop the CLI already
    used internally."""
    import sqlite3

    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    created = json.loads(capsys.readouterr().out)
    token_id = created["id"]

    # Pre-revoke: row exists with revoked_at NULL (lookup matches).
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT engineer, revoked_at FROM engineer_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
    assert row == ("alex/claude/main", None)

    rc = _run(["tokens", "revoke", token_id], db_path, monkeypatch)
    assert rc == 0

    # Post-revoke: revoked_at is a non-NULL ISO timestamp ending Z.
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT engineer, revoked_at FROM engineer_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "alex/claude/main"
    assert row[1] is not None
    assert row[1].endswith("Z")


def test_revoke_unknown_id_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    rc = _run(
        ["tokens", "revoke", "00000000-0000-0000-0000-000000000000"],
        db_path,
        monkeypatch,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown token id" in err


def test_list_empty_db_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No issued tokens is a legitimate state (fresh install) and must
    return rc=0 with a friendly message rather than blowing up."""
    db_path = tmp_path / "db.sqlite"
    rc = _run(["tokens", "list"], db_path, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No tokens issued" in out


def test_create_token_format_is_coordt_plus_64_hex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``coordt_`` prefix is what makes a leaked token grep-able
    in CI logs and clipboard scanning. Lock the format so a future
    refactor doesn't silently change it."""
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    payload = json.loads(capsys.readouterr().out)
    raw = payload["token"]
    assert raw.startswith("coordt_")
    body = raw.removeprefix("coordt_")
    assert len(body) == 64
    int(body, 16)  # raises ValueError if non-hex
