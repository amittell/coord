"""Regression guards for the per-repo / per-engineer advisory locks (design
5.3, 5.4) on the Postgres backend.

The latent risk these tests close: a ``pg_advisory_xact_lock`` only serializes
the grant if a transaction is actually open on the very connection it is issued
on. Issued outside a transaction, asyncpg autocommits the ``SELECT`` in its own
single-statement transaction and the lock releases the instant that statement
returns -- leaving the overlap re-check + insert UNLOCKED (silent double-grant).
:meth:`PostgresStore.repo_lock` / :meth:`PostgresStore.engineer_lock` now
``_ensure_tx`` and hard-assert ``is_in_transaction`` before locking; these tests
fail if that guard regresses.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_PG_URL = os.environ.get("COORD_DATABASE_URL", "")
_PG_SELECTED = _PG_URL.startswith("postgresql://") or _PG_URL.startswith(
    "postgres://"
)

pytestmark = pytest.mark.skipif(
    not _PG_SELECTED,
    reason="advisory-lock semantics are only meaningful on the PG backend",
)


async def _store(tmp_path: Path):
    from coordination.pg_backend import PostgresStore, _get_pool

    store = PostgresStore(tmp_path / "locks.sqlite")
    await store.init()
    pool = await _get_pool(store._dsn)
    return store, pool


async def test_repo_lock_without_open_tx_raises(tmp_path: Path) -> None:
    """A managed adapter whose transaction was never started (the failure mode
    a buggy ``transaction()`` would produce) must raise rather than take a lock
    that immediately releases."""
    from coordination.pg_backend import _PGConnAdapter

    store, pool = await _store(tmp_path)
    raw = await pool.acquire()
    try:
        await store._set_search_path(raw)
        # managed=True so _ensure_tx is a no-op; we deliberately do NOT start a
        # transaction, mimicking the broken-invariant case.
        adapter = _PGConnAdapter(raw, managed=True)
        assert not raw.is_in_transaction()
        with pytest.raises(RuntimeError, match="open transaction"):
            await store.repo_lock(adapter, "example-org/app")
    finally:
        await pool.release(raw)


async def test_engineer_lock_without_open_tx_raises(tmp_path: Path) -> None:
    from coordination.pg_backend import _PGConnAdapter

    store, pool = await _store(tmp_path)
    raw = await pool.acquire()
    try:
        await store._set_search_path(raw)
        adapter = _PGConnAdapter(raw, managed=True)
        assert not raw.is_in_transaction()
        with pytest.raises(RuntimeError, match="open transaction"):
            async with store.engineer_lock(adapter, "eng-a"):
                pass
    finally:
        await pool.release(raw)


async def test_repo_lock_opens_lazy_tx_on_unmanaged_adapter(
    tmp_path: Path,
) -> None:
    """On an unmanaged adapter (lazy-tx model) repo_lock must force the
    transaction open BEFORE issuing the advisory lock, so the lock is held for
    the life of that transaction rather than autocommitting away."""
    from coordination.pg_backend import _PGConnAdapter

    store, pool = await _store(tmp_path)
    raw = await pool.acquire()
    try:
        await store._set_search_path(raw)
        adapter = _PGConnAdapter(raw, managed=False)
        assert not raw.is_in_transaction()
        await store.repo_lock(adapter, None)  # NULL-repo bucket
        # The lock forced a real transaction open; it is still active (held).
        assert raw.is_in_transaction()
        await adapter._rollback_open()
    finally:
        await pool.release(raw)


async def test_grant_transaction_holds_lock_to_commit(tmp_path: Path) -> None:
    """End-to-end: inside ``db.transaction()`` (the real grant path) the
    advisory lock is taken on a connection that is genuinely in a transaction,
    so ``pg_advisory_xact_lock`` survives to commit."""
    store, _pool = await _store(tmp_path)
    async with store.transaction() as conn:
        await store.repo_lock(conn, "example-org/app")
        assert conn._raw.is_in_transaction()
        async with store.engineer_lock(conn, "eng-a"):
            assert conn._raw.is_in_transaction()
