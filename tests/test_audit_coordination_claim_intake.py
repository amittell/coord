"""Audit regression tests for claim intake and read-path hardening.

Covers:

- pattern canonicalization at claim intake (the intake half of the
  symbol-overlap path-spelling defect: "./src/a.py" vs "src/a.py" must
  collapse before storage or the symbol/symbol classifier compares
  different dict keys and lets two claims on the same symbol coexist);
- auto-resolution wiring keyed by item index so a batch with duplicate
  patterns wires each created claim to its own coexist partner links;
- check_conflicts(all_repos=True) harvesting the caller's own claims
  across repo buckets so coexist partners in tagged repos are excluded;
- the best-effort activity ping never failing the read that carried it
  when the coalescing interval is disabled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    db_path = tmp_path / "intake.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        _env_file=None,
    )
    return CoordinationService(db=db, settings=settings)


async def test_patterns_canonicalized_at_intake(
    service: CoordinationService,
) -> None:
    """Leading "./", backslashes, and trailing "/" collapse to the same
    canonical form the overlap engine matches with before the claim row
    (and any claim_symbols rows) are stored."""

    result = await service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            branch="feat",
            session_id="sess-a",
            claims=[
                ClaimItem(type="file", pattern="./src/a.py"),
                ClaimItem(type="file", pattern="src\\b.py"),
                ClaimItem(type="module", pattern="docs/"),
            ],
        )
    )
    assert result.claim_ids, f"grant failed: {result.warnings!r}"

    rows = await service.list_claims(active_only=True)
    stored = sorted(r["pattern"] for r in rows)
    assert stored == ["docs/**", "src/a.py", "src/b.py"]


async def test_same_symbol_different_spelling_conflicts(
    service: CoordinationService,
) -> None:
    """Two claims on the SAME symbol of the same file must 409 even when
    the second spells the path differently ("./src/a.py"). Before intake
    canonicalization the symbol/symbol classifier compared the raw
    strings as dict keys, found no intersection, and auto-coexisted two
    claims on one symbol."""

    first = await service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            branch="feat-a",
            session_id="sess-a",
            claims=[
                ClaimItem(type="file", pattern="src/a.py", symbols=["handler"])
            ],
        )
    )
    assert first.claim_ids

    second = await service.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            branch="feat-b",
            session_id="sess-b",
            claims=[
                ClaimItem(
                    type="file", pattern="./src/a.py", symbols=["handler"]
                )
            ],
        )
    )
    assert second.claim_ids == []
    assert second.conflicts, (
        "same-symbol claim with a './'-prefixed spelling was granted "
        "instead of conflicting"
    )


async def test_duplicate_patterns_wire_each_items_auto_resolution(
    service: CoordinationService,
) -> None:
    """A batch carrying two ClaimItems on the same pattern (different
    symbol sets) must wire coexist partner links for BOTH created claims.
    The old pattern-keyed lookup collapsed both auto-resolutions onto the
    last inserted claim id, leaving one claim without its partner link."""

    holder = await service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            branch="feat-a",
            session_id="sess-a",
            claims=[
                ClaimItem(type="file", pattern="src/x.py", symbols=["a"])
            ],
        )
    )
    assert holder.claim_ids
    holder_id = holder.claim_ids[0]

    batch = await service.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            branch="feat-b",
            session_id="sess-b",
            claims=[
                ClaimItem(type="file", pattern="src/x.py", symbols=["b"]),
                ClaimItem(type="file", pattern="src/x.py", symbols=["c"]),
            ],
        )
    )
    assert len(batch.claim_ids) == 2, (
        f"expected both disjoint symbol claims granted: {batch.conflicts!r}"
    )

    rows = await service.db.list_active_claims_rows(exclude_engineer=None)
    by_id = {str(r["id"]): r for r in rows}

    def partners(claim_id: str) -> set[str]:
        raw = by_id[claim_id].get("coexists_with")
        if not raw:
            return set()
        return {str(x) for x in json.loads(raw)}

    for cid in batch.claim_ids:
        assert holder_id in partners(cid), (
            f"created claim {cid} is missing its coexist link to the holder"
        )
    assert partners(holder_id) == set(batch.claim_ids), (
        "holder must link back to BOTH created claims"
    )


async def test_all_repos_conflict_check_excludes_tagged_coexist_partners(
    service: CoordinationService,
) -> None:
    """An operator's all_repos conflict check must still harvest the
    caller's own (repo-tagged) claims for coexist self-exclusion. The
    old harvest pinned repo=None, found no own claims on a fully tagged
    deployment, and reported explicitly granted partners as conflicts."""

    holder = await service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            branch="feat-a",
            session_id="sess-a",
            repo="org/r1",
            claims=[ClaimItem(type="file", pattern="src/f.py")],
        )
    )
    assert holder.claim_ids
    holder_id = holder.claim_ids[0]

    own = await service.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            branch="feat-b",
            session_id="sess-b",
            repo="org/r1",
            claims=[ClaimItem(type="file", pattern="src/other.py")],
        )
    )
    assert own.claim_ids
    own_id = own.claim_ids[0]

    # Explicitly granted coexistence between the two claims (the v0.11
    # decision verb wires exactly these edges).
    await service.db.attach_coexist_partner(holder_id, own_id)
    await service.db.attach_coexist_partner(own_id, holder_id)

    result = await service.check_conflicts(
        patterns=["src/f.py"],
        engineer="bob",
        repo=None,
        all_repos=True,
        session_ids=["sess-b"],
    )
    assert result.conflicts == [], (
        "all_repos check reported an explicitly granted coexist partner "
        f"as a conflict: {result.conflicts!r}"
    )


@dataclass
class _FailingPingDb:
    calls: list[str] = field(default_factory=list)

    async def touch_session_activity(self, session_id: str, *, repo=None) -> int:
        self.calls.append(session_id)
        raise RuntimeError("SQLITE_BUSY: database is locked")


async def test_zero_interval_ping_failure_is_swallowed() -> None:
    """With coalescing disabled (interval=0) a failed liveness write is
    dropped, not re-raised into the read path that carried it."""

    settings = Settings(
        activity_ping_min_interval_sec=0,
        allow_insecure_no_auth=True,
        _env_file=None,
    )
    svc = CoordinationService(db=_FailingPingDb(), settings=settings)  # type: ignore[arg-type]
    await svc._maybe_touch("sess-a", "org/repo")
    assert svc.db.calls == ["sess-a"]
