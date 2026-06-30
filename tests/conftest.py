"""Test harness shared fixtures / backend shims.

Running the *entire* existing suite against PostgreSQL (design Section 9) is
the primary guard against dialect drift. Most tests drive the public
``Database`` / service API, which already dispatches to ``PostgresStore`` when
``COORD_DATABASE_URL`` is a ``postgresql://`` DSN -- those run on PG with no
help here.

A subset of tests, however, reach *past* the abstraction and open a raw
``aiosqlite.connect(db.path)`` to seed rows or fast-forward timestamps in the
SQLite file directly. Under PG that file is empty (the data lives in a PG
schema). To keep those tests meaningful on PG without rewriting each one, we
redirect ``aiosqlite.connect`` -- only when the PG backend is selected -- to a
connection bound to the *same* PG schema the ``PostgresStore`` for that path
uses (schema is derived deterministically from the path). The raw SQLite SQL
the tests run is translated by the same dialect layer the store uses, so the
seed/read lands exactly where the API reads/writes.

When ``COORD_DATABASE_URL`` is unset (the default SQLite suite) this module is
inert and ``aiosqlite.connect`` is untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

_PG_URL = os.environ.get("COORD_DATABASE_URL", "")
_PG_SELECTED = _PG_URL.startswith("postgresql://") or _PG_URL.startswith(
    "postgres://"
)


if _PG_SELECTED:
    import aiosqlite
    import pytest

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

    _orig_aiosqlite_connect = aiosqlite.connect

    def _pg_aiosqlite_connect(database, *args, **kwargs):
        """Drop-in for ``aiosqlite.connect(path)`` that, in PG mode, returns an
        async context manager bound to the PG schema for ``path`` -- the same
        schema the ``PostgresStore`` for that path manages. Extra args
        (timeout, isolation_level, ...) are SQLite-only and ignored."""
        store = PostgresStore(Path(database))
        return store._connect()

    aiosqlite.connect = _pg_aiosqlite_connect  # type: ignore[assignment]
