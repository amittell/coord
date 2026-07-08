"""Audit fixes: the repo scope-guard lookups must ride the overridable
``_connect`` backend seam (db.py claim_repo/request_repo/queue_entry_repo
formerly opened raw ``aiosqlite.connect(self.path)``, silently breaking
the guards on the Postgres backend), and every ``webhook_outbox``
statement must survive the SQLite -> PostgreSQL ``translate()`` pass so a
future edit cannot crash the leader replica's delivery loop at runtime.

These tests subclass ``Database`` to interpose on ``_connect`` -- a
SQLite-shaped move -- so they are marked ``sqlite_only``; the seam
property they pin is exactly what makes the same method bodies correct
on the Postgres store.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from coordination.db import Database
from coordination.pg_backend import translate

pytestmark = pytest.mark.sqlite_only

_DB_SOURCE = Path(__file__).resolve().parent.parent / "coordination" / "db.py"

_SCOPE_GUARD_METHODS = ("claim_repo", "request_repo", "queue_entry_repo")


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _mk_claim(
    db: Database,
    *,
    engineer: str = "alice",
    pattern: str = "src/app.py",
    repo: str | None = None,
) -> str:
    cid = str(uuid4())
    exp = _iso(datetime.now(UTC) + timedelta(hours=1))
    await db.insert_claims_batch(
        engineer=engineer,
        branch=None,
        description=None,
        items=[(cid, "file", pattern, "soft", exp)],
        repo=repo,
    )
    return cid


class _SeamCountingDatabase(Database):
    """Counts how often the overridable ``_connect`` seam is used."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.seam_calls = 0

    def _connect(self):
        self.seam_calls += 1
        return super()._connect()


def test_scope_guard_methods_do_not_open_raw_sqlite() -> None:
    """AST regression guard: none of the three scope-guard lookups may
    call ``aiosqlite.connect`` / ``sqlite3.connect`` directly. A raw
    connect bypasses the seam ``PostgresStore`` overrides, so on a PG
    deployment the guard would consult a stray local SQLite file: 500s
    on a fresh environment, silent cross-repo access with a stale one.
    """
    tree = ast.parse(_DB_SOURCE.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name not in _SCOPE_GUARD_METHODS:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "connect"
                and isinstance(f.value, ast.Name)
                and f.value.id in ("aiosqlite", "sqlite3")
            ):
                offenders.append(f"{node.name} @ line {call.lineno}")
    assert not offenders, (
        "scope-guard lookups must use the self._connect() seam, not raw "
        f"driver connects (breaks repo isolation on Postgres): {offenders}"
    )


async def test_claim_repo_uses_connect_seam(tmp_path: Path) -> None:
    db = _SeamCountingDatabase(tmp_path / "seam.sqlite")
    cid = await _mk_claim(db, repo="acme/app")

    before = db.seam_calls
    assert await db.claim_repo(cid) == (True, "acme/app")
    assert db.seam_calls > before, "claim_repo bypassed the _connect seam"

    # Unknown id falls through so the handler's own 404 logic runs.
    assert await db.claim_repo("no-such-id") == (False, None)


async def test_request_repo_uses_connect_seam(tmp_path: Path) -> None:
    db = _SeamCountingDatabase(tmp_path / "seam.sqlite")
    cid = await _mk_claim(db, repo="acme/app")
    now = datetime.now(UTC)
    rid = str(uuid4())
    await db.create_request(
        request_id=rid,
        claim_id=cid,
        requester_engineer="bob",
        requester_session_id=None,
        requested_pattern="src/app.py",
        reason="need it",
        urgency="normal",
        original_expires_at=_iso(now + timedelta(hours=1)),
        shortened_expires_at=_iso(now + timedelta(minutes=5)),
        new_claim_expires_at=_iso(now + timedelta(minutes=5)),
    )

    before = db.seam_calls
    assert await db.request_repo(rid) == (True, "acme/app")
    assert db.seam_calls > before, "request_repo bypassed the _connect seam"
    assert await db.request_repo("no-such-id") == (False, None)


async def test_queue_entry_repo_uses_connect_seam(tmp_path: Path) -> None:
    db = _SeamCountingDatabase(tmp_path / "seam.sqlite")
    cid = await _mk_claim(db, repo="acme/app")
    row = await db.enqueue_claim_request(
        blocking_claim_id=cid,
        requester_engineer="bob",
        requester_session_id=None,
        requester_branch=None,
        requester_description=None,
        repo="acme/app",
        claim_type="file",
        pattern="src/app.py",
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=30,
    )

    before = db.seam_calls
    assert await db.queue_entry_repo(row["id"]) == (True, "acme/app")
    assert db.seam_calls > before, (
        "queue_entry_repo bypassed the _connect seam"
    )
    assert await db.queue_entry_repo("no-such-id") == (False, None)


class _RecordingConn:
    """Delegating connection wrapper that logs every SQL string."""

    def __init__(self, inner, log: list[str]) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value) -> None:
        setattr(object.__getattribute__(self, "_inner"), name, value)

    async def execute(self, sql, params=()):
        object.__getattribute__(self, "_log").append(sql)
        return await object.__getattribute__(self, "_inner").execute(
            sql, params
        )

    async def executemany(self, sql, seq):
        object.__getattribute__(self, "_log").append(sql)
        return await object.__getattribute__(self, "_inner").executemany(
            sql, seq
        )


class _RecordingDatabase(Database):
    """Routes _connect through a wrapper that records executed SQL."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.statements: list[str] = []

    def _connect(self):
        inner = super()._connect()
        log = self.statements

        @asynccontextmanager
        async def _cm():
            async with inner as conn:
                yield _RecordingConn(conn, log)

        return _cm()


async def test_webhook_outbox_statements_translate_to_postgres(
    tmp_path: Path,
) -> None:
    """Drive the webhook_outbox surface end to end on SQLite while
    recording every executed statement, then assert each one passes the
    fail-fast ``translate()``. ``translate`` raises ValueError on any
    construct it refuses (julianday, unixepoch, modifier-form datetime,
    unsupported strftime), so an untranslatable webhook query fails here
    in CI instead of 500ing the leader's background delivery loop."""
    db = _RecordingDatabase(tmp_path / "outbox.sqlite")

    oid = await db.enqueue_webhook(
        url="https://example.test/hook",
        event_type="claim_granted",
        payload_json='{"claim_ids": ["c1"]}',
        hmac_signature="sig",
    )
    pending = await db.list_pending_webhooks()
    assert [r["id"] for r in pending] == [oid]

    await db.mark_webhook_failed(
        oid,
        last_error="connect timeout",
        next_attempt_at=_iso(datetime.now(UTC) - timedelta(seconds=1)),
    )
    await db.reset_webhook_outbox(["failed"])
    await db.mark_webhook_delivered(oid)
    await db.webhook_delivery_stats()
    await db.recent_webhook_outbox(limit=10)
    await db.count_webhook_outbox(["delivered"])
    assert await db.delete_webhook_outbox(["delivered"]) == 1

    webhook_stmts = [
        s for s in db.statements if "webhook_outbox" in s
    ]
    assert len(webhook_stmts) >= 8, (
        "expected the drive above to touch every webhook_outbox statement; "
        f"captured only {len(webhook_stmts)}"
    )
    for sql in webhook_stmts:
        translated = translate(sql)  # raises ValueError when unsupported
        assert translated
