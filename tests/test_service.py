from __future__ import annotations

from pathlib import Path

import pytest

from coordination.config import Settings
from coordination.db import Database
from coordination.service import CoordinationService


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    from coordination.engine import _clear_ls_files_cache

    _clear_ls_files_cache()
    yield
    _clear_ls_files_cache()


@pytest.fixture()
async def service(tmp_path: Path) -> CoordinationService:
    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
        repo_scope="svc-a",
    )
    return CoordinationService(db=db, settings=settings)


@pytest.mark.asyncio
async def test_scope_mode_allows_narrow_in_scope_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When COORD_REPO_SCOPE is set, the max_claim_ratio guardrail is skipped.
    Rationale: scope declares the working area; ratio-within-scope is trivially
    100% for any single-file claim in a small scope, which defeats scoping.
    Absolute max_claim_files still applies in scope mode."""
    import coordination.service as service_module
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
        repo_scope="svc-a",
        max_claim_ratio=0.1,  # intentionally tight to catch accidental enforcement
    )
    svc = CoordinationService(db=db, settings=settings)

    # Scope has 1 file; a claim covers 100% of the scope. The ratio check
    # should NOT fire because scope mode skips ratio entirely.
    async def fake_git_ls_files(root, scope=None):
        if scope == "svc-a":
            return ["svc-a/src/one.ts"]
        raise AssertionError(
            "scope mode must not make a second unscoped git_ls_files call"
        )

    monkeypatch.setattr(service_module, "git_ls_files", fake_git_ls_files)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            branch="feat",
            claims=[ClaimItem(type="file", pattern="svc-a/src/one.ts")],
        )
    )

    assert result.warnings == [], f"Expected no warnings; got: {result.warnings!r}"
    assert result.claim_ids, "Claim should have been created"


@pytest.mark.asyncio
async def test_scope_mode_still_enforces_absolute_max_claim_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scope mode skips the ratio check but keeps max_claim_files as a hard cap."""
    import coordination.service as service_module
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
        repo_scope="svc-a",
        max_claim_files=5,  # small absolute cap
    )
    svc = CoordinationService(db=db, settings=settings)

    async def fake_git_ls_files(root, scope=None):
        return [f"svc-a/f{i}.ts" for i in range(20)]  # 20 files in scope

    monkeypatch.setattr(service_module, "git_ls_files", fake_git_ls_files)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[ClaimItem(type="file", pattern="svc-a/**")],
        )
    )

    assert result.claim_ids == []
    assert any("20 files" in w and "max is 5" in w for w in result.warnings), (
        f"Expected max_claim_files warning; got: {result.warnings!r}"
    )


@pytest.mark.asyncio
async def test_no_scope_mode_still_enforces_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: without COORD_REPO_SCOPE, the ratio check still fires."""
    import coordination.service as service_module
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
        repo_scope=None,  # no scope -> ratio applies
        max_claim_files=1000,
        max_claim_ratio=0.1,
    )
    svc = CoordinationService(db=db, settings=settings)

    async def fake_git_ls_files(root, scope=None):
        # 10 files total; claim covering all 10 hits 100% > 10% cap.
        return [f"pkg/f{i}.ts" for i in range(10)]

    monkeypatch.setattr(service_module, "git_ls_files", fake_git_ls_files)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[ClaimItem(type="file", pattern="pkg/**")],
        )
    )

    assert result.claim_ids == []
    assert any("100%" in w and "max is 10%" in w for w in result.warnings), (
        f"Expected ratio warning in non-scope mode; got: {result.warnings!r}"
    )


@pytest.mark.asyncio
async def test_zero_match_pattern_emits_warning_but_claim_still_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim pattern that matches zero known files (typo, case mismatch,
    nonexistent directory) should surface a warning but still be created -
    the file may be about to be introduced in the branch."""
    import coordination.service as service_module
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
    )
    svc = CoordinationService(db=db, settings=settings)

    async def fake_git_ls_files(root, scope=None):
        return ["src/auth/login.ts", "src/billing/index.ts"]

    monkeypatch.setattr(service_module, "git_ls_files", fake_git_ls_files)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[ClaimItem(type="file", pattern="src/nonexistent/**")],
        )
    )

    assert result.claim_ids, "Claim should still be created (future-file case)"
    assert any("zero files" in w or "no matching files" in w for w in result.warnings), (
        f"Expected zero-match warning; got: {result.warnings!r}"
    )


