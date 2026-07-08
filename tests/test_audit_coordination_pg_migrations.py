"""Audit regression tests for the PostgreSQL backend bootstrap and
migration path (coordination/pg_backend.py).

Covers:

- ``_full_schema_sql`` stamps the newest ``MIGRATIONS`` version (derived,
  not the historical hardcoded 18), keeping the stamp in lockstep with the
  DDL replayed into the consolidated body.
- ``PostgresStore.init()`` applies pending migrations to an
  already-bootstrapped schema instead of freezing it at its bootstrap
  version forever (previously an existing PG deployment never gained
  migration 19's ``engineer_tokens.repo`` column).
- Migration DDL replayed on PG is idempotent (``IF NOT EXISTS``), so a
  schema whose bootstrap body already contained v19 DDL but was stamped 18
  upgrades cleanly instead of crashing on duplicate_column at every start.
- ``init()`` takes the advisory lock BEFORE ``CREATE SCHEMA IF NOT
  EXISTS`` so two first-boot replicas cannot race on the non-atomic
  check-then-create and die on the pg_namespace unique index.
- ``strftime('%Y-%m-%d')`` translation pins the day bucket to UTC and
  ``_set_search_path`` pins the session timezone to UTC, matching SQLite's
  UTC bucketing on any PG server default timezone.
- ``PostgresStore`` fails fast with a clear startup error when the PG
  backend is selected but asyncpg is not importable.

All tests run without a live PostgreSQL server: ``init()`` is exercised
against a recording fake connection behind a monkeypatched ``_get_pool``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordination.pg_backend as pg_backend
from coordination.db import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    _split_sql_statements,
)
from coordination.pg_backend import (
    PostgresStore,
    _build_schema_body,
    _full_schema_sql,
    _latest_migration_version,
    _translate_migration_stmt,
    translate,
)


# ---------------------------------------------------------------------------
# Fakes: a recording asyncpg-connection/pool stand-in for the init() path.
# ---------------------------------------------------------------------------


class _FakeTransaction:
    def __init__(self, conn: "_FakeRaw") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeTransaction":
        self._conn.tx_open = True
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self._conn.tx_open = False
        return False


class _FakeRaw:
    """Records every statement (with the transaction state at execution
    time) and simulates just enough of the bootstrap catalog queries:
    ``to_regclass`` existence and the ``schema_version`` stamp."""

    def __init__(self, *, bootstrapped: bool, version: int | None = None) -> None:
        self.bootstrapped = bootstrapped
        self.version = version
        self.executed: list[tuple[str, tuple, bool]] = []
        self.tx_open = False

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append((sql, args, self.tx_open))
        if "INSERT INTO schema_version (id, version) VALUES (1, $1)" in sql:
            self.version = int(args[0])  # type: ignore[arg-type]
        return "OK"

    async def fetchval(self, sql: str, *args: object):
        self.executed.append((sql, args, self.tx_open))
        if "to_regclass" in sql:
            return "schema_version" if self.bootstrapped else None
        if "SELECT version FROM schema_version" in sql:
            return self.version
        return None


class _FakeAcquire:
    def __init__(self, raw: _FakeRaw) -> None:
        self._raw = raw

    async def __aenter__(self) -> _FakeRaw:
        return self._raw

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, raw: _FakeRaw) -> None:
        self._raw = raw

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._raw)


def _store_with_fake_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: _FakeRaw
) -> PostgresStore:
    # The construction guard requires an importable asyncpg; these tests
    # never touch the real driver, so stub the module object when the
    # postgres extra is not installed in the test environment.
    if pg_backend.asyncpg is None:
        monkeypatch.setattr(pg_backend, "asyncpg", object())

    async def _fake_get_pool(dsn: str) -> _FakePool:
        return _FakePool(raw)

    monkeypatch.setattr(pg_backend, "_get_pool", _fake_get_pool)
    return PostgresStore(tmp_path / "audit.sqlite")


def _sqls(raw: _FakeRaw) -> list[str]:
    return [sql for sql, _args, _tx in raw.executed]


# ---------------------------------------------------------------------------
# Bootstrap stamp: derived from the migration chain, never hardcoded.
# ---------------------------------------------------------------------------


def test_full_schema_sql_stamps_latest_migration_version() -> None:
    latest = _latest_migration_version()
    assert latest == CURRENT_SCHEMA_VERSION
    assert latest > 18  # the historical hardcoded stamp is behind the chain
    sql = _full_schema_sql(_build_schema_body())
    assert f"INSERT INTO schema_version (id, version) VALUES (1, {latest})" in sql
    assert "VALUES (1, 18)" not in sql


# ---------------------------------------------------------------------------
# Migration statement translation: PG-valid and idempotent.
# ---------------------------------------------------------------------------


def test_translate_migration_stmt_makes_add_column_idempotent() -> None:
    out = _translate_migration_stmt(
        "ALTER TABLE engineer_tokens ADD COLUMN repo TEXT;"
    )
    assert "ADD COLUMN IF NOT EXISTS repo TEXT" in out


def test_translate_migration_stmt_makes_create_table_and_index_idempotent() -> None:
    out = _translate_migration_stmt("CREATE TABLE foo (id TEXT PRIMARY KEY);")
    assert out.startswith("CREATE TABLE IF NOT EXISTS foo")
    out = _translate_migration_stmt("CREATE INDEX idx_foo ON foo (id);")
    assert out.startswith("CREATE INDEX IF NOT EXISTS idx_foo")
    out = _translate_migration_stmt("CREATE UNIQUE INDEX idx_bar ON foo (id);")
    assert out.startswith("CREATE UNIQUE INDEX IF NOT EXISTS idx_bar")


def test_translate_migration_stmt_does_not_double_wrap() -> None:
    out = _translate_migration_stmt("CREATE TABLE IF NOT EXISTS foo (id TEXT);")
    assert out.count("IF NOT EXISTS") == 1
    out = _translate_migration_stmt(
        "ALTER TABLE t ADD COLUMN IF NOT EXISTS c TEXT;"
    )
    assert out.count("IF NOT EXISTS") == 1


def test_translate_migration_stmt_maps_boolean_affinity() -> None:
    out = _translate_migration_stmt(
        "ALTER TABLE claims ADD COLUMN ttl_shortened BOOLEAN DEFAULT 0;"
    )
    assert "INTEGER" in out
    assert "BOOLEAN" not in out


def test_every_migration_ddl_statement_becomes_idempotent() -> None:
    """Every ADD COLUMN / CREATE TABLE / CREATE INDEX in the real chain must
    carry IF NOT EXISTS after translation, so replaying a migration whose
    effect is already baked into the consolidated bootstrap body no-ops."""
    for _version, sql in MIGRATIONS:
        for stmt in _split_sql_statements(sql):
            out = _translate_migration_stmt(stmt)
            upper = out.upper()
            head = upper.lstrip()
            if head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
                assert "IF NOT EXISTS" in upper, out
            if "ADD COLUMN" in upper:
                assert "ADD COLUMN IF NOT EXISTS" in upper, out


# ---------------------------------------------------------------------------
# init(): schema-creation race and the upgrade path.
# ---------------------------------------------------------------------------


async def test_init_takes_advisory_lock_before_create_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _FakeRaw(bootstrapped=False)
    store = _store_with_fake_pool(tmp_path, monkeypatch, raw)
    await store.init()
    sqls = _sqls(raw)
    lock_idx = next(
        i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s
    )
    schema_idx = next(
        i for i, s in enumerate(sqls) if "CREATE SCHEMA IF NOT EXISTS" in s
    )
    assert lock_idx < schema_idx
    # Both must run inside the same transaction that holds the xact lock.
    assert raw.executed[lock_idx][2] is True
    assert raw.executed[schema_idx][2] is True


async def test_init_fresh_bootstrap_stamps_latest_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _FakeRaw(bootstrapped=False)
    store = _store_with_fake_pool(tmp_path, monkeypatch, raw)
    await store.init()
    bootstrap = [s for s in _sqls(raw) if "coord_leader_lease" in s]
    assert bootstrap
    assert (
        f"INSERT INTO schema_version (id, version) VALUES (1, {_latest_migration_version()})"
        in bootstrap[0]
    )


async def test_init_upgrades_stamped_18_schema_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen-forever case: a PG schema bootstrapped by a build that
    stamped 18 (whose body already contained the v19 column) must be walked
    up to the current version with IF NOT EXISTS DDL, not re-bootstrapped
    and not crashed by duplicate_column."""
    raw = _FakeRaw(bootstrapped=True, version=18)
    store = _store_with_fake_pool(tmp_path, monkeypatch, raw)
    await store.init()
    sqls = _sqls(raw)
    applied = [s for s in sqls if "ADD COLUMN IF NOT EXISTS repo" in s]
    assert applied, "migration 19 was not applied to the stamped-18 schema"
    assert raw.version == _latest_migration_version()
    # No re-bootstrap of the consolidated body on the upgrade path.
    assert not any("coord_leader_lease" in s for s in sqls)
    # The whole upgrade runs inside the advisory-locked transaction.
    for sql, _args, tx_open in raw.executed:
        if "ADD COLUMN IF NOT EXISTS repo" in sql:
            assert tx_open is True


