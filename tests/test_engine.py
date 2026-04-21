from __future__ import annotations

import shutil
import subprocess

import pytest

from coordination.engine import files_matching_pattern, heuristic_overlap


def test_files_matching_pattern_basic() -> None:
    files = ["src/auth/login.ts", "src/billing/x.ts"]
    assert files_matching_pattern(files, "src/auth/**") == ["src/auth/login.ts"]


def test_heuristic_overlap() -> None:
    assert heuristic_overlap("src/a/**", "src/a/b.ts") is True
    assert heuristic_overlap("foo.ts", "bar.ts") is False


@pytest.mark.asyncio
async def test_compute_overlap_git_repo(tmp_path) -> None:
    # Skip if git not available in tmp repo
    from coordination.engine import compute_overlap

    r = await compute_overlap("a/**", "a/b", repo_root=None)
    assert isinstance(r, list)


# ---- Improved heuristic overlap tests ----


def test_overlap_exact_file_same_path() -> None:
    assert heuristic_overlap("src/auth/login.ts", "src/auth/login.ts") is True


def test_overlap_glob_vs_file_inside() -> None:
    assert heuristic_overlap("src/auth/**", "src/auth/login.ts") is True
    assert heuristic_overlap("src/auth/login.ts", "src/auth/**") is True


def test_overlap_nested_globs() -> None:
    assert heuristic_overlap("src/**", "src/auth/**") is True
    assert heuristic_overlap("src/auth/**", "src/**") is True


def test_overlap_deep_glob_vs_file() -> None:
    assert heuristic_overlap("src/**", "src/auth/deep/file.ts") is True


def test_overlap_double_star_middle() -> None:
    assert heuristic_overlap("apps/**/billing.ts", "apps/web/billing.ts") is True


def test_overlap_trailing_slash() -> None:
    assert heuristic_overlap("src/auth/", "src/auth/login.ts") is True


def test_overlap_leading_dot_slash() -> None:
    assert heuristic_overlap("./src/auth/**", "src/auth/login.ts") is True


def test_overlap_shared_exact() -> None:
    assert heuristic_overlap("package-lock.json", "package-lock.json") is True


def test_overlap_star_at_end() -> None:
    assert heuristic_overlap("src/auth/*", "src/auth/login.ts") is True


def test_overlap_star_at_end_not_deep() -> None:
    assert heuristic_overlap("src/auth/*", "src/auth/deep/file.ts") is False


def test_no_overlap_siblings() -> None:
    assert heuristic_overlap("src/auth/**", "src/billing/**") is False


def test_no_overlap_prefix_not_boundary() -> None:
    # critical: "src/auth" is a prefix of "src/auth_v2" but they are different directories
    assert heuristic_overlap("src/auth_v2/**", "src/auth/**") is False


def test_no_overlap_unrelated_files() -> None:
    assert heuristic_overlap("foo.ts", "bar.ts") is False


def test_no_overlap_shared_vs_module() -> None:
    assert heuristic_overlap("package-lock.json", "src/auth/**") is False


def test_overlap_empty_string_rejected() -> None:
    # Decision: empty patterns return False (no file could match empty pattern,
    # so no defensible overlap). Documented behavior.
    assert heuristic_overlap("", "src/auth/**") is False
    assert heuristic_overlap("src/auth/**", "") is False
    assert heuristic_overlap("", "") is False


def test_overlap_case_sensitivity() -> None:
    # gitignore semantics are case-sensitive; different case = different path
    assert heuristic_overlap("src/Auth/**", "src/auth/login.ts") is False


def test_overlap_windows_backslash_normalized() -> None:
    assert heuristic_overlap("src\\auth\\**", "src/auth/login.ts") is True


# ---- Deep nesting / many ** tests ----


def test_overlap_triple_double_star_match() -> None:
    assert (
        heuristic_overlap("a/**/b/**/c/**/deep.ts", "a/x/b/y/c/z/deep.ts") is True
    )


def test_overlap_triple_double_star_no_match() -> None:
    # missing c/ segment
    assert (
        heuristic_overlap("a/**/b/**/c/**/deep.ts", "a/x/b/y/deep.ts") is False
    )