@pytest.mark.asyncio
async def test_zero_match_with_uppercase_suggests_lowercase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case-insensitive-filesystem users (common on macOS) often type
    patterns in the wrong case. When the original pattern matches zero files
    but the lowercased variant would match, hint at the fix."""
    import coordination.service as service_module
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=tmp_path,
    )
    svc = CoordinationService(db=db, settings=settings)

    async def fake_git_ls_files(root, scope=None):
        return ["src/auth/login.ts"]

    monkeypatch.setattr(service_module, "git_ls_files", fake_git_ls_files)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[ClaimItem(type="file", pattern="src/Auth/**")],
        )
    )

    assert result.claim_ids
    hint_texts = " ".join(result.warnings).lower()
    assert "src/auth/**" in hint_texts, (
        f"Expected a lowercase suggestion; got: {result.warnings!r}"
    )


@pytest.mark.asyncio
async def test_zero_match_warning_skipped_without_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without COORD_REPO_ROOT, we have no ground truth to call "zero match";
    skip the warning rather than guess."""
    import coordination.service as service_module
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
    )
    svc = CoordinationService(db=db, settings=settings)

    async def fake_git_ls_files(root, scope=None):
        raise AssertionError("git_ls_files must not be called without repo_root")

    monkeypatch.setattr(service_module, "git_ls_files", fake_git_ls_files)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[ClaimItem(type="file", pattern="anything/**")],
        )
    )

    assert result.claim_ids
    assert result.warnings == []


@pytest.mark.asyncio
async def test_check_conflicts_passes_scope_to_engine(
    service: CoordinationService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Seed one active claim belonging to a different engineer so the inner
    # loop actually reaches compute_overlap.
    import coordination.service as service_module

    await service.db.insert_claims_batch(
        engineer="other",
        branch="feat",
        description=None,
        items=[("cid1", "exclusive", "svc-a/src/**", "soft", "2099-01-01T00:00:00Z")],
    )

    captured: dict[str, object] = {}

    async def fake_compute_overlap(pattern_a, pattern_b, *, repo_root, scope=None):
        captured["pattern_a"] = pattern_a
        captured["pattern_b"] = pattern_b
        captured["repo_root"] = repo_root
        captured["scope"] = scope
        return ["svc-a/src/f.ts"]

    monkeypatch.setattr(service_module, "compute_overlap", fake_compute_overlap)

    # Ensure _validate_claim_scope does not short-circuit us; stub out the
    # underlying git_ls_files to return a single file so limits are satisfied.
    async def fake_git_ls_files(root, scope=None):
        return ["svc-a/src/f.ts"]

    monkeypatch.setattr(service_module, "git_ls_files", fake_git_ls_files)

    resp = await service.check_conflicts(
        patterns=["svc-a/src/new.ts"], engineer="alice"
    )
    assert resp.has_conflicts is True
    assert captured["scope"] == "svc-a"
    assert captured["repo_root"] == tmp_path


# --- Repo-scoped conflict detection (v0.4.0) -------------------------------
#
# Pre-v0.4.0 the conflict check ran across the whole claims pool, so a claim
# in repo A holding `client/js/**` would block an unrelated claim with the
# same path in repo B. From v0.4.0 the check partitions by the repo column
# on each claim:
#   - claim with repo=X only sees claims with repo=X
#   - claim with repo=NULL (legacy / un-tagged client) only sees other
#     repo=NULL claims
# Legacy NULL claims age out naturally; we never spuriously match a
# repo-tagged claim against a NULL one.


@pytest.fixture()
async def repo_service(tmp_path: Path) -> CoordinationService:
    """Service fixture without a repo_scope, so create_claims doesn't
    short-circuit on _validate_claim_scope. We want the conflict path to
    actually run."""
    db_path = tmp_path / "repo_svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
    )
    return CoordinationService(db=db, settings=settings)


