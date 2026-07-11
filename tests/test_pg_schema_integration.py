"""Explicit PostgreSQL schema and durable SQLite-import regression tests.

The unit cases always run. The integration cases activate only when
``COORD_DATABASE_URL`` points at the real PostgreSQL CI service and prove the
operator sequence end to end: bootstrap/upgrade the configured application
schema, then import durable SQLite state into that exact schema.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType

import pytest

from coordination.config import (
    POSTGRES_SCHEMA_DEFAULT,
    Settings,
    validate_postgres_schema,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
_PG_URL = os.environ.get("COORD_DATABASE_URL", "")
_PG_SELECTED = _PG_URL.startswith(("postgresql://", "postgres://"))


def _load_migration_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "migrate_tokens_ownership.py"
    name = "migrate_tokens_ownership_schema_tests"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _build_source_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 19);

            CREATE TABLE engineer_tokens (
                id TEXT PRIMARY KEY,
                engineer TEXT NOT NULL,
                token_sha256 TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                repo TEXT
            );
            INSERT INTO engineer_tokens (
                id, engineer, token_sha256, description, created_at,
                request_count, repo
            ) VALUES (
                'tok-imported', 'migration-test',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'durable import', '2026-07-11T12:00:00Z', 7,
                'amittell/coord'
            );

            CREATE TABLE ownership_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                yaml_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO ownership_config (id, yaml_text, updated_at)
            VALUES (1, 'shared_files:\n  - src/shared/**\n',
                    '2026-07-11T12:01:00Z');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_runtime_and_migration_schema_validators_stay_in_lockstep() -> None:
    migration = _load_migration_script()
    valid = ["coord", "coord_install_2", "_coord"]
    invalid = [
        "",
        "Coord",
        "coord-prod",
        "1coord",
        "a" * 64,
        "public",
        "information_schema",
        "pg_temp",
        'coord";drop schema public;--',
    ]
    for value in valid:
        assert validate_postgres_schema(value) == value
        assert migration.validate_postgres_schema(value) == value
    for value in invalid:
        with pytest.raises(ValueError):
            validate_postgres_schema(value)
        with pytest.raises(ValueError):
            migration.validate_postgres_schema(value)


def test_settings_distinguish_default_from_operator_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COORD_POSTGRES_SCHEMA", raising=False)
    default = Settings()
    assert default.postgres_schema == POSTGRES_SCHEMA_DEFAULT == "coord"
    assert default.postgres_schema_is_explicit is False

    monkeypatch.setenv("COORD_POSTGRES_SCHEMA", "coord_install_2")
    configured = Settings()
    assert configured.postgres_schema == "coord_install_2"
    assert configured.postgres_schema_is_explicit is True

def test_postgres_store_prefers_explicit_schema_but_keeps_low_level_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coordination.pg_backend as pg_backend
    from coordination.pg_backend import PostgresStore, _schema_for_path

    if pg_backend.asyncpg is None:
        monkeypatch.setattr(pg_backend, "asyncpg", object())
    monkeypatch.delenv("COORD_POSTGRES_SCHEMA", raising=False)

    first_path = tmp_path / "one.sqlite"
    second_path = tmp_path / "two.sqlite"
    explicit_a = PostgresStore(first_path, postgres_schema="coord_install")
    explicit_b = PostgresStore(second_path, postgres_schema="coord_install")
    fallback_a = PostgresStore(first_path)
    fallback_b = PostgresStore(second_path)

    assert explicit_a._schema == explicit_b._schema == "coord_install"
    assert fallback_a._schema == _schema_for_path(first_path)
    assert fallback_b._schema == _schema_for_path(second_path)
    assert fallback_a._schema != fallback_b._schema


def test_durable_import_sql_qualifies_exact_target_schema(tmp_path: Path) -> None:
    migration = _load_migration_script()
    source = tmp_path / "source.sqlite"
    _build_source_sqlite(source)

    sql = migration.build_sql(source, "coord_install_2")

    assert "-- target Postgres schema: coord_install_2" in sql
    assert 'INSERT INTO "coord_install_2"."engineer_tokens"' in sql
    assert 'INSERT INTO "coord_install_2"."ownership_config"' in sql
    assert "INSERT INTO engineer_tokens" not in sql
    assert "INSERT INTO ownership_config" not in sql


def _integration_schema(tmp_path: Path, label: str) -> str:
    digest = hashlib.sha1(str(tmp_path).encode()).hexdigest()[:12]
    return f"coord_it_{label}_{digest}"


@asynccontextmanager
async def _clean_pg_schemas(*schemas: str):
    from coordination.pg_backend import asyncpg, terminate_pool

    assert asyncpg is not None
    terminate_pool()
    conn = await asyncpg.connect(_PG_URL)
    try:
        for schema in schemas:
            validate_postgres_schema(schema)
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()
    try:
        yield
    finally:
        terminate_pool()
        conn = await asyncpg.connect(_PG_URL)
        try:
            for schema in schemas:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await conn.close()


@pytest.mark.skipif(not _PG_SELECTED, reason="requires the real PostgreSQL CI service")
async def test_real_pg_fresh_bootstrap_uses_configured_schema(
    tmp_path: Path,
) -> None:
    from coordination.db import CURRENT_SCHEMA_VERSION
    from coordination.pg_backend import PostgresStore, asyncpg

    assert asyncpg is not None
    schema = _integration_schema(tmp_path, "fresh")
    async with _clean_pg_schemas(schema):
        store = PostgresStore(tmp_path / "vestigial.sqlite", postgres_schema=schema)
        await store.init()

        conn = await asyncpg.connect(_PG_URL)
        try:
            version = await conn.fetchval(
                f'SELECT version FROM "{schema}".schema_version WHERE id = 1'
            )
            tables = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = $1",
                schema,
            )
        finally:
            await conn.close()
        assert version == CURRENT_SCHEMA_VERSION
        assert tables > 10


@pytest.mark.skipif(not _PG_SELECTED, reason="requires the real PostgreSQL CI service")
async def test_real_pg_existing_schema_is_upgraded_in_place(tmp_path: Path) -> None:
    from coordination.db import CURRENT_SCHEMA_VERSION
    from coordination.pg_backend import PostgresStore, asyncpg

    assert asyncpg is not None
    schema = _integration_schema(tmp_path, "upgrade")
    async with _clean_pg_schemas(schema):
        await PostgresStore(tmp_path / "first.sqlite", postgres_schema=schema).init()

        conn = await asyncpg.connect(_PG_URL)
        try:
            await conn.execute(f'ALTER TABLE "{schema}".engineer_tokens DROP COLUMN repo')
            await conn.execute(f'UPDATE "{schema}".schema_version SET version = 18 WHERE id = 1')
        finally:
            await conn.close()

        await PostgresStore(tmp_path / "different.sqlite", postgres_schema=schema).init()

        conn = await asyncpg.connect(_PG_URL)
        try:
            version = await conn.fetchval(
                f'SELECT version FROM "{schema}".schema_version WHERE id = 1'
            )
            repo_column = await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = 'engineer_tokens' "
                "AND column_name = 'repo'",
                schema,
            )
        finally:
            await conn.close()
        assert version == CURRENT_SCHEMA_VERSION
        assert repo_column == 1


@pytest.mark.skipif(not _PG_SELECTED, reason="requires the real PostgreSQL CI service")
async def test_real_pg_implicit_default_refuses_legacy_beta_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordination.pg_backend import PostgresStore, _schema_for_path

    monkeypatch.delenv("COORD_POSTGRES_SCHEMA", raising=False)
    path = tmp_path / "legacy-beta.sqlite"
    other_path = tmp_path / "other-install.sqlite"
    legacy_schema = _schema_for_path(path)
    other_schema = _schema_for_path(other_path)
    async with _clean_pg_schemas(legacy_schema, other_schema):
        await PostgresStore(path).init()
        await PostgresStore(other_path).init()

        current = PostgresStore(
            path,
            postgres_schema="coord",
            postgres_schema_explicit=False,
        )
        with pytest.raises(RuntimeError, match=legacy_schema) as exc_info:
            await current.init()
        message = str(exc_info.value)
        assert message.index(repr(legacy_schema)) < message.index(repr(other_schema))
        assert "matching the current COORD_DATABASE_PATH" in message


@pytest.mark.skipif(not _PG_SELECTED, reason="requires the real PostgreSQL CI service")
async def test_real_pg_durable_sqlite_import_lands_only_in_app_schema(
    tmp_path: Path,
) -> None:
    from coordination.pg_backend import PostgresStore, asyncpg

    assert asyncpg is not None
    migration = _load_migration_script()
    schema = _integration_schema(tmp_path, "import")
    sibling = _integration_schema(tmp_path, "sibling")
    async with _clean_pg_schemas(schema, sibling):
        store = PostgresStore(tmp_path / "app.sqlite", postgres_schema=schema)
        sibling_store = PostgresStore(tmp_path / "sibling.sqlite", postgres_schema=sibling)
        await store.init()
        await sibling_store.init()

        source = tmp_path / "durable.sqlite"
        _build_source_sqlite(source)
        sql = migration.build_sql(source, schema)

        conn = await asyncpg.connect(_PG_URL)
        try:
            # Apply twice: the schema-qualified import must also remain
            # idempotent under the exact operator rerun shape.
            await conn.execute(sql)
            await conn.execute(sql)
        finally:
            await conn.close()

        tokens = await store.list_engineer_tokens(include_revoked=True)
        assert len(tokens) == 1
        assert tokens[0]["id"] == "tok-imported"
        assert tokens[0]["repo"] == "amittell/coord"
        assert await store.get_ownership_yaml() == "shared_files:\n  - src/shared/**\n"
        assert await sibling_store.list_engineer_tokens(include_revoked=True) == []
        assert await sibling_store.get_ownership_yaml() is None
