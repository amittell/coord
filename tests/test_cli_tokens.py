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

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from coordination import cli, cli_tokens


def _run(argv: list[str], db_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    # The CLI reads COORD_AUTH_TOKEN at startup to verify config.
    monkeypatch.setenv("COORD_AUTH_TOKEN", "smoke")
    return cli.main(argv)


def _parse_z(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _force_expire(db_path: Path, token_id: str) -> None:
    """Backdate a token's expiry directly in sqlite. The CLI only
    accepts future durations, so manufacturing an already-expired
    token has to go under the hood."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE engineer_tokens SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", token_id),
        )
        conn.commit()


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


# --- v0.29.4: expiry, rotation, activity display ---------------------------


def test_create_expires_in_sets_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    rc = _run(
        ["tokens", "create", "alex/claude/main", "--expires-in", "30d", "--json"],
        db_path,
        monkeypatch,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["expires_at"].endswith("Z")
    # ~30 days out (allow a little slack for test runtime).
    delta = _parse_z(payload["expires_at"]) - datetime.now(UTC)
    assert timedelta(days=29) < delta <= timedelta(days=30)
    # The DB row carries the same value byte-for-byte.
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT expires_at FROM engineer_tokens WHERE id = ?",
            (payload["id"],),
        ).fetchone()
    assert row == (payload["expires_at"],)


def test_create_without_expires_in_is_unbounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    rc = _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["expires_at"] is None


def test_create_human_output_shows_expires_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    rc = _run(
        ["tokens", "create", "alex/claude/main", "--expires-in", "12h"],
        db_path,
        monkeypatch,
    )
    assert rc == 0
    out = capsys.readouterr().out
    expires_lines = [
        line for line in out.splitlines() if line.startswith("Expires:")
    ]
    assert len(expires_lines) == 1
    assert expires_lines[0].rstrip().endswith("Z")


def test_create_rejects_garbage_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bad --expires-in must fail before any DB work: exit 1, the
    parse error on stderr, no token minted, no DB file created."""
    db_path = tmp_path / "db.sqlite"
    rc = _run(
        ["tokens", "create", "alex/claude/main", "--expires-in", "soon"],
        db_path,
        monkeypatch,
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "Invalid duration" in captured.err
    assert "coordt_" not in captured.out
    assert not db_path.exists()


def test_rotate_mints_successor_and_grace_expires_old(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    created = json.loads(capsys.readouterr().out)
    old_id = created["id"]
    old_raw = created["token"]

    rc = _run(["tokens", "rotate", old_id], db_path, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    # The new raw token appears exactly once, in the same padded form
    # create uses, and is a fresh value (not the old credential).
    raw_lines = [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(cli_tokens.TOKEN_PREFIX)
    ]
    assert len(raw_lines) == 1, f"expected one token line, got: {raw_lines}"
    assert raw_lines[0] != old_raw
    assert len(raw_lines[0]) == 7 + 64
    # The output names the old id and when it stops working.
    assert old_id in out
    assert "stops working" in out
    assert "NOT be shown again" in out

    # The chain is visible in list --json: the successor row points
    # back at the old id via rotated_from; the old row carries an
    # open grace window (default 24h, still in the future).
    rc = _run(["tokens", "list", "--json"], db_path, monkeypatch)
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    by_origin = {r["rotated_from"]: r for r in rows}
    old_row = by_origin[None]
    successor = by_origin[old_id]
    assert old_row["id"] == old_id
    assert old_row["rotation_grace_until"] is not None
    assert _parse_z(old_row["rotation_grace_until"]) > datetime.now(UTC)
    assert successor["rotation_grace_until"] is None
    assert successor["engineer"] == "alex/claude/main"


def test_rotate_json_payload_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    old_id = json.loads(capsys.readouterr().out)["id"]

    rc = _run(
        ["tokens", "rotate", old_id, "--expires-in", "7d", "--json"],
        db_path,
        monkeypatch,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "token",
        "token_id",
        "engineer",
        "rotated_from",
        "grace_until",
        "expires_at",
    }
    assert payload["token"].startswith("coordt_")
    assert payload["rotated_from"] == old_id
    assert payload["engineer"] == "alex/claude/main"
    assert payload["grace_until"].endswith("Z")
    # --expires-in applies to the successor: ~7 days out.
    delta = _parse_z(payload["expires_at"]) - datetime.now(UTC)
    assert timedelta(days=6) < delta <= timedelta(days=7)


def test_rotate_grace_zero_kills_old_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    old_id = json.loads(capsys.readouterr().out)["id"]

    rc = _run(
        ["tokens", "rotate", old_id, "--grace", "0h", "--json"],
        db_path,
        monkeypatch,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Zero grace means the window closed the moment it opened.
    assert _parse_z(payload["grace_until"]) <= datetime.now(UTC)

    # The human list view agrees: the old row reads grace-elapsed.
    rc = _run(["tokens", "list"], db_path, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    old_line = next(
        line for line in out.splitlines() if line.startswith(old_id[:8])
    )
    assert "grace-elapsed" in old_line


def test_rotate_rejects_garbage_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    old_id = json.loads(capsys.readouterr().out)["id"]

    rc = _run(
        ["tokens", "rotate", old_id, "--grace", "whenever"],
        db_path,
        monkeypatch,
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "Invalid duration" in captured.err
    assert "coordt_" not in captured.out
    # No successor was minted; the original row is untouched.
    rc = _run(["tokens", "list", "--json"], db_path, monkeypatch)
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["rotation_grace_until"] is None


def test_rotate_unknown_id_maps_to_not_found_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    bogus = "00000000-0000-0000-0000-000000000000"
    rc = _run(["tokens", "rotate", bogus], db_path, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert f"No token with id {bogus}." in err


def test_rotate_revoked_token_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    token_id = json.loads(capsys.readouterr().out)["id"]
    _run(["tokens", "revoke", token_id], db_path, monkeypatch)
    capsys.readouterr()

    rc = _run(["tokens", "rotate", token_id], db_path, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert (
        f"Token {token_id} is revoked; create a fresh token instead." in err
    )


def test_rotate_expired_token_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    token_id = json.loads(capsys.readouterr().out)["id"]
    _force_expire(db_path, token_id)

    rc = _run(["tokens", "rotate", token_id], db_path, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert (
        f"Token {token_id} has expired; create a fresh token instead." in err
    )


def test_rotate_twice_refused_with_chain_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--json"],
        db_path,
        monkeypatch,
    )
    token_id = json.loads(capsys.readouterr().out)["id"]
    rc = _run(["tokens", "rotate", token_id], db_path, monkeypatch)
    assert rc == 0
    capsys.readouterr()

    rc = _run(["tokens", "rotate", token_id], db_path, monkeypatch)
    assert rc == 1
    err = capsys.readouterr().err
    assert f"Token {token_id} was already rotated" in err
    assert "rotate its successor instead" in err
    assert "rotated_from" in err


def test_list_human_output_shows_status_words(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One row per lifecycle state; each line carries the right derived
    status word. grace-elapsed is covered by the zero-grace test."""
    db_path = tmp_path / "db.sqlite"

    def _create(engineer: str) -> str:
        _run(["tokens", "create", engineer, "--json"], db_path, monkeypatch)
        return str(json.loads(capsys.readouterr().out)["id"])

    active_id = _create("active/claude/main")
    rotating_id = _create("rotating/claude/main")
    revoked_id = _create("revoked/claude/main")
    expired_id = _create("expired/claude/main")
    _run(["tokens", "rotate", rotating_id], db_path, monkeypatch)
    _run(["tokens", "revoke", revoked_id], db_path, monkeypatch)
    _force_expire(db_path, expired_id)
    capsys.readouterr()

    rc = _run(["tokens", "list", "--include-revoked"], db_path, monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    # New activity columns are part of the header.
    header = out.splitlines()[0]
    for column in ("STATUS", "EXPIRES", "LAST USED", "REQS", "LAST IP"):
        assert column in header

    def line_for(token_id: str) -> str:
        return next(
            line for line in out.splitlines() if line.startswith(token_id[:8])
        )

    assert " active" in line_for(active_id)
    assert " rotating" in line_for(rotating_id)
    assert " revoked" in line_for(revoked_id)
    assert " expired" in line_for(expired_id)


def test_list_json_carries_activity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The JSON view passes the v15 columns straight through so
    dashboards and scripts see expiry, rotation chain, and request
    activity without a schema-specific query."""
    db_path = tmp_path / "db.sqlite"
    _run(
        ["tokens", "create", "alex/claude/main", "--expires-in", "30d"],
        db_path,
        monkeypatch,
    )
    capsys.readouterr()

    rc = _run(["tokens", "list", "--json"], db_path, monkeypatch)
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    for field in (
        "expires_at",
        "rotated_from",
        "rotation_grace_until",
        "request_count",
        "last_source_ip",
        "last_user_agent",
    ):
        assert field in row, f"missing {field} in list --json row"
    assert row["expires_at"].endswith("Z")
    assert row["rotated_from"] is None
    assert row["rotation_grace_until"] is None