@pytest.mark.asyncio
async def test_create_claims_does_not_conflict_across_repos(
    repo_service: CoordinationService,
) -> None:
    """Same pattern, different repo, different engineer -> no conflict.
    This is the cross-repo false-positive that v0.4.0 fixes."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    # Existing active claim in repo A.
    await repo_service.db.insert_claims_batch(
        engineer="bob",
        branch="feat",
        description=None,
        items=[("cidA", "module", "client/js/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
    )

    # Alice tries to claim the same path under a different repo.
    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo="example-org/astrowars",
            claims=[ClaimItem(type="module", pattern="client/js/**")],
        )
    )
    assert result.conflicts == [], (
        f"cross-repo claims must not conflict; got {result.conflicts!r}"
    )
    assert result.claim_ids, "claim should have been created"


@pytest.mark.asyncio
async def test_create_claims_still_conflicts_within_same_repo(
    repo_service: CoordinationService,
) -> None:
    """Same pattern, same repo, different engineer -> conflict (regression)."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    await repo_service.db.insert_claims_batch(
        engineer="bob",
        branch="feat",
        description=None,
        items=[("cid_same", "module", "client/js/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
    )

    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo="amittell/coord",
            claims=[ClaimItem(type="module", pattern="client/js/**")],
        )
    )
    assert result.conflicts, "same-repo overlap must still conflict"
    assert result.claim_ids == []


@pytest.mark.asyncio
async def test_create_claims_null_repo_isolated_from_repo_tagged(
    repo_service: CoordinationService,
) -> None:
    """A legacy NULL-repo claim must not block a repo-tagged claim with
    the same pattern, and vice versa. NULL forms its own bucket."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    await repo_service.db.insert_claims_batch(
        engineer="bob",
        branch="feat",
        description=None,
        items=[("cid_null", "module", "client/js/**", "soft", "2099-01-01T00:00:00Z")],
        repo=None,
    )

    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo="example-org/astrowars",
            claims=[ClaimItem(type="module", pattern="client/js/**")],
        )
    )
    assert result.conflicts == [], (
        f"NULL-repo claim must not block repo-tagged client; got {result.conflicts!r}"
    )
    assert result.claim_ids


@pytest.mark.asyncio
async def test_create_claims_null_repo_conflicts_with_other_null_repo(
    repo_service: CoordinationService,
) -> None:
    """Legacy clients (no repo) keep their own self-consistent pool: a
    NULL claim still conflicts with another NULL claim on overlapping
    patterns. This is the back-compat half of the partition."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    await repo_service.db.insert_claims_batch(
        engineer="bob",
        branch="feat",
        description=None,
        items=[("cid_n1", "module", "client/js/**", "soft", "2099-01-01T00:00:00Z")],
        repo=None,
    )

    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            # No repo supplied -> defaults to None (legacy client).
            claims=[ClaimItem(type="module", pattern="client/js/**")],
        )
    )
    assert result.conflicts, "NULL vs NULL on the same pattern must still conflict"


