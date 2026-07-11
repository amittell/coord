"""Audit regression tests for the rename-sweep collision guard.

The sweep's Python-side collision check reads a snapshot on its own
connection; every await between that read and the write is a yield point
a concurrent grant can land in. The authoritative recheck now lives
INSIDE ``update_claim_symbol_rename``'s BEGIN IMMEDIATE transaction
(``guard_new_path_collision=True``), so the check and the rewrite are
one atomic unit and the rename can never manufacture a second active
claim on the same or an ancestor/descendant symbol path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordination.config import Settings
from coordination.db import Database
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    from coordination.engine import _clear_ls_files_cache

    _clear_ls_files_cache()
    yield
    _clear_ls_files_cache()


@pytest.fixture()
async def service(tmp_path: Path) -> CoordinationService:
    db_path = tmp_path / "renameguard.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        _env_file=None,
    )
    return CoordinationService(db=db, settings=settings)


async def _symbol_claim(
    service: CoordinationService,
    *,
    engineer: str,
    session_id: str,
    pattern: str,
    symbols: list[str],
    repo: str | None = None,
) -> str:
    result = await service.create_claims(
        CreateClaimsRequest(
            engineer=engineer,
            branch=f"feat-{engineer}",
            session_id=session_id,
            repo=repo,
            claims=[ClaimItem(type="file", pattern=pattern, symbols=symbols)],
        )
    )
    assert result.claim_ids, (
        f"seed claim for {engineer} failed: {result.conflicts!r} "
        f"{result.warnings!r}"
    )
    return result.claim_ids[0]


async def test_guard_blocks_rename_onto_symbol_held_by_another_claim(
    service: CoordinationService,
) -> None:
    claim_a = await _symbol_claim(
        service,
        engineer="alice",
        session_id="sess-a",
        pattern="src/m.py",
        symbols=["old_fn"],
    )
    await _symbol_claim(
        service,
        engineer="bob",
        session_id="sess-b",
        pattern="src/m.py",
        symbols=["new_fn"],
    )

    updated = await service.db.update_claim_symbol_rename(
        claim_a,
        file_path="src/m.py",
        old_symbol_name="old_fn",
        new_symbol_name="new_fn",
        new_start_line=1,
        new_start_col=None,
        new_end_line=2,
        new_end_col=None,
        resolved_by="parser",
        new_pattern=None,
        guard_new_path_collision=True,
        repo=None,
    )
    assert updated is False, (
        "guarded rename must abort when another active claim already "
        "holds the new symbol path on this file"
    )

    rows = await service.db.get_claim_symbols(claim_a)
    assert [r["symbol_name"] for r in rows] == ["old_fn"], (
        "aborted rename must leave the claim_symbols row untouched"
    )
    renames = await service.db.list_symbol_renames_for_claims([claim_a])
    assert renames == [], "aborted rename must not write an audit row"


@pytest.mark.parametrize("held_symbol", ["New::method", "New::Inner::method"])
async def test_guard_blocks_rename_onto_ancestor_of_held_symbol(
    service: CoordinationService,
    held_symbol: str,
) -> None:
    claim_a = await _symbol_claim(
        service,
        engineer="alice",
        session_id="sess-a",
        pattern="src/m.py",
        symbols=["Old"],
    )
    claim_b = await _symbol_claim(
        service,
        engineer="bob",
        session_id="sess-b",
        pattern="src/m.py",
        symbols=[held_symbol],
    )

    updated = await service.db.update_claim_symbol_rename(
        claim_a,
        file_path="src/m.py",
        old_symbol_name="Old",
        new_symbol_name="New",
        new_start_line=1,
        new_start_col=None,
        new_end_line=2,
        new_end_col=None,
        resolved_by="parser",
        new_pattern=None,
        guard_new_path_collision=True,
        repo=None,
    )

    assert updated is False
    assert [
        (row["parent_symbol"], row["symbol_name"])
        for row in await service.db.get_claim_symbols(claim_a)
    ] == [(None, "Old")]
    assert [
        (row["parent_symbol"], row["symbol_name"])
        for row in await service.db.get_claim_symbols(claim_b)
    ] == [held_symbol.rpartition("::")[::2]]
    assert await service.db.list_symbol_renames_for_claims([claim_a]) == []


async def test_guard_allows_rename_next_to_nonoverlapping_symbol_prefix(
    service: CoordinationService,
) -> None:
    claim_a = await _symbol_claim(
        service,
        engineer="alice",
        session_id="sess-a",
        pattern="src/m.py",
        symbols=["Old"],
    )
    await _symbol_claim(
        service,
        engineer="bob",
        session_id="sess-b",
        pattern="src/m.py",
        symbols=["Newer::method"],
    )

    updated = await service.db.update_claim_symbol_rename(
        claim_a,
        file_path="src/m.py",
        old_symbol_name="Old",
        new_symbol_name="New",
        new_start_line=1,
        new_start_col=None,
        new_end_line=2,
        new_end_col=None,
        resolved_by="parser",
        new_pattern=None,
        guard_new_path_collision=True,
        repo=None,
    )

    assert updated is True
    assert [
        (row["parent_symbol"], row["symbol_name"])
        for row in await service.db.get_claim_symbols(claim_a)
    ] == [(None, "New")]


async def test_guard_allows_rename_when_new_path_is_free(
    service: CoordinationService,
) -> None:
    claim_a = await _symbol_claim(
        service,
        engineer="alice",
        session_id="sess-a",
        pattern="src/m.py",
        symbols=["old_fn"],
    )

    updated = await service.db.update_claim_symbol_rename(
        claim_a,
        file_path="src/m.py",
        old_symbol_name="old_fn",
        new_symbol_name="brand_new",
        new_start_line=1,
        new_start_col=None,
        new_end_line=2,
        new_end_col=None,
        resolved_by="parser",
        new_pattern=None,
        guard_new_path_collision=True,
        repo=None,
    )
    assert updated is True

    rows = await service.db.get_claim_symbols(claim_a)
    assert [r["symbol_name"] for r in rows] == ["brand_new"]
    renames = await service.db.list_symbol_renames_for_claims([claim_a])
    assert len(renames) == 1
    assert renames[0]["new_symbol_name"] == "brand_new"


async def test_guard_scopes_collision_to_repo_bucket(
    service: CoordinationService,
) -> None:
    """A claim holding the target symbol in a DIFFERENT repo bucket does
    not block the rename -- matching the conflict pipeline's repo
    bucketing."""

    claim_a = await _symbol_claim(
        service,
        engineer="alice",
        session_id="sess-a",
        pattern="src/m.py",
        symbols=["old_fn"],
        repo=None,
    )
    await _symbol_claim(
        service,
        engineer="bob",
        session_id="sess-b",
        pattern="src/m.py",
        symbols=["new_fn"],
        repo="org/other",
    )

    updated = await service.db.update_claim_symbol_rename(
        claim_a,
        file_path="src/m.py",
        old_symbol_name="old_fn",
        new_symbol_name="new_fn",
        new_start_line=1,
        new_start_col=None,
        new_end_line=2,
        new_end_col=None,
        resolved_by="parser",
        new_pattern=None,
        guard_new_path_collision=True,
        repo=None,
    )
    assert updated is True

    rows = await service.db.get_claim_symbols(claim_a)
    assert [r["symbol_name"] for r in rows] == ["new_fn"]