async def test_init_noop_when_schema_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _latest_migration_version()
    raw = _FakeRaw(bootstrapped=True, version=latest)
    store = _store_with_fake_pool(tmp_path, monkeypatch, raw)
    await store.init()
    sqls = _sqls(raw)
    assert not any("ADD COLUMN" in s for s in sqls)
    assert not any("coord_leader_lease" in s for s in sqls)
    assert raw.version == latest


async def test_init_refuses_schema_from_newer_coord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _FakeRaw(bootstrapped=True, version=_latest_migration_version() + 1)
    store = _store_with_fake_pool(tmp_path, monkeypatch, raw)
    with pytest.raises(RuntimeError, match="newer version"):
        await store.init()


# ---------------------------------------------------------------------------
# UTC pinning: day buckets and the session timezone.
# ---------------------------------------------------------------------------


def test_strftime_day_bucket_translates_to_utc() -> None:
    out = translate(
        "SELECT strftime('%Y-%m-%d', re.created_at) AS day, COUNT(*) "
        "FROM request_events re GROUP BY day"
    )
    assert "to_char(" in out
    assert "AT TIME ZONE 'UTC'" in out


async def test_set_search_path_pins_session_timezone_to_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _FakeRaw(bootstrapped=True, version=_latest_migration_version())
    store = _store_with_fake_pool(tmp_path, monkeypatch, raw)
    await store._set_search_path(raw)
    sql = raw.executed[-1][0]
    assert "search_path" in sql
    assert "SET TIME ZONE 'UTC'" in sql


# ---------------------------------------------------------------------------
# asyncpg fail-fast guard.
# ---------------------------------------------------------------------------


def test_postgres_store_requires_asyncpg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pg_backend, "asyncpg", None)
    with pytest.raises(RuntimeError, match="asyncpg"):
        PostgresStore(tmp_path / "guard.sqlite")


def test_database_dispatch_fails_fast_without_asyncpg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup shape: COORD_DATABASE_URL selects PG, driver missing --
    Database(path) must raise the clear guard error at construction."""
    from coordination.db import Database

    monkeypatch.setenv("COORD_DATABASE_URL", "postgresql://u:p@localhost/x")
    monkeypatch.setattr(pg_backend, "asyncpg", None)
    with pytest.raises(RuntimeError, match="COORD_DATABASE_URL"):
        Database(tmp_path / "guard.sqlite")