@pytest.mark.asyncio
async def test_check_conflicts_filters_by_repo(
    repo_service: CoordinationService,
) -> None:
    """check_conflicts(repo=X) returns clean against a claim in repo Y."""
    await repo_service.db.insert_claims_batch(
        engineer="bob",
        branch="feat",
        description=None,
        items=[("cid_other_repo", "module", "client/js/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
    )

    resp = await repo_service.check_conflicts(
        patterns=["client/js/**"],
        engineer="alice",
        repo="example-org/astrowars",
    )
    assert resp.has_conflicts is False
    assert resp.conflicts == []
    assert resp.safe is True


@pytest.mark.asyncio
async def test_check_conflicts_same_repo_still_flags(
    repo_service: CoordinationService,
) -> None:
    await repo_service.db.insert_claims_batch(
        engineer="bob",
        branch="feat",
        description=None,
        items=[("cid_same_repo", "module", "client/js/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
    )

    resp = await repo_service.check_conflicts(
        patterns=["client/js/**"],
        engineer="alice",
        repo="amittell/coord",
    )
    assert resp.has_conflicts is True
    assert len(resp.conflicts) == 1


@pytest.mark.asyncio
async def test_check_conflicts_no_repo_arg_is_legacy_null_bucket(
    repo_service: CoordinationService,
) -> None:
    """Calling check_conflicts without repo defaults to the legacy NULL
    bucket and must not surface claims from any tagged repo."""
    await repo_service.db.insert_claims_batch(
        engineer="bob",
        branch="feat",
        description=None,
        items=[("cid_tagged", "module", "client/js/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
    )

    resp = await repo_service.check_conflicts(
        patterns=["client/js/**"],
        engineer="alice",
    )
    assert resp.has_conflicts is False, (
        "untagged caller must not see tagged claims"
    )


# --- Session-scoped self-exclusion (v0.5.0) --------------------------------
#
# A single agent process (Claude Code, Codex, Cursor) often spawns multiple
# subagents with distinct engineer names ("codex-server-review",
# "codex-render-review", ...). Pre-v0.5 the conflict check excluded only
# when the engineer name matched verbatim, so subagent A's claims would
# block subagent B's overlapping claims even though both are the same
# logical actor. v0.5 adds a per-process session_id (auto-generated by
# coord-mcp at startup) and the conflict check additionally self-excludes
# any row whose session_id matches the requester's. Different sessions
# (separate Codex restarts, two terminals on the same repo) still
# adversarially conflict, which is correct.


@pytest.mark.asyncio
async def test_create_claims_self_excludes_same_session_different_engineer(
    repo_service: CoordinationService,
) -> None:
    """A subagent in the same session must not be blocked by an earlier
    subagent's overlapping claim, even when the two use different
    engineer names."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    await repo_service.db.insert_claims_batch(
        engineer="codex-server-review",
        branch=None,
        description=None,
        items=[("cid_sess1", "module", "server/**", "soft", "2099-01-01T00:00:00Z")],
        repo="example-org/astrowars",
        session_id="sess-a-1234",
    )

    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="codex-shared-review",
            repo="example-org/astrowars",
            session_id="sess-a-1234",
            claims=[ClaimItem(type="module", pattern="server/**")],
        )
    )
    assert result.conflicts == [], (
        f"same session must self-exclude across engineer names; "
        f"got {result.conflicts!r}"
    )
    assert result.claim_ids


@pytest.mark.asyncio
async def test_create_claims_still_conflicts_across_different_sessions(
    repo_service: CoordinationService,
) -> None:
    """Two sessions are two genuine actors; they must still conflict on
    overlapping patterns. This guards against the temptation to make
    session_id silently override the conflict check."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    await repo_service.db.insert_claims_batch(
        engineer="codex-server",
        branch=None,
        description=None,
        items=[("cid_sess2a", "module", "server/**", "soft", "2099-01-01T00:00:00Z")],
        repo="example-org/astrowars",
        session_id="sess-a",
    )

    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="codex-render",
            repo="example-org/astrowars",
            session_id="sess-b",
            claims=[ClaimItem(type="module", pattern="server/**")],
        )
    )
    assert result.conflicts, (
        "different sessions must still conflict on overlapping patterns"
    )


@pytest.mark.asyncio
async def test_create_claims_session_id_optional_falls_back_to_engineer_match(
    repo_service: CoordinationService,
) -> None:
    """Pre-v0.5 clients (no session_id) keep their old behaviour:
    same engineer name self-excludes, different engineer adversarial."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    await repo_service.db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("cid_legacy", "module", "src/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
        session_id=None,
    )

    # Same engineer, no session: self-excludes via engineer match.
    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo="amittell/coord",
            claims=[ClaimItem(type="module", pattern="src/**")],
        )
    )
    assert result.claim_ids, "same engineer must self-exclude (legacy behaviour)"

    # Different engineer, no session: still adversarial (legacy behaviour).
    result = await repo_service.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            repo="amittell/coord",
            claims=[ClaimItem(type="module", pattern="src/**")],
        )
    )
    assert result.conflicts, (
        "different engineer with no session must remain adversarial"
    )


@pytest.mark.asyncio
async def test_check_conflicts_honors_session_id(
    repo_service: CoordinationService,
) -> None:
    await repo_service.db.insert_claims_batch(
        engineer="codex-a",
        branch=None,
        description=None,
        items=[("cid_chk", "module", "src/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
        session_id="sess-z",
    )

    # Same session, different engineer: clean.
    resp = await repo_service.check_conflicts(
        patterns=["src/**"],
        engineer="codex-b",
        repo="amittell/coord",
        session_ids=["sess-z"],
    )
    assert resp.has_conflicts is False
    assert resp.conflicts == []

    # Different session: still flagged.
    resp = await repo_service.check_conflicts(
        patterns=["src/**"],
        engineer="codex-b",
        repo="amittell/coord",
        session_ids=["sess-other"],
    )
    assert resp.has_conflicts is True


@pytest.mark.asyncio
async def test_check_conflicts_session_ids_singleton_matches_pre_v010(
    repo_service: CoordinationService,
) -> None:
    # A list with exactly one session_id reproduces the pre-v0.10
    # single-value path: that session's claim is excluded, an
    # unrelated session's claim is still flagged.
    await repo_service.db.insert_claims_batch(
        engineer="codex-a",
        branch=None,
        description=None,
        items=[
            ("cid_singleton", "module", "src/**", "soft", "2099-01-01T00:00:00Z")
        ],
        repo="amittell/coord",
        session_id="sess-solo",
    )

    clean = await repo_service.check_conflicts(
        patterns=["src/**"],
        engineer="codex-b",
        repo="amittell/coord",
        session_ids=["sess-solo"],
    )
    assert clean.has_conflicts is False
    assert clean.conflicts == []

    flagged = await repo_service.check_conflicts(
        patterns=["src/**"],
        engineer="codex-b",
        repo="amittell/coord",
        session_ids=["sess-someone-else"],
    )
    assert flagged.has_conflicts is True


@pytest.mark.asyncio
async def test_check_conflicts_session_ids_excludes_any_match(
    repo_service: CoordinationService,
) -> None:
    # When the caller is one agent process carrying multiple live
    # session_ids (parent dispatcher + per-worktree subagents), claims
    # from any of those sessions must be excluded.
    await repo_service.db.insert_claims_batch(
        engineer="codex-a",
        branch=None,
        description=None,
        items=[
            ("cid_a", "module", "src/a/**", "soft", "2099-01-01T00:00:00Z")
        ],
        repo="amittell/coord",
        session_id="sess-A",
    )
    await repo_service.db.insert_claims_batch(
        engineer="codex-b",
        branch=None,
        description=None,
        items=[
            ("cid_b", "module", "src/b/**", "soft", "2099-01-01T00:00:00Z")
        ],
        repo="amittell/coord",
        session_id="sess-B",
    )
    await repo_service.db.insert_claims_batch(
        engineer="codex-c",
        branch=None,
        description=None,
        items=[
            ("cid_c", "module", "src/c/**", "soft", "2099-01-01T00:00:00Z")
        ],
        repo="amittell/coord",
        session_id="sess-C",
    )

    resp = await repo_service.check_conflicts(
        patterns=["src/a/x.py", "src/b/y.py", "src/c/z.py"],
        engineer="outsider",
        repo="amittell/coord",
        session_ids=["sess-A", "sess-B"],
    )
    assert resp.has_conflicts is True, "sess-C is not in the exclude set"
    flagged_ids = {c["pattern"] for c in resp.conflicts}
    assert flagged_ids == {"src/c/**"}, (
        f"only sess-C should remain; got {flagged_ids}"
    )


@pytest.mark.asyncio
async def test_check_conflicts_session_ids_none_or_empty_is_legacy(
    repo_service: CoordinationService,
) -> None:
    # No session_ids (None or []) means no self-exclusion and no touch
    # ping: identical to the pre-v0.5 NULL bucket behaviour.
    await repo_service.db.insert_claims_batch(
        engineer="codex-a",
        branch=None,
        description=None,
        items=[
            ("cid_legacy", "module", "src/**", "soft", "2099-01-01T00:00:00Z")
        ],
        repo="amittell/coord",
        session_id="sess-anything",
    )

    resp_none = await repo_service.check_conflicts(
        patterns=["src/**"],
        engineer="codex-b",
        repo="amittell/coord",
        session_ids=None,
    )
    assert resp_none.has_conflicts is True

    resp_empty = await repo_service.check_conflicts(
        patterns=["src/**"],
        engineer="codex-b",
        repo="amittell/coord",
        session_ids=[],
    )
    assert resp_empty.has_conflicts is True


@pytest.mark.asyncio
async def test_release_for_session_releases_all_claims_in_session(
    repo_service: CoordinationService,
) -> None:
    """End-of-session cleanup: a single call releases every claim the
    session created, regardless of which subagent name created it."""
    await repo_service.db.insert_claims_batch(
        engineer="codex-foo",
        branch=None,
        description=None,
        items=[
            ("cid_r1", "module", "a/**", "soft", "2099-01-01T00:00:00Z"),
            ("cid_r2", "module", "b/**", "soft", "2099-01-01T00:00:00Z"),
        ],
        repo="amittell/coord",
        session_id="sess-cleanup",
    )
    await repo_service.db.insert_claims_batch(
        engineer="codex-bar",
        branch=None,
        description=None,
        items=[("cid_r3", "module", "c/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
        session_id="sess-cleanup",
    )
    # Sibling session that must NOT be touched.
    await repo_service.db.insert_claims_batch(
        engineer="other-agent",
        branch=None,
        description=None,
        items=[("cid_keep", "module", "z/**", "soft", "2099-01-01T00:00:00Z")],
        repo="amittell/coord",
        session_id="sess-different",
    )

    n = await repo_service.db.release_for_session("sess-cleanup")
    assert n == 3, f"expected 3 releases, got {n}"

    remaining = await repo_service.db.list_active_claims_rows()
    remaining_ids = {r["id"] for r in remaining}
    assert "cid_keep" in remaining_ids
    assert "cid_r1" not in remaining_ids
    assert "cid_r2" not in remaining_ids
    assert "cid_r3" not in remaining_ids


# --- Activity-based auto-expiration & pending-requests inbox (v0.6.0) -----
#
# v0.6 closes the "agent walked away with claims still held" failure mode
# by tracking last_activity on each session-tagged claim and expiring it
# when the session has been silent for longer than COORD_IDLE_TIMEOUT_SEC.
# It also surfaces a pending-requests inbox so an active holder can see
# who has been blocked on its scope and decide whether to release.


@pytest.fixture()
async def idle_service(tmp_path: Path) -> CoordinationService:
    """Service with a small idle timeout so tests can exercise the
    expiration path without sleeping."""
    db_path = tmp_path / "idle_svc.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
        idle_timeout_sec=60,
    )
    return CoordinationService(db=db, settings=settings)


@pytest.mark.asyncio
async def test_create_claims_sets_last_activity_when_session_id_given(
    idle_service: CoordinationService,
) -> None:
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    result = await idle_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo="amittell/coord",
            session_id="sess-1",
            claims=[ClaimItem(type="file", pattern="src/foo.py")],
        )
    )
    assert result.claim_ids
    rows = await idle_service.db.list_active_claims_rows()
    row = next(r for r in rows if r["id"] == result.claim_ids[0])
    assert row.get("last_activity") is not None, (
        "session-tagged claim must have last_activity stamped on insert"
    )


@pytest.mark.asyncio
async def test_legacy_null_session_claims_have_null_last_activity(
    idle_service: CoordinationService,
) -> None:
    """Pre-v0.5 clients (no session_id) should not be subject to idle
    expiration. The simplest way to express that is to leave
    last_activity NULL on insert."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    result = await idle_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo="amittell/coord",
            claims=[ClaimItem(type="file", pattern="src/foo.py")],
        )
    )
    rows = await idle_service.db.list_active_claims_rows()
    row = next(r for r in rows if r["id"] == result.claim_ids[0])
    assert row.get("last_activity") is None


@pytest.mark.asyncio
async def test_idle_session_claims_get_expired(
    idle_service: CoordinationService,
) -> None:
    """A session that hasn't pinged activity for longer than
    idle_timeout_sec should have its claims released by the next
    expire_stale_claims sweep, even though their TTL is far in the
    future."""
    # Direct DB insert with a stale last_activity.
    from datetime import UTC, datetime, timedelta

    stale = (datetime.now(UTC) - timedelta(seconds=300)).replace(microsecond=0)
    stale_iso = stale.isoformat().replace("+00:00", "Z")
    far_future = "2099-01-01T00:00:00Z"

    await idle_service.db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("idle-cid", "file", "src/foo.py", "soft", far_future)],
        repo="amittell/coord",
        session_id="sess-idle",
        last_activity=stale_iso,
    )

    n = await idle_service.db.expire_stale_claims(
        idle_service.settings.idle_timeout_sec
    )
    assert n >= 1
    rows = await idle_service.db.list_active_claims_rows()
    assert "idle-cid" not in {r["id"] for r in rows}