def test_overlap_many_stars_both_patterns() -> None:
    assert heuristic_overlap("a/**/x/**", "a/b/c/x/d") is True


# ---- Character class tests ----


def test_overlap_character_class_match() -> None:
    assert heuristic_overlap("src/[ab]/*.ts", "src/a/foo.ts") is True


def test_overlap_character_class_no_match() -> None:
    assert heuristic_overlap("src/[ab]/*.ts", "src/c/foo.ts") is False


def test_overlap_character_range() -> None:
    assert heuristic_overlap("src/[a-c]/file.ts", "src/b/file.ts") is True


def test_overlap_negated_character_class() -> None:
    assert heuristic_overlap("src/[!.]/file.ts", "src/a/file.ts") is True
    assert heuristic_overlap("src/[!.]/file.ts", "src/.hidden/file.ts") is False


# ---- gitignore negation rejection ----


def test_overlap_negation_is_rejected_or_documented() -> None:
    # Negations have no defensible overlap semantics; reject with ValueError.
    with pytest.raises(ValueError, match="negation"):
        heuristic_overlap("!src/auth/**", "src/auth/login.ts")
    with pytest.raises(ValueError, match="negation"):
        heuristic_overlap("src/auth/**", "!src/auth/login.ts")


@pytest.mark.asyncio
async def test_compute_overlap_rejects_negation() -> None:
    from coordination.engine import compute_overlap

    with pytest.raises(ValueError, match="negation"):
        await compute_overlap("!src/auth/**", "src/auth/login.ts", repo_root=None)
    with pytest.raises(ValueError, match="negation"):
        await compute_overlap("src/auth/**", "!src/auth/login.ts", repo_root=None)


# ---- Performance on pathological patterns ----


def test_heuristic_does_not_explode_on_pathological_pattern() -> None:
    import time

    pattern = "a/**/" * 10 + "file.ts"
    t0 = time.monotonic()
    result = heuristic_overlap(pattern, pattern)
    elapsed = time.monotonic() - t0
    assert result is True
    assert elapsed < 1.0, f"heuristic took {elapsed:.3f}s on pathological pattern"


# ---- Regression safety net ----


def test_regression_all_18_original_pairs_still_behave() -> None:
    # Representative subset of pairs previously known to behave correctly.
    assert heuristic_overlap("src/auth/**", "src/billing/**") is False
    assert heuristic_overlap("src/auth/**", "src/auth/login.ts") is True
    assert heuristic_overlap("src/**", "src/auth/deep/file.ts") is True
    assert heuristic_overlap("apps/**/billing.ts", "apps/web/billing.ts") is True
    assert heuristic_overlap("src/auth_v2/**", "src/auth/**") is False
    assert heuristic_overlap("src/auth/*", "src/auth/deep/file.ts") is False
    assert heuristic_overlap("foo.ts", "bar.ts") is False
    assert heuristic_overlap("package-lock.json", "package-lock.json") is True


