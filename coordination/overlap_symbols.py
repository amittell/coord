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
    - ``CALLSITE_OVERLAP`` (v0.31 wave 2): purely advisory. The
      requester's scope contains lines a holder's claimed symbol is
      called from (per the language server's recorded references).
      Never routed to a 409 and never queued -- the grant proceeds and
      the service attaches a warning so the requester knows a semantic
      (not textual) conflict is possible. :func:`check_overlap` never
      returns this kind; the service computes it post-grant via
      :func:`group_callsite_overlaps`.
    """

    NO_OVERLAP = "no_overlap"
    FILE_OVERLAP = "file_overlap"
    SYMBOL_OVERLAP = "symbol_overlap"
    AUTO_COEXIST = "auto_coexist"
    AUTO_NARROW = "auto_narrow"
    PARTIAL_GRANT = "partial_grant"
    CALLSITE_OVERLAP = "callsite_overlap"


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


SymbolPath = tuple[str | None, str]


def parse_symbol_path(symbol: str) -> SymbolPath:
    """Split a claim notation string into ``(parent, name)``.

    ``"Foo"`` -> ``(None, "Foo")`` (top-level, v0.14 semantics).
    ``"Foo::handleA"`` -> ``("Foo", "handleA")`` (method, v0.16).
    ``"Outer::Inner::method"`` -> ``("Outer::Inner", "method")`` (v0.17).

    The split uses ``rpartition`` so the LAST ``::`` separates the leaf
    name from the ancestor path. ``parent_symbol`` therefore holds the
    full ancestor chain joined by ``::``; the overlap engine treats it
    as a pre-joined string and prefix-matches on the full canonical
    path. This keeps the schema unchanged (still one ``parent_symbol``
    column) while supporting arbitrarily deep nesting.
    """
    if "::" not in symbol:
        return (None, symbol)
    parent, _, name = symbol.rpartition("::")
    return (parent, name)


def format_symbol_path(parent: str | None, name: str) -> str:
    """Inverse of :func:`parse_symbol_path`. Used to canonicalise
    overlap-result strings the conflict response surfaces back to the
    requester. Parents already contain ``::`` separators when the
    symbol is nested more than one level."""
    return f"{parent}::{name}" if parent else name


def _full_path(p: SymbolPath) -> str:
    """Join ``(parent, name)`` into the canonical ``"a::b::c"`` form."""
    parent, name = p
    return f"{parent}::{name}" if parent else name


def symbol_paths_overlap(a: SymbolPath, b: SymbolPath) -> bool:
    """Return True iff ``a`` and ``b`` overlap under the v0.17 recursive
    prefix-match rule.

    Two paths overlap iff one equals the other or one (followed by the
    separator) is a strict prefix of the other. Concretely:

    - ``"Foo"`` overlaps ``"Foo"`` (identity).
    - ``"Foo"`` overlaps ``"Foo::handleA"`` (class covers method).
    - ``"Foo"`` overlaps ``"Foo::Inner::method"`` (class covers any
      descendant, v0.17 recursive extension of the v0.16 rule).
    - ``"Foo::Inner"`` overlaps ``"Foo::Inner::method"`` (inner class
      covers its method) but NOT ``"Foo::Other::method"``.
    - ``"Foo::a"`` and ``"Foo::b"`` do not overlap (sibling methods).
    - ``"Foo::a"`` and ``"Bar::a"`` do not overlap (different parents).
    """
    pa = _full_path(a)
    pb = _full_path(b)
    if pa == pb:
        return True
    if pb.startswith(pa + "::"):
        return True
    if pa.startswith(pb + "::"):
        return True
    return False


async def _classify_symbol_symbol(
    *,
    db: Database,
    holder: dict[str, Any],
    requester_symbols_by_file: dict[str, list[str]],
    overlapping_paths: tuple[str, ...],
) -> OverlapResult:
    """Decide AUTO_COEXIST vs SYMBOL_OVERLAP for two symbol-scope claims.

    v0.16 namespace overlap: each symbol is a ``(parent, name)`` path
    (parent is None for top-level, set for methods). Two claims
    overlap on a file iff any pair of their symbol paths overlap
    under :func:`symbol_paths_overlap` (two-level prefix matching).

    Loads the holder's ``claim_symbols`` rows once and groups them by
    file. Requester strings come pre-parsed from
    ``requester_symbols_by_file`` (one ``"Foo::handleA"`` entry per
    list slot); the service layer is responsible for parsing the
    ``"Parent::child"`` notation at insert time, but this helper also
    parses on read so callers that pass raw API strings work without
    pre-processing.
    """
    holder_id = str(holder.get("id") or "")
    holder_symbols_rows = await db.get_claim_symbols(holder_id) if holder_id else []
    holder_by_file: dict[str, list[SymbolPath]] = {}
    for row in holder_symbols_rows:
        f = str(row["file_path"])
        parent = row.get("parent_symbol")
        parent_str: str | None = str(parent) if parent else None
        name = str(row["symbol_name"])
        holder_by_file.setdefault(f, []).append((parent_str, name))

    requester_by_file: dict[str, list[SymbolPath]] = {}
    for f, syms in requester_symbols_by_file.items():
        requester_by_file[str(f)] = [parse_symbol_path(str(s)) for s in syms]

    candidate_files: set[str]
    if overlapping_paths == ("<unknown>",) or not overlapping_paths:
        candidate_files = set(holder_by_file) | set(requester_by_file)
    else:
        candidate_files = set(overlapping_paths) | (
            set(holder_by_file) & set(requester_by_file)
        )

    overlaps: list[tuple[str, tuple[str, ...]]] = []
    for f in sorted(candidate_files):
        held_paths = holder_by_file.get(f, [])
        req_paths = requester_by_file.get(f, [])
        overlapping_pairs: set[str] = set()
        for a in held_paths:
            for b in req_paths:
                if symbol_paths_overlap(a, b):
                    # Surface the requester's path in the response so
                    # the 409 payload echoes what the caller sent.
                    overlapping_pairs.add(format_symbol_path(b[0], b[1]))
        if overlapping_pairs:
            overlaps.append((f, tuple(sorted(overlapping_pairs))))

    if not overlaps:
        return OverlapResult(
            kind=OverlapKind.AUTO_COEXIST,
            overlapping_paths=overlapping_paths,
            overlapping_symbols=(),
            notes="symbol-disjoint on every overlapping file (v0.16 namespace)",
        )

    return OverlapResult(
        kind=OverlapKind.SYMBOL_OVERLAP,
        overlapping_paths=overlapping_paths,
        overlapping_symbols=tuple(overlaps),
        notes="symbol intersection on at least one overlapping file (v0.16 namespace)",
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


@dataclass(frozen=True)
class CallsiteOverlap:
    """One advisory CALLSITE_OVERLAP finding (v0.31 wave 2).

    ``lines`` are the recorded callsite line numbers (1-based, sorted,
    de-duplicated) inside the requester's scope on ``file_path``. The
    service renders these into the advisory warning string; nothing
    here participates in grant routing.
    """

    kind: OverlapKind
    holder_claim_id: str
    holder_engineer: str
    holder_pattern: str
    file_path: str
    lines: tuple[int, ...]


def group_callsite_overlaps(
    rows: list[dict[str, Any]],
    *,
    requester_engineer: str,
    requester_session_id: str | None,
    requester_repo: str | None,
    exclude_claim_ids: set[str] | frozenset[str] = frozenset(),
) -> list[CallsiteOverlap]:
    """Classify raw ``callsites_intersecting`` rows into advisory
    :class:`CallsiteOverlap` findings.

    Pure function (no I/O) so the filtering rules are unit-testable in
    isolation. Mirrors the conflict pipeline's adversary definition:

    - the requester's own engineer never warns against itself;
    - a holder claim sharing the requester's session_id is cooperative
      (parent dispatcher + subagents), not adversarial;
    - holders from a different repo bucket are invisible, exactly like
      the v0.4 repo-scoped conflict check (NULL repo is its own legacy
      bucket);
    - ``exclude_claim_ids`` drops the requester's just-created claims,
      which would otherwise self-report once their own enrichment runs.

    Output is grouped per (holder claim, file) and sorted for stable
    rendering.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        claim_id = str(row.get("claim_id") or "")
        if not claim_id or claim_id in exclude_claim_ids:
            continue
        if str(row.get("engineer") or "") == requester_engineer:
            continue
        if (
            requester_session_id
            and row.get("session_id") == requester_session_id
        ):
            continue
        if row.get("repo") != requester_repo:
            continue
        line = row.get("line")
        if line is None:
            continue
        key = (claim_id, str(row.get("file_path") or ""))
        slot = grouped.setdefault(
            key,
            {
                "engineer": str(row.get("engineer") or ""),
                "pattern": str(row.get("pattern") or ""),
                "lines": set(),
            },
        )
        slot["lines"].add(int(line))
    out: list[CallsiteOverlap] = []
    for (claim_id, file_path), slot in sorted(grouped.items()):
        out.append(
            CallsiteOverlap(
                kind=OverlapKind.CALLSITE_OVERLAP,
                holder_claim_id=claim_id,
                holder_engineer=slot["engineer"],
                holder_pattern=slot["pattern"],
                file_path=file_path,
                lines=tuple(sorted(slot["lines"])),
            )
        )
    return out


async def record_auto_resolution(
    *,
    db: Database,
    kind: OverlapKind,
    holder_claim_id: str,
    requester_claim_id: str,
    overlapping_paths: tuple[str, ...],
    overlapping_symbols: tuple[tuple[str, tuple[str, ...]], ...],
    service: Any = None,
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

    v0.27: when ``service`` is non-None (a CoordinationService), also
    emits an ``auto-coexist`` or ``auto-narrow`` webhook with the same
    detail dict that landed in the audit row. The parameter is typed
    ``Any`` to avoid a circular import between this module and
    :mod:`coordination.service`; callers that don't want webhook
    emission simply omit it.
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
    if service is not None:
        await service.fire_webhook(event_type, detail)


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