@pytest.mark.asyncio
async def test_active_session_claims_survive_idle_sweep(
    idle_service: CoordinationService,
) -> None:
    """Activity ping keeps the claim alive."""
    from datetime import UTC, datetime

    fresh_iso = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    far_future = "2099-01-01T00:00:00Z"

    await idle_service.db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("active-cid", "file", "src/foo.py", "soft", far_future)],
        repo="amittell/coord",
        session_id="sess-active",
        last_activity=fresh_iso,
    )

    await idle_service.db.expire_stale_claims(
        idle_service.settings.idle_timeout_sec
    )
    rows = await idle_service.db.list_active_claims_rows()
    assert "active-cid" in {r["id"] for r in rows}


@pytest.mark.asyncio
async def test_check_conflicts_bumps_last_activity_for_session(
    idle_service: CoordinationService,
) -> None:
    """Calling check_conflicts from a session must refresh the
    last_activity of every active claim that session holds, otherwise
    a session that stops creating new claims but is still actively
    *checking* gets unfairly idle-expired."""
    from datetime import UTC, datetime, timedelta

    # Seed last_activity 30s ago: within the 60s idle window (so the
    # claim survives the expire sweep) but distinct from "now" so we
    # can detect the touch.
    stale_iso = (
        (datetime.now(UTC) - timedelta(seconds=30)).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )
    far_future = "2099-01-01T00:00:00Z"

    await idle_service.db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("touch-me", "file", "src/foo.py", "soft", far_future)],
        repo="amittell/coord",
        session_id="sess-touch",
        last_activity=stale_iso,
    )

    await idle_service.check_conflicts(
        patterns=["src/bar.py"],
        engineer="alice",
        repo="amittell/coord",
        session_ids=["sess-touch"],
    )

    rows = await idle_service.db.list_active_claims_rows()
    row = next(r for r in rows if r["id"] == "touch-me")
    new_activity = row["last_activity"]
    assert new_activity != stale_iso, (
        "check_conflicts must bump last_activity for session-tagged claims"
    )


