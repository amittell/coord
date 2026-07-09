"""Audit: repo-scope the rename auto-follow sweep.

On a shared multi-repo service the rename sweep walks ACTIVE claims from
every repo but resolves their file paths against the single checkout at
``COORD_REPO_ROOT``. Without a declared repo identity for that checkout,
a claim from another repo whose relative path happens to exist under the
root could be "renamed" based on the wrong repo's file content.

``COORD_REPO_ROOT_REPO`` declares which repo id the checkout represents:

- set: only claims tagged with that exact repo id are swept; claims from
  other repos AND legacy NULL-repo claims are excluded.
- unset (default): the single-repo behaviour is preserved -- every claim
  is swept against ``repo_root``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordination.config import Settings
from coordination.db import Database
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "mod.py").write_text(
        "def handler():\n    return 1\n", encoding="utf-8"
    )
    return repo


async def _make_sweep_service(
    tmp_path: Path, repo: Path, **settings_overrides
) -> CoordinationService:
    """Sweep-oriented service: LSP nominally enabled (the sweep is gated
    on it) but pointing at a nonexistent binary, so claim-time spans and
    the sweep's re-extraction both ride the parser path."""
    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    defaults = dict(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=repo,
        max_claim_ratio=1.0,
        lsp_enabled=True,
        lsp_command_python="/nonexistent/coord-test-lsp-binary",
    )
    defaults.update(settings_overrides)
    return CoordinationService(db=db, settings=Settings(**defaults))


async def _claim_handler(
    svc: CoordinationService, *, repo_id: str | None
) -> str:
    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            repo=repo_id,
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )
    assert result.claim_ids, f"claim rejected: {result.warnings!r}"
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)
    return result.claim_ids[0]


def _rename_on_disk(repo: Path) -> None:
    (repo / "mod.py").write_text(
        "def handle_event():\n    return 1\n", encoding="utf-8"
    )


async def _symbol_names(svc: CoordinationService, cid: str) -> list[str]:
    rows = await svc.db.get_claim_symbols(cid)
    return sorted(str(r["symbol_name"]) for r in rows)


async def test_sweep_skips_claims_from_other_repos_when_root_repo_set(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(
        tmp_path, repo, repo_root_repo="example-org/this-repo"
    )
    cid = await _claim_handler(svc, repo_id="example-org/other-repo")
    _rename_on_disk(repo)

    assert await svc.rename_sweep() == 0
    assert await _symbol_names(svc, cid) == ["handler"]


async def test_sweep_skips_null_repo_claims_when_root_repo_set(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(
        tmp_path, repo, repo_root_repo="example-org/this-repo"
    )
    cid = await _claim_handler(svc, repo_id=None)
    _rename_on_disk(repo)

    assert await svc.rename_sweep() == 0
    assert await _symbol_names(svc, cid) == ["handler"]


async def test_sweep_follows_matching_repo_when_root_repo_set(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(
        tmp_path, repo, repo_root_repo="example-org/this-repo"
    )
    cid = await _claim_handler(svc, repo_id="example-org/this-repo")
    _rename_on_disk(repo)

    assert await svc.rename_sweep() == 1
    assert await _symbol_names(svc, cid) == ["handle_event"]


async def test_sweep_unscoped_default_preserves_single_repo_behaviour(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(tmp_path, repo)
    assert svc.settings.repo_root_repo is None
    cid = await _claim_handler(svc, repo_id="example-org/any-repo")
    _rename_on_disk(repo)

    assert await svc.rename_sweep() == 1
    assert await _symbol_names(svc, cid) == ["handle_event"]
