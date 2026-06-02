"""Tests for the symbol-aware overlap helpers (``coordination/overlap_symbols``).

Covers every branch of the ``check_overlap`` decision table from
``docs/design/sub-file-claims.md`` plus the auto-resolution side-effect
helper. Fixtures use ``aiosqlite`` directly to flip ``scope_type`` and
``narrowable`` on inserted claims, since the production insert path
treats both as defaulted columns.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
import pytest

from coordination.db import Database, _configure_sqlite
from coordination.overlap_symbols import (
    OverlapKind,
    OverlapResult,
    check_overlap,
    record_auto_resolution,
)


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """Fresh Database per test. Schema is migrated to current on init."""
    d = Database(tmp_path / "overlap_symbols.sqlite")
    await d.init()
    yield d


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _future() -> str:
    return _iso(datetime.now(UTC) + timedelta(hours=4))


async def _seed_file_claim(
    db: Database,
    *,
    pattern: str,
    engineer: str = "alice",
    claim_type: str = "file",
    narrowable: bool = True,
    repo: str = "amittell/coord",
    session_id: str | None = None,
) -> str:
    """Insert a whole-file claim and return its id.

    ``insert_claims_batch`` leaves ``scope_type='file'`` (default) and
    ``narrowable=1``. When the test wants ``narrowable=False`` we do a
    follow-up UPDATE so the column persists what the conflict engine
    will read.
    """
    cid = str(uuid4())
    await db.insert_claims_batch(
        engineer=engineer,
        branch=None,
        description=None,
        items=[(cid, claim_type, pattern, "soft", _future())],
        repo=repo,
        session_id=session_id,
    )
    if not narrowable:
        async with aiosqlite.connect(db.path) as conn:
            await _configure_sqlite(conn)
            await conn.execute(
                "UPDATE claims SET narrowable = 0 WHERE id = ?", (cid,)
            )
            await conn.commit()
    return cid


async def _seed_symbol_claim(
    db: Database,
    *,
    pattern: str,
    symbols_by_file: dict[str, list[str]],
    engineer: str = "alice",
    claim_type: str = "file",
    repo: str = "amittell/coord",
    session_id: str | None = None,
) -> str:
    """Insert a symbol-scope claim: file row with scope_type='symbol'
    plus one claim_symbols row per (file, symbol) entry.
    """
    cid = await _seed_file_claim(
        db,
        pattern=pattern,
        engineer=engineer,
        claim_type=claim_type,
        narrowable=False,  # symbol claims are never narrowable
        repo=repo,
        session_id=session_id,
    )
    async with aiosqlite.connect(db.path) as conn:
        await _configure_sqlite(conn)
        await conn.execute(
            "UPDATE claims SET scope_type = 'symbol' WHERE id = ?", (cid,)
        )
        await conn.commit()
    rows: list[tuple[str, str, str, str, str]] = []
    for f, syms in symbols_by_file.items():
        for s in syms:
            rows.append((str(uuid4()), cid, f, s, "function"))
    await db.insert_claim_symbols(rows=rows)
    return cid


async def _load_claim(db: Database, claim_id: str) -> dict[str, Any]:
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        await _configure_sqlite(conn)
        cur = await conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,))
        row = await cur.fetchone()
        assert row is not None, f"claim {claim_id!r} not found"
        return dict(row)


# ---------------------------------------------------------------------------
# file/file
# ---------------------------------------------------------------------------


async def test_file_file_disjoint_paths_is_no_overlap(db: Database) -> None:
    """Two whole-file claims on different paths -> NO_OVERLAP, no
    symbol detail. This is the baseline backwards-compat path: nothing
    about scope_type / narrowable should change the legacy answer.
    """
    holder_id = await _seed_file_claim(db, pattern="src/auth/login.ts")
    holder = await _load_claim(db, holder_id)

    result: OverlapResult = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/billing/index.ts",
        requester_scope_type="file",
        requester_symbols_by_file={},
    )

    assert result.kind is OverlapKind.NO_OVERLAP
    assert result.overlapping_paths == ()
    assert result.overlapping_symbols == ()


async def test_file_file_overlapping_paths_is_file_overlap(db: Database) -> None:
    """Two whole-file claims on the same path -> FILE_OVERLAP. The
    legacy 409 branch must keep firing for unchanged client behaviour.
    """
    holder_id = await _seed_file_claim(db, pattern="src/auth/login.ts")
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="file",
        requester_symbols_by_file={},
    )

    assert result.kind is OverlapKind.FILE_OVERLAP
    assert result.overlapping_paths  # non-empty
    assert result.overlapping_symbols == ()


# ---------------------------------------------------------------------------
# symbol/symbol
# ---------------------------------------------------------------------------


async def test_symbol_symbol_disjoint_symbols_is_auto_coexist(db: Database) -> None:
    """Two symbol-scope claims on the same file with disjoint symbol
    sets must auto-coexist. This is the headline v0.14 case: two
    agents editing different functions in the same file get a 201,
    not a 409.
    """
    holder_id = await _seed_symbol_claim(
        db,
        pattern="src/auth/login.ts",
        symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="symbol",
        requester_symbols_by_file={"src/auth/login.ts": ["logSignin"]},
    )

    assert result.kind is OverlapKind.AUTO_COEXIST
    assert result.overlapping_symbols == ()


async def test_symbol_symbol_overlapping_symbols_is_symbol_overlap(
    db: Database,
) -> None:
    """When the symbol sets intersect, only the colliding symbols are
    reported -- non-overlapping symbols on the same file are filtered
    out so the dashboard / pre-push hook can render a minimal diff.
    """
    holder_id = await _seed_symbol_claim(
        db,
        pattern="src/auth/login.ts",
        symbols_by_file={
            "src/auth/login.ts": ["handleLogin", "logSignin"],
        },
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="symbol",
        requester_symbols_by_file={
            "src/auth/login.ts": ["handleLogin", "validateCredentials"],
        },
    )

    assert result.kind is OverlapKind.SYMBOL_OVERLAP
    assert result.overlapping_symbols == (
        ("src/auth/login.ts", ("handleLogin",)),
    )


async def test_symbol_symbol_different_files_is_no_overlap(db: Database) -> None:
    """Two symbol claims on disjoint files -> NO_OVERLAP from the path
    engine; the symbol branch never runs.
    """
    holder_id = await _seed_symbol_claim(
        db,
        pattern="src/auth/login.ts",
        symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/billing/charge.ts",
        requester_scope_type="symbol",
        requester_symbols_by_file={"src/billing/charge.ts": ["handleLogin"]},
    )

    assert result.kind is OverlapKind.NO_OVERLAP


# ---------------------------------------------------------------------------
# file (holder) + symbol (requester)
# ---------------------------------------------------------------------------


async def test_holder_file_narrowable_requester_symbol_is_auto_narrow(
    db: Database,
) -> None:
    """A narrowable file holder + symbol requester -> AUTO_NARROW. The
    server-side default: pre-v0.14 claims (narrowable=1) get auto-narrowed
    transparently when a v0.14 client comes along with a symbol claim.
    """
    holder_id = await _seed_file_claim(
        db, pattern="src/auth/login.ts", narrowable=True
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="symbol",
        requester_symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )

    assert result.kind is OverlapKind.AUTO_NARROW
    assert result.overlapping_symbols == (
        ("src/auth/login.ts", ("handleLogin",)),
    )


async def test_holder_file_not_narrowable_requester_symbol_is_file_overlap(
    db: Database,
) -> None:
    """Explicit narrowable=False on a file claim forces the legacy
    request flow even when the requester is symbol-scoped.
    """
    holder_id = await _seed_file_claim(
        db, pattern="src/auth/login.ts", narrowable=False
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="symbol",
        requester_symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )

    assert result.kind is OverlapKind.FILE_OVERLAP


async def test_holder_shared_file_requester_symbol_is_file_overlap(
    db: Database,
) -> None:
    """shared_file's whole point is to surface overlap; auto-narrow
    would defeat that. The branch must reject auto-narrow regardless
    of the narrowable column.
    """
    holder_id = await _seed_file_claim(
        db,
        pattern="src/auth/login.ts",
        claim_type="shared_file",
        narrowable=True,  # even with narrowable=1, shared_file blocks
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="symbol",
        requester_symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )

    assert result.kind is OverlapKind.FILE_OVERLAP


async def test_holder_module_requester_symbol_is_file_overlap(db: Database) -> None:
    """module is the deliberately coarse claim type; the operator opted
    in to whole-tree scope so auto-narrowing under them is a surprise.
    """
    holder_id = await _seed_file_claim(
        db,
        pattern="src/auth/**",
        claim_type="module",
        narrowable=True,  # even with narrowable=1, module blocks
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="symbol",
        requester_symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )

    assert result.kind is OverlapKind.FILE_OVERLAP


# ---------------------------------------------------------------------------
# symbol (holder) + file (requester)
# ---------------------------------------------------------------------------


async def test_holder_symbol_requester_file_is_partial_grant(db: Database) -> None:
    """A symbol-scope holder + whole-file requester is a PARTIAL_GRANT
    case: the holder owns only some symbols and the requester wants
    the whole file. Decision UX is the caller's; we just label it.
    """
    holder_id = await _seed_symbol_claim(
        db,
        pattern="src/auth/login.ts",
        symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )
    holder = await _load_claim(db, holder_id)

    result = await check_overlap(
        db=db,
        holder=holder,
        requester_pattern="src/auth/login.ts",
        requester_scope_type="file",
        requester_symbols_by_file={},
    )

    assert result.kind is OverlapKind.PARTIAL_GRANT


# ---------------------------------------------------------------------------
# record_auto_resolution
# ---------------------------------------------------------------------------


async def _coexists_with(db: Database, claim_id: str) -> list[str]:
    row = await _load_claim(db, claim_id)
    raw = row.get("coexists_with")
    if not raw:
        return []
    return list(json.loads(raw))


async def _count_auto_events(
    db: Database,
    *,
    event_type: str,
    holder_claim_id: str,
    requester_claim_id: str,
) -> int:
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        await _configure_sqlite(conn)
        cur = await conn.execute(
            """
            SELECT COUNT(*) AS n FROM request_events
            WHERE event_type = ?
              AND request_id IS NULL
              AND json_extract(detail, '$.holder_claim_id') = ?
              AND json_extract(detail, '$.requester_claim_id') = ?
            """,
            (event_type, holder_claim_id, requester_claim_id),
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0


async def test_record_auto_resolution_attaches_partners_both_ways(
    db: Database,
) -> None:
    """AUTO_COEXIST resolution must wire pairwise coexist edges in both
    directions and write exactly one audit row.
    """
    holder_id = await _seed_symbol_claim(
        db,
        pattern="src/auth/login.ts",
        symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
    )
    requester_id = await _seed_symbol_claim(
        db,
        pattern="src/auth/login.ts",
        symbols_by_file={"src/auth/login.ts": ["logSignin"]},
        engineer="bob",
    )

    await record_auto_resolution(
        db=db,
        kind=OverlapKind.AUTO_COEXIST,
        holder_claim_id=holder_id,
        requester_claim_id=requester_id,
        overlapping_paths=("src/auth/login.ts",),
        overlapping_symbols=(),
    )

    assert requester_id in await _coexists_with(db, holder_id)
    assert holder_id in await _coexists_with(db, requester_id)
    assert (
        await _count_auto_events(
            db,
            event_type="auto-coexist",
            holder_claim_id=holder_id,
            requester_claim_id=requester_id,
        )
        == 1
    )


async def test_record_auto_resolution_is_idempotent(db: Database) -> None:
    """Replaying record_auto_resolution must not duplicate partner ids
    or audit rows. The partner-attach helper is already idempotent at
    the DB layer; this confirms the event-insert check is too.
    """
    holder_id = await _seed_file_claim(db, pattern="src/auth/login.ts")
    requester_id = await _seed_symbol_claim(
        db,
        pattern="src/auth/login.ts",
        symbols_by_file={"src/auth/login.ts": ["handleLogin"]},
        engineer="bob",
    )

    # Fire twice with identical args.
    for _ in range(2):
        await record_auto_resolution(
            db=db,
            kind=OverlapKind.AUTO_NARROW,
            holder_claim_id=holder_id,
            requester_claim_id=requester_id,
            overlapping_paths=("src/auth/login.ts",),
            overlapping_symbols=(("src/auth/login.ts", ("handleLogin",)),),
        )

    holder_partners = await _coexists_with(db, holder_id)
    requester_partners = await _coexists_with(db, requester_id)
    assert holder_partners.count(requester_id) == 1
    assert requester_partners.count(holder_id) == 1
    assert (
        await _count_auto_events(
            db,
            event_type="auto-narrow",
            holder_claim_id=holder_id,
            requester_claim_id=requester_id,
        )
        == 1
    )


async def test_record_auto_resolution_rejects_other_kinds(db: Database) -> None:
    """Defensive guard: passing a non-auto OverlapKind is a programming
    error and must raise rather than silently no-op.
    """
    with pytest.raises(ValueError):
        await record_auto_resolution(
            db=db,
            kind=OverlapKind.FILE_OVERLAP,
            holder_claim_id="x",
            requester_claim_id="y",
            overlapping_paths=("a",),
            overlapping_symbols=(),
        )