@pytest.mark.asyncio
async def test_check_conflicts_bumps_last_activity_for_every_session_id(
    idle_service: CoordinationService,
) -> None:
    # When the caller carries multiple live session_ids, every one of
    # them must keep its claims warm, otherwise a subagent session that
    # isn't creating new claims gets idle-expired despite the parent
    # process being actively at work.
    from datetime import UTC, datetime, timedelta

    stale_iso = (
        (datetime.now(UTC) - timedelta(seconds=30)).replace(microsecond=0)
        .isoformat().replace("+00:00", "Z")
    )
    far_future = "2099-01-01T00:00:00Z"

    await idle_service.db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("touch-A", "file", "src/a.py", "soft", far_future)],
        repo="amittell/coord",
        session_id="sess-A",
        last_activity=stale_iso,
    )
    await idle_service.db.insert_claims_batch(
        engineer="bob",
        branch=None,
        description=None,
        items=[("touch-B", "file", "src/b.py", "soft", far_future)],
        repo="amittell/coord",
        session_id="sess-B",
        last_activity=stale_iso,
    )

    await idle_service.check_conflicts(
        patterns=["src/c.py"],
        engineer="alice",
        repo="amittell/coord",
        session_ids=["sess-A", "sess-B"],
    )

    rows = await idle_service.db.list_active_claims_rows()
    by_id = {r["id"]: r for r in rows}
    assert by_id["touch-A"]["last_activity"] != stale_iso
    assert by_id["touch-B"]["last_activity"] != stale_iso


