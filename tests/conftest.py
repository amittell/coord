"""Test harness shared fixtures / backend shims.

Running the *entire* existing suite against PostgreSQL (design Section 9) is
the primary guard against dialect drift. Most tests drive the public
``Database`` / service API, which already dispatches to ``PostgresStore`` when
``COORD_DATABASE_URL`` is a ``postgresql://`` DSN -- those run on PG with no
help here.

A subset of tests, however, reach *past* the abstraction and open a raw
``aiosqlite.connect(db.path)`` to seed rows or fast-forward timestamps in the
SQLite file directly. Under PG that file is empty (the data lives in a PG
schema).

The forward-looking fix is :func:`seam_connection`: a backend-agnostic test
DB accessor that routes seeds/reads through the store's own overridable
``_connect()`` seam, so a test lands in the SAME backend the public API uses
(the local SQLite file on the default suite, the ``PostgresStore`` schema on
PG) with NO monkeypatch. New tests should use it; existing raw-connect call
sites are being migrated onto it file by file.

Until every raw call site is migrated, a **transitional deprecation shim**
below keeps the un-migrated ones working on PG by redirecting
``aiosqlite.connect`` -- only when the PG backend is selected -- to a
connection bound to the same PG schema the ``PostgresStore`` for that path
uses (schema is derived deterministically from the path). The shim is
deprecated: it structurally masks the raw-SQLite seam-bypass bug class (a
production module reaching past ``Database`` to SQLite looks fine on PG only
because this shim silently reroutes it). Set
``COORD_TEST_DISABLE_AIOSQLITE_SHIM=1`` to prove a migrated file no longer
needs it.

When ``COORD_DATABASE_URL`` is unset (the default SQLite suite) the shim is
inert and ``aiosqlite.connect`` is untouched; :func:`seam_connection` works
identically on either backend.
"""

from __future__ import annotations

import os
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pytest

from coordination.db import _configure_sqlite

_PG_URL = os.environ.get("COORD_DATABASE_URL", "")
_PG_SELECTED = _PG_URL.startswith("postgresql://") or _PG_URL.startswith(
    "postgres://"
)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def seam_connection(db):
    """Backend-agnostic test DB access through the store's own seam.

    Opens a connection via the ``Database._connect()`` seam that
    ``PostgresStore`` overrides, so test-side seeds and reads land in the
    SAME backend the public API uses -- the local SQLite file under the
    default suite, the ``PostgresStore`` PG schema when
    ``COORD_DATABASE_URL`` selects Postgres -- with NO ``aiosqlite.connect``
    monkeypatch. The yielded connection has ``row_factory`` set and the
    SQLite pragmas applied (both no-ops on the PG adapter), matching every
    Database read/write path. Commits on clean exit, rolls back on error.

    This is the drop-in replacement for
    ``async with aiosqlite.connect(db.path) as conn:`` in tests: swap the
    connect for ``async with seam_connection(db) as conn:`` and drop any
    manual ``_configure_sqlite`` call or trailing ``await conn.commit()``
    (both are handled here). Because it dispatches through the instance's
    own ``_connect``, it never touches the deprecated aiosqlite shim and is
    correct on both backends by construction.
    """
    await db.init()
    async with db._connect() as conn:
        conn.row_factory = aiosqlite.Row
        await _configure_sqlite(conn)
        try:
            yield conn
            await conn.commit()
        except BaseException:
            try:
                await conn.rollback()
            except Exception:
                pass
            raise


@pytest.fixture()
def seam_conn():
    """Fixture wrapper around :func:`seam_connection` for tests that prefer
    dependency injection over an import: ``async with seam_conn(db) as conn:``.
    Returns the context-manager factory itself, so a single fixture serves any
    number of connections within a test."""
    return seam_connection


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``@pytest.mark.sqlite_only`` tests when running against Postgres.

    Those tests exercise SQLite-internal mechanics the PG backend replaces by
    design (the migrations chain -> a consolidated v1 schema; flock/subprocess
    single-writer -> PG advisory locks; design Sections 7 / 7.4). They are not
    meaningful on PG and reaching into the empty local SQLite file would only
    produce noise. Inert in the default SQLite suite, where these tests run and
    must pass."""
    if not _PG_SELECTED:
        return
    skip_pg = pytest.mark.skip(
        reason="sqlite_only: SQLite-internal mechanics (migrations chain / "
        "flock single-writer) replaced by design on the Postgres backend"
    )
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip_pg)


if _PG_SELECTED:
    from coordination.pg_backend import PostgresStore, terminate_pool

    @pytest.fixture(autouse=True)
    async def _pg_drain_pool():
        """Run after every test (PG mode): terminate the asyncpg pool while the
        event loop is still alive. Some tests spawn background tasks (wave-2
        callsite enrichment) and never drain them; on asyncpg a task left
        blocked on a PG round-trip does not respond to the loop-close
        cancellation and hangs ``_cancel_all_tasks`` forever. Forcibly closing
        the pool's transports here unblocks those tasks (connection error,
        swallowed at debug level by the enrichment) so the loop closes cleanly.
        Inert on SQLite (this module only loads the fixture in PG mode)."""
        yield
        terminate_pool()

    # Transitional deprecation shim (see module docstring). Kept ON by default
    # so the ~60 not-yet-migrated raw ``aiosqlite.connect(db.path)`` call sites
    # still resolve to the PG schema. It masks the raw-SQLite seam-bypass bug
    # class, so it is deprecated: migrate call sites onto ``seam_connection``
    # and, once a file is migrated, prove it no longer depends on the shim by
    # running with ``COORD_TEST_DISABLE_AIOSQLITE_SHIM=1``.
    if not _env_truthy("COORD_TEST_DISABLE_AIOSQLITE_SHIM"):
        warnings.warn(
            "tests/conftest.py is monkeypatching aiosqlite.connect for the "
            "Postgres backend. This shim is deprecated: it masks any code "
            "path that reaches past the Database abstraction to raw SQLite. "
            "Migrate test seeds/reads onto conftest.seam_connection(db); set "
            "COORD_TEST_DISABLE_AIOSQLITE_SHIM=1 once a file is migrated.",
            DeprecationWarning,
            stacklevel=2,
        )

        _orig_aiosqlite_connect = aiosqlite.connect

        def _pg_aiosqlite_connect(database, *args, **kwargs):
            """Drop-in for ``aiosqlite.connect(path)`` that, in PG mode,
            returns an async context manager bound to the PG schema for
            ``path`` -- the same schema the ``PostgresStore`` for that path
            manages. Extra args (timeout, isolation_level, ...) are
            SQLite-only and ignored."""
            store = PostgresStore(Path(database))
            return store._connect()

        aiosqlite.connect = _pg_aiosqlite_connect  # type: ignore[assignment]
