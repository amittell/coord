"""Audit regression tests for coordination/engine.py.

Covers two findings:

- engine.py:17 -- ``_git_ls_files_sync`` must swallow subprocess failures
  (git binary missing, 120s hang) and degrade to the heuristic overlap
  path instead of surfacing a 500 on the live claim/conflict path.
- engine.py:285 -- ``heuristic_overlap`` had glob-vs-glob false negatives
  (``src/*_test.py`` vs ``src/test_*.py`` both match
  ``src/test_x_test.py`` but were reported non-conflicting). Fixed with
  targeted multi-candidate expansion that borrows literal fragments from
  the peer pattern; the expansion must NOT flip known-disjoint pairs to
  overlap=True.
"""

from __future__ import annotations

import subprocess

import pytest

from coordination import engine
from coordination.engine import (
    _clear_ls_files_cache,
    _git_ls_files_sync,
    compute_overlap,
    heuristic_overlap,
)


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    _clear_ls_files_cache()
    yield
    _clear_ls_files_cache()


# ---------------------------------------------------------------------------
# _git_ls_files_sync error handling
# ---------------------------------------------------------------------------


def test_git_ls_files_sync_returns_empty_on_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=120)

    monkeypatch.setattr(engine.subprocess, "run", _hang)

    assert _git_ls_files_sync(tmp_path) == []


def test_git_ls_files_sync_returns_empty_when_git_binary_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(engine.subprocess, "run", _missing)

    assert _git_ls_files_sync(tmp_path) == []


@pytest.mark.asyncio
async def test_compute_overlap_degrades_to_heuristic_on_git_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git failure must fall through to heuristic_overlap, not raise."""

    def _boom(*args, **kwargs):
        raise OSError("no git in container image")

    monkeypatch.setattr(engine.subprocess, "run", _boom)

    result = await compute_overlap(
        "src/auth/**", "src/auth/login.ts", repo_root=tmp_path
    )
    assert result == ["<unknown>"]

    disjoint = await compute_overlap(
        "src/auth/**", "src/billing/**", repo_root=tmp_path
    )
    assert disjoint == []


# ---------------------------------------------------------------------------
# heuristic_overlap glob-vs-glob false negatives
# ---------------------------------------------------------------------------


def test_glob_vs_glob_prefix_suffix_collision_detected() -> None:
    # Both match src/test_x_test.py; the single 'x' placeholder missed it.
    assert heuristic_overlap("src/*_test.py", "src/test_*.py") is True
    assert heuristic_overlap("src/test_*.py", "src/*_test.py") is True


def test_glob_vs_glob_contains_collision_detected() -> None:
    # Both match 'ab' (and 'bab' as a witness with borrowed fragments).
    assert heuristic_overlap("*a*", "*b*") is True
    assert heuristic_overlap("*b*", "*a*") is True


def test_glob_vs_glob_symmetric_fragment_borrowing() -> None:
    # Witness 'axb' is only reachable by filling b's star with a's 'x'.
    assert heuristic_overlap("*x*", "a*b") is True
    assert heuristic_overlap("a*b", "*x*") is True


def test_glob_vs_glob_shared_prefix_collision() -> None:
    # Both match 'abc'.
    assert heuristic_overlap("a*c", "ab*") is True


def test_glob_vs_glob_disjoint_pairs_stay_false() -> None:
    # The expansion must not bias toward overlap=True wholesale.
    assert heuristic_overlap("a*c", "b*d") is False
    assert heuristic_overlap("src/*.py", "docs/*.md") is False
    assert heuristic_overlap("*.py", "*.md") is False
    assert heuristic_overlap("src/auth/**", "src/billing/**") is False
    assert heuristic_overlap("src/auth_v2/**", "src/auth/**") is False


def test_concrete_vs_glob_behaviour_unchanged() -> None:
    # Single-candidate probe is exact when one side is concrete; the
    # multi-candidate block must not fire for these.
    assert heuristic_overlap("src/auth/*", "src/auth/deep/file.ts") is False
    assert heuristic_overlap("src/auth/**", "src/auth/login.ts") is True
    assert heuristic_overlap("src/[!.]/file.ts", "src/.hidden/file.ts") is False
    assert heuristic_overlap("foo.ts", "bar.ts") is False


def test_glob_vs_glob_expansion_stays_fast() -> None:
    import time

    a = "a/**/" * 10 + "*_suffix_*.ts"
    b = "a/**/" * 10 + "*_other_*.md"
    t0 = time.monotonic()
    result = heuristic_overlap(a, b)
    elapsed = time.monotonic() - t0
    assert result is False
    assert elapsed < 1.0, f"expansion took {elapsed:.3f}s"