@pytest.mark.asyncio
async def test_pending_requests_returns_conflicts_against_held_claims(
    idle_service: CoordinationService,
) -> None:
    """When a different session is blocked by my session's claim, my
    `pending_requests` view must include that attempt with the
    requester's engineer and pattern, so I can decide whether to
    release."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    holder = await idle_service.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo="amittell/coord",
            session_id="holder-sess",
            claims=[ClaimItem(type="module", pattern="server/**")],
        )
    )
    assert holder.claim_ids

    # Different session attempts overlapping pattern; gets blocked.
    blocked = await idle_service.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            repo="amittell/coord",
            session_id="requester-sess",
            claims=[ClaimItem(type="module", pattern="server/**")],
        )
    )
    assert blocked.conflicts, "second session should have been blocked"

    pending = await idle_service.pending_requests("holder-sess")
    assert len(pending) >= 1, f"holder must see the blocked attempt; got {pending}"
    one = pending[0]
    assert one["attempted_by"] == "bob"
    assert one["attempted_pattern"] == "server/**"
    # The conflict log records which session attempted, so the holder
    # can distinguish its own subagents from a foreign session.
    assert one.get("attempted_session_id") == "requester-sess"


@pytest.mark.asyncio
async def test_pending_requests_excludes_other_sessions_inbox(
    idle_service: CoordinationService,
) -> None:
    """My pending-requests inbox must only show conflicts logged
    against MY claims. Conflicts on someone else's claims must not
    leak into my view."""
    from coordination.schemas import ClaimItem, CreateClaimsRequest

    # Two unrelated sessions each hold something.
    for sess, eng, pat in [
        ("session-a", "alice", "a/**"),
        ("session-b", "bob", "b/**"),
    ]:
        await idle_service.create_claims(
            CreateClaimsRequest(
                engineer=eng,
                repo="amittell/coord",
                session_id=sess,
                claims=[ClaimItem(type="module", pattern=pat)],
            )
        )

    # A third session conflicts against session-a only.
    await idle_service.create_claims(
        CreateClaimsRequest(
            engineer="carol",
            repo="amittell/coord",
            session_id="session-c",
            claims=[ClaimItem(type="module", pattern="a/**")],
        )
    )

    a_pending = await idle_service.pending_requests("session-a")
    b_pending = await idle_service.pending_requests("session-b")

    assert any(p["attempted_by"] == "carol" for p in a_pending)
    assert all(p["attempted_by"] != "carol" for p in b_pending), (
        f"session-b must not see conflicts against session-a; got {b_pending}"
    )
