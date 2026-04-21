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
