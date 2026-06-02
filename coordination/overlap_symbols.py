"""Symbol-aware overlap helpers for sub-file (symbol-level) claims.

This module sits on top of the path-overlap engine in
:mod:`coordination.engine` and adds a post-filter step that knows about
``scope_type`` and the per-claim symbol set introduced in schema v8.
The conflict pipeline calls :func:`check_overlap` for each (holder,
requester) pair and routes the resulting :class:`OverlapResult.kind`
back into 201 / 409 / auto-resolution branches.

The split mirrors the design contract in
``docs/design/sub-file-claims.md`` ("Overlap algorithm" and "State
machine deltas") so the engine module stays pattern-only and this
module owns every branch that cares about ``scope_type``,
``narrowable``, or the ``claim_symbols`` table.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from coordination.engine import compute_overlap

if TYPE_CHECKING:
    from coordination.db import Database


class OverlapKind(str, Enum):
    """Result classes the conflict pipeline routes on.

    - ``NO_OVERLAP``: pattern intersection was empty; nothing to do.
    - ``FILE_OVERLAP``: legacy whole-file conflict; emit a 409 as today.
    - ``SYMBOL_OVERLAP``: both sides symbol-scope and at least one
      ``(file, symbol)`` pair collides; emit 409 with ``symbol_overlap``
      detail so the requester can see which symbols clashed.
    - ``AUTO_COEXIST``: both sides symbol-scope but the symbol sets are
      disjoint on the overlapping file(s); server records both claims
      and writes an ``auto-coexist`` audit event.
    - ``AUTO_NARROW``: holder is a narrowable file claim, requester is
      symbol-scope; server grants the requester alongside the holder
      and writes an ``auto-narrow`` audit event.
    - ``PARTIAL_GRANT``: holder is symbol-scope, requester wants the
      whole file; caller decides whether to surface this as a 409, a
      narrowed grant, or a coexistence offer. This module just labels
      the case.
    """

    NO_OVERLAP = "no_overlap"
    FILE_OVERLAP = "file_overlap"
    SYMBOL_OVERLAP = "symbol_overlap"
    AUTO_COEXIST = "auto_coexist"
    AUTO_NARROW = "auto_narrow"
    PARTIAL_GRANT = "partial_grant"


@dataclass(frozen=True)
class OverlapResult:
    """Output of :func:`check_overlap`.

    ``overlapping_paths`` is the path-set intersection between the two
    claims (gitignore-style matching). For heuristic-only resolutions
    (no ``repo_root`` available) this may contain the placeholder
    ``"<unknown>"`` returned by :func:`compute_overlap`.

    ``overlapping_symbols`` is non-empty only when ``kind`` is
    ``SYMBOL_OVERLAP``. Each tuple is ``(file_path, (symbol_names,))``
    in sorted order so callers can render a stable diff.

    ``notes`` is a free-form human-readable label intended for audit
    logging and dashboard rows; it never feeds back into routing.
    """

    kind: OverlapKind
    overlapping_paths: tuple[str, ...]
    overlapping_symbols: tuple[tuple[str, tuple[str, ...]], ...]
    notes: str | None = None


_FILE_SCOPE = "file"
_SYMBOL_SCOPE = "symbol"


def _scope_of(value: str | None) -> str:
    """Normalise a scope_type column value to ``'file'`` or ``'symbol'``.

    ``claims.scope_type`` is ``NOT NULL DEFAULT 'file'`` post-v8, but
    legacy rows or test fixtures may still surface ``None``; treat
    those as whole-file scope.
    """
    if value == _SYMBOL_SCOPE:
        return _SYMBOL_SCOPE
    return _FILE_SCOPE


def _is_narrowable(holder: dict[str, Any]) -> bool:
    """Return True iff this holder row is eligible for auto-narrow.

    Three independently sufficient blockers exist (any one demotes the
    case to a plain file-overlap):

    - ``narrowable=0`` was set explicitly at claim time.
    - ``claim_type='shared_file'`` (the holder asked to see overlap).
    - ``claim_type='module'`` (operator deliberately picked a coarse
      scope; auto-narrowing under them would surprise them).
    """
    if not bool(holder.get("narrowable", 1)):
        return False
    claim_type = str(holder.get("claim_type") or "")
    if claim_type == "shared_file":
        return False
    if claim_type == "module":
        return False
    return True


async def check_overlap(
    *,
    db: Database,
    holder: dict[str, Any],
    requester_pattern: str,
    requester_scope_type: str,
    requester_symbols_by_file: dict[str, list[str]],
) -> OverlapResult:
    """Classify the (holder, requester) pair into an :class:`OverlapKind`.

    Algorithm follows ``docs/design/sub-file-claims.md`` ("Overlap
    algorithm"):

    1. Path intersection via :func:`compute_overlap`. Empty -> NO_OVERLAP.
    2. Branch on ``(holder.scope_type, requester_scope_type)``:
       - file/file: FILE_OVERLAP (legacy behaviour).
       - symbol/symbol: load the holder's symbols via
         :meth:`Database.get_claim_symbols`; intersect with
         ``requester_symbols_by_file`` per file. Empty intersection ->
         AUTO_COEXIST, otherwise SYMBOL_OVERLAP.
       - file/symbol: AUTO_NARROW unless the holder is not narrowable
         (``shared_file``, ``module``, or explicit ``narrowable=False``),
         in which case fall through to FILE_OVERLAP.
       - symbol/file: PARTIAL_GRANT; the caller decides what UX to
         present.

    ``compute_overlap`` is invoked with ``repo_root=None`` so this
    helper is self-contained and testable without a repo. The service
    layer can pre-compute a richer path set and feed paths directly
    via the ``requester_symbols_by_file`` keys when symbol scope is in
    play; the symbol/symbol branch never relies on the ``"<unknown>"``
    sentinel that the heuristic mode returns.
    """
    holder_pattern = str(holder.get("pattern") or "")
    path_intersection = await compute_overlap(
        holder_pattern,
        requester_pattern,
        repo_root=None,
    )
    if not path_intersection:
        return OverlapResult(
            kind=OverlapKind.NO_OVERLAP,
            overlapping_paths=(),
            overlapping_symbols=(),
            notes="empty path intersection",
        )

    holder_scope = _scope_of(holder.get("scope_type"))
    requester_scope = _scope_of(requester_scope_type)
    overlapping_paths = tuple(path_intersection)

    if holder_scope == _FILE_SCOPE and requester_scope == _FILE_SCOPE:
        return OverlapResult(
            kind=OverlapKind.FILE_OVERLAP,
            overlapping_paths=overlapping_paths,
            overlapping_symbols=(),
            notes="legacy file/file overlap",
        )

    if holder_scope == _SYMBOL_SCOPE and requester_scope == _SYMBOL_SCOPE:
        return await _classify_symbol_symbol(
            db=db,
            holder=holder,
            requester_symbols_by_file=requester_symbols_by_file,
            overlapping_paths=overlapping_paths,
        )

    if holder_scope == _FILE_SCOPE and requester_scope == _SYMBOL_SCOPE:
        if not _is_narrowable(holder):
            return OverlapResult(
                kind=OverlapKind.FILE_OVERLAP,
                overlapping_paths=overlapping_paths,
                overlapping_symbols=(),
                notes="holder is file-scope and not narrowable",
            )
        return OverlapResult(
            kind=OverlapKind.AUTO_NARROW,
            overlapping_paths=overlapping_paths,
            overlapping_symbols=_symbols_tuple_from_requester(
                overlapping_paths, requester_symbols_by_file
            ),
            notes="auto-narrow: narrowable file holder + symbol requester",
        )

    # holder_scope == 'symbol' and requester_scope == 'file'
    return OverlapResult(
        kind=OverlapKind.PARTIAL_GRANT,
        overlapping_paths=overlapping_paths,
        overlapping_symbols=(),
        notes="partial grant: symbol holder + file requester",
    )


async def _classify_symbol_symbol(
    *,
    db: Database,
    holder: dict[str, Any],
    requester_symbols_by_file: dict[str, list[str]],
    overlapping_paths: tuple[str, ...],
) -> OverlapResult:
    """Decide AUTO_COEXIST vs SYMBOL_OVERLAP for two symbol-scope claims.

    Loads the holder's ``claim_symbols`` rows once, groups them by
    file, and intersects with ``requester_symbols_by_file`` per file.
    The "files to consider" set is the union of:

    - files the holder claimed (from ``get_claim_symbols``), and
    - files the requester named in ``requester_symbols_by_file``.

    Intersected with ``overlapping_paths`` when the path engine
    returned concrete entries; when the heuristic path returned
    ``"<unknown>"`` we fall back to the holder + requester file union
    so symbol comparison still happens (otherwise a heuristic match
    would always degrade to AUTO_COEXIST, which is unsafe).
    """
    holder_id = str(holder.get("id") or "")
    holder_symbols_rows = await db.get_claim_symbols(holder_id) if holder_id else []
    holder_by_file: dict[str, set[str]] = {}
    for row in holder_symbols_rows:
        f = str(row["file_path"])
        s = str(row["symbol_name"])
        holder_by_file.setdefault(f, set()).add(s)

    requester_by_file: dict[str, set[str]] = {
        str(f): {str(s) for s in syms}
        for f, syms in requester_symbols_by_file.items()
    }

    candidate_files: set[str]
    if overlapping_paths == ("<unknown>",) or not overlapping_paths:
        candidate_files = set(holder_by_file) | set(requester_by_file)
    else:
        candidate_files = set(overlapping_paths) | (
            set(holder_by_file) & set(requester_by_file)
        )

    overlaps: list[tuple[str, tuple[str, ...]]] = []
    for f in sorted(candidate_files):
        held = holder_by_file.get(f, set())
        req = requester_by_file.get(f, set())
        both = held & req
        if both:
            overlaps.append((f, tuple(sorted(both))))

    if not overlaps:
        return OverlapResult(
            kind=OverlapKind.AUTO_COEXIST,
            overlapping_paths=overlapping_paths,
            overlapping_symbols=(),
            notes="symbol-disjoint on every overlapping file",
        )

    return OverlapResult(
        kind=OverlapKind.SYMBOL_OVERLAP,
        overlapping_paths=overlapping_paths,
        overlapping_symbols=tuple(overlaps),
        notes="symbol intersection on at least one overlapping file",
    )


def _symbols_tuple_from_requester(
    overlapping_paths: tuple[str, ...],
    requester_symbols_by_file: dict[str, list[str]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Project the requester's symbol map onto the overlapping path set.

    Used by AUTO_NARROW so callers can write a faithful audit row even
    when the holder has no claim_symbols of its own (file-scope holder).
    Files the requester claimed but that aren't in the path
    intersection are filtered out so the audit log isn't polluted
    with non-overlapping noise.
    """
    if not requester_symbols_by_file:
        return ()
    if overlapping_paths == ("<unknown>",) or not overlapping_paths:
        keep = set(requester_symbols_by_file)
    else:
        keep = set(overlapping_paths) & set(requester_symbols_by_file)
    out: list[tuple[str, tuple[str, ...]]] = []
    for f in sorted(keep):
        syms = tuple(sorted({str(s) for s in requester_symbols_by_file[f]}))
        if syms:
            out.append((f, syms))
    return tuple(out)


async def record_auto_resolution(
    *,
    db: Database,
    kind: OverlapKind,
    holder_claim_id: str,
    requester_claim_id: str,
    overlapping_paths: tuple[str, ...],
    overlapping_symbols: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    """Persist a server-side auto-resolution: wire coexist partners and
    append a single audit row to ``request_events``.

    ``kind`` must be :attr:`OverlapKind.AUTO_COEXIST` or
    :attr:`OverlapKind.AUTO_NARROW`; any other value is a programming
    error and raises :class:`ValueError`.

    The function is idempotent:

    - :meth:`Database.attach_coexist_partner` already no-ops when the
      partner id is already present in the target row's array, so
      replaying this call won't duplicate edges.
    - The ``request_events`` insert checks for a prior matching row
      (same ``event_type``, holder, requester) and skips if one
      exists. The check uses ``actor_session_id`` only to short-circuit;
      ``actor_engineer`` and ``actor_session_id`` remain NULL because
      this is a server action with no human actor.
    """
    if kind not in (OverlapKind.AUTO_COEXIST, OverlapKind.AUTO_NARROW):
        raise ValueError(
            "record_auto_resolution expects AUTO_COEXIST or AUTO_NARROW, "
            f"got {kind!r}"
        )
    event_type = (
        "auto-coexist" if kind is OverlapKind.AUTO_COEXIST else "auto-narrow"
    )

    # Pairwise coexist edge so future overlap checks self-exclude these
    # two claims from each other (mirrors the v0.11 `coexist` decision
    # verb). Both attach calls are idempotent at the DB layer.
    await db.attach_coexist_partner(holder_claim_id, requester_claim_id)
    await db.attach_coexist_partner(requester_claim_id, holder_claim_id)

    if await _auto_resolution_event_exists(
        db,
        event_type=event_type,
        holder_claim_id=holder_claim_id,
        requester_claim_id=requester_claim_id,
    ):
        return

    detail: dict[str, Any] = {
        "holder_claim_id": holder_claim_id,
        "requester_claim_id": requester_claim_id,
        "overlapping_paths": list(overlapping_paths),
        "overlapping_symbols": [
            {"file": f, "symbols": list(syms)}
            for f, syms in overlapping_symbols
        ],
    }
    await db.record_request_event(
        event_type,
        request_id=None,
        actor_engineer=None,
        actor_session_id=None,
        detail=detail,
    )


async def _auto_resolution_event_exists(
    db: Database,
    *,
    event_type: str,
    holder_claim_id: str,
    requester_claim_id: str,
) -> bool:
    """Return True iff an auto-resolution event for this pair already
    exists. Idempotency check for :func:`record_auto_resolution`.

    We compare on (event_type, holder_claim_id, requester_claim_id)
    extracted from the JSON detail blob. SQLite's ``json_extract``
    handles the lookup natively when JSON1 is compiled in (default in
    every modern build, including the wheel aiosqlite links against).
    """
    import aiosqlite

    from coordination.db import _configure_sqlite

    await db.init()
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        await _configure_sqlite(conn)
        cur = await conn.execute(
            """
            SELECT 1 FROM request_events
            WHERE event_type = ?
              AND request_id IS NULL
              AND json_extract(detail, '$.holder_claim_id') = ?
              AND json_extract(detail, '$.requester_claim_id') = ?
            LIMIT 1
            """,
            (event_type, holder_claim_id, requester_claim_id),
        )
        row = await cur.fetchone()
        return row is not None
