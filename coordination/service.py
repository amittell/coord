from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from uuid import uuid4

from coordination import metrics
from coordination.config import Settings, get_settings
from coordination.db import Database
from coordination.engine import compute_overlap, files_matching_pattern, git_ls_files
from coordination.overlap_symbols import (
    OverlapKind,
    check_overlap as check_symbol_overlap,
    record_auto_resolution,
)
from coordination.ownership import PathRule, parse_ownership_yaml, severity_for_pattern
from coordination.schemas import (
    ClaimItem,
    ConflictCheckResponse,
    ConflictEntry,
    ConflictingClaim,
    ConflictingSymbol,
    CreateClaimsRequest,
    CreateClaimsResponse,
)

logger = logging.getLogger(__name__)


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


@dataclass
class CoordinationService:
    db: Database
    settings: Settings

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

    async def check_conflicts(
        self,
        *,
        patterns: list[str],
        engineer: str,
        repo: str | None = None,
        session_ids: list[str] | None = None,
    ) -> ConflictCheckResponse:
        for pat in patterns:
            err = _validate_pattern_syntax(pat)
            if err:
                raise ValueError(err)
        await self.db.expire_stale_claims(self.settings.idle_timeout_sec)
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
                await self.db.touch_session_activity(sid)
        active = await self.db.list_active_claims_rows(exclude_engineer=engineer)
        # Repo-scoped check (v0.4.0): only consider claims from the same
        # repo as the caller. NULL repo forms its own legacy bucket so
        # tagged callers never collide with un-tagged historical claims
        # and vice versa.
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
            own_claims = [
                c
                for c in own_claims
                if c.get("session_id") in own_session_set
                and c.get("repo") == repo
            ]
            partner_ids = _coexist_partner_ids_from_rows(own_claims)
            if partner_ids:
                active = [r for r in active if str(r.get("id")) not in partner_ids]
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
        safe = len(conflicts) == 0
        suggestion: str | None = None
        if not safe:
            c0 = conflicts[0]
            suggestion = (
                f"Conflict with {c0.get('engineer')} on {c0.get('pattern')} "
                f"(expires {c0.get('expires_at')}). Wait for TTL, narrow your patterns, "
                "or coordinate with the other engineer."
            )
        return ConflictCheckResponse(
            has_conflicts=not safe,
            conflicts=conflicts,
            safe_to_proceed=safe,
            safe=safe,
            suggestion=suggestion,
        )

    async def create_claims(self, body: CreateClaimsRequest) -> CreateClaimsResponse:
        await self.db.expire_stale_claims(self.settings.idle_timeout_sec)
        # Activity ping: making a claim is the strongest possible
        # liveness signal -- bump last_activity for every claim this
        # session already holds before we decide what's stale.
        if body.session_id:
            await self.db.touch_session_activity(body.session_id)
        rules = await self._rules()
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

        zero_match_warnings = await self._zero_match_warnings(patterns)

        conflicts: list[ConflictEntry] = []
        # Queued auto-resolutions (v0.14): pairs of (item, holder_row, result)
        # discovered during the overlap pass that should bypass 409 and be
        # recorded as auto-coexist / auto-narrow events after the
        # requester's claim row is inserted. We hold the requester's
        # claim id back until insert_claims_batch returns.
        auto_resolutions: list[tuple[ClaimItem, dict[str, Any], Any]] = []

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
                active = [r for r in active if str(r.get("id")) not in partner_ids]
        for item in body.claims:
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
                    auto_resolutions.append((item, row, result))
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

        if conflicts:
            return CreateClaimsResponse(
                claim_ids=[],
                conflicts=conflicts,
                warnings=[],
                options=["wait", "narrow_claim", "escalate", "override"],
            )

        ttl = body.ttl_hours or self.settings.default_ttl_hours
        ids: list[tuple[str, str, str, str, str]] = []
        # Parallel to ids: per-item (cid, item) so post-insert wiring can
        # find the right ClaimItem for each created row without re-zipping.
        item_for_cid: dict[str, ClaimItem] = {}
        for item in body.claims:
            cid = str(uuid4())
            sev = severity_for_pattern(item.pattern, rules) if rules else "soft"
            if item.type == "shared_file":
                exp = _expires_at(self.settings.shared_ttl_hours)
            else:
                exp = _expires_at(ttl)
            ids.append((cid, item.type, item.pattern, sev, exp))
            item_for_cid[cid] = item

        created = await self.db.insert_claims_batch(
            engineer=body.engineer,
            branch=body.branch,
            description=body.description,
            items=ids,
            repo=body.repo,
            session_id=body.session_id,
        )
        # v0.14: post-insert scope_type / narrowable / symbol rows. We
        # defer this from insert_claims_batch to keep its signature stable
        # and the migration footprint minimal -- the create_claims handler
        # owns the symbol contract.
        await self._finalise_v14_scope(
            created=created, item_for_cid=item_for_cid
        )

        # v0.14: persist any auto-resolutions queued during overlap pass.
        # We look up the requester's claim id by (item.pattern) match
        # against the just-inserted batch. Each ClaimItem produces exactly
        # one row in ``ids`` so the pattern is unique within this batch.
        if auto_resolutions:
            cid_by_pattern: dict[str, str] = {
                pat: cid for cid, _ctype, pat, _sev, _exp in ids if cid in created
            }
            for item, holder_row, result in auto_resolutions:
                requester_cid = cid_by_pattern.get(item.pattern)
                if not requester_cid:
                    continue
                await record_auto_resolution(
                    db=self.db,
                    kind=result.kind,
                    holder_claim_id=str(holder_row["id"]),
                    requester_claim_id=requester_cid,
                    overlapping_paths=result.overlapping_paths,
                    overlapping_symbols=result.overlapping_symbols,
                )

        # Count one tick per successfully inserted claim. We look back at
        # the computed severity for each item so the label distribution
        # mirrors the ownership configuration.
        for _cid, _ctype, _pattern, sev, _exp in ids:
            if _cid in created:
                metrics.claims_created_total.inc(severity=sev)
        return CreateClaimsResponse(
            claim_ids=created,
            conflicts=[],
            warnings=zero_match_warnings,
            options=[],
        )

    async def _finalise_v14_scope(
        self,
        *,
        created: list[str],
        item_for_cid: dict[str, ClaimItem],
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
        """
        if not created:
            return
        import aiosqlite  # local import: keep service.py import surface stable

        from coordination.db import _configure_sqlite

        symbol_rows: list[tuple[str, str, str, str, str]] = []
        async with aiosqlite.connect(self.db.path) as conn:
            await _configure_sqlite(conn)
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
                    for name in item.symbols:
                        symbol_rows.append(
                            (
                                str(uuid4()),
                                cid,
                                item.pattern,
                                name,
                                "unknown",
                            )
                        )
            await conn.commit()
        if symbol_rows:
            await self.db.insert_claim_symbols(rows=symbol_rows)

    async def list_claims(
        self,
        *,
        active_only: bool = True,
        engineer: str | None = None,
        module_substring: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        await self.db.expire_stale_claims(self.settings.idle_timeout_sec)
        # Activity ping: an agent reading the claim list is still alive,
        # so keep its claims warm. No-op when session_id is unset
        # (legacy / non-MCP callers).
        if session_id:
            await self.db.touch_session_activity(session_id)
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

    async def pending_requests(self, session_id: str) -> list[dict[str, Any]]:
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
        """
        if not session_id:
            return []
        # First-class requests get the audit-event treatment so the
        # operator can prove "the holder did/didn't see this".
        open_requests = await self.db.list_open_requests_for_session(session_id)
        for r in open_requests:
            await self.db.record_request_notify(
                r["id"],
                holder_engineer=r.get("holder_engineer"),
                holder_session_id=session_id,
            )
        request_rows = [{"kind": "request", **r} for r in open_requests]

        # Auto-conflict entries (the v0.6 pre-existing inbox).
        conflicts = await self.db.pending_requests_for_session(session_id)
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
    ) -> dict[str, Any] | None:
        """Forward to the DB layer with v0.11 decision verbs.

        For ``decision='narrowed'`` the service enforces that
        ``narrowed_pattern`` is a (non-strict) subset of the holder's
        current claim pattern. A disjoint or broader pattern is a
        contract violation that the API handler maps to 400. Coexist
        deliberately skips the subset check because coexisting claims
        are intentionally on the same scope (or compatible scopes the
        holder explicitly agreed to).
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
        # Floor the new claim's TTL at the default working window so that a
        # narrowed or coexist claim created in response to a request does not
        # inherit the shortened deadline that request_release imposed on the
        # holder's original claim.
        min_expires_at = _expires_at(self.settings.default_ttl_hours)
        return await self.db.respond_to_request(
            request_id=request_id,
            decision=decision,
            actor_engineer=actor_engineer,
            actor_session_id=actor_session_id,
            note=note,
            narrowed_pattern=narrowed_pattern,
            coexist_pattern=coexist_pattern,
            min_expires_at=min_expires_at,
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
        n = await self.db.release_claims(claim_ids, engineer)
        for _ in range(n):
            metrics.claims_released_total.inc()
        return n

    async def extend_claim(self, claim_id: str, body_engineer: str, ttl_hours: int) -> bool:
        new_exp = _expires_at(ttl_hours)
        return await self.db.extend_claim(claim_id, body_engineer, new_exp)

    async def set_ownership_yaml(self, yaml_text: str) -> None:
        await self.db.set_ownership_yaml(yaml_text)

    async def get_ownership_yaml(self) -> str | None:
        return await self.db.get_ownership_yaml()


def build_service() -> CoordinationService:
    s = get_settings()
    return CoordinationService(db=Database(s.database_path), settings=s)
