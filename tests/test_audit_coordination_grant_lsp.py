"""Audit regression tests: LSP span resolution must run in Phase A of
create_claims, OUTSIDE the claim-grant transaction.

The v0.44 writer queue makes ``db.transaction()`` hold the process-wide
``_writer_lock`` for the whole grant unit-of-work. ``_finalise_v14_scope``
used to perform its own ``documentSymbol`` roundtrips inside that block,
so a slow or timing-out language server stalled every write in the
process. These tests pin the fix: the LSP call happens before the
transaction opens (no bound connection, writer lock free) and the
resolved spans still land on the ``claim_symbols`` rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import coordination.db as db_mod
import coordination.service as service_mod
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
async def lsp_service(tmp_path: Path) -> CoordinationService:
    db_path = tmp_path / "grantlsp.sqlite"
    # writer_queue=True mirrors the production default
    # (settings.sqlite_writer_queue) so the writer-lock assertion below
    # exercises the exact configuration the finding described.
    db = Database(db_path, writer_queue=True)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
        lsp_enabled=True,
        max_claim_ratio=1.0,
        _env_file=None,
    )
    return CoordinationService(db=db, settings=settings)


async def test_lsp_span_resolution_runs_outside_grant_transaction(
    lsp_service: CoordinationService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documentSymbol roundtrip fires before db.transaction() opens:
    no connection is bound and the shared writer lock is free while the
    (fake) language server is on the wire. The LSP-resolved span still
    persists on the claim_symbols row."""

    (tmp_path / "mod.py").write_text(
        "def handler():\n    return 1\n", encoding="utf-8"
    )

    observed: list[dict[str, Any]] = []
    svc = lsp_service

    async def fake_lsp_document_symbols(
        self: CoordinationService,
        pattern: str,
        resolved: Path,
        cache: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        observed.append(
            {
                "pattern": pattern,
                "bound_conn": db_mod._BOUND_CONN.get() is not None,
                "writer_locked": svc.db._writer_lock.locked(),
            }
        )
        return [
            {
                "name": "handler",
                "parent_path": None,
                "start_line": 1,
                "start_col": 0,
                "end_line": 2,
                "end_col": 12,
            }
        ]

    monkeypatch.setattr(
        service_mod.CoordinationService,
        "_lsp_document_symbols",
        fake_lsp_document_symbols,
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            branch="feat",
            session_id="sess-a",
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )

    assert result.claim_ids, f"grant failed: {result.warnings!r}"
    assert observed, "the LSP span resolution was never invoked"
    for call in observed:
        assert call["bound_conn"] is False, (
            "LSP roundtrip executed inside the grant transaction "
            "(a connection was bound)"
        )
        assert call["writer_locked"] is False, (
            "LSP roundtrip executed while the v0.44 writer lock was held"
        )

    symbol_rows = await svc.db.get_claim_symbols(result.claim_ids[0])
    assert len(symbol_rows) == 1
    row = symbol_rows[0]
    assert row["symbol_name"] == "handler"
    assert row["resolved_by"] == "lsp"
    assert int(row["start_line"]) == 1
    assert int(row["end_line"]) == 2
    assert row["start_col"] is not None


async def test_parser_spans_persist_when_lsp_disabled(
    tmp_path: Path,
) -> None:
    """Sanity: with LSP off the pre-resolved parser spans still flow
    through _resolve_symbol_spans into the claim_symbols rows."""

    db_path = tmp_path / "grantparser.sqlite"
    db = Database(db_path, writer_queue=True)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
        lsp_enabled=False,
        max_claim_ratio=1.0,
        _env_file=None,
    )
    svc = CoordinationService(db=db, settings=settings)
    (tmp_path / "mod.py").write_text(
        "def handler():\n    return 1\n", encoding="utf-8"
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            branch="feat",
            session_id="sess-a",
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )

    assert result.claim_ids, f"grant failed: {result.warnings!r}"
    symbol_rows = await svc.db.get_claim_symbols(result.claim_ids[0])
    assert len(symbol_rows) == 1
    row = symbol_rows[0]
    assert row["resolved_by"] == "parser"
    assert int(row["start_line"]) == 1
    assert row["start_col"] is None
