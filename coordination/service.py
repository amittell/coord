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
from coordination.ownership import PathRule, parse_ownership_yaml, severity_for_pattern
from coordination.schemas import (
    ConflictCheckResponse,
    ConflictEntry,
    ConflictingClaim,
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
        session_id: str | None = None,
    ) -> ConflictCheckResponse:
        for pat in patterns:
            err = _validate_pattern_syntax(pat)
            if err:
                raise ValueError(err)
        await self.db.expire_stale_claims(self.settings.idle_timeout_sec)
        # Activity ping: a session that's actively checking conflicts is
        # still alive even if it isn't creating new claims, so refresh
        # last_activity for everything it currently holds before we
        # decide what counts as "stale".
        if session_id:
            await self.db.touch_session_activity(session_id)
        active = await self.db.list_active_claims_rows(exclude_engineer=engineer)
        # Repo-scoped check (v0.4.0): only consider claims from the same
        # repo as the caller. NULL repo forms its own legacy bucket so
        # tagged callers never collide with un-tagged historical claims
        # and vice versa.
        active = [r for r in active if r.get("repo") == repo]
        # Session-scoped self-exclusion (v0.5.0): when the caller passes
        # a session_id (coord-mcp generates one per process), drop any
        # active claim that shares that session_id. This makes subagents
        # within one Codex/Claude run cooperative even when they use
        # distinct engineer names. Different sessions remain adversarial.
        if session_id:
            active = [r for r in active if r.get("session_id") != session_id]
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
        active = await self.db.list_active_claims_rows(exclude_engineer=body.engineer)
        # Repo-scoped check (v0.4.0): see check_conflicts for rationale.
        active = [r for r in active if r.get("repo") == body.repo]
        # Session-scoped self-exclusion (v0.5.0): see check_conflicts.
        if body.session_id:
            active = [r for r in active if r.get("session_id") != body.session_id]
        for item in body.claims:
            for row in active:
                overlap = await compute_overlap(
                    item.pattern,
                    row["pattern"],
                    repo_root=self.settings.repo_root,
                    scope=self.settings.repo_scope,
                )
                if not overlap:
                    continue
                conflicts.append(
                    ConflictEntry(
                        your_pattern=item.pattern,
                        conflicting_claim=ConflictingClaim(
                            id=row["id"],
                            engineer=row["engineer"],
                            pattern=row["pattern"],
                            severity=row["severity"],
                            description=row.get("description"),
                            expires_at=row["expires_at"],
                        ),
                        overlap=overlap,
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
        for item in body.claims:
            cid = str(uuid4())
            sev = severity_for_pattern(item.pattern, rules) if rules else "soft"
            if item.type == "shared_file":
                exp = _expires_at(self.settings.shared_ttl_hours)
            else:
                exp = _expires_at(ttl)
            ids.append((cid, item.type, item.pattern, sev, exp))

        created = await self.db.insert_claims_batch(
            engineer=body.engineer,
            branch=body.branch,
            description=body.description,
            items=ids,
            repo=body.repo,
            session_id=body.session_id,
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
        """Return the inbox of recent conflict-log entries logged
        against claims this session currently holds. Active holders
        poll this between operations to discover whether anyone has
        been blocked on their scope, so they can voluntarily release.
        """
        return await self.db.pending_requests_for_session(session_id)

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
