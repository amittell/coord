from __future__ import annotations

import asyncio
import json
import time as _time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from coordination import metrics
from coordination.config import Settings, get_settings
from coordination.db import Database
from coordination.engine import (
    _normalize_pattern,
    compute_overlap,
    files_matching_pattern,
    git_ls_files,
)
from coordination.lsp import get_lsp_pool, language_for_path, relpath_under_root
from coordination.overlap_symbols import (
    OverlapKind,
    SymbolPath,
    check_overlap as check_symbol_overlap,
    format_symbol_path,
    group_callsite_overlaps,
    parse_symbol_path,
    record_auto_resolution,
    symbol_paths_overlap,
)
from coordination.ownership import PathRule, parse_ownership_yaml, severity_for_pattern
from coordination.schemas import (
    ClaimItem,
    ClaimRefactorRequest,
    ConflictCheckResponse,
    ConflictEntry,
    ConflictingClaim,
    ConflictingSymbol,
    CreateClaimsRequest,
    CreateClaimsResponse,
)
from coordination.symbols import Symbol, extract_symbols

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """v0.30: a per-engineer or per-repo quota would be breached.

    ``scope`` says which quota fired:

    - ``"claims"``: the engineer's active-claim cap
      (``COORD_MAX_CLAIMS_PER_ENGINEER``) would be exceeded by the
      rows about to be inserted.
    - ``"queue"``: the engineer's live queue-entry cap
      (``COORD_MAX_QUEUED_PER_ENGINEER``) is already full.
    - ``"repo_queue"``: the repo's waiting-queue depth cap
      (``COORD_MAX_QUEUE_DEPTH_PER_REPO``) is already full.

    ``retry_after_sec`` is the server's best guess at when retrying
    might succeed (for the claims scope: seconds until the engineer's
    soonest active claim TTL-expires, clamped to [5, 3600]). The API
    layer maps this exception to HTTP 429 with a ``Retry-After``
    header; the drain path treats it as "skip this waiter, try the
    next one".
    """

    def __init__(
        self, *, scope: str, detail: str, retry_after_sec: int
    ) -> None:
        super().__init__(detail)
        self.scope = scope
        self.detail = detail
        self.retry_after_sec = retry_after_sec


class LspUnavailable(Exception):
    """v0.31 wave 2: ``POST /claims/refactor`` needs a live language
    server and there is none to be had -- LSP is disabled, no repo root
    is configured, the file's language has no registered server, or the
    pool could not answer (circuit open, spawn failure, timeout).

    Unlike every other LSP touchpoint (which fails soft to parser
    behaviour), refactor claims are MEANINGLESS without references, so
    this is the one place an LSP outage becomes a real error. The API
    layer maps it to HTTP 503.
    """


# v0.31 wave 2: hard ceiling on stored callsites per claim. References
# results for a hot symbol can run into the thousands; the advisory
# value of callsite N for large N is nil, and the table is consulted on
# every grant, so we keep it bounded. Not operator-tunable on purpose:
# this is a storage guardrail, not a policy knob.
CALLSITE_CAP = 200


def _expires_at(ttl_hours: int) -> str:
    dt = datetime.now(UTC) + timedelta(hours=ttl_hours)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_pattern_syntax(pattern: str) -> str | None:
    if not pattern or not pattern.strip():
        return "Empty pattern is not allowed"
    stripped = pattern.strip()
    if stripped.startswith("!"):
        return (
            f"Pattern {pattern!r} starts with '!' (gitignore negation). Negation "
            "patterns have no coherent overlap semantics and are rejected."
        )
    return None


def _is_subset_pattern(narrowed: str, original: str) -> bool:
    """Return True iff every path matched by ``narrowed`` is also
    matched by ``original``.

    Strategy mirrors the heuristic-overlap path in :mod:`coordination.engine`:
    synthesize a single concrete path that ``narrowed`` would match (via
    ``_synthesize_candidate``), then ask the original pattern's PathSpec
    whether it also matches. This is a sound subset proxy because the
    engine's synthesizer is constructed to produce a representative
    instance for any wildcard token, so a hit on the original means
    every concrete path of ``narrowed`` lies inside ``original``. Equal
    patterns trivially pass.

    Empty / unparseable inputs short-circuit to False so the caller
    surfaces a clear rejection instead of silently accepting nonsense.
    """
    import pathspec

    from coordination.engine import _normalize_pattern, _synthesize_candidate

    norm_narrowed = _normalize_pattern(narrowed)
    norm_original = _normalize_pattern(original)
    if not norm_narrowed or not norm_original:
        return False
    candidate = _synthesize_candidate(narrowed)
    if candidate is None:
        return False
    spec_original = pathspec.PathSpec.from_lines("gitignore", [norm_original])
    return spec_original.match_file(candidate)


def _coexist_partner_ids_from_rows(rows: list[dict[str, Any]]) -> set[str]:
    """Collect the union of ``coexists_with`` partner ids from a row set.

    Centralised so the conflict and create paths apply identical
    semantics: anything in any of these lists is a known coexisting
    partner of the caller and must be invisible to the caller's
    overlap check. Defensive against malformed JSON (treats it as no
    partners rather than crashing the whole call).
    """
    import json as _json

    partners: set[str] = set()
    for row in rows:
        cw = row.get("coexists_with")
        if not cw:
            continue
        try:
            ids = _json.loads(cw)
        except (TypeError, ValueError):
            continue
        if isinstance(ids, list):
            partners.update(str(x) for x in ids)
    return partners


def _blanket_skip_partner_ids(
    partner_ids: set[str], active: list[dict[str, Any]]
) -> set[str]:
    """Return the subset of ``partner_ids`` that should be invisible to
    the caller's overlap check (v0.35 scope-aware coexist exclusion).

    Pre-v0.35 every coexist partner was blanket-skipped: an in-pair edit
    is cooperative, so the partner's claim never collided with the
    caller. That stays exactly right for a FILE-scoped partner -- the two
    sides agreed to share the whole file, so the caller never bounces on
    it.

    A SYMBOL-scoped coexist partner is different. The pair was granted
    coexistence only on disjoint symbols, so a LATER claim by the caller
    that touches the same file must be judged against the partner's
    actual ``claim_symbols`` via the normal symbol-overlap path: it 409s
    when it collides with the partner's granted symbols and auto-coexists
    when disjoint. Blanket-skipping it would hide a real conflict, so a
    symbol-scoped partner is deliberately NOT returned here and stays in
    the adversarial ``active`` set.

    Partner ids not present in ``active`` (expired / released) are
    omitted; they are already gone from ``active`` so excluding them is a
    no-op either way.
    """
    by_id = {str(r.get("id")): r for r in active}
    skip: set[str] = set()
    for pid in partner_ids:
        row = by_id.get(pid)
        if row is None:
            continue
        if str(row.get("scope_type") or "file") != "symbol":
            skip.add(pid)
    return skip


def _lsp_symbol_path_set(flattened: list[dict[str, Any]]) -> set[str]:
    """Build the set of claimable ``Outer::Inner::leaf`` paths from a
    flattened LSP documentSymbol result (v0.31).

    Mirrors the parser-side set construction in
    ``_validate_claim_symbols``: every full path is claimable, and so is
    every ancestor prefix, so a claim on ``"Outer"`` is accepted even if
    the server only emitted leaf entries.
    """

    paths: set[str] = set()
    for entry in flattened:
        name = str(entry.get("name") or "")
        if not name:
            continue
        parent = entry.get("parent_path")
        parent = str(parent) if parent else None
        paths.add(format_symbol_path(parent, name))
        while parent:
            paths.add(parent)
            parent = parent.rsplit("::", 1)[0] if "::" in parent else None
    return paths


