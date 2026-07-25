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
uses. An autouse fixture gives every test a unique valid
``COORD_POSTGRES_SCHEMA`` so application constructors that use the stable
production schema default remain isolated too. The shim is deprecated: it
structurally masks the raw-SQLite seam-bypass bug class (a production module
reaching past ``Database`` to SQLite looks fine on PG only because this shim
silently reroutes it). Set
``COORD_TEST_DISABLE_AIOSQLITE_SHIM=1`` to prove a migrated file no longer
needs it.

When ``COORD_DATABASE_URL`` is unset (the default SQLite suite) the shim is
inert and ``aiosqlite.connect`` is untouched; :func:`seam_connection` works
identically on either backend.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
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


@pytest.fixture(autouse=True)
def _isolate_coord_env_namespace():
    """Restore every ``COORD_*`` environment variable after each test.

    The name is deliberately narrow: this isolates the ``COORD_*`` NAMESPACE,
    not "coord's environment". The package also reads ``CLAUDE_SESSION_ID``
    and ``APPDATA``, which are out of scope here and safe to leave alone --
    both are read-only in ``coordination`` (``os.environ.get``, no assignment
    anywhere), so neither can produce the leak below. A test that sets them
    via ``monkeypatch`` is restored by ``monkeypatch`` itself; only a
    non-monkeypatch, process-global write can escape a test, and no such
    writer exists for them. If one is ever added, widen this fixture with it.

    ``mcp_server._load_local_env`` deliberately bootstraps ``os.environ``
    from the nearest ``.coordination/local.env`` -- correct for the
    long-lived MCP wrapper, but process-global. When the suite runs from a
    real checkout, any test that exercises that load path permanently
    exports the checkout's own ``COORD_REPO_ID`` (and URL/token) into the
    test process, and every later test that consults ``os.environ`` first
    (``claude_hooks._repo_id``) inherits it. Observed live: running
    ``test_mcp_server.py`` before ``test_fleet_enforcement.py`` flipped
    four hook tests from green to red because claims suddenly carried
    ``repo=amittell/coord``. Tests pass alone, fail warm -- the classic
    order-pollution signature. Snapshotting and restoring the ``COORD_*``
    namespace makes every test order-independent regardless of which
    module leaked first; intentional per-test env still goes through
    ``monkeypatch.setenv`` exactly as before.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith("COORD_")}
    yield
    for key in [k for k in os.environ if k.startswith("COORD_")]:
        if key in saved:
            os.environ[key] = saved[key]
        else:
            del os.environ[key]
    for key, val in saved.items():
        if key not in os.environ:
            os.environ[key] = val


def _is_coordination_task(task: "asyncio.Task") -> bool:
    """True when ``task`` is running a coroutine defined in the
    ``coordination`` package (so the drain below only touches this
    project's fire-and-forget tasks, never pytest-asyncio's own)."""
    coro = task.get_coro()
    code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
    return code is not None and "coordination/" in code.co_filename


@pytest.fixture(autouse=True)
async def _drain_coord_bg_tasks():
    """After every test, drain coordination's fire-and-forget background
    tasks and reap the LSP pool while the event loop is still alive.

    ``CoordinationService`` schedules callsite enrichment
    (``_enrich_claim_callsites``) as a fire-and-forget task that awaits an
    LSP ``references`` round-trip. That round-trip reads from a language
    server subprocess's stdout via an asyncio ``StreamReader``. When a test
    leaves such a task pending, pytest-asyncio's loop close cancels it via
    ``_cancel_all_tasks`` -- but a coroutine suspended on a subprocess-backed
    reader cannot reliably complete cancellation during loop teardown (the
    reader's EOF is delivered by the very loop that is shutting down), so
    ``_cancel_all_tasks`` waits on it forever and the whole run hangs. It is
    the SQLite/LSP twin of the asyncpg hang ``_pg_drain_pool`` guards
    against, and it stayed silent until ``pytest-timeout`` turned it into a
    hard failure. Production is unaffected: the ``asyncio.wait_for`` bound on
    each LSP request fires normally on a healthy loop.

    Draining here -- on the still-healthy loop, where cancellation is
    delivered normally -- lets those tasks unwind before the loop closes.
    Every step is time-bounded and error-swallowed so the fixture can never
    itself hang or fail a test, and it only touches tasks whose coroutine
    lives in ``coordination`` so pytest-asyncio's machinery is left alone.
    Inert when nothing was scheduled."""
    yield
    loop = asyncio.get_running_loop()
    me = asyncio.current_task()
    lingering = [
        obj
        for obj in gc.get_objects()
        if isinstance(obj, asyncio.Task)
        and obj is not me
        and not obj.done()
        and obj.get_loop() is loop
        and _is_coordination_task(obj)
    ]
    for task in lingering:
        task.cancel()
    if lingering:
        try:
            await asyncio.wait_for(
                asyncio.gather(*lingering, return_exceptions=True), 5
            )
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            pass
    try:
        from coordination import lsp as _lsp
    except Exception:  # noqa: BLE001 - import guard
        return
    if _lsp._POOL is not None:
        try:
            await asyncio.wait_for(_lsp._POOL.shutdown_all(), 5)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            pass
        _lsp._reset_pool()


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
    def _pg_schema_per_test(
        request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
    ):
        """Give every PG test a stable application schema of its own.

        Production defaults to the explicit ``coord`` schema. The full test
        suite, however, assumes each test starts with a fresh database and
        includes ``build_service()`` paths that now correctly use the
        configured schema instead of deriving one from ``database_path``.
        Hash the node id (plus worker/process identity) into a valid schema so
        those application-level constructors stay isolated under serial and
        xdist runs. Low-level multiple-store tests can still pass an explicit
        ``postgres_schema`` when they need two schemas inside one test.
        """
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        seed = f"{worker}:{os.getpid()}:{request.node.nodeid}"
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
        monkeypatch.setenv("COORD_POSTGRES_SCHEMA", f"coord_test_{digest}")

        from coordination import deps

        clear_cache = getattr(deps.get_service, "cache_clear", None)
        if clear_cache is not None:
            clear_cache()
        yield
        clear_cache = getattr(deps.get_service, "cache_clear", None)
        if clear_cache is not None:
            clear_cache()

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
