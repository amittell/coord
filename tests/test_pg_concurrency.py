"""Concurrency regression for the advisory-locked claim grant (design 5.1-5.4).

These tests are only meaningful on PostgreSQL: each ``create_claims`` call
opens its own ``db.transaction()`` -> its own pooled asyncpg connection, so a
fan-out of N concurrent claimers for the same scope is N *real* connections
contending on ``pg_advisory_xact_lock`` (design 5.1). On SQLite the single
event loop / single writer would make the test pass trivially and prove
nothing (design Section 9), so it is skipped there.

The invariant under test: among many concurrent claimers of an *overlapping*
scope in one repo -- including the NULL-repo bucket (design 5.4) and the
symbol-scoped path -- exactly one wins and the active-claim set never contains
a double grant.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from coordination.db import Database
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService

_PG_URL = os.environ.get("COORD_DATABASE_URL", "")
_PG_SELECTED = _PG_URL.startswith("postgresql://") or _PG_URL.startswith(
    "postgres://"
)

pytestmark = pytest.mark.skipif(
    not _PG_SELECTED,
    reason="advisory-lock concurrency is only meaningful on the PG backend",
)

# asyncpg pool max_size is 10; keep the fan-out comfortably above it so the
# contention is genuine (claimers queue for connections and for the lock).
_FANOUT = 16


def _make_service(tmp_path: Path) -> CoordinationService:
    from coordination.config import Settings

    db = Database(tmp_path / "concurrency.sqlite")
    settings = Settings(
        database_path=tmp_path / "concurrency.sqlite",
        allow_insecure_no_auth=True,
        max_claim_ratio=1.0,
    )
    return CoordinationService(db=db, settings=settings)


async def _active_rows_for_pattern(
    svc: CoordinationService, pattern: str, repo: str | None
) -> list[dict]:
    rows = await svc.db.list_active_claims_rows()
    return [r for r in rows if r.get("pattern") == pattern and r.get("repo") == repo]


async def _claim(
    svc: CoordinationService,
    *,
    engineer: str,
    pattern: str,
    repo: str | None,
    symbols: list[str] | None = None,
):
    return await svc.create_claims(
        CreateClaimsRequest(
            engineer=engineer,
            repo=repo,
            claims=[ClaimItem(type="file", pattern=pattern, symbols=symbols)],
        )
    )


async def test_concurrent_overlapping_file_claims_single_winner(
    tmp_path: Path,
) -> None:
    svc = _make_service(tmp_path)
    await svc.db.init()
    pattern = "src/contended.py"
    repo = "example-org/app"

    results = await asyncio.gather(
        *(
            _claim(svc, engineer=f"eng{i}", pattern=pattern, repo=repo)
            for i in range(_FANOUT)
        )
    )

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"expected exactly one winner, got {len(winners)}: "
        f"{[r.claim_ids for r in results]}"
    )
    # The decisive invariant: the active-claim set holds exactly one row for
    # the contended pattern. A double grant (the race the advisory lock
    # closes) would surface here as two.
    active = await _active_rows_for_pattern(svc, pattern, repo)
    assert len(active) == 1, f"double-active row: {active!r}"


async def test_concurrent_null_repo_claims_single_winner(
    tmp_path: Path,
) -> None:
    # design 5.4: pg_advisory_xact_lock(NULL) takes NO lock, so the NULL-repo
    # bucket would run unserialized and double-grant unless the key uses
    # coalesce(repo, ''). This is the regression guard for exactly that.
    svc = _make_service(tmp_path)
    await svc.db.init()
    pattern = "src/legacy_null.py"

    results = await asyncio.gather(
        *(
            _claim(svc, engineer=f"eng{i}", pattern=pattern, repo=None)
            for i in range(_FANOUT)
        )
    )

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"NULL-repo bucket double-granted: {[r.claim_ids for r in results]}"
    )
    active = await _active_rows_for_pattern(svc, pattern, None)
    assert len(active) == 1, f"double-active NULL-repo row: {active!r}"


async def test_concurrent_overlapping_symbol_claims_single_winner(
    tmp_path: Path,
) -> None:
    # Symbol-scoped claims on the SAME symbol of the SAME file overlap, so the
    # locked grant must still admit exactly one (disjoint symbols would
    # auto-coexist; identical symbols contend).
    svc = _make_service(tmp_path)
    await svc.db.init()
    pattern = "src/shared_module.py"
    repo = "example-org/app"

    results = await asyncio.gather(
        *(
            _claim(
                svc,
                engineer=f"eng{i}",
                pattern=pattern,
                repo=repo,
                symbols=["TargetClass"],
            )
            for i in range(_FANOUT)
        )
    )

    winners = [r for r in results if r.claim_ids]
    assert len(winners) == 1, (
        f"symbol claim double-granted: {[r.claim_ids for r in results]}"
    )
    active = await _active_rows_for_pattern(svc, pattern, repo)
    assert len(active) == 1, f"double-active symbol row: {active!r}"