def _lsp_span_map(
    flattened: list[dict[str, Any]],
) -> dict[str, tuple[int, int, int, int]]:
    """Map full symbol path -> (start_line, start_col, end_line,
    end_col) from a flattened documentSymbol result. Lines arrive
    already converted to 1-based by the pool; columns are 0-based as
    the server reported them. Entries with missing pieces are skipped
    rather than persisted half-formed."""

    spans: dict[str, tuple[int, int, int, int]] = {}
    for entry in flattened:
        name = str(entry.get("name") or "")
        if not name:
            continue
        parent = entry.get("parent_path")
        parent = str(parent) if parent else None
        try:
            span = (
                int(entry["start_line"]),
                int(entry["start_col"]),
                int(entry["end_line"]),
                int(entry["end_col"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        spans[format_symbol_path(parent, name)] = span
    return spans


def _tightest_enclosing_symbol(
    flattened: list[dict[str, Any]] | None,
    line: int,
    character: int,
) -> str | None:
    """Find the tightest documentSymbol entry containing the position
    ``(line, character)`` -- 1-based line, 0-based column, matching the
    pool's normalised output -- and return its full ``Outer::leaf``
    path, or ``None`` when no entry contains the position (module-level
    code) or ``flattened`` itself is ``None`` (the server could not
    describe the file).

    Containment follows LSP Range semantics: the start position is
    inclusive and the end position is EXCLUSIVE, so a position exactly
    at ``(end_line, end_col)`` is outside the symbol (it is the first
    position after the symbol's last character).

    "Tightest" picks the smallest line span, breaking ties on nesting
    depth (a method beats the class wrapping it when their spans
    coincide on a one-line class)."""

    if not flattened:
        return None
    best: tuple[int, int, str] | None = None
    for entry in flattened:
        try:
            start_line = int(entry["start_line"])
            start_col = int(entry["start_col"])
            end_line = int(entry["end_line"])
            end_col = int(entry["end_col"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        if line < start_line or line > end_line:
            continue
        if line == start_line and character < start_col:
            continue
        # LSP range ends are exclusive: a position equal to the end
        # is OUTSIDE (e.g. a callsite starting immediately after a
        # symbol's closing brace on the same line is module-level,
        # not inside that symbol).
        if line == end_line and character >= end_col:
            continue
        parent = entry.get("parent_path")
        parent = str(parent) if parent else None
        full_path = format_symbol_path(parent, name)
        depth = full_path.count("::")
        size = end_line - start_line
        key = (size, -depth, full_path)
        if best is None or key < (best[0], best[1], best[2]):
            best = (size, -depth, full_path)
    return best[2] if best is not None else None


# v0.21: FIFO queue waiters. Keyed on claim_queue.id. The release-path
# drain sets the event and the granted_claim_id (or None for "give up
# and surface the original 409"); the create_claims long-poll awaits the
# event. Per-process state: a restart causes outstanding long-polls to
# time out and the client to retry, which is the safe failure mode.
_QUEUE_WAITERS: dict[str, tuple[asyncio.Event, dict[str, Any]]] = {}

# v0.24: cross-process queue backend. The in-memory ``_QUEUE_WAITERS``
# event only fires when the release happens in the same Python process
# as the long-poll. In multi-replica deployments (one replica long-polls,
# another handles the release) the waiter never wakes via the event.
# ``_enqueue_and_wait`` runs a hybrid loop: short event-wait (fast path
# when same-process) plus a DB state poll on ``POLL_INTERVAL`` so a
# cross-process grant/expiry is observed within that interval. 0.5s is a
# balance between perceived latency (near-instant grants) and SQLite
# read pressure when many waiters are stacked.
POLL_INTERVAL = 0.5


def _register_waiter(queue_id: str) -> tuple[asyncio.Event, dict[str, Any]]:
    """Register an in-process waiter for ``queue_id`` and return the
    (event, payload-dict) tuple. The payload dict is mutated by
    :func:`_notify_waiter` so the long-poll caller can read the
    grant decision off it after ``event.wait()`` returns."""

    event = asyncio.Event()
    payload: dict[str, Any] = {}
    _QUEUE_WAITERS[queue_id] = (event, payload)
    return event, payload


def _notify_waiter(queue_id: str, payload: dict[str, Any]) -> None:
    """Wake the long-poll waiter for ``queue_id``, if any, and hand it
    the payload (granted_claim_id, or None for expiry)."""

    entry = _QUEUE_WAITERS.get(queue_id)
    if entry is None:
        return
    event, slot = entry
    slot.update(payload)
    event.set()


def _drop_waiter(queue_id: str) -> None:
    """Remove a queue-waiter entry after the long-poll concluded.
    Idempotent."""

    _QUEUE_WAITERS.pop(queue_id, None)


def _consume_finalise_result(task: asyncio.Task) -> None:
    """Consume the outcome of an orphaned queue-wait finalise task.

    Attached only when the awaiting handler was cancelled mid-finalise
    (a second disconnect signal): the shielded task keeps running so
    the queue row still reaches a terminal state, and this callback
    retrieves its result so a failure is logged instead of surfacing
    as 'Task exception was never retrieved' at GC time."""

    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "queue-wait finalise failed after handler cancellation: %s",
            exc,
        )


@dataclass
class CoordinationService:
    db: Database
    settings: Settings
    # v0.30: serializes the queue-enqueue count-then-act critical
    # section (enqueue quota checks + enqueue). Each Database call runs
    # on its own connection in its own transaction, so without this two
    # concurrent requests both read an under-quota count before either
    # write lands and the queue cap overshoots. One asyncio.Lock is
    # sufficient on SQLite: the flock instance lock guarantees a single
    # process per database, and a single event loop serializes
    # everything else. Held only across the check+write pair -- never
    # across the queue long-poll.
    #
    # The active-claim cap (max_claims_per_engineer) no longer rides this
    # lock: design 5.3 re-homed it onto db.engineer_lock so the count and
    # insert run on the bound grant connection under a per-engineer guard
    # that also serializes across replicas on Postgres (this in-process
    # lock cannot). The queue-enqueue cap stays here pending its own
    # DB-side re-home.
    _quota_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # v0.31 wave 2: strong references to in-flight callsite-enrichment
    # tasks. asyncio.create_task only holds a weak reference, so a
    # fire-and-forget task with no other referent can be garbage
    # collected mid-flight; parking it here (and discarding on done)
    # is the canonical anti-GC pattern. Tests also use this set to
    # await enrichment deterministically:
    # ``await asyncio.gather(*service._enrichment_tasks)``.
    _enrichment_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # v0.44 scale: per-(session, repo) monotonic timestamp of the last
    # activity ping actually written, for coalescing (see _maybe_touch).
    _last_ping: dict[tuple[str, str | None], float] = field(default_factory=dict)
    # One-shot latch so the unsigned-webhook operator warning (webhook_url
    # configured without webhook_secret; see fire_webhook) logs once per
    # process instead of once per emitted event.
    _warned_unsigned_webhook: bool = False

    async def _maybe_touch(self, session_id: str | None, repo: str | None) -> None:
        """Activity ping with v0.44 coalescing. Most reads write a liveness
        ping (``touch_session_activity``); at hundreds of agents that
        write-on-read is the dominant SQLite write load. When
        ``activity_ping_min_interval_sec > 0`` a (session, repo) is pinged at
        most once per interval (best-effort, in-process); intervening pings
        are skipped. A ping is liveness only -- coalescing or dropping one is
        safe, it just delays idle-expiry of an already-idle session slightly.
        """
        if not session_id:
            return
        interval = self.settings.activity_ping_min_interval_sec
        if interval <= 0:
            try:
                await self.db.touch_session_activity(session_id, repo=repo)
            except Exception:
                # Best-effort by contract: a ping is liveness only, so a
                # write failure (SQLITE_BUSY under contention, IO error)
                # degrades to a slightly delayed idle-expiry instead of
                # turning the read that carried it into a 500.
                logger.debug(
                    "activity ping for session %s failed; dropped",
                    session_id,
                    exc_info=True,
                )
            return
        # Defensive clamp: coalescing must never out-pace idle expiry, or a
        # read-only session could be expired as idle between pings. Half the
        # idle window guarantees at least ~2 ping opportunities per window; a
        # misconfigured interval degrades to more frequent pings, not false
        # expiry. (idle_timeout_sec == 0 disables idle expiry entirely, so no
        # clamp is needed there.)
        idle = self.settings.idle_timeout_sec
        if idle > 0:
            interval = min(interval, max(1, idle // 2))
        key = (session_id, repo)
        last = self._last_ping.get(key)
        nowm = _time.monotonic()
        if last is not None and (nowm - last) < interval:
            return
        # Stamp BEFORE the await so concurrent callers of the same session
        # coalesce onto one write (checking after would let interleaved tasks
        # all pass the staleness check and ping in duplicate).
        self._last_ping[key] = nowm
        # Hard-bounded: past the high-water mark, first drop entries older
        # than the interval (semantically dead weight -- they would ping
        # anyway on next touch), then, if churn keeps everything fresh, evict
        # the oldest down to the cap. Evicting a fresh entry merely allows
        # one extra ping (harmless liveness), so the bound costs nothing
        # semantically. Trigger at 2x the cap so the O(n) pass amortizes
        # instead of running on every touch at steady-state high churn.
        if len(self._last_ping) > 8192:
            self._last_ping = {
                k: v for k, v in self._last_ping.items() if (nowm - v) < interval
            }
            if len(self._last_ping) > 4096:
                for k, _v in sorted(
                    self._last_ping.items(), key=lambda kv: kv[1]
                )[: len(self._last_ping) - 4096]:
                    del self._last_ping[k]
        try:
            await self.db.touch_session_activity(session_id, repo=repo)
        except Exception:
            # The write did not land -- roll the stamp back so the next call
            # retries immediately instead of being suppressed for a full
            # interval on the strength of a failed ping. Then SWALLOW the
            # error: the docstring's contract is that dropping a ping is
            # safe, and the read paths that carry these pings
            # (check_conflicts, list_claims) do not guard the call -- a
            # re-raise here turned an otherwise-successful read into an
            # unhandled 500 under exactly the write contention this
            # coalescing exists to absorb.
            self._last_ping.pop(key, None)
            logger.debug(
                "activity ping for session %s failed; dropped",
                session_id,
                exc_info=True,
            )
        except BaseException:
            # Cancellation / interpreter shutdown must still propagate,
            # but the stamp rollback applies the same way.
            self._last_ping.pop(key, None)
            raise

    async def count_queued_for(
        self, engineer: str, *, repo: str | None = None
    ) -> int:
        """v0.28: return how many waiting queue rows the given engineer
        currently owns. Drives the ``X-Coord-Queue-Depth`` backpressure
        header so clients can self-regulate without an extra round trip
        to ``/requests?queued=true``.

        v0.42: ``repo`` confines the count to a single repo so a
        repo-scoped token cannot read the engineer's cross-repo queue
        depth. ``repo=None`` (operator) counts across every repo.
        """
        rows = await self.db.list_queued_with_holder(
            engineer=engineer, state="waiting", repo=repo
        )
        return len(rows)

    async def _rules(self) -> list[PathRule]:
        raw = await self.db.get_ownership_yaml()
        if not raw:
            return []
        try:
            return parse_ownership_yaml(raw)
        except ValueError:
            logger.warning("Ignoring invalid stored ownership config")
            return []

    async def _count_claim_files(self, pattern: str) -> int:
        root = self.settings.repo_root
        if root and root.is_dir():
            files = await git_ls_files(root, scope=self.settings.repo_scope)
            if files:
                return len(files_matching_pattern(files, pattern))
        return 1

    async def _zero_match_warnings(self, patterns: list[str]) -> list[str]:
        """Warn when a pattern matches zero tracked files. Only runs when
        COORD_REPO_ROOT is configured (otherwise we have no ground truth).
        For uppercase-containing patterns that miss, suggest the lowercase
        variant if it would match - a common mistake on case-insensitive
        filesystems (default macOS)."""
        root = self.settings.repo_root
        if not root or not root.is_dir():
            return []
        files = await git_ls_files(root, scope=self.settings.repo_scope)
        if not files:
            return []
        warnings: list[str] = []
        for pattern in patterns:
            if files_matching_pattern(files, pattern):
                continue
            lower = pattern.lower()
            if lower != pattern and files_matching_pattern(files, lower):
                warnings.append(
                    f"Pattern {pattern!r} matched zero files under "
                    f"COORD_REPO_ROOT. Did you mean {lower!r}? "
                    "Matching is case-sensitive (gitignore semantics) "
                    "regardless of filesystem case-sensitivity."
                )
            else:
                warnings.append(
                    f"Pattern {pattern!r} matched zero files under "
                    "COORD_REPO_ROOT. The claim was still created in case "
                    "the file is about to be added; double-check the path "
                    "if this was not intentional."
                )
        return warnings

    async def _validate_claim_scope(self, patterns: list[str]) -> str | None:
        if not patterns:
            return "No patterns provided"
        root = self.settings.repo_root
        if not root or not root.is_dir():
            return None

        # Absolute max_claim_files applies in both modes (scoped and unscoped).
        # The ratio guardrail only makes sense when the denominator is
        # well-defined; when the operator has set COORD_REPO_SCOPE, the scope
        # itself declares the working area and within-scope ratios are
        # trivially saturated by small scopes. Skip ratio in scope mode.
        scope_mode = self.settings.repo_scope is not None

        if scope_mode:
            for p in patterns:
                n = await self._count_claim_files(p)
                if n > self.settings.max_claim_files:
                    return f"Pattern {p!r} matches {n} files; max is {self.settings.max_claim_files}"
            return None

        # Unscoped mode: the full repo is the working area; ratio applies.
        files = await git_ls_files(root, scope=None)
        all_files = max(len(files), 1)
        total = 0
        for p in patterns:
            n = await self._count_claim_files(p)
            total += n
            if n > self.settings.max_claim_files:
                return f"Pattern {p!r} matches {n} files; max is {self.settings.max_claim_files}"
            if all_files and n / all_files > self.settings.max_claim_ratio:
                return (
                    f"Pattern {p!r} covers {n/all_files:.0%} of repo; max is "
                    f"{self.settings.max_claim_ratio:.0%}"
                )
        if all_files and total / all_files > self.settings.max_claim_ratio:
            return "Combined claim scope exceeds max fraction of repository"
        return None

    async def _lsp_document_symbols(
        self,
        pattern: str,
        resolved: Path,
        cache: dict[str, list[dict[str, Any]] | None] | None,
    ) -> list[dict[str, Any]] | None:
        """v0.31: one ``documentSymbol`` call per claimed file per
        request, shared between validation fallback and span upgrade
        via the caller-owned ``cache`` (pattern -> flattened entries,
        with None cached too so a failed call is not retried within
        the same request). Returns ``None`` whenever LSP is disabled,
        the language is unsupported, or the pool reports any failure --
        callers fall back to the parser path in every one of those
        cases."""

        root = self.settings.repo_root
        if not self.settings.lsp_enabled or not root:
            return None
        if cache is not None and pattern in cache:
            return cache[pattern]
        result: list[dict[str, Any]] | None = None
        language = language_for_path(str(resolved))
        if language is not None:
            pool = get_lsp_pool(self.settings)
            result = await pool.document_symbols(root, language, resolved)
        if cache is not None:
            cache[pattern] = result
        return result

    async def _validate_claim_symbols(
        self,
        body: CreateClaimsRequest,
        *,
        parser_symbols_out: dict[str, list[Symbol]] | None = None,
        lsp_symbols_cache: dict[str, list[dict[str, Any]] | None] | None = None,
    ) -> str | None:
        """Validate that every symbol in every symbol-scope claim exists
        in the corresponding file.

        v0.17: runs only when ``COORD_REPO_ROOT`` is configured (the
        server needs filesystem access to the application repo to
        parse). When the repo root is unset the call is a silent no-op,
        preserving the v0.14-v0.16 trust-the-client posture so legacy
        deployments keep working unchanged.

        Per-claim behaviour:

        - Items without a ``symbols`` payload are skipped (whole-file
          scope has no symbols to validate).
        - Resolve the claim's pattern against ``settings.repo_root``;
          if the resolved path is missing on disk, skip silently. The
          claim may legitimately reference a file landing in the same
          commit, and refusing such claims would create a chicken-and-
          egg failure mode at file-creation time.
        - Otherwise parse the file with
          :func:`coordination.symbols.extract_symbols` and build the
          set of known canonical paths via
          :func:`coordination.overlap_symbols.format_symbol_path`. The
          set also includes every ancestor path so a claim like
          ``"Outer"`` is accepted when the parser only emits leaves
          (``Outer::Inner::method``).

        Returns a single combined error string listing the missing
        symbols per file plus up to 20 of the file's actual symbols as
        a hint, or ``None`` when every claimed symbol checks out.

        v0.31 additions, both optional so the validation contract is
        untouched for callers that pass neither:

        - ``parser_symbols_out``: filled with pattern -> extracted
          :class:`Symbol` list for every file this method actually
          parsed, so span persistence can reuse the extraction instead
          of re-parsing.
        - ``lsp_symbols_cache``: when ``COORD_LSP_ENABLED`` is on, a
          symbol the parser cannot find gets one more chance via a
          language-server ``documentSymbol`` lookup before rejection
          (one call per file, cached in this dict across the request).
          The LSP can only ever ACCEPT symbols the parser missed --
          parser-validated symbols never consult it -- and any LSP
          failure silently restores the exact v0.17 rejection
          behaviour.
        """

        root = self.settings.repo_root
        if not root or not root.is_dir():
            return None

        per_file_errors: list[str] = []
        for item in body.claims:
            if not item.symbols:
                continue
            resolved = (root / item.pattern).resolve()
            if relpath_under_root(resolved, root) is None:
                # Path escapes the repo root or cannot be resolved;
                # leave validation to the scope-check layer.
                continue
            if not resolved.is_file():
                # File may be arriving in the same commit; skip rather
                # than block. Mirrors the zero-match warning policy.
                continue
            try:
                content = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning(
                    "symbol validation: failed to read %s: %s", resolved, exc
                )
                continue
            symbols = extract_symbols(str(resolved), content)
            if parser_symbols_out is not None:
                parser_symbols_out[item.pattern] = symbols
            if not symbols:
                # Unsupported extension or empty file: no ground truth
                # to validate against. Skip silently so non-TS/Py/Go
                # claims aren't blocked by the absence of a parser.
                continue
            valid_paths: set[str] = set()
            for sym in symbols:
                valid_paths.add(format_symbol_path(sym.parent, sym.name))
                # Also accept intermediate ancestor paths so a claim
                # on "Outer" is valid when the parser only emits
                # "Outer::Inner::method" (leaves-only backends).
                parent = sym.parent
                while parent:
                    valid_paths.add(parent)
                    if "::" in parent:
                        parent = parent.rsplit("::", 1)[0]
                    else:
                        parent = None
            missing = [s for s in item.symbols if s not in valid_paths]
            if missing and self.settings.lsp_enabled:
                # v0.31 validation fallback: the parser said no, but
                # parsers miss dynamically-significant declarations
                # (conditional defs, decorated factories). Ask the
                # language server before rejecting; an LSP failure
                # (None) leaves ``missing`` exactly as the parser saw
                # it, which is byte-identical to the v0.17 behaviour.
                flattened = await self._lsp_document_symbols(
                    item.pattern, resolved, lsp_symbols_cache
                )
                if flattened is not None:
                    lsp_paths = _lsp_symbol_path_set(flattened)
                    missing = [s for s in missing if s not in lsp_paths]
            if not missing:
                continue
            hint_symbols = sorted(valid_paths)[:20]
            per_file_errors.append(
                f"Unknown symbols in {item.pattern!r}: "
                f"{sorted(missing)!r}. "
                f"Known symbols (up to 20): {hint_symbols!r}"
            )

        if not per_file_errors:
            return None
        return "; ".join(per_file_errors)

    async def check_conflicts(
        self,
        *,
        patterns: list[str],
        engineer: str,
        repo: str | None = None,
        all_repos: bool = False,
        session_ids: list[str] | None = None,
        pushing_branch: str | None = None,
    ) -> ConflictCheckResponse:
        for pat in patterns:
            err = _validate_pattern_syntax(pat)
            if err:
                raise ValueError(err)
        await self.expire_stale_claims()
        # Activity ping: a session that's actively checking conflicts is
        # still alive even if it isn't creating new claims, so refresh
        # last_activity for everything it currently holds before we
        # decide what counts as "stale". v0.10 generalises this from a
        # single session_id to a list -- one agent process can carry
        # multiple live session_ids in the repo at once (parent
        # dispatcher + per-worktree subagents), and every one of them
        # needs to keep its claims warm.
        if session_ids:
            for sid in session_ids:
                await self._maybe_touch(sid, repo)
        active = await self.db.list_active_claims_rows(exclude_engineer=engineer)
        # Repo-scoped check (v0.4.0): only consider claims from the same
        # repo as the caller. NULL repo forms its own legacy bucket so
        # tagged callers never collide with un-tagged historical claims
        # and vice versa.
        #
        # v0.42: an operator explicitly asking for all_repos wants to see
        # every repo's claims, so skip the bucket filter entirely.
        # Without this, all_repos resolves to repo=None and the filter
        # would compare only against the legacy NULL bucket -- on a fully
        # repo-tagged deployment that returns zero and silently
        # under-reports. (A scoped token never reaches here with
        # all_repos: _effective_read_repo 403s it first.)
        if not all_repos:
            active = [r for r in active if r.get("repo") == repo]
        # Session-scoped self-exclusion (v0.5.0, generalised in v0.10):
        # drop any active claim whose session_id matches one of the
        # caller's live session_ids. The pre-push hook reads every line
        # of .coordination/sessions.live and forwards them so an
        # agent's own subagent claims under different engineer names
        # don't false-positive on its own push. Different sessions
        # outside that set remain adversarial.
        if session_ids:
            exclude = set(session_ids)
            active = [r for r in active if r.get("session_id") not in exclude]
        # Coexist self-exclusion (v0.11+): if any of the caller's
        # session(s) hold a claim that's coexisting with another claim
        # X, X is invisible to the caller's conflict check. The pair
        # was explicitly granted by both sides via the request flow,
        # so an in-pair edit is cooperative not adversarial. Outsiders
        # still see X normally because we only ever harvest partners
        # from the caller's own claim rows.
        if session_ids:
            own_session_set = set(session_ids)
            own_claims = await self.db.list_active_claims_rows(exclude_engineer=None)
            # The own-claims harvest mirrors the adversarial set's repo
            # handling: ``all_repos=True`` resolves to ``repo=None``, so
            # keeping the bucket filter here would confine the harvest to
            # the legacy NULL-repo bucket -- on a fully repo-tagged
            # deployment that harvests nothing and the operator's global
            # view reports conflicts against claims that are explicitly
            # granted coexist partners of the caller's sessions.
            own_claims = [
                c
                for c in own_claims
                if c.get("session_id") in own_session_set
                and (all_repos or c.get("repo") == repo)
            ]
            partner_ids = _coexist_partner_ids_from_rows(own_claims)
            if partner_ids:
                # v0.35: only blanket-skip FILE-scoped partners. A
                # symbol-scoped partner stays in ``active`` so a later
                # claim is re-evaluated against its granted symbols.
                skip = _blanket_skip_partner_ids(partner_ids, active)
                if skip:
                    active = [r for r in active if str(r.get("id")) not in skip]
        # v0.34: when the GitHub integration is enabled we accumulate a
        # parallel ``bounced`` list off the same overlap computation so a
        # conflict can be surfaced as a PR comment. Each entry carries the
        # holder claim's branch/description/pattern plus the overlapping
        # files. The list is only built when github_token is set so a
        # repo without the integration pays zero extra cost.
        github_enabled = bool((self.settings.github_token or "").strip())
        bounced: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for pat in patterns:
            for row in active:
                overlap = await compute_overlap(
                    pat,
                    row["pattern"],
                    repo_root=self.settings.repo_root,
                    scope=self.settings.repo_scope,
                )
                if not overlap:
                    continue
                conflicts.append(
                    {
                        "your_pattern": pat,
                        "engineer": row["engineer"],
                        "pattern": row["pattern"],
                        "severity": row["severity"],
                        "expires_at": row["expires_at"],
                        "overlap": overlap,
                    }
                )
                if github_enabled:
                    bounced.append(
                        {
                            "files": overlap,
                            "holder_engineer": row["engineer"],
                            "holder_branch": row.get("branch"),
                            "holder_pattern": row["pattern"],
                            "holder_description": row.get("description"),
                        }
                    )
        safe = len(conflicts) == 0
        suggestion: str | None = None
        if not safe:
            c0 = conflicts[0]
            suggestion = (
                f"Conflict with {c0.get('engineer')} on {c0.get('pattern')} "
                f"(expires {c0.get('expires_at')}). Wait for TTL, narrow your patterns, "
                "or coordinate with the other engineer."
            )
        # v0.34: a bounced push emits a ``push_bounced`` event routed
        # through the webhook outbox with kind='github'. The delivery
        # loop hands the detail to the GitHub adapter, which posts/updates
        # a de-duplicated comment on the open PR for ``pushing_branch``.
        # Fully gated on github_token: when it is unset ``github_enabled``
        # is False, no row is built or enqueued, and the whole feature is
        # a no-op. The conflict response below is unchanged either way.
        if github_enabled and not safe:
            # Merge holder sections so a holder overlapping via multiple
            # pushed patterns renders once with the union of bounced files.
            merged: dict[tuple, dict] = {}
            for b in bounced:
                key = (b["holder_engineer"], b["holder_pattern"], b["holder_branch"])
                if key in merged:
                    files = merged[key]["files"]
                    for f in b["files"]:
                        if f not in files:
                            files.append(f)
                else:
                    merged[key] = {**b, "files": list(b["files"])}
            bounced = list(merged.values())
            detail = {
                "repo": repo or "",
                "pushing_engineer": engineer,
                "pushing_branch": pushing_branch or "",
                "bounced": bounced,
            }
            await self.fire_webhook("push_bounced", detail, kind="github")
        return ConflictCheckResponse(
            has_conflicts=not safe,
            conflicts=conflicts,
            safe_to_proceed=safe,
            safe=safe,
            suggestion=suggestion,
        )

    async def _enforce_active_claim_cap(
        self, *, engineer: str, about_to_insert: int, repo: str | None = None
    ) -> None:
        """v0.30 active-claim cap: raise :class:`RateLimitExceeded`
        when inserting ``about_to_insert`` new claim rows would push
        ``engineer`` past ``max_claims_per_engineer``.

        Called immediately before the insert path -- NOT at the top of
        ``create_claims`` -- because a request that is going to 409 (or
        queue behind a holder) never inserts anything: an at-cap
        engineer must still be allowed to queue future work, with the
        cap re-checked when the drain actually tries to grant.

        There is no supersede flow to exclude: ``create_claims`` never
        closes one of the requester's existing claims while opening a
        replacement in the same call (auto-narrow narrows the HOLDER's
        claim, a different engineer, and the v0.11 ``narrowed``
        decision runs entirely in the DB layer), so a plain count of
        the engineer's active rows is the correct denominator.

        Retry-After is the time until the engineer's soonest active
        claim TTL-expires, clamped to [5, 3600] so a far-future expiry
        cannot tell a client to sleep for hours and a just-expiring
        claim cannot produce a zero/negative hint. When the engineer
        somehow has no active claims at all (the batch alone exceeds
        the limit), there is no expiry to anchor on and we fall back
        to a flat 60.
        """
        limit = self.settings.max_claims_per_engineer
        if limit <= 0:
            return
        count, soonest_expiry = await self.db.count_active_claims_for_engineer(
            engineer, repo=repo
        )
        if count + about_to_insert <= limit:
            return
        retry_after = 60
        if soonest_expiry is not None:
            try:
                exp = datetime.fromisoformat(
                    soonest_expiry.replace("Z", "+00:00")
                )
                retry_after = int((exp - datetime.now(UTC)).total_seconds())
            except ValueError:
                retry_after = 60
            retry_after = max(5, min(3600, retry_after))
        detail = (
            f"engineer {engineer!r} holds {count} active claims and this "
            f"request would insert {about_to_insert} more; the limit is "
            f"{limit} (COORD_MAX_CLAIMS_PER_ENGINEER). Release finished "
            "claims or wait for TTL expiry."
        )
        if about_to_insert > limit:
            detail += (
                f" The batch alone ({about_to_insert} claims) exceeds the "
                "limit; reduce the batch size."
            )
        raise RateLimitExceeded(
            scope="claims", detail=detail, retry_after_sec=retry_after
        )

    async def create_claims(
        self,
        body: CreateClaimsRequest,
        *,
        auto_promote_allowed: bool = True,
    ) -> CreateClaimsResponse:
        await self.expire_stale_claims()
        # Activity ping: making a claim is the strongest possible
        # liveness signal -- bump last_activity for every claim this
        # session already holds before we decide what's stale.
        if body.session_id:
            await self._maybe_touch(body.session_id, body.repo)
        rules = await self._rules()
        # Intake canonicalization: collapse every claim pattern to the same
        # canonical form the overlap engine matches with (backslashes ->
        # forward slashes, leading "./" and "/" stripped, trailing "/"
        # expanded to "/**") BEFORE validation, storage, and symbol
        # bookkeeping. The pattern is the join key everywhere downstream --
        # ``claims.pattern``, ``claim_symbols.file_path``, and the requester
        # side of the symbol-overlap classifier -- so two spellings of the
        # same file ("./src/a.py" vs "src/a.py") must collapse here or the
        # symbol/symbol classifier compares different dict keys and lets two
        # claims on the same symbol silently auto-coexist.
        for item in body.claims:
            item.pattern = _normalize_pattern(item.pattern)
        patterns = [c.pattern for c in body.claims]
        for pat in patterns:
            syntax_err = _validate_pattern_syntax(pat)
            if syntax_err:
                return CreateClaimsResponse(
                    claim_ids=[],
                    conflicts=[],
                    warnings=[syntax_err],
                    options=["narrow_claim"],
                )
        scope_err = await self._validate_claim_scope(patterns)
        if scope_err:
            return CreateClaimsResponse(
                claim_ids=[],
                conflicts=[],
                warnings=[scope_err],
                options=["narrow_claim", "escalate"],
            )

        # v0.17: when COORD_REPO_ROOT is set, validate that every claimed
        # symbol exists in its file. The helper short-circuits when the
        # repo root is unset so legacy deployments keep working.
        #
        # v0.31: the validation pass already parses every claimed file,
        # so we capture its extraction (and any LSP documentSymbol
        # results the fallback fetched) and hand both to the span
        # persistence in _finalise_v14_scope -- no file is parsed twice
        # and no file gets more than one LSP call per request.
        parser_symbols_by_file: dict[str, list[Symbol]] = {}
        lsp_symbols_by_file: dict[str, list[dict[str, Any]] | None] = {}
        symbol_err = await self._validate_claim_symbols(
            body,
            parser_symbols_out=parser_symbols_by_file,
            lsp_symbols_cache=lsp_symbols_by_file,
        )
        if symbol_err:
            return CreateClaimsResponse(
                claim_ids=[],
                conflicts=[],
                warnings=[symbol_err],
                options=["narrow_claim"],
            )

        zero_match_warnings = await self._zero_match_warnings(patterns)

        # v0.45.x audit: resolve v0.31 span sources HERE, in Phase A,
        # before the grant transaction opens. _finalise_v14_scope used to
        # run its own LSP documentSymbol roundtrips while the claim-grant
        # transaction held the v0.44 shared writer lock (SQLite) / the
        # per-repo advisory lock (Postgres), so a slow or timing-out
        # language server stalled every write in the process. Pre-resolving
        # per unique pattern keeps the transaction pure DB work; a batch
        # that ends up 409ing wastes at most one cached LSP call per file.
        parser_span_by_pattern, lsp_span_by_pattern = (
            await self._resolve_symbol_spans(
                body.claims,
                parser_symbols_by_file=parser_symbols_by_file,
                lsp_symbols_by_file=lsp_symbols_by_file,
            )
        )

        conflicts: list[ConflictEntry] = []
        # Queued auto-resolutions (v0.14): tuples of (item_index,
        # holder_row, result) discovered during the overlap pass that
        # should bypass 409 and be recorded as auto-coexist / auto-narrow
        # events after the requester's claim row is inserted. We key by
        # the ClaimItem's index in ``body.claims`` (NOT its pattern):
        # nothing rejects duplicate patterns within one batch, and a
        # pattern-keyed lookup would collapse duplicates onto the last
        # inserted claim id, doubling one item's coexist partner links
        # while dropping the other's.
        auto_resolutions: list[tuple[int, dict[str, Any], Any]] = []

        # Phase B (design 5.1/5.2): the claims-table overlap RE-CHECK,
        # the claim insert, the v0.14 scope/symbol finalization and the
        # auto-resolution bookkeeping run as ONE unit-of-work on ONE
        # connection, under a per-repo lock, so the grant is atomic
        # against concurrent writers. Phase A above (pattern expansion,
        # git ls-files, LSP, scope/symbol validation) stayed lock-free.
        # On SQLite ``repo_lock`` is a no-op and ``transaction`` yields a
        # single connection every nested Database call reuses; the
        # Postgres backend (P3) makes the lock real. The conflict tail
        # (auto-promote, the wait-queue long-poll, the 409) runs OUTSIDE
        # the transaction so slow work never executes under the lock.
        created: list[str] = []
        ids: list[tuple[str, str, str, str, str]] = []
        # Parallel to ids: per-item (cid, item) so post-insert wiring can
        # find the right ClaimItem for each created row without re-zipping.
        item_for_cid: dict[str, ClaimItem] = {}
        async with self.db.transaction() as conn:
            await self.db.repo_lock(conn, body.repo)
            active = await self.db.list_active_claims_rows(exclude_engineer=body.engineer)
            # Repo-scoped check (v0.4.0): see check_conflicts for rationale.
            active = [r for r in active if r.get("repo") == body.repo]
            # Session-scoped self-exclusion (v0.5.0): see check_conflicts.
            if body.session_id:
                active = [r for r in active if r.get("session_id") != body.session_id]
            # Coexist self-exclusion (v0.11+): mirror check_conflicts so a
            # coexist partner adding a NEW claim alongside their existing
            # one isn't blocked by the partner they were explicitly granted
            # coexistence with.
            if body.session_id:
                own_claims = await self.db.list_active_claims_rows(exclude_engineer=None)
                own_claims = [
                    c
                    for c in own_claims
                    if c.get("session_id") == body.session_id
                    and c.get("repo") == body.repo
                ]
                partner_ids = _coexist_partner_ids_from_rows(own_claims)
                if partner_ids:
                    # v0.35: only blanket-skip FILE-scoped partners. A
                    # symbol-scoped partner stays in ``active`` so this new
                    # claim is re-evaluated against its granted symbols via
                    # the normal symbol-overlap path (409 on collision,
                    # auto-coexist when disjoint).
                    skip = _blanket_skip_partner_ids(partner_ids, active)
                    if skip:
                        active = [r for r in active if str(r.get("id")) not in skip]
            for item_idx, item in enumerate(body.claims):
                requester_scope = "symbol" if item.symbols else "file"
                requester_symbols_by_file: dict[str, list[str]] = (
                    {item.pattern: list(item.symbols)} if item.symbols else {}
                )
                for row in active:
                    result = await check_symbol_overlap(
                        db=self.db,
                        holder=row,
                        requester_pattern=item.pattern,
                        requester_scope_type=requester_scope,
                        requester_symbols_by_file=requester_symbols_by_file,
                    )
                    if result.kind is OverlapKind.NO_OVERLAP:
                        continue
                    if result.kind in (
                        OverlapKind.AUTO_COEXIST,
                        OverlapKind.AUTO_NARROW,
                    ):
                        auto_resolutions.append((item_idx, row, result))
                        continue
                    # FILE_OVERLAP, SYMBOL_OVERLAP, PARTIAL_GRANT all 409.
                    holder_symbols: list[str] | None = None
                    if row.get("scope_type") == "symbol":
                        holder_symbol_rows = await self.db.get_claim_symbols(
                            str(row["id"])
                        )
                        holder_symbols = sorted(
                            {s["symbol_name"] for s in holder_symbol_rows}
                        )
                    symbol_overlap_payload: list[ConflictingSymbol] | None = None
                    if result.kind is OverlapKind.SYMBOL_OVERLAP:
                        symbol_overlap_payload = [
                            ConflictingSymbol(file=f, symbols=list(syms))
                            for f, syms in result.overlapping_symbols
                        ]
                    conflicts.append(
                        ConflictEntry(
                            your_pattern=item.pattern,
                            your_symbols=(
                                list(item.symbols) if item.symbols else None
                            ),
                            conflicting_claim=ConflictingClaim(
                                id=row["id"],
                                engineer=row["engineer"],
                                pattern=row["pattern"],
                                severity=row["severity"],
                                description=row.get("description"),
                                expires_at=row["expires_at"],
                                scope_type=row.get("scope_type"),
                                symbols=holder_symbols,
                            ),
                            overlap=list(result.overlapping_paths),
                            symbol_overlap=symbol_overlap_payload,
                        )
                    )
                    await self.db.log_conflict(
                        claim_id=row["id"],
                        attempted_by=body.engineer,
                        attempted_pattern=item.pattern,
                        resolution=None,
                        attempted_session_id=body.session_id,
                    )
                    metrics.claims_conflicts_total.inc()

            if not conflicts:
                ttl = body.ttl_hours or self.settings.default_ttl_hours
                for item in body.claims:
                    cid = str(uuid4())
                    sev = severity_for_pattern(item.pattern, rules) if rules else "soft"
                    if item.type == "shared_file":
                        exp = _expires_at(self.settings.shared_ttl_hours)
                    else:
                        exp = _expires_at(ttl)
                    ids.append((cid, item.type, item.pattern, sev, exp))
                    item_for_cid[cid] = item

                # v0.30 active-claim cap: enforced here, on the only path in
                # this method that inserts active claim rows, so that requests
                # which 409 (or queue with wait_seconds) above are never
                # blocked by the cap. Drain-time grants re-enter create_claims
                # and hit this same check, so a queue grant cannot blast
                # through the cap either -- _drain_queue_for catches the raise
                # and moves on to the next waiter. The count and the insert run
                # on the bound grant connection under db.engineer_lock (design
                # 5.3): a per-engineer guard that is an in-process asyncio.Lock
                # on SQLite and a per-engineer pg_advisory_xact_lock on Postgres,
                # so two concurrent requests -- in one process or across three
                # replicas -- cannot both observe an under-cap count and
                # overshoot. When the cap is disabled (the default) the insert
                # skips the guard entirely.

                async def _insert_batch() -> Any:
                    return await self.db.insert_claims_batch(
                        engineer=body.engineer,
                        branch=body.branch,
                        description=body.description,
                        items=ids,
                        repo=body.repo,
                        session_id=body.session_id,
                    )

                if self.settings.max_claims_per_engineer > 0:
                    async with self.db.engineer_lock(conn, body.engineer):
                        await self._enforce_active_claim_cap(
                            engineer=body.engineer,
                            about_to_insert=len(ids),
                            repo=body.repo,
                        )
                        created = await _insert_batch()
                else:
                    created = await _insert_batch()
                # v0.14: post-insert scope_type / narrowable / symbol rows. We
                # defer this from insert_claims_batch to keep its signature stable
                # and the migration footprint minimal -- the create_claims handler
                # owns the symbol contract.
                await self._finalise_v14_scope(
                    created=created,
                    item_for_cid=item_for_cid,
                    parser_span_by_pattern=parser_span_by_pattern,
                    lsp_span_by_pattern=lsp_span_by_pattern,
                )

                # v0.14: persist any auto-resolutions queued during overlap pass.
                # ``ids`` was built by iterating ``body.claims`` in order, so
                # ``ids[item_idx]`` is exactly the row inserted for
                # ``body.claims[item_idx]``. Keying by index (not pattern)
                # keeps the wiring correct when a batch carries duplicate
                # patterns -- e.g. two symbol claims on different symbol sets
                # of the same file.
                if auto_resolutions:
                    created_set = set(created)
                    for item_idx, holder_row, result in auto_resolutions:
                        requester_cid = (
                            ids[item_idx][0] if item_idx < len(ids) else None
                        )
                        if not requester_cid or requester_cid not in created_set:
                            continue
                        await record_auto_resolution(
                            db=self.db,
                            kind=result.kind,
                            holder_claim_id=str(holder_row["id"]),
                            requester_claim_id=requester_cid,
                            overlapping_paths=result.overlapping_paths,
                            overlapping_symbols=result.overlapping_symbols,
                            service=self,
                        )

                # Count one tick per successfully inserted claim. We look back at
                # the computed severity for each item so the label distribution
                # mirrors the ownership configuration.
                for _cid, _ctype, _pattern, sev, _exp in ids:
                    if _cid in created:
                        metrics.claims_created_total.inc(severity=sev)
                # v0.27: emit a single ``claim_granted`` webhook for the whole
                # batch so external receivers see one event per
                # ``POST /claims`` call instead of one per claim row. The detail
                # carries the full list of created ids plus the caller identity
                # so downstream subscribers can correlate against their own
                # state without re-querying the API.
                if created:
                    await self.fire_webhook(
                        "claim_granted",
                        {
                            "claim_ids": list(created),
                            "engineer": body.engineer,
                            "repo": body.repo,
                            "session_id": body.session_id,
                        },
                    )

        if conflicts:
            # v0.22 hard auto-promote: when blocked patterns have crossed
            # the configured hotspot threshold within the window, write
            # a ``shared_file`` rule for them into the active
            # ownership YAML and record an audit event. The current
            # 409 response is unchanged -- the new rule governs the
            # NEXT overlap on this pattern.
            # Auto-promote mutates the GLOBAL ownership YAML, so it is
            # an operator-only action. A repo-scoped caller passes
            # ``auto_promote_allowed=False`` (v0.42) to keep its claim
            # activity from silently rewriting shared config across the
            # whole deployment -- the operator-only manual promote /
            # config-write endpoints are the sanctioned path.
            if self.settings.auto_promote_threshold > 0 and auto_promote_allowed:
                await self._maybe_auto_promote(conflicts)
            # v0.21: if the caller passed wait_seconds > 0, enqueue the
            # FIRST conflicting requester item behind its blocking
            # holder and long-poll for an auto-grant from a release.
            # On timeout we fall through to the legacy 409 path so
            # legacy clients see the same shape.
            wait_seconds = getattr(body, "wait_seconds", None) or 0
            if wait_seconds and wait_seconds > 0:
                granted = await self._enqueue_and_wait(
                    body=body,
                    conflicts=conflicts,
                    wait_seconds=int(wait_seconds),
                )
                if granted is not None:
                    return granted
            return CreateClaimsResponse(
                claim_ids=[],
                conflicts=conflicts,
                warnings=[],
                options=["wait", "narrow_claim", "escalate", "override"],
            )

        # v0.31 wave 2: background callsite enrichment. For every
        # symbol-scope claim that just landed, a fire-and-forget task
        # asks the language server who calls each claimed symbol and
        # records the answers in claim_symbol_callsites. Strictly
        # advisory data, so the task never blocks (or fails) the grant
        # response. Gated exactly like span persistence: LSP on and a
        # repo root to resolve against.
        if self.settings.lsp_enabled and self.settings.repo_root:
            for cid in created:
                created_item = item_for_cid.get(cid)
                if created_item is not None and created_item.symbols:
                    self._schedule_callsite_enrichment(cid)

        # v0.31 wave 2: advisory CALLSITE_OVERLAP. The grant already
        # happened; this only decorates the response with warnings when
        # someone else's claimed symbol is called from inside the scope
        # just granted. Riding on the existing ``warnings`` field is
        # safe on a successful grant: main.py's warnings->400 mapping
        # fires only when ``claim_ids`` is empty.
        advisory_warnings: list[str] = []
        if self.settings.lsp_enabled and created:
            advisory_warnings = await self._callsite_advisories(
                body=body, created=created, item_for_cid=item_for_cid
            )

        return CreateClaimsResponse(
            claim_ids=created,
            conflicts=[],
            warnings=zero_match_warnings + advisory_warnings,
            options=[],
        )

    async def _maybe_auto_promote(
        self, conflicts: list[ConflictEntry]
    ) -> None:
        """v0.22 hard auto-promote with v0.26 pattern-class granularity.

        Two-phase process.

        Phase 1 collects every unique ``your_pattern`` from this batch
        that the hotspot query reports as having crossed
        ``auto_promote_threshold`` attempts within the rolling
        ``auto_promote_window_days`` window.

        Phase 2 groups qualifying leaves by their parent directory
        (everything up to the last ``/``; top-level files share the
        empty-string bucket). For each directory that has at least
        ``auto_promote_subtree_min_files`` qualifying leaves, the
        subtree glob ``{dir}/**`` is written into the active ownership
        YAML once (instead of writing each leaf as a separate
        ``shared_files`` entry). For directories with fewer qualifying
        leaves, each leaf is promoted individually -- the v0.22
        behaviour. When ``auto_promote_subtree_min_files == 0``,
        Phase 2 is skipped entirely so the v0.22 per-file behaviour is
        preserved exactly.

        Audit ``request_events`` rows record the resulting YAML change:
        subtree promotes carry ``subtree=True`` plus the list of source
        leaves and the source count; per-file promotes carry
        ``subtree=False``.

        Called only when ``auto_promote_threshold > 0``. Failures from
        the YAML patch (e.g. an operator-introduced parse error) are
        logged and swallowed so a malformed ownership document cannot
        break the 409 response path; the next claim that crosses the
        threshold will retry.
        """

        threshold = self.settings.auto_promote_threshold
        window = self.settings.auto_promote_window_days
        subtree_min = self.settings.auto_promote_subtree_min_files

        # Deduplicate ``your_pattern`` across the batch while
        # preserving first-seen order so the audit log mirrors the
        # order the agent's own request emitted them.
        seen: set[str] = set()
        unique_patterns: list[str] = []
        for entry in conflicts:
            pat = entry.your_pattern
            if pat in seen:
                continue
            seen.add(pat)
            unique_patterns.append(pat)
        if not unique_patterns:
            return

        # Single hotspot query covers every candidate in this batch; we
        # then filter the result against ``unique_patterns``. This also
        # means a malformed query short-circuits the whole batch rather
        # than retrying per-pattern.
        try:
            hotspots = await self.db.hotspot_files(
                days=window,
                min_attempts=threshold,
            )
        except Exception:  # noqa: BLE001 - audit path is best-effort
            logger.exception(
                "auto-promote: hotspot_files query failed; skipping batch"
            )
            return
        hot_patterns: set[str] = {
            str(h["pattern"]) for h in hotspots if h.get("pattern")
        }

        # Phase 1: qualifying leaves preserve their input order.
        qualifying: list[str] = [
            p for p in unique_patterns if p in hot_patterns
        ]
        if not qualifying:
            return

        # Phase 2: group by parent directory unless subtree promotion
        # is disabled. ``covered_by_subtree`` collects leaves that
        # were promoted via their subtree glob so we don't also emit
        # individual entries for them.
        groups: dict[str, list[str]] = {}
        for pattern in qualifying:
            parent = pattern.rsplit("/", 1)[0] if "/" in pattern else ""
            groups.setdefault(parent, []).append(pattern)

        subtree_promotions: list[tuple[str, list[str]]] = []
        leaf_promotions: list[str] = []
        if subtree_min > 0:
            for parent, leaves in groups.items():
                if not parent:
                    # Top-level files have no meaningful subtree;
                    # always promote individually.
                    leaf_promotions.extend(leaves)
                    continue
                if len(leaves) >= subtree_min:
                    subtree_promotions.append(
                        (f"{parent}/**", list(leaves))
                    )
                else:
                    leaf_promotions.extend(leaves)
        else:
            leaf_promotions.extend(qualifying)

        # Subtree promotions first so the YAML reads top-down as
        # "broad rule then any per-file specifics" -- matches how an
        # operator would hand-write the file.
        for subtree_glob, source_leaves in subtree_promotions:
            before = await self.db.get_ownership_yaml() or ""
            try:
                after = await self.promote_hotspot(
                    action="shared_file",
                    pattern=subtree_glob,
                    note=None,
                    managed=True,
                )
            except ValueError:
                logger.exception(
                    "auto-promote: failed to patch owners.yaml for %r",
                    subtree_glob,
                )
                continue
            if after == before:
                # Already present; idempotent no-op, no audit.
                continue
            subtree_detail = {
                "pattern": subtree_glob,
                "source_count": len(source_leaves),
                "source_patterns": source_leaves,
                "threshold": threshold,
                "window_days": window,
                "subtree": True,
            }
            await self.db.record_request_event(
                event_type="auto-promote",
                request_id=None,
                actor_engineer=None,
                actor_session_id=None,
                detail=subtree_detail,
            )
            await self.fire_webhook("auto-promote", subtree_detail)

        for pattern in leaf_promotions:
            before = await self.db.get_ownership_yaml() or ""
            try:
                after = await self.promote_hotspot(
                    action="shared_file",
                    pattern=pattern,
                    note=None,
                    managed=True,
                )
            except ValueError:
                # patch helpers raise ValueError on a malformed stored
                # YAML; log and move on so the conflict response still
                # returns cleanly.
                logger.exception(
                    "auto-promote: failed to patch owners.yaml for %r",
                    pattern,
                )
                continue
            if after == before:
                # Already in the rule set; idempotent no-op, no audit.
                continue
            leaf_detail = {
                "pattern": pattern,
                "threshold": threshold,
                "window_days": window,
                "subtree": False,
            }
            await self.db.record_request_event(
                event_type="auto-promote",
                request_id=None,
                actor_engineer=None,
                actor_session_id=None,
                detail=leaf_detail,
            )
            await self.fire_webhook("auto-promote", leaf_detail)

    async def _resolve_symbol_spans(
        self,
        items: list[ClaimItem],
        *,
        parser_symbols_by_file: dict[str, list[Symbol]] | None,
        lsp_symbols_by_file: dict[str, list[dict[str, Any]] | None] | None,
    ) -> tuple[
        dict[str, dict[str, tuple[int, int]]],
        dict[str, dict[str, tuple[int, int, int, int]]],
    ]:
        """Resolve v0.31 span sources for every symbol-scope item in a
        claim batch. One entry per unique pattern; duplicate patterns in
        a batch share the work.

        This runs in Phase A of ``create_claims`` -- BEFORE
        ``db.transaction()`` opens -- because the LSP documentSymbol
        roundtrip can take up to the request timeout and must never
        execute under the v0.44 shared writer lock (or the Postgres
        per-repo advisory lock). ``_finalise_v14_scope`` then consumes
        the returned maps as pure data, so the grant unit-of-work stays
        DB-only.

        ``parser_symbols_by_file`` is the extraction the validation pass
        already produced (parser spans: 1-based lines, NULL columns,
        resolved_by='parser'). When LSP is enabled, one documentSymbol
        call per claimed file (reusing ``lsp_symbols_by_file`` entries
        the validation fallback may have cached) yields exact ranges
        for resolved_by='lsp'; a pool failure leaves that pattern with
        an empty LSP map so the parser spans win downstream.
        """
        parser_span_by_pattern: dict[str, dict[str, tuple[int, int]]] = {}
        lsp_span_by_pattern: dict[str, dict[str, tuple[int, int, int, int]]] = {}
        root = self.settings.repo_root
        for item in items:
            if not item.symbols:
                continue
            if item.pattern not in parser_span_by_pattern:
                spans: dict[str, tuple[int, int]] = {}
                for sym in (parser_symbols_by_file or {}).get(item.pattern, []):
                    spans[format_symbol_path(sym.parent, sym.name)] = (
                        sym.start_line,
                        sym.end_line,
                    )
                parser_span_by_pattern[item.pattern] = spans
            if (
                item.pattern not in lsp_span_by_pattern
                and self.settings.lsp_enabled
                and root
            ):
                flattened = await self._lsp_document_symbols(
                    item.pattern, root / item.pattern, lsp_symbols_by_file
                )
                lsp_span_by_pattern[item.pattern] = (
                    _lsp_span_map(flattened) if flattened is not None else {}
                )
        return parser_span_by_pattern, lsp_span_by_pattern

    async def _finalise_v14_scope(
        self,
        *,
        created: list[str],
        item_for_cid: dict[str, ClaimItem],
        parser_span_by_pattern: dict[str, dict[str, tuple[int, int]]] | None = None,
        lsp_span_by_pattern: (
            dict[str, dict[str, tuple[int, int, int, int]]] | None
        ) = None,
    ) -> None:
        """Apply v0.14 ``scope_type`` / ``narrowable`` / ``claim_symbols``
        to each just-inserted claim row.

        v0.14 fields default to ``scope_type='file'`` and ``narrowable=1``
        at the schema level. The conflict pipeline then promotes rows
        per ClaimItem: symbol claims get ``scope_type='symbol'`` plus
        ``claim_symbols`` rows; ``shared_file`` / ``module`` / explicit
        opt-out claims get ``narrowable=0``. Splitting this out of
        ``insert_claims_batch`` keeps the legacy contract intact and
        makes the v0.14 surface self-contained.

        v0.31 span persistence: each ``claim_symbols`` row carries the
        symbol's location at claim time. ``parser_span_by_pattern`` and
        ``lsp_span_by_pattern`` arrive pre-resolved from
        :meth:`_resolve_symbol_spans`, which ``create_claims`` runs in
        Phase A so no LSP (or any other slow) work ever executes here --
        this method is called inside the claim-grant transaction and
        must stay pure DB writes (see the Phase B design note in
        ``create_claims``). LSP spans win when present; otherwise the
        parser's line span; otherwise the span columns stay NULL and
        overlap detection remains purely lexical.
        """
        if not created:
            return

        parser_span_by_pattern = parser_span_by_pattern or {}
        lsp_span_by_pattern = lsp_span_by_pattern or {}

        symbol_rows: list[tuple[Any, ...]] = []
        # Routed through the Database transaction seam: when this runs
        # inside the claim-grant unit-of-work the scope UPDATEs land on
        # the bound connection and commit with the rest of the grant;
        # outside one it opens and commits its own connection, identical
        # to the legacy connection-per-op behaviour.
        async with self.db._acquire() as (conn, owns):
            for cid in created:
                item = item_for_cid.get(cid)
                if item is None:
                    continue
                want_scope = "symbol" if item.symbols else "file"
                # Narrowable resolution: explicit value wins; shared_file
                # / module / symbol scope all default to non-narrowable;
                # plain file claims default narrowable=1.
                if item.narrowable is not None:
                    narrowable = 1 if item.narrowable else 0
                elif item.type in ("shared_file", "module") or want_scope == "symbol":
                    narrowable = 0
                else:
                    narrowable = 1
                if want_scope != "file" or narrowable != 1:
                    await conn.execute(
                        "UPDATE claims SET scope_type = ?, narrowable = ? "
                        "WHERE id = ?",
                        (want_scope, narrowable, cid),
                    )
                if item.symbols:
                    parser_spans = parser_span_by_pattern.get(item.pattern, {})
                    lsp_spans = lsp_span_by_pattern.get(item.pattern, {})
                    for raw in item.symbols:
                        # v0.16: split "Parent::child" notation at insert
                        # time. parent_symbol=NULL for legacy top-level
                        # entries; non-NULL for method-scope.
                        # v0.17: rpartition so the LAST "::" separates leaf
                        # from ancestor path; supports "A::B::method" as
                        # parent="A::B", leaf="method".
                        if "::" in raw:
                            parent, _, leaf = raw.rpartition("::")
                        else:
                            parent, leaf = None, raw
                        # v0.31: span resolution. LSP wins when it knows
                        # this exact path (full range, resolved_by='lsp');
                        # otherwise the parser's line span (columns NULL,
                        # resolved_by='parser'); otherwise all-NULL --
                        # e.g. ancestor-only claims a leaves-only backend
                        # validated without emitting a matching span.
                        span: tuple[Any, ...] = (None, None, None, None, None)
                        lsp_hit = lsp_spans.get(raw)
                        if lsp_hit is not None:
                            span = (*lsp_hit, "lsp")
                        else:
                            parser_hit = parser_spans.get(raw)
                            if parser_hit is not None:
                                span = (
                                    parser_hit[0],
                                    None,
                                    parser_hit[1],
                                    None,
                                    "parser",
                                )
                        symbol_rows.append(
                            (
                                str(uuid4()),
                                cid,
                                item.pattern,
                                leaf,
                                "unknown",
                                parent,
                                *span,
                            )
                        )
            if owns:
                await conn.commit()
        if symbol_rows:
            await self.db.insert_claim_symbols(rows=symbol_rows)

    # ------------------------------------------------------------------
    # v0.31 wave 2: callsite enrichment + advisory CALLSITE_OVERLAP
    # ------------------------------------------------------------------

    def _schedule_callsite_enrichment(self, claim_id: str) -> None:
        """Fire-and-forget a callsite-enrichment task for one claim.

        The task is parked in ``self._enrichment_tasks`` (strong ref,
        discarded on completion) so the event loop cannot garbage
        collect it mid-flight and tests can await the set to make
        enrichment deterministic.
        """
        task = asyncio.create_task(self._enrich_claim_callsites(claim_id))
        self._enrichment_tasks.add(task)
        task.add_done_callback(self._enrichment_tasks.discard)

    async def _enrich_claim_callsites(self, claim_id: str) -> None:
        """Ask the language server who calls each claimed symbol and
        persist the answers (wholesale replace) into
        ``claim_symbol_callsites``.

        Advisory data with advisory guarantees: every failure -- LSP
        down, circuit open, garbage result, even a DB hiccup -- is
        swallowed at debug level and the claim simply has no (or stale)
        callsite rows. We only write when at least one ``references``
        call actually succeeded, so a total LSP outage cannot wipe
        callsites a healthier earlier run recorded. Stored rows are
        capped at :data:`CALLSITE_CAP` per claim.
        """
        try:
            root = self.settings.repo_root
            if not self.settings.lsp_enabled or not root:
                return
            symbol_rows = await self.db.get_claim_symbols(claim_id)
            pool = get_lsp_pool(self.settings)
            callsites: list[tuple[str, int, int | None, str | None]] = []
            any_success = False
            for row in symbol_rows:
                start_line = row.get("start_line")
                if start_line is None:
                    # No persisted span, no definition position to ask
                    # references at. Parser-miss / pre-v16 rows simply
                    # do not enrich.
                    continue
                file_path = str(row["file_path"])
                language = language_for_path(file_path)
                if language is None:
                    continue
                refs = await pool.references(
                    root,
                    language,
                    file_path,
                    int(start_line),
                    int(row.get("start_col") or 0),
                )
                if refs is None:
                    continue
                any_success = True
                symbol_path = format_symbol_path(
                    row.get("parent_symbol") or None,
                    str(row["symbol_name"]),
                )
                for ref in refs:
                    character = ref.get("character")
                    callsites.append(
                        (
                            str(ref["file_path"]),
                            int(ref["line"]),
                            int(character) if character is not None else None,
                            symbol_path,
                        )
                    )
            if not any_success:
                return
            if len(callsites) > CALLSITE_CAP:
                logger.info(
                    "callsite enrichment: claim %s produced %d callsites; "
                    "truncating to %d",
                    claim_id,
                    len(callsites),
                    CALLSITE_CAP,
                )
                callsites = callsites[:CALLSITE_CAP]
            await self.db.insert_claim_callsites(claim_id, callsites)
        except Exception:  # noqa: BLE001 - enrichment is best-effort
            logger.debug(
                "callsite enrichment for claim %s failed; skipped",
                claim_id,
                exc_info=True,
            )

    async def _callsite_advisories(
        self,
        *,
        body: CreateClaimsRequest,
        created: list[str],
        item_for_cid: dict[str, ClaimItem],
    ) -> list[str]:
        """Compute advisory CALLSITE_OVERLAP warnings for a batch that
        was just granted.

        For each created claim, the probe range is the whole file for
        file-scope items and the persisted symbol spans for
        symbol-scope items (span-less symbol rows contribute nothing --
        without a range there is no "inside"). Holders are filtered by
        the same adversary rules as the conflict pipeline (different
        engineer, different session, same repo bucket) inside
        :func:`group_callsite_overlaps`.

        Known limitation: the whole-file probe queries
        ``callsites_intersecting`` with the claim pattern verbatim, and
        stored callsite rows carry concrete file paths -- so a
        file-scope claim whose pattern is a glob (``src/auth/*.ts``,
        ``docs/**``) never matches a stored row and gets no CALLSITE
        advisory. Expanding globs would cost a ``git ls-files`` walk per
        claim on the grant hot path for a purely advisory feature, so
        the gap is accepted by design: callsite advisories apply to
        literal-path claims only.

        Each finding also lands as a ``callsite-advisory``
        ``request_events`` audit row -- the same mechanism the
        auto-coexist / auto-narrow resolutions use -- so the dashboard's
        event stream sees it. Failures are swallowed: an advisory must
        never break a grant that already happened.
        """
        advisories: list[str] = []
        created_set = set(created)
        try:
            for cid in created:
                item = item_for_cid.get(cid)
                if item is None:
                    continue
                ranges: list[tuple[str, int, int]] = []
                if item.symbols:
                    for row in await self.db.get_claim_symbols(cid):
                        if (
                            row.get("start_line") is None
                            or row.get("end_line") is None
                        ):
                            continue
                        ranges.append(
                            (
                                str(row["file_path"]),
                                int(row["start_line"]),
                                int(row["end_line"]),
                            )
                        )
                else:
                    # Whole-file scope: every recorded line counts.
                    ranges.append((item.pattern, 1, 1_000_000_000))
                hit_rows: list[dict[str, Any]] = []
                for file_path, lo, hi in ranges:
                    hit_rows.extend(
                        await self.db.callsites_intersecting(file_path, lo, hi)
                    )
                for overlap in group_callsite_overlaps(
                    hit_rows,
                    requester_engineer=body.engineer,
                    requester_session_id=body.session_id,
                    requester_repo=body.repo,
                    exclude_claim_ids=created_set,
                ):
                    shown = ", ".join(str(n) for n in overlap.lines[:8])
                    if len(overlap.lines) > 8:
                        shown += f", +{len(overlap.lines) - 8} more"
                    advisories.append(
                        f"advisory: {overlap.holder_engineer} claim "
                        f"{overlap.holder_claim_id} on "
                        f"{overlap.holder_pattern!r} has "
                        f"{len(overlap.lines)} recorded callsite(s) inside "
                        f"{item.pattern!r} (lines {shown}); coordinate or "
                        "expect semantic conflicts"
                    )
                    await self.db.record_request_event(
                        "callsite-advisory",
                        request_id=None,
                        actor_engineer=None,
                        actor_session_id=None,
                        detail={
                            "holder_claim_id": overlap.holder_claim_id,
                            "requester_claim_id": cid,
                            "file": overlap.file_path,
                            "lines": list(overlap.lines),
                        },
                    )
        except Exception:  # noqa: BLE001 - advisory must not break a grant
            logger.debug(
                "callsite advisory pass failed; grant unaffected",
                exc_info=True,
            )
        return advisories

    # ------------------------------------------------------------------
    # v0.31 wave 2: rename auto-follow sweep
    # ------------------------------------------------------------------

    async def rename_sweep(self, *, max_claims: int = 20) -> int:
        """Conservative rename auto-follow: detect claimed symbols that
        were renamed on disk and update the claim rows to track them.

        Per pass (the cleanup loop calls this on its cadence): up to
        ``max_claims`` ACTIVE symbol-scope claims are inspected. For
        each claimed symbol with a persisted span whose file still
        exists under the repo root, extraction re-runs (parser always,
        LSP refinement when the pool answers). Then:

        - symbol still present (parser or LSP view): nothing to do.
        - symbol vanished: candidate replacements are CURRENT symbols
          with the same parent (and the same kind when the stored kind
          is meaningful -- claim-time rows store ``'unknown'`` today,
          which matches anything) whose span overlaps the STORED span
          with a +/- 5 line tolerance. EXACTLY ONE candidate means we
          follow the rename: :meth:`Database.update_claim_symbol_rename`
          atomically rewrites the claim_symbols row, appends the audit
          row, and (never today -- see below) would update
          ``claims.pattern``. Zero or 2+ candidates means ambiguity,
          and ambiguity means hands off (debug log only). The follow
          is also skipped (debug log) when another active claim
          already holds the new symbol path on the same file -- the
          rewrite bypasses the conflict pipeline, so applying it would
          silently create the same-symbol overlap that claim-time
          enforcement rejects.

        ``new_pattern`` is always ``None``: ``claims.pattern`` holds
        the FILE pattern for symbol claims (the symbol path lives only
        in ``claim_symbols`` rows -- see ``_finalise_v14_scope``), so a
        symbol rename never changes the pattern. The plumbing exists in
        the DB helper for any future pattern scheme that embeds symbol
        paths.

        Emits one ``symbol_renamed`` webhook per applied rename.
        Returns the number of renames applied.
        """
        if not self.settings.lsp_enabled:
            return 0
        root = self.settings.repo_root
        if not root or not root.is_dir():
            return 0
        active = await self.db.list_active_claims_rows(exclude_engineer=None)
        symbol_claims = [
            r for r in active if r.get("scope_type") == "symbol"
        ][:max_claims]
        applied = 0
        for claim in symbol_claims:
            claim_id = str(claim["id"])
            rows = await self.db.get_claim_symbols(claim_id)
            by_file: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                if row.get("start_line") is None or row.get("end_line") is None:
                    continue
                by_file.setdefault(str(row["file_path"]), []).append(row)
            for file_path, file_rows in by_file.items():
                resolved = (root / file_path).resolve()
                if relpath_under_root(resolved, root) is None:
                    continue
                if not resolved.is_file():
                    # Deleted or moved file: a rename we cannot follow.
                    continue
                try:
                    content = resolved.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
                parser_syms = extract_symbols(str(resolved), content)
                if not parser_syms:
                    # Unsupported extension or unparseable file: no
                    # ground truth, no action.
                    continue
                current_paths: set[str] = set()
                for sym in parser_syms:
                    current_paths.add(
                        format_symbol_path(sym.parent, sym.name)
                    )
                    parent = sym.parent
                    while parent:
                        current_paths.add(parent)
                        parent = (
                            parent.rsplit("::", 1)[0]
                            if "::" in parent
                            else None
                        )
                flattened: list[dict[str, Any]] | None = None
                language = language_for_path(file_path)
                if language is not None:
                    pool = get_lsp_pool(self.settings)
                    flattened = await pool.document_symbols(
                        root, language, resolved
                    )
                lsp_spans: dict[str, tuple[int, int, int, int]] = {}
                if flattened is not None:
                    current_paths |= _lsp_symbol_path_set(flattened)
                    lsp_spans = _lsp_span_map(flattened)
                for row in file_rows:
                    old_leaf = str(row["symbol_name"])
                    parent_str = row.get("parent_symbol") or None
                    old_path = format_symbol_path(parent_str, old_leaf)
                    if old_path in current_paths:
                        continue
                    stored_start = int(row["start_line"])
                    stored_end = int(row["end_line"])
                    stored_kind = str(row.get("symbol_kind") or "unknown")
                    candidates = [
                        sym
                        for sym in parser_syms
                        if (sym.parent or None) == parent_str
                        and (
                            stored_kind in ("", "unknown")
                            or sym.kind == stored_kind
                        )
                        and sym.start_line <= stored_end + 5
                        and sym.end_line >= stored_start - 5
                    ]
                    if len(candidates) != 1:
                        logger.debug(
                            "rename sweep: claim %s symbol %r in %s "
                            "vanished with %d candidates; leaving "
                            "untouched",
                            claim_id,
                            old_path,
                            file_path,
                            len(candidates),
                        )
                        continue
                    cand = candidates[0]
                    new_path = format_symbol_path(cand.parent, cand.name)
                    # The conflict pipeline never sees this rewrite (it
                    # is a direct DB update, not a claim grant), so
                    # guard against silently creating a second active
                    # claim on the new path: if ANY other active
                    # symbol-scope claim in the same repo bucket
                    # already holds the new path on this file,
                    # following the rename would manufacture exactly
                    # the overlap that claim-time enforcement would
                    # have 409'd. Ambiguity rule applies: doubt means
                    # no action.
                    #
                    # This read is a cheap early skip only -- it runs on
                    # its own connection and every await between here
                    # and the write is a yield point a concurrent grant
                    # can land in. The AUTHORITATIVE collision check is
                    # re-run inside update_claim_symbol_rename's own
                    # BEGIN IMMEDIATE transaction (guard flag below), so
                    # the check and the rewrite are one atomic unit.
                    existing_rows = await self.db.get_symbol_rows_on_file(
                        file_path=file_path,
                        repo=claim.get("repo"),
                    )
                    collision = any(
                        str(r.get("id")) != claim_id
                        and format_symbol_path(
                            r.get("overlapping_parent_symbol") or None,
                            str(r.get("overlapping_symbol")),
                        )
                        == new_path
                        for r in existing_rows
                    )
                    if collision:
                        logger.debug(
                            "rename sweep: claim %s symbol %r in %s "
                            "would follow to %r, but another active "
                            "claim already holds that symbol; leaving "
                            "untouched",
                            claim_id,
                            old_path,
                            file_path,
                            new_path,
                        )
                        continue
                    lsp_hit = lsp_spans.get(new_path)
                    if lsp_hit is not None:
                        new_span: tuple[
                            int, int | None, int, int | None
                        ] = lsp_hit
                        resolved_by = "lsp"
                    else:
                        new_span = (
                            cand.start_line,
                            None,
                            cand.end_line,
                            None,
                        )
                        resolved_by = "parser"
                    updated = await self.db.update_claim_symbol_rename(
                        claim_id,
                        file_path=file_path,
                        old_symbol_name=old_leaf,
                        new_symbol_name=cand.name,
                        new_start_line=new_span[0],
                        new_start_col=new_span[1],
                        new_end_line=new_span[2],
                        new_end_col=new_span[3],
                        resolved_by=resolved_by,
                        new_pattern=None,
                        guard_new_path_collision=True,
                        repo=claim.get("repo"),
                    )
                    if not updated:
                        continue
                    applied += 1
                    logger.info(
                        "rename sweep: claim %s followed %r -> %r in %s "
                        "(%s)",
                        claim_id,
                        old_path,
                        new_path,
                        file_path,
                        resolved_by,
                    )
                    await self.fire_webhook(
                        "symbol_renamed",
                        {
                            "claim_id": claim_id,
                            "file": file_path,
                            "old": old_path,
                            "new": new_path,
                            "engineer": claim.get("engineer"),
                        },
                    )
        return applied

    # ------------------------------------------------------------------
    # v0.31 wave 2: refactor claims (symbol + every callsite, one shot)
    # ------------------------------------------------------------------

    async def create_refactor_claims(
        self,
        body: ClaimRefactorRequest,
        *,
        auto_promote_allowed: bool = True,
    ) -> CreateClaimsResponse:
        """Expand a (file, symbol) refactor intent into a normal
        ``create_claims`` batch covering the definition and every
        reference the language server can see.

        Hard LSP requirement: refactor claims exist to reserve
        callsites, and only ``textDocument/references`` knows where
        those are. The DEFINITION span tolerates a parser fallback
        (the parser can find a declaration the server formats oddly),
        but a failed documentSymbol or references call raises
        :class:`LspUnavailable` -> HTTP 503. No silent degradation
        into a single-file claim that pretends to cover a refactor.

        Expansion rules:

        - every reference whose tightest enclosing symbol resolves gets
          a symbol claim on that path in its file;
        - references at module level (or in files the server cannot
          describe) get a whole-file claim on their file;
        - the definition symbol claim is ALWAYS included, unless its
          file is already covered by a whole-file claim from a
          module-level reference in the same file;
        - patterns dedupe; references resolving outside the repo root
          are skipped (claims are repo-relative patterns and cannot
          address foreign paths).

        The generated batch is capped at ``settings.max_claim_files``
        (the existing per-pattern guardrail doubles as the natural
        batch ceiling here). Conflicts, queueing (``wait_seconds``),
        and rate limits all apply unchanged because the batch goes
        through the normal :meth:`create_claims` pipeline.
        """
        root = self.settings.repo_root
        if not self.settings.lsp_enabled or not root or not root.is_dir():
            raise LspUnavailable(
                "refactor claims require COORD_LSP_ENABLED=true and a "
                "configured COORD_REPO_ROOT"
            )
        language = language_for_path(body.file)
        if language is None:
            raise LspUnavailable(
                f"no language server is registered for {body.file!r} "
                "(supported: .py, .ts/.tsx/.js/.jsx, .go)"
            )
        pool = get_lsp_pool(self.settings)
        flattened = await pool.document_symbols(root, language, body.file)
        if flattened is None:
            raise LspUnavailable(
                f"the language server could not answer documentSymbol for "
                f"{body.file!r} (server missing, circuit open, or timeout)"
            )

        # Definition span: LSP first, parser fallback for the
        # DEFINITION only. References below have no fallback.
        def_spans = _lsp_span_map(flattened)
        def_hit = def_spans.get(body.symbol)
        def_line: int | None = None
        def_col = 0
        if def_hit is not None:
            def_line, def_col = def_hit[0], def_hit[1]
        else:
            resolved = (root / body.file).resolve()
            if resolved.is_file():
                try:
                    content = resolved.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    content = ""
                for sym in extract_symbols(str(resolved), content):
                    if format_symbol_path(sym.parent, sym.name) == body.symbol:
                        def_line, def_col = sym.start_line, 0
                        break
        if def_line is None:
            return CreateClaimsResponse(
                claim_ids=[],
                conflicts=[],
                warnings=[
                    f"Unknown symbol {body.symbol!r} in {body.file!r}: "
                    "neither the language server nor the parser can find "
                    "its definition"
                ],
                options=["narrow_claim"],
            )

        refs = await pool.references(
            root, language, body.file, def_line, def_col
        )
        if refs is None:
            raise LspUnavailable(
                f"the language server could not answer references for "
                f"{body.symbol!r} in {body.file!r}"
            )

        # Per-reference enclosing-symbol resolution, one documentSymbol
        # call per distinct reference file (cached).
        doc_symbols_by_file: dict[str, list[dict[str, Any]] | None] = {
            body.file: flattened
        }
        file_scope: set[str] = set()
        symbol_scope: dict[str, set[str]] = {body.file: {body.symbol}}
        for ref in refs:
            ref_file = str(ref["file_path"])
            if Path(ref_file).is_absolute():
                # Outside the repo root: claims are repo-relative
                # patterns and cannot reserve foreign paths.
                logger.debug(
                    "refactor claims: skipping out-of-repo reference %s",
                    ref_file,
                )
                continue
            if ref_file not in doc_symbols_by_file:
                ref_language = language_for_path(ref_file)
                doc_symbols_by_file[ref_file] = (
                    await pool.document_symbols(root, ref_language, ref_file)
                    if ref_language is not None
                    else None
                )
            enclosing = _tightest_enclosing_symbol(
                doc_symbols_by_file[ref_file],
                int(ref["line"]),
                int(ref.get("character") or 0),
            )
            if enclosing is None:
                file_scope.add(ref_file)
            else:
                symbol_scope.setdefault(ref_file, set()).add(enclosing)
        # A whole-file claim swallows any symbol claims on the same
        # file (including the definition claim when a module-level
        # reference lives next to the definition).
        for f in file_scope:
            symbol_scope.pop(f, None)

        claims: list[ClaimItem] = [
            ClaimItem(type="file", pattern=f) for f in sorted(file_scope)
        ] + [
            ClaimItem(type="file", pattern=f, symbols=sorted(syms))
            for f, syms in sorted(symbol_scope.items())
        ]
        cap = self.settings.max_claim_files
        if len(claims) > cap:
            return CreateClaimsResponse(
                claim_ids=[],
                conflicts=[],
                warnings=[
                    f"refactor on {body.symbol!r} would generate "
                    f"{len(claims)} claims; max is {cap} "
                    "(COORD_MAX_CLAIM_FILES). Narrow the refactor or "
                    "raise the limit."
                ],
                options=["narrow_claim", "escalate"],
            )

        if body.description:
            description = body.description
        elif body.new_name:
            description = f"refactor: rename {body.symbol} -> {body.new_name}"
        else:
            description = f"refactor: {body.symbol}"

        result = await self.create_claims(
            CreateClaimsRequest(
                engineer=body.engineer,
                branch=body.branch,
                description=description,
                claims=claims,
                ttl_hours=body.ttl_hours,
                repo=body.repo,
                session_id=body.session_id,
                wait_seconds=body.wait_seconds,
                urgency=body.urgency,
            ),
            # Auto-promote writes global ownership YAML, so a repo-scoped
            # caller must not trigger it via the refactor path either
            # (v0.42; mirrors the POST /claims gate).
            auto_promote_allowed=auto_promote_allowed,
        )
        # Partial-coverage guard. The v0.21 queue enqueues only the
        # single conflicted item, so a wait_seconds grant that arrives
        # via the drain covers ONE pattern of this machine-generated
        # batch -- and unlike a hand-built batch, the caller never saw
        # the expanded pattern list, so a silently partial reservation
        # would read as a fully reserved refactor. Decorate the grant
        # rather than redesigning queue semantics: name what was NOT
        # reserved so the agent can claim the remainder explicitly.
        if result.claim_ids and len(result.claim_ids) < len(claims):
            granted_rows = await self.db.list_active_claims_rows()
            granted_patterns = {
                str(r["pattern"])
                for r in granted_rows
                if str(r["id"]) in set(result.claim_ids)
            }
            dropped = [
                c.pattern for c in claims if c.pattern not in granted_patterns
            ]
            if dropped:
                result.warnings.append(
                    f"refactor reservation is PARTIAL: the queue grant "
                    f"covered {len(result.claim_ids)} of {len(claims)} "
                    f"generated claims. Not reserved: {sorted(dropped)}. "
                    "Claim these before editing them, or re-run "
                    "claim_refactor."
                )
        return result

    async def list_claims(
        self,
        *,
        active_only: bool = True,
        engineer: str | None = None,
        module_substring: str | None = None,
        session_id: str | None = None,
        repo: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.expire_stale_claims()
        # Activity ping: an agent reading the claim list is still alive,
        # so keep its claims warm. No-op when session_id is unset
        # (legacy / non-MCP callers). ``repo`` scopes the warm-up so a
        # repo-scoped reader cannot refresh another repo's claims via a
        # shared session id (v0.42).
        if session_id:
            await self._maybe_touch(session_id, repo)
        if active_only:
            rows = await self.db.list_active_claims_rows(exclude_engineer=None)
        else:
            rows = await self.db.list_recent_claims(200)
        if engineer:
            rows = [r for r in rows if r["engineer"] == engineer]
        if module_substring:
            m = module_substring.lower()
            rows = [r for r in rows if m in (r.get("pattern") or "").lower()]
        return rows

    async def pending_requests(
        self, session_id: str, *, repo: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the inbox the holder polls. Merges two streams:

        - First-class release ``requests`` (decision='pending', kind='request').
          The holder is being explicitly asked to release; their next
          response moves the state machine. First-time-seen-by-this-session
          fires a ``notified`` audit event so we have evidence the holder
          saw it.
        - Auto-conflict-log entries (kind='auto-conflict'). Recorded every
          time someone's ``claim_files`` got 409'd against one of this
          session's claims. Read-only; informational.

        Each row in the returned list carries a ``kind`` discriminator so
        the agent / dashboard can render them appropriately.

        v0.42: ``repo`` scopes the inbox. When set (a repo-scoped
        token), only requests / conflicts against the caller's own repo
        are returned -- and, critically, only in-scope requests fire a
        ``notified`` audit event, so a session id spanning repos never
        leaks (or records evidence of seeing) another repo's rows.
        """
        if not session_id:
            return []
        # First-class requests get the audit-event treatment so the
        # operator can prove "the holder did/didn't see this". The repo
        # filter is applied in the query so out-of-scope requests are
        # neither returned nor notified.
        open_requests = await self.db.list_open_requests_for_session(
            session_id, repo=repo
        )
        for r in open_requests:
            await self.db.record_request_notify(
                r["id"],
                holder_engineer=r.get("holder_engineer"),
                holder_session_id=session_id,
            )
        request_rows = [{"kind": "request", **r} for r in open_requests]

        # Auto-conflict entries (the v0.6 pre-existing inbox).
        conflicts = await self.db.pending_requests_for_session(
            session_id, repo=repo
        )
        conflict_rows = [{"kind": "auto-conflict", **c} for c in conflicts]
        return request_rows + conflict_rows

    # --- v0.9.0 release-request flow ----------------------------------

    async def file_request(
        self,
        *,
        claim_id: str,
        requester: str,
        requester_session_id: str | None,
        reason: str | None,
        urgency: str,
        requested_scope: str | None = None,
    ) -> dict[str, Any]:
        """Create a release request and shorten the holder's claim TTL.

        Pure DB orchestration: the long-poll wait is the caller's
        responsibility (so the API handler can use FastAPI's async
        primitives directly without entangling them with this layer).
        Returns the request row.

        ``requested_scope`` (v0.11+) is what the requester actually
        needs, often a sub-pattern of the holder's claim pattern. It is
        recorded on the request row and the ``filed`` audit event so
        the holder (and the dashboard) can decide whether to narrow,
        coexist, approve, or deny.

        Raises:
        - ``KeyError`` if the claim does not exist.
        - ``ValueError`` if the claim is already released or expired.
        """
        # Lookup current claim state.
        rows = await self.db.list_active_claims_rows()
        claim = next((c for c in rows if str(c["id"]) == claim_id), None)
        if claim is None:
            # Active list excludes released and TTL-past rows. Disambiguate
            # missing-vs-already-released for a useful error.
            recent = await self.db.list_recent_claims(500)
            for c in recent:
                if str(c["id"]) == claim_id:
                    raise ValueError(
                        f"claim {claim_id} is no longer active "
                        f"(released_at={c.get('released_at')}, "
                        f"expires_at={c.get('expires_at')}); "
                        "no need to file a request, retry your claim"
                    )
            raise KeyError(f"unknown claim_id {claim_id!r}")

        original_expires_at = str(claim["expires_at"])
        now = datetime.now(UTC).replace(microsecond=0)
        shortened = (
            now + timedelta(seconds=self.settings.request_ttl_short_sec)
        ).isoformat().replace("+00:00", "Z")

        # Never extend: if the existing expires_at is sooner than our
        # shortened deadline, leave the claim alone.
        try:
            current_exp = datetime.fromisoformat(
                original_expires_at.replace("Z", "+00:00")
            )
        except ValueError:
            current_exp = None
        new_exp = (
            shortened
            if current_exp is not None
            and current_exp > datetime.fromisoformat(
                shortened.replace("Z", "+00:00")
            )
            else original_expires_at
        )

        return await self.db.create_request(
            request_id=str(uuid4()),
            claim_id=claim_id,
            requester_engineer=requester,
            requester_session_id=requester_session_id,
            requested_pattern=str(claim["pattern"]),
            reason=reason,
            urgency=urgency,
            original_expires_at=original_expires_at,
            shortened_expires_at=shortened,
            new_claim_expires_at=new_exp,
            requested_scope=requested_scope,
        )

    async def respond_to_request(
        self,
        *,
        request_id: str,
        decision: str,
        actor_engineer: str | None,
        actor_session_id: str | None,
        note: str | None = None,
        narrowed_pattern: str | None = None,
        coexist_pattern: str | None = None,
        coexist_symbols: dict[str, list[str]] | None = None,
    ) -> dict[str, Any] | None:
        """Forward to the DB layer with v0.11 decision verbs.

        For ``decision='narrowed'`` the service enforces that
        ``narrowed_pattern`` is a (non-strict) subset of the holder's
        current claim pattern. A disjoint or broader pattern is a
        contract violation that the API handler maps to 400. File-scope
        coexist (``coexist_pattern``) deliberately skips the subset
        check because coexisting claims are intentionally on the same
        scope (or compatible scopes the holder explicitly agreed to).

        v0.35 symbol-scoped coexist (``coexist_symbols``) is the trust
        boundary for the new path: the holder's decision is where the
        grant is validated. When ``coexist_symbols`` is supplied the
        service enforces, raising ``ValueError`` (-> 400) on any
        violation, that:

        - both the holder's claim AND the requester's original
          (symbol-scoped) claim exist and are ``scope_type='symbol'``;
        - every granted symbol path is covered by the requester's own
          claimed symbols (you can only grant what the requester asked
          for);
        - every granted symbol is disjoint from the holder's claimed
          symbols on the same file under
          :func:`symbol_paths_overlap` -- a coexist that hid a real
          symbol collision must be refused.

        The validated grant is forwarded to the DB layer, which creates
        the requester's sibling claim ``scope_type='symbol'`` with the
        granted symbols.
        """
        if decision == "narrowed":
            if not narrowed_pattern:
                # The DB layer would reject this too, but raising here
                # gives the API handler a clearer error message before
                # any DB roundtrip.
                raise ValueError(
                    "decision='narrowed' requires a non-empty 'narrowed_pattern'"
                )
            request_row = await self.db.get_request(request_id)
            if request_row is None:
                # Surface the not-found through the DB's normal return
                # path (None) so the handler maps it to 404.
                return None
            claim_id = str(request_row["claim_id"])
            active_rows = await self.db.list_active_claims_rows(exclude_engineer=None)
            holder_claim = next(
                (c for c in active_rows if str(c.get("id")) == claim_id),
                None,
            )
            if holder_claim is None:
                raise ValueError(
                    f"narrowed: holder claim {claim_id!r} is no longer active; "
                    "the request can no longer be narrowed"
                )
            original_pattern = str(holder_claim["pattern"])
            if not _is_subset_pattern(narrowed_pattern, original_pattern):
                raise ValueError(
                    f"narrowed_pattern {narrowed_pattern!r} is not a subset of "
                    f"the holder's current pattern {original_pattern!r}; "
                    "narrowing must reduce scope, not move it"
                )
        if decision == "coexist" and coexist_symbols:
            await self._validate_symbol_coexist(
                request_id=request_id, coexist_symbols=coexist_symbols
            )
        # Floor the new claim's TTL at the default working window so that a
        # narrowed or coexist claim created in response to a request does not
        # inherit the shortened deadline that request_release imposed on the
        # holder's original claim.
        min_expires_at = _expires_at(self.settings.default_ttl_hours)
        row = await self.db.respond_to_request(
            request_id=request_id,
            decision=decision,
            actor_engineer=actor_engineer,
            actor_session_id=actor_session_id,
            note=note,
            narrowed_pattern=narrowed_pattern,
            coexist_pattern=coexist_pattern,
            coexist_symbols=coexist_symbols,
            min_expires_at=min_expires_at,
        )
        if row is None:
            return None
        # Drain the FIFO queue for every claim this decision actually
        # closed ('approved' releases the holder's claim; 'narrowed'
        # releases the original before opening the tighter one).
        # Without this, waiters queued behind that claim would burn
        # their whole wait_seconds against scope that is already free.
        # The key is private DB->service transport: popped here (with a
        # default, since the responded-late path never releases) so it
        # never reaches the API response.
        for cid in row.pop("_released_claim_ids", []):
            await self._drain_queue_for(cid)
        return row

    async def _validate_symbol_coexist(
        self,
        *,
        request_id: str,
        coexist_symbols: dict[str, list[str]],
    ) -> None:
        """Respond-time validation for v0.35 symbol-scoped coexist.

        Raises ``ValueError`` (which the API handler maps to 400) when
        the grant is not safe. The holder's decision is the trust
        boundary -- mirroring the ``narrowed_pattern`` subset check --
        so all enforcement happens here before the DB writes anything.

        Returns ``None`` when the grant is valid (or when the request
        row is missing, in which case the DB call surfaces the 404 via
        its own ``None`` return).
        """
        request_row = await self.db.get_request(request_id)
        if request_row is None:
            # Let the DB layer's None-return drive the 404; nothing to
            # validate against a request that doesn't exist.
            return
        claim_id = str(request_row["claim_id"])
        active_rows = await self.db.list_active_claims_rows(exclude_engineer=None)
        by_id = {str(c.get("id")): c for c in active_rows}
        holder_claim = by_id.get(claim_id)
        if holder_claim is None:
            raise ValueError(
                f"coexist: holder claim {claim_id!r} is no longer active; "
                "the request can no longer be granted"
            )
        if str(holder_claim.get("scope_type") or "file") != "symbol":
            raise ValueError(
                "coexist_symbols requires the holder's claim to be "
                "symbol-scoped (scope_type='symbol'); use coexist_pattern "
                "for a file-scope grant"
            )

        # Locate the requester's original claim(s): the requester's
        # active symbol-scoped claims in the same repo as the holder.
        # The granted symbols must be a subset of what the requester
        # actually claimed, so we union the requester's claim_symbols
        # per file.
        requester_engineer = str(request_row["requester_engineer"])
        holder_repo = holder_claim.get("repo")
        requester_claims = [
            c
            for c in active_rows
            if c.get("engineer") == requester_engineer
            and str(c.get("scope_type") or "file") == "symbol"
            and c.get("repo") == holder_repo
        ]
        if not requester_claims:
            raise ValueError(
                "coexist_symbols requires the requester to hold an active "
                "symbol-scoped claim; none found for engineer "
                f"{requester_engineer!r}"
            )
        requester_by_file: dict[str, list[SymbolPath]] = {}
        for c in requester_claims:
            rows = await self.db.get_claim_symbols(str(c.get("id")))
            for row in rows:
                f = str(row["file_path"])
                parent = row.get("parent_symbol")
                parent_str = str(parent) if parent else None
                requester_by_file.setdefault(f, []).append(
                    (parent_str, str(row["symbol_name"]))
                )

        # Holder's claimed symbols, grouped by file, for the disjoint
        # check.
        holder_rows = await self.db.get_claim_symbols(claim_id)
        holder_by_file: dict[str, list[SymbolPath]] = {}
        for row in holder_rows:
            f = str(row["file_path"])
            parent = row.get("parent_symbol")
            parent_str = str(parent) if parent else None
            holder_by_file.setdefault(f, []).append(
                (parent_str, str(row["symbol_name"]))
            )

        for file_path, syms in coexist_symbols.items():
            # A symbol coexist can only be granted on a file the HOLDER
            # actually claims symbols on. Without this, a holder could mint
            # the requester a sibling on a file outside this request's
            # subject (e.g. an unrelated claim the requester holds), and the
            # disjoint check below would no-op because the holder has no
            # symbols on that file to compare against.
            if file_path not in holder_by_file:
                raise ValueError(
                    f"coexist_symbols: file {file_path!r} is not part of the "
                    "holder's symbol claim; a symbol coexist can only be "
                    "granted on a file the holder actually holds"
                )
            granted = [parse_symbol_path(str(s)) for s in syms]
            req_paths = requester_by_file.get(file_path, [])
            held_paths = holder_by_file.get(file_path, [])
            for g in granted:
                g_str = format_symbol_path(g[0], g[1])
                # Subset: the granted symbol must be covered by one of
                # the requester's own claimed symbols (exact match or a
                # claimed ancestor that contains it).
                covered = any(
                    format_symbol_path(r[0], r[1]) == g_str
                    or g_str.startswith(format_symbol_path(r[0], r[1]) + "::")
                    for r in req_paths
                )
                if not covered:
                    raise ValueError(
                        f"coexist_symbols grants {g_str!r} in {file_path!r}, "
                        "which is not within the requester's claimed symbols"
                    )
                # Disjoint: the granted symbol must not overlap any of
                # the holder's claimed symbols on this file.
                for h in held_paths:
                    if symbol_paths_overlap(g, h):
                        raise ValueError(
                            f"coexist_symbols grants {g_str!r} in "
                            f"{file_path!r}, which overlaps the holder's "
                            f"claimed symbol {format_symbol_path(h[0], h[1])!r}; "
                            "a coexist must not hide a real symbol conflict"
                        )

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        return await self.db.get_request(request_id)

    async def list_requests(
        self,
        *,
        requester_engineer: str | None = None,
        claim_id: str | None = None,
        decision: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return await self.db.list_requests(
            requester_engineer=requester_engineer,
            claim_id=claim_id,
            decision=decision,
            limit=limit,
        )

    async def list_request_events(
        self, request_id: str
    ) -> list[dict[str, Any]]:
        return await self.db.list_request_events(request_id)

    async def wait_for_decision(
        self,
        request_id: str,
        *,
        timeout_seconds: int,
        poll_interval_seconds: float = 1.0,
    ) -> dict[str, Any] | None:
        """Block until the request reaches a terminal state or
        ``timeout_seconds`` elapses, whichever comes first.

        Implemented as a tight DB poll rather than an in-process event
        because the responder may be on a different replica (or the
        same process but a different request handler) and we want a
        single mechanism that works regardless. Poll interval defaults
        to 1s; with the typical 5-minute request TTL the load is
        bounded at ~300 selects per waiting agent.

        Returns the request row when it transitions out of 'pending',
        or the most-recent row if the timeout fired (still 'pending').
        Returns None if the request_id never existed.
        """
        import asyncio

        deadline = asyncio.get_event_loop().time() + max(0, timeout_seconds)
        while True:
            row = await self.db.get_request(request_id)
            if row is None:
                return None
            if row.get("decision") and row["decision"] != "pending":
                return row
            if asyncio.get_event_loop().time() >= deadline:
                return row
            await asyncio.sleep(poll_interval_seconds)

    async def release_claims(self, claim_ids: list[str], engineer: str | None) -> int:
        released_ids = await self.db.release_claims(claim_ids, engineer)
        for _ in released_ids:
            metrics.claims_released_total.inc()
        # v0.21: drain the FIFO queue -- but only for the ids that were
        # actually released. Draining an id that did not close (wrong
        # engineer, already released, unknown) would pop every waiter
        # behind the still-active claim, re-conflict each of them
        # against the unchanged holder, and expel the whole queue with
        # immediate 409s despite the wait_seconds they asked for.
        for cid in released_ids:
            await self._drain_queue_for(cid)
        return len(released_ids)

    async def release_session(
        self, session_id: str, *, repo: str | None = None
    ) -> int:
        """End-of-session bulk release, routed through the service
        layer so every claim the session held drains its FIFO queue.
        ``POST /sessions/{id}/release`` is the protocol-recommended
        cleanup call, so waiters queued behind any of the session's
        claims must be granted here exactly like an explicit
        release_claims would. Returns the number of claims closed."""
        released_ids = await self.db.release_for_session(
            session_id, repo=repo
        )
        for cid in released_ids:
            await self._drain_queue_for(cid)
        return len(released_ids)

    async def expire_stale_claims(self) -> list[str]:
        """TTL/idle sweep plus FIFO-queue drain for every claim the
        sweep actually closed. request_release exists to shorten the
        holder's TTL; when that shortened TTL fires, the waiters queued
        behind the claim must be granted rather than burning their full
        wait_seconds against scope that is already free. Returns the
        expired claim ids."""
        expired_ids = await self.db.expire_stale_claims(
            self.settings.idle_timeout_sec
        )
        for cid in expired_ids:
            await self._drain_queue_for(cid)
        return expired_ids

    async def cancel_queue_request(
        self,
        queue_id: str,
        *,
        engineer: str | None = None,
    ) -> bool:
        """v0.26: cancel a waiting queue entry and wake its in-process
        long-poll. Returns True when the row was cancelled, False when
        the row is missing or already terminal.

        The waiter (if same-process) sees event.is_set() True with
        payload {granted_claim_id: None, cancelled: True}; the
        _enqueue_and_wait loop treats this as a non-grant and returns
        None so the caller surfaces the legacy 409 + the cancelled
        marker.
        """
        # Snapshot the row before cancellation so the v0.27 webhook
        # detail can carry the requester and pattern even after the
        # state flip. ``get_queue_entry`` returns None for a missing
        # row, which lines up with the False return below.
        pre_row = await self.db.get_queue_entry(queue_id)
        cancelled = await self.db.cancel_queue_entry(
            queue_id, requester_engineer=engineer
        )
        if cancelled:
            _notify_waiter(
                queue_id,
                {"granted_claim_id": None, "cancelled": True},
            )
            await self.fire_webhook(
                "queue_cancel",
                {
                    "queue_id": str(queue_id),
                    "requester_engineer": (
                        pre_row.get("requester_engineer")
                        if pre_row is not None
                        else None
                    ),
                    "pattern": (
                        pre_row.get("pattern")
                        if pre_row is not None
                        else None
                    ),
                },
            )
        return cancelled

    # ------------------------------------------------------------------
    # v0.21 FIFO queue
    # ------------------------------------------------------------------

    async def _enqueue_and_wait(
        self,
        *,
        body: CreateClaimsRequest,
        conflicts: list[ConflictEntry],
        wait_seconds: int,
    ) -> CreateClaimsResponse | None:
        """Enqueue the first conflicting requester item behind its
        blocking holder and long-poll for up to ``wait_seconds`` seconds.

        Returns a CreateClaimsResponse with the granted claim_ids when
        the release path drains the queue in our favour. Returns None
        when the timeout fires or the drain marks the entry expired so
        the caller falls through to the legacy 409 response shape.
        """

        # v0.30 enqueue-time admission control. Both checks run BEFORE
        # the row is written and never again: pop/drain works through
        # whatever is already in the queue regardless of how deep it
        # has since become, so a quota change or a burst of admissions
        # cannot strand entries that were legitimately accepted. The
        # drain path cannot trip these either -- rehydrated grant
        # requests carry wait_seconds=0 and never reach this method.
        # Checks and the enqueue write share _quota_lock so concurrent
        # requests cannot both observe under-quota counts and overshoot;
        # the lock is released before the wait loop below.
        first_conflict = conflicts[0]
        blocking_claim_id = first_conflict.conflicting_claim.id
        # Find the requester ClaimItem that produced this conflict by
        # matching the your_pattern field; falling back to the first
        # claim item if matching fails. Either way one item is enqueued.
        target_item = body.claims[0]
        for item in body.claims:
            if item.pattern == first_conflict.your_pattern:
                target_item = item
                break

        async with self._quota_lock:
            engineer_queue_limit = self.settings.max_queued_per_engineer
            if engineer_queue_limit > 0:
                queued_count = await self.db.count_queue_entries_for_engineer(
                    body.engineer, repo=body.repo
                )
                if queued_count + 1 > engineer_queue_limit:
                    raise RateLimitExceeded(
                        scope="queue",
                        detail=(
                            f"engineer {body.engineer!r} already has "
                            f"{queued_count} live queue entries; the limit is "
                            f"{engineer_queue_limit} "
                            "(COORD_MAX_QUEUED_PER_ENGINEER). Wait for an "
                            "existing entry to resolve or cancel one."
                        ),
                        retry_after_sec=60,
                    )
            repo_depth_limit = self.settings.max_queue_depth_per_repo
            if repo_depth_limit > 0:
                repo_depth = await self.db.queue_depth_for_repo(body.repo)
                if repo_depth + 1 > repo_depth_limit:
                    raise RateLimitExceeded(
                        scope="repo_queue",
                        detail=(
                            f"queue for this repo is at capacity "
                            f"({repo_depth} waiting); service degraded, retry "
                            "later or work without wait_seconds"
                        ),
                        retry_after_sec=60,
                    )

            entry = await self.db.enqueue_claim_request(
                blocking_claim_id=blocking_claim_id,
                requester_engineer=body.engineer,
                requester_session_id=body.session_id,
                requester_branch=body.branch,
                requester_description=body.description,
                repo=body.repo,
                claim_type=target_item.type,
                pattern=target_item.pattern,
                symbols=list(target_item.symbols) if target_item.symbols else None,
                narrowable=target_item.narrowable,
                ttl_hours=body.ttl_hours,
                wait_seconds=wait_seconds,
                priority=body.urgency or "normal",
            )

        # v0.24: hybrid wait loop. Short event-wait covers the
        # same-process fast path (release-drain in this Python process
        # fires ``_notify_waiter`` and we wake instantly); the DB poll
        # on each iteration covers the cross-process case where another
        # replica marks the queue row granted/expired without our
        # in-memory event ever firing.
        event, payload = _register_waiter(entry["id"])
        deadline = _time.monotonic() + wait_seconds
        granted_cid: str | None = None
        # v0.26: track whether the wait exited via a cancellation so we
        # don't clobber the 'cancelled' DB state with 'expired' after the
        # loop falls through.
        was_cancelled = False
        try:
            while True:
                time_remaining = deadline - _time.monotonic()
                if time_remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        event.wait(),
                        timeout=min(POLL_INTERVAL, time_remaining),
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                # Whether the event fired OR the short wait timed out,
                # check both the in-memory payload and the DB state for
                # a verdict.
                if payload.get("granted_claim_id"):
                    granted_cid = payload["granted_claim_id"]
                    break
                if event.is_set() and payload.get("cancelled"):
                    # v0.26: requester invoked DELETE /requests/{queue_id}
                    # while we were waiting. Bail out via the no-grant
                    # branch so the caller surfaces the legacy 409
                    # conflict-shape response.
                    was_cancelled = True
                    break
                if event.is_set():
                    # Same-process expiry: ``_notify_waiter`` was called
                    # with ``granted_claim_id=None`` (drain failed). The
                    # payload above is empty so fall through to the
                    # ``None`` return.
                    break
                # Cross-process check: did another replica mark this
                # entry granted or expired?
                row = await self.db.get_queue_entry(entry["id"])
                if row is None:
                    # Cascade-deleted; treat as expired.
                    break
                if row["state"] == "granted" and row.get("granted_claim_id"):
                    granted_cid = row["granted_claim_id"]
                    break
                if row["state"] == "cancelled":
                    was_cancelled = True
                    break
                if row["state"] == "expired":
                    break
        finally:
            _drop_waiter(entry["id"])
            # Terminal DB transition, INSIDE the finally so it runs on
            # every exit path -- most importantly CancelledError, which
            # the ASGI server raises into this handler when the
            # long-polling client disconnects. The old placement (after
            # the try block) was skipped on cancellation, leaking the
            # row in state='waiting' forever: it counted against the
            # per-engineer and per-repo queue caps and stayed poppable
            # by a future drain, minting a real claim for a requester
            # who was long gone. v0.26: skip when the requester
            # cancelled -- the row is already 'cancelled' and must not
            # be re-labelled 'expired'.
            if granted_cid is None and not was_cancelled:
                finalise = asyncio.ensure_future(
                    self._finalise_queue_wait(entry["id"])
                )
                try:
                    adopted = await asyncio.shield(finalise)
                except asyncio.CancelledError:
                    # A(nother) cancellation landed while finalising.
                    # The shielded task keeps running to completion in
                    # the background so the row still reaches a
                    # terminal state; consume its outcome so a failure
                    # doesn't warn unretrieved at GC time.
                    finalise.add_done_callback(_consume_finalise_result)
                    raise
                if adopted is not None:
                    granted_cid = adopted

        if granted_cid:
            grant_warnings: list[str] = []
            if len(body.claims) > 1:
                # The v0.21 queue enqueues only the single conflicted
                # item, so a wait_seconds grant covers ONE pattern of
                # this batch; every other item -- including ones that
                # never conflicted, since a batch with any conflict
                # inserts nothing -- was NOT claimed. Name them so the
                # caller does not edit files it never reserved.
                # Deliberately a warning rather than a re-attempt of
                # the remaining items: a response carrying claim_ids
                # plus non-empty conflicts reads as failure to every
                # existing client.
                uncovered = sorted(
                    {
                        item.pattern
                        for item in body.claims
                        if item is not target_item
                    }
                )
                grant_warnings.append(
                    f"queued grant is PARTIAL: it covers only "
                    f"{target_item.pattern!r}. Not reserved: "
                    f"{uncovered}. Claim these separately before "
                    "editing them."
                )
            return CreateClaimsResponse(
                claim_ids=[granted_cid],
                conflicts=[],
                warnings=grant_warnings,
                options=[],
            )
        return None

    async def _finalise_queue_wait(self, queue_id: str) -> str | None:
        """Drive a finished long-poll wait (timeout, client disconnect,
        or a verdictless wake) to a terminal DB state.

        Attempts the waiting/in_progress -> expired transition first.
        When that loses -- rowcount 0 because the row is already
        terminal -- the row is re-read: if an in-flight drain won the
        race and marked it granted, the granted claim id is returned so
        the caller adopts the grant instead of surfacing a 409 for a
        claim that now exists in the requester's name. Returns None
        when the row was expired here or was already terminal without
        a grant."""
        expired = await self.db.mark_queue_expired(queue_id)
        if expired:
            return None
        row = await self.db.get_queue_entry(queue_id)
        if (
            row is not None
            and row.get("state") == "granted"
            and row.get("granted_claim_id")
        ):
            return str(row["granted_claim_id"])
        return None

    async def _drain_queue_for(self, released_claim_id: str) -> None:
        """Pop FIFO queue entries waiting on ``released_claim_id`` and
        try to grant them by re-issuing the equivalent of a fresh
        ``create_claims`` for that requester. Loops while grants succeed
        -- a single release can satisfy multiple queued requesters if
        their scopes turn out to be symbol-disjoint after the new
        landscape is computed.

        Each grant attempt re-runs the conflict check against the
        post-release world. If the requester would still 409 (a
        different new holder slipped in, or the requester's scope
        overlaps something else), the queue entry is marked expired
        and its long-poll is notified with no granted_claim_id so the
        client surfaces the original 409.
        """

        while True:
            entry = await self.db.pop_next_waiting_queue_entry(
                released_claim_id,
                age_boost_seconds=self.settings.queue_age_boost_seconds,
                fairness_interval=self.settings.queue_fairness_interval,
                priority_decay_sec=self.settings.queue_priority_decay_sec,
            )
            if entry is None:
                return
            grant_body = self._queue_entry_to_create_request(entry)
            try:
                # A drain grant is an internal, server-initiated re-issue on
                # behalf of a queued waiter whose token scope is not available
                # here. Auto-promote mutates global ownership YAML and is an
                # operator-only action, so it must never fire from this path
                # (v0.42) -- otherwise a repo-scoped requester's grant could
                # rewrite deployment-wide config. Direct operator POST /claims
                # still promotes.
                resp = await self.create_claims(
                    grant_body, auto_promote_allowed=False
                )
            except RateLimitExceeded as exc:
                # v0.30: a queue grant must not blast through the
                # active-claim cap -- the waiter's engineer is at
                # capacity, and granting now would hand them more than
                # the operator allowed. The popped entry cannot stay
                # in_progress (it would wedge the queue) and cannot be
                # granted, so route it through the existing
                # queue-expiry machinery (claim_queue has no
                # reason column and v0.30 ships without a schema
                # change, so the reason lives in this log line) and
                # keep draining: the next waiter may belong to an
                # under-cap engineer.
                logger.info(
                    "FIFO drain: queue %s expired -- requester %s is "
                    "rate limited (scope=%s): %s",
                    entry["id"],
                    entry.get("requester_engineer"),
                    exc.scope,
                    exc.detail,
                )
                await self.db.mark_queue_expired(entry["id"])
                _notify_waiter(entry["id"], {"granted_claim_id": None})
                continue
            except Exception:  # noqa: BLE001 - the grant is best-effort
                logger.exception(
                    "FIFO drain: grant attempt for queue %s raised",
                    entry["id"],
                )
                await self.db.mark_queue_expired(entry["id"])
                _notify_waiter(entry["id"], {"granted_claim_id": None})
                continue
            if resp.claim_ids:
                granted_cid = resp.claim_ids[0]
                granted = await self.db.mark_queue_granted(
                    entry["id"], granted_cid
                )
                if not granted:
                    # The waiter is gone: its timeout, a cancel, or the
                    # queue reaper moved the row to a terminal state
                    # while the grant re-issue was in flight. Finalising
                    # anyway would mint a live claim for a requester who
                    # was told there is no grant, blocking everyone else
                    # until TTL/idle expiry. Release the claim(s) just
                    # created -- through the service path, so any waiter
                    # already queued behind the short-lived claim drains
                    # too -- and keep draining: the freed scope may
                    # satisfy the next waiter.
                    logger.info(
                        "FIFO drain: queue %s lost the grant race (row "
                        "already terminal); releasing orphan claim %s",
                        entry["id"],
                        granted_cid,
                    )
                    await self.release_claims(
                        list(resp.claim_ids),
                        entry.get("requester_engineer"),
                    )
                    continue
                _notify_waiter(
                    entry["id"], {"granted_claim_id": granted_cid}
                )
                # v0.27: emit a ``queue_grant`` webhook so subscribers
                # know a queued requester was auto-promoted into a real
                # claim. Detail mirrors the queue-row fields the
                # dashboard already surfaces.
                await self.fire_webhook(
                    "queue_grant",
                    {
                        "queue_id": str(entry["id"]),
                        "granted_claim_id": granted_cid,
                        "requester_engineer": entry.get(
                            "requester_engineer"
                        ),
                        "pattern": entry.get("pattern"),
                    },
                )
            else:
                await self.db.mark_queue_expired(entry["id"])
                _notify_waiter(entry["id"], {"granted_claim_id": None})

    @staticmethod
    def _queue_entry_to_create_request(
        entry: dict[str, Any],
    ) -> CreateClaimsRequest:
        """Hydrate a CreateClaimsRequest from a queued claim_queue row.

        The grant attempt re-uses the full create_claims pipeline so
        validation, scope checks, severity inference, symbol writes and
        auto-resolution audit all stay consistent with a normal POST.
        wait_seconds is deliberately set to 0 on the rehydrated request
        so a drain-time re-conflict doesn't enqueue itself recursively.
        """

        symbols_field: list[str] | None = None
        raw_symbols = entry.get("symbols")
        if raw_symbols:
            try:
                parsed = json.loads(raw_symbols)
                if isinstance(parsed, list):
                    symbols_field = [str(s) for s in parsed]
            except (TypeError, ValueError):
                symbols_field = None
        narrowable_field: bool | None
        nv = entry.get("narrowable")
        if nv is None:
            narrowable_field = None
        else:
            narrowable_field = bool(int(nv))
        item = ClaimItem(
            type=str(entry["claim_type"]),
            pattern=str(entry["pattern"]),
            symbols=symbols_field,
            narrowable=narrowable_field,
        )
        return CreateClaimsRequest(
            engineer=str(entry["requester_engineer"]),
            branch=entry.get("requester_branch"),
            description=entry.get("requester_description"),
            claims=[item],
            ttl_hours=entry.get("ttl_hours"),
            repo=entry.get("repo"),
            session_id=entry.get("requester_session_id"),
            wait_seconds=0,
        )

    async def extend_claim(self, claim_id: str, body_engineer: str, ttl_hours: int) -> bool:
        new_exp = _expires_at(ttl_hours)
        return await self.db.extend_claim(claim_id, body_engineer, new_exp)

    async def fire_webhook(
        self,
        event_type: str,
        detail: dict[str, Any],
        kind: str = "webhook",
    ) -> str | None:
        """v0.27: emit an event into the webhook delivery outbox.

        No-op when COORD_WEBHOOK_URL is unset. Filters against
        COORD_WEBHOOK_EVENTS when that is non-empty (comma-separated
        event-type allowlist). Computes HMAC-SHA256 over the JSON
        payload at emit time using COORD_WEBHOOK_SECRET so the
        receiver can verify provenance even after the row sits in
        the outbox through a process restart. Returns the new
        outbox row id, or None when skipped.

        The actual HTTP POST happens in the background delivery
        loop (v0.27 agent A); this method is fire-and-forget from
        the caller's perspective.

        ``kind`` (v0.34) is persisted on the outbox row and tells the
        delivery loop which transport to use -- 'webhook' (default)
        for the HTTP POST path, 'github' for the GitHub PR-comment
        adapter.

        Gating differs per transport. The default 'webhook' kind is
        gated on COORD_WEBHOOK_URL and filtered by COORD_WEBHOOK_EVENTS,
        exactly as before. The 'github' kind is instead gated on
        COORD_GITHUB_TOKEN (mirroring how webhook_url gates webhooks):
        with no token configured this returns None and no row is
        enqueued, so the GitHub feature is a complete no-op. The
        webhook events allowlist does not apply to github rows -- that
        filter governs the HTTP webhook surface only. The github
        delivery path ignores the row's ``url``, so it is set to the
        configured webhook_url when present and a stable sentinel
        otherwise just for dashboard legibility.
        """
        if kind == "github":
            if not (self.settings.github_token or "").strip():
                return None
            url = (self.settings.webhook_url or "").strip() or "github://pr-comment"
        else:
            url = (self.settings.webhook_url or "").strip()
            if not url:
                return None
            events_filter = (self.settings.webhook_events or "").strip()
            if events_filter:
                allowed = {
                    tok.strip()
                    for tok in events_filter.split(",")
                    if tok.strip()
                }
                if event_type not in allowed:
                    return None
        import hashlib
        import hmac
        import json as _json

        payload = {
            "event_type": event_type,
            "timestamp": datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "detail": detail,
        }
        payload_json = _json.dumps(payload, sort_keys=True)
        secret = (self.settings.webhook_secret or "").encode("utf-8")
        signature = (
            hmac.new(secret, payload_json.encode("utf-8"), hashlib.sha256)
            .hexdigest()
            if secret
            else ""
        )
        if not secret and kind != "github" and not self._warned_unsigned_webhook:
            # Fail-open but loudly: the outbox column is NOT NULL so an
            # empty signature satisfies the schema and nothing else tells
            # the operator that COORD_WEBHOOK_URL was configured without
            # COORD_WEBHOOK_SECRET. The delivery loop omits the
            # X-Coord-Signature header for unsigned rows so a receiver
            # cannot mistake an empty signature for an authenticated one.
            self._warned_unsigned_webhook = True
            logger.warning(
                "COORD_WEBHOOK_URL is set but COORD_WEBHOOK_SECRET is empty; "
                "outgoing webhooks are UNSIGNED and will be delivered "
                "without an X-Coord-Signature header. Configure a secret if "
                "receivers must authenticate events."
            )
        try:
            return await self.db.enqueue_webhook(
                url=url,
                event_type=event_type,
                payload_json=payload_json,
                hmac_signature=signature,
                kind=kind,
            )
        except Exception:  # noqa: BLE001 - best-effort emit
            logger.exception(
                "fire_webhook: failed to enqueue %s outbox row",
                event_type,
            )
            return None

    async def deliver_pending_webhooks(self) -> dict[str, int]:
        """v0.27: process the webhook outbox.

        POSTs every due row to its target URL with the
        ``X-Coord-Signature`` HMAC header carried in the row. On a 2xx
        response the row is marked delivered; on any non-2xx response
        or transport-level exception the row is marked failed and the
        next attempt scheduled at ``now + backoff * 2**retry_count``
        seconds (the exponent is capped at ``webhook_max_retries`` to
        prevent overflow). Once ``retry_count + 1`` reaches
        ``settings.webhook_max_retries`` the row is marked exhausted
        instead of failed so the loop stops considering it.

        Per-row exception handling means one bad receiver does not
        break the batch -- each row is committed (delivered, failed,
        or exhausted) independently. Returns the
        ``{delivered, failed, exhausted}`` counters for the loop's
        log line.
        """
        import httpx

        counts = {"delivered": 0, "failed": 0, "exhausted": 0}
        try:
            rows = await self.db.list_pending_webhooks()
        except Exception:  # noqa: BLE001 - best-effort background sweep
            logger.exception("deliver_pending_webhooks: list query failed")
            return counts
        if not rows:
            return counts

        max_retries = max(int(self.settings.webhook_max_retries), 1)
        backoff = max(int(self.settings.webhook_retry_backoff_sec), 1)
        # Cap the exponent at max_retries so backoff * 2**retry_count
        # cannot overflow even if a row's retry_count drifted past the
        # max (e.g. a config change shrank the cap).
        exponent_cap = max_retries

        async with httpx.AsyncClient(timeout=10.0) as client:
            for row in rows:
                outbox_id = str(row["id"])
                if str(row.get("kind") or "webhook") == "github":
                    await self._deliver_github_row(
                        row=row,
                        backoff=backoff,
                        max_retries=max_retries,
                        exponent_cap=exponent_cap,
                        counts=counts,
                    )
                    continue
                # Resolve the target from CURRENT settings at delivery time
                # (mirroring the github path, which ignores the stored url):
                # rows enqueued before an operator rotated COORD_WEBHOOK_URL
                # -- including away from a decommissioned or compromised
                # receiver -- must follow the rotation, not the endpoint
                # frozen at emit time. The stored row url stays as the
                # fallback so pending rows still deliver if the setting is
                # cleared between emit and delivery.
                target_url = (
                    self.settings.webhook_url or ""
                ).strip() or str(row["url"])
                # X-Coord-Delivery-Id carries the stable outbox row id so
                # receivers can dedup: delivery is at-least-once (a lost 2xx
                # or a failed mark_webhook_delivered re-POSTs the same row),
                # and without a stable key the receiver cannot suppress the
                # duplicate side effects. The signature header is omitted
                # entirely for unsigned rows -- an empty X-Coord-Signature
                # looks authentic to a receiver that only checks presence.
                headers = {
                    "Content-Type": "application/json",
                    "X-Coord-Event-Type": row["event_type"],
                    "X-Coord-Delivery-Id": outbox_id,
                }
                signature = str(row.get("hmac_signature") or "")
                if signature:
                    headers["X-Coord-Signature"] = signature
                try:
                    response = await client.post(
                        target_url,
                        content=row["payload_json"],
                        headers=headers,
                    )
                except Exception as exc:  # noqa: BLE001 - per-row isolation
                    error = f"{type(exc).__name__}: {exc}"
                    await self._record_webhook_failure(
                        row=row,
                        error=error,
                        backoff=backoff,
                        max_retries=max_retries,
                        exponent_cap=exponent_cap,
                        counts=counts,
                    )
                    continue

                if 200 <= response.status_code < 300:
                    try:
                        await self.db.mark_webhook_delivered(outbox_id)
                        counts["delivered"] += 1
                    except Exception:  # noqa: BLE001 - audit best-effort
                        logger.exception(
                            "deliver_pending_webhooks: mark_delivered "
                            "failed for %s",
                            outbox_id,
                        )
                    continue

                error = f"HTTP {response.status_code}"
                await self._record_webhook_failure(
                    row=row,
                    error=error,
                    backoff=backoff,
                    max_retries=max_retries,
                    exponent_cap=exponent_cap,
                    counts=counts,
                )

        return counts

    async def _record_webhook_failure(
        self,
        *,
        row: dict[str, Any],
        error: str,
        backoff: int,
        max_retries: int,
        exponent_cap: int,
        counts: dict[str, int],
    ) -> None:
        """Helper for deliver_pending_webhooks: compute the next-attempt
        timestamp, decide exhausted vs failed, and mark the row. Counter
        bookkeeping happens here too so the call site stays compact.
        Per-row exception handling: a failure to write the failure row
        is logged but never propagates, otherwise one bad row would
        stall the rest of the batch."""

        outbox_id = str(row["id"])
        current_retries = int(row.get("retry_count") or 0)
        # exponent_cap keeps backoff * 2**n bounded; current_retries can
        # legitimately equal max_retries - 1 going into the final attempt.
        exponent = min(current_retries, exponent_cap)
        delay_sec = backoff * (2 ** exponent)
        next_dt = datetime.now(UTC) + timedelta(seconds=delay_sec)
        next_attempt_at = (
            next_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        exhausted = current_retries + 1 >= max_retries
        try:
            await self.db.mark_webhook_failed(
                outbox_id,
                last_error=error,
                next_attempt_at=next_attempt_at,
                exhausted=exhausted,
            )
        except Exception:  # noqa: BLE001 - audit best-effort
            logger.exception(
                "deliver_pending_webhooks: mark_failed failed for %s",
                outbox_id,
            )
            return
        if exhausted:
            counts["exhausted"] += 1
        else:
            counts["failed"] += 1

    async def _deliver_github_row(
        self,
        *,
        row: dict[str, Any],
        backoff: int,
        max_retries: int,
        exponent_cap: int,
        counts: dict[str, int],
    ) -> None:
        """v0.34: deliver a ``kind='github'`` outbox row.

        Routes the row's ``detail`` to the GitHub adapter, which
        find-or-updates a de-duplicated comment on the open PR for the
        pushing branch. When ``github_token`` is empty the feature is
        disabled: the row is marked delivered with a skipped note so it
        is never retried forever (a github row only exists when the
        token was set at emit time, but the token may have been cleared
        between emit and delivery). Any HTTP failure raised by the
        adapter routes through the shared ``_record_webhook_failure``
        retry/backoff path, identical to the webhook transport."""

        from coordination import github_adapter

        outbox_id = str(row["id"])
        token = (self.settings.github_token or "").strip()
        if not token:
            try:
                await self.db.mark_webhook_delivered(outbox_id)
                counts["delivered"] += 1
            except Exception:  # noqa: BLE001 - audit best-effort
                logger.exception(
                    "deliver_pending_webhooks: github skip mark_delivered "
                    "failed for %s",
                    outbox_id,
                )
            return

        try:
            payload = json.loads(row["payload_json"])
            detail = payload["detail"]
        except (ValueError, KeyError, TypeError) as exc:
            # A malformed payload can never succeed -- mark it failed so
            # backoff applies; it will exhaust rather than loop forever.
            await self._record_webhook_failure(
                row=row,
                error=f"github payload error: {type(exc).__name__}: {exc}",
                backoff=backoff,
                max_retries=max_retries,
                exponent_cap=exponent_cap,
                counts=counts,
            )
            return

        try:
            await github_adapter.post_bounce_comment(self.settings, detail)
        except Exception as exc:  # noqa: BLE001 - per-row isolation
            await self._record_webhook_failure(
                row=row,
                error=f"{type(exc).__name__}: {exc}",
                backoff=backoff,
                max_retries=max_retries,
                exponent_cap=exponent_cap,
                counts=counts,
            )
            return

        try:
            await self.db.mark_webhook_delivered(outbox_id)
            counts["delivered"] += 1
        except Exception:  # noqa: BLE001 - audit best-effort
            logger.exception(
                "deliver_pending_webhooks: github mark_delivered "
                "failed for %s",
                outbox_id,
            )

    async def set_ownership_yaml(self, yaml_text: str) -> None:
        await self.db.set_ownership_yaml(yaml_text)

    async def get_ownership_yaml(self) -> str | None:
        return await self.db.get_ownership_yaml()

    async def promote_hotspot(
        self,
        *,
        action: str,
        pattern: str,
        note: str | None,
        managed: bool = False,
    ) -> str:
        """v0.21 soft auto-promote: write ``pattern`` into the active
        owners.yaml as either a shared_file rule (action='shared_file')
        or a split-suggestion entry (action='split'). Idempotent.

        Returns the patched YAML so the operator can verify the result.

        ``managed=True`` (v0.23) tags a ``shared_file`` insertion with
        the ``# auto-promoted=YYYY-MM-DD`` marker so the auto-demote
        sweep can later distinguish coord-owned entries from operator
        ones. Ignored for ``action='split'``.
        """
        from coordination.ownership import (
            patch_owners_yaml_with_shared_file,
            patch_owners_yaml_with_split_suggestion,
        )

        current = await self.db.get_ownership_yaml() or ""
        if action == "shared_file":
            patched = patch_owners_yaml_with_shared_file(
                current, pattern, managed=managed
            )
        elif action == "split":
            patched = patch_owners_yaml_with_split_suggestion(
                current,
                pattern=pattern,
                note=note,
                suggested_at=datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )
        else:
            raise ValueError(
                f"unknown promote action {action!r}; "
                "expected 'shared_file' or 'split'"
            )
        if patched != current:
            await self.db.set_ownership_yaml(patched)
        return patched

    async def _maybe_auto_demote(self) -> int:
        """v0.23 auto-demote sweep.

        Inspect the active owners.yaml for coord-managed
        ``shared_files`` entries (those tagged with the
        ``# auto-promoted=YYYY-MM-DD`` marker by
        :meth:`_maybe_auto_promote`). For each one, compute the file's
        rolling 409 count via
        :meth:`Database.hotspot_files` with
        ``days=settings.auto_demote_window_days`` and
        ``min_attempts=1`` so even a single recent attempt keeps the
        entry; if the count is strictly below
        ``settings.auto_promote_threshold`` the entry is removed and
        an ``auto-demote`` ``request_events`` row is recorded.

        Operator-added entries (no marker) are never touched -- the
        sweep is the inverse half of :meth:`_maybe_auto_promote` and
        deliberately stays in its lane.

        v0.25: entries carrying the operator-set
        ``# coord-managed=permanent`` marker are also skipped. Operators
        pin a pattern (typically a package lock file, an app shell, or
        a schema index) when they want it to outlive its rolling
        hotspot activity. A pattern that carries both the
        auto-promoted marker and the permanent marker is treated as
        permanent: operator intent wins.

        Returns the number of entries removed. Skipped silently when
        ``auto_promote_threshold == 0`` (the feature is disabled). YAML
        parse errors are logged and swallowed so a malformed operator
        document cannot break the background loop.
        """
        from coordination.ownership import (
            list_coord_managed_shared_files,
            list_permanent_shared_files,
            patch_owners_yaml_remove_shared_file,
        )

        threshold = self.settings.auto_promote_threshold
        if threshold <= 0:
            return 0
        window_days = self.settings.auto_demote_window_days

        current = await self.db.get_ownership_yaml() or ""
        if not current.strip():
            return 0
        try:
            managed_entries = list_coord_managed_shared_files(current)
            permanent_patterns = set(list_permanent_shared_files(current))
        except Exception:  # noqa: BLE001 - sweep is best-effort
            logger.exception("auto-demote: failed to parse owners.yaml")
            return 0
        if permanent_patterns:
            managed_entries = [
                (pat, when)
                for pat, when in managed_entries
                if pat not in permanent_patterns
            ]
        if not managed_entries:
            return 0

        try:
            hotspots = await self.db.hotspot_files(
                days=window_days,
                min_attempts=1,
            )
        except Exception:  # noqa: BLE001 - sweep is best-effort
            logger.exception("auto-demote: hotspot_files query failed")
            return 0
        counts: dict[str, int] = {}
        for h in hotspots:
            pat = h.get("pattern")
            if not isinstance(pat, str):
                continue
            counts[pat] = counts.get(pat, 0) + int(h.get("attempts") or 0)

        removed = 0
        patched = current
        for pattern, _promoted_at in managed_entries:
            count_in_window = counts.get(pattern, 0)
            if count_in_window >= threshold:
                continue
            try:
                next_patched = patch_owners_yaml_remove_shared_file(
                    patched, pattern
                )
            except ValueError:
                logger.exception(
                    "auto-demote: failed to remove %r from owners.yaml",
                    pattern,
                )
                continue
            if next_patched == patched:
                # Idempotent no-op: already absent. Skip the audit row
                # so we don't spam events on every sweep.
                continue
            patched = next_patched
            demote_detail = {
                "pattern": pattern,
                "count_in_window": count_in_window,
                "threshold": threshold,
                "window_days": window_days,
            }
            await self.db.record_request_event(
                event_type="auto-demote",
                request_id=None,
                actor_engineer=None,
                actor_session_id=None,
                detail=demote_detail,
            )
            await self.fire_webhook("auto-demote", demote_detail)
            removed += 1

        if patched != current:
            await self.db.set_ownership_yaml(patched)
        return removed


def build_service() -> CoordinationService:
    s = get_settings()
    return CoordinationService(
        db=Database(s.database_path, writer_queue=s.sqlite_writer_queue),
        settings=s,
    )