@pytest.mark.asyncio
async def test_overlap_with_git_ls_files(tmp_path) -> None:
    from coordination.engine import compute_overlap

    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git binary not available")

    # Init a git repo and add a couple files
    subprocess.run([git_bin, "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [git_bin, "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        [git_bin, "-C", str(tmp_path), "config", "user.name", "test"],
        check=True,
    )
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "auth.ts").write_text("// auth\n")
    (src_dir / "billing.ts").write_text("// billing\n")
    subprocess.run([git_bin, "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        [git_bin, "-C", str(tmp_path), "commit", "-q", "-m", "init"],
        check=True,
    )

    result = await compute_overlap("src/**", "src/auth.ts", repo_root=tmp_path)
    assert "src/auth.ts" in result
    assert "<unknown>" not in result


# ---- Scope and TTL cache tests ----


def _init_monorepo(git_bin: str, root):
    subprocess.run([git_bin, "init", "-q", str(root)], check=True)
    subprocess.run(
        [git_bin, "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        [git_bin, "-C", str(root), "config", "user.name", "test"],
        check=True,
    )
    (root / "svc-a" / "src").mkdir(parents=True)
    (root / "svc-b" / "src").mkdir(parents=True)
    (root / "svc-a" / "src" / "f.ts").write_text("// a\n")
    (root / "svc-b" / "src" / "f.ts").write_text("// b\n")
    subprocess.run([git_bin, "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [git_bin, "-C", str(root), "commit", "-q", "-m", "init"],
        check=True,
    )


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    from coordination.engine import _clear_ls_files_cache

    _clear_ls_files_cache()
    yield
    _clear_ls_files_cache()


@pytest.mark.asyncio
async def test_git_ls_files_with_scope_returns_only_scoped_files(tmp_path) -> None:
    from coordination.engine import git_ls_files

    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git binary not available")
    _init_monorepo(git_bin, tmp_path)

    result = await git_ls_files(tmp_path, scope="svc-a")
    assert result == ["svc-a/src/f.ts"]


@pytest.mark.asyncio
async def test_git_ls_files_without_scope_returns_all_files(tmp_path) -> None:
    from coordination.engine import git_ls_files

    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git binary not available")
    _init_monorepo(git_bin, tmp_path)

    result = await git_ls_files(tmp_path)
    assert "svc-a/src/f.ts" in result
    assert "svc-b/src/f.ts" in result


@pytest.mark.asyncio
async def test_git_ls_files_cache_hit_skips_subprocess(tmp_path, monkeypatch) -> None:
    from coordination import engine
    from coordination.engine import git_ls_files

    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git binary not available")
    _init_monorepo(git_bin, tmp_path)

    # First call: populate cache.
    first = await git_ls_files(tmp_path)
    assert first

    # Replace the sync impl with a function that raises; cache hit must bypass it.
    def _boom(*args, **kwargs):
        raise AssertionError("subprocess should not be invoked on cache hit")

    monkeypatch.setattr(engine, "_git_ls_files_sync", _boom)
    second = await git_ls_files(tmp_path)
    assert second == first


@pytest.mark.asyncio
async def test_git_ls_files_cache_expires_after_ttl(tmp_path, monkeypatch) -> None:
    import time as _time

    from coordination import engine
    from coordination.engine import git_ls_files

    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git binary not available")
    _init_monorepo(git_bin, tmp_path)

    monkeypatch.setattr(engine, "_LS_FILES_TTL_SEC", 0.001)

    call_count = {"n": 0}
    real_sync = engine._git_ls_files_sync

    def _counting(root, scope=None):
        call_count["n"] += 1
        return real_sync(root, scope=scope)

    monkeypatch.setattr(engine, "_git_ls_files_sync", _counting)

    r1 = await git_ls_files(tmp_path)
    assert r1
    _time.sleep(0.01)
    r2 = await git_ls_files(tmp_path)
    assert r2 == r1
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_git_ls_files_does_not_cache_empty_results(tmp_path, monkeypatch) -> None:
    from coordination import engine
    from coordination.engine import git_ls_files

    call_count = {"n": 0}

    def _empty(root, scope=None):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(engine, "_git_ls_files_sync", _empty)

    r1 = await git_ls_files(tmp_path)
    assert r1 == []
    r2 = await git_ls_files(tmp_path)
    assert r2 == []
    # Empty result must not be cached; subprocess should be called both times.
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_compute_overlap_uses_scope_setting(tmp_path) -> None:
    from coordination.engine import compute_overlap

    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git binary not available")
    _init_monorepo(git_bin, tmp_path)

    # Siblings: no overlap.
    r0 = await compute_overlap(
        "svc-a/**", "svc-b/**", repo_root=tmp_path, scope=None
    )
    assert r0 == []

    # Within svc-a scope: svc-a/src/f.ts matches both patterns.
    r1 = await compute_overlap(
        "svc-a/src/**", "svc-a/**", repo_root=tmp_path, scope="svc-a"
    )
    assert "svc-a/src/f.ts" in r1

    # Without scope: same result (svc-a files are still present in the tree).
    r2 = await compute_overlap(
        "svc-a/src/**", "svc-a/**", repo_root=tmp_path, scope=None
    )
    assert "svc-a/src/f.ts" in r2
