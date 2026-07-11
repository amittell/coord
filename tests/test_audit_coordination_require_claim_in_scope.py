"""PG-backend guard for main.py's repo-scope enforcement over the fixed
``_connect`` seam.

``_require_claim_in_scope`` decides a scoped token's cross-repo 403 by asking
``Database.claim_repo`` which repo a claim belongs to. That lookup formerly
opened a raw ``aiosqlite.connect(self.path)``; on the Postgres backend it then
consulted a stray/empty local SQLite file instead of the real store, so the
guard either 500'd (fresh deploy, no file) or read stale rows and skipped the
403 (a cross-repo leak). The fix routes ``claim_repo`` through the overridable
``_connect`` seam that ``PostgresStore`` implements.

This test drives the enforcement end to end against whatever backend the
harness selects -- crucially the real ``PostgresStore`` under
``COORD_DATABASE_URL`` -- so a regression that reintroduces a raw connect (or a
seam that returns the wrong repo on PG) fails here. The conftest aiosqlite shim
would MASK a db-layer raw connect by rerouting it to the PG schema; because
``claim_repo`` now uses the seam, these assertions hold even with the shim
disabled (``COORD_TEST_DISABLE_AIOSQLITE_SHIM=1``), the shim-off variant this
file is meant to guard on Postgres.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from coordination.main import _require_claim_in_scope

REPO_A = "acme/app"
REPO_B = "acme/other"


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _request(token_repo: str | None):
    """Minimal stand-in for a Starlette ``Request``: the scope helpers read
    only ``request.state.token_repo`` (via ``main._token_repo``)."""
    return types.SimpleNamespace(
        state=types.SimpleNamespace(token_repo=token_repo)
    )


@pytest.fixture()
async def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh cached service backed by the harness-selected store. In PG mode
    the ``COORD_DATABASE_PATH`` derives a unique schema, so the seed and the
    ``claim_repo`` lookup share one isolated PostgresStore schema."""
    monkeypatch.setenv("COORD_AUTH_TOKEN", "scope-seam-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    svc = deps.get_service()
    await svc.db.init()
    try:
        yield svc
    finally:
        deps.get_service.cache_clear()


async def _seed_claim(svc, *, repo: str | None) -> str:
    cid = str(uuid4())
    exp = _iso(datetime.now(UTC) + timedelta(hours=1))
    await svc.db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[(cid, "file", "src/app.py", "soft", exp)],
        repo=repo,
    )
    return cid


async def test_scoped_token_cross_repo_claim_is_403(service) -> None:
    # The claim lives in REPO_A; a REPO_B-scoped token must be refused. On PG
    # this proves claim_repo read the real store (not an empty SQLite file,
    # which would have returned (False, None) and let the request through).
    cid = await _seed_claim(service, repo=REPO_A)
    with pytest.raises(HTTPException) as ei:
        await _require_claim_in_scope(_request(REPO_B), cid)
    assert ei.value.status_code == 403


async def test_scoped_token_same_repo_claim_passes(service) -> None:
    cid = await _seed_claim(service, repo=REPO_A)
    # No exception: the claim is in the token's own repo.
    await _require_claim_in_scope(_request(REPO_A), cid)


async def test_unknown_claim_falls_through_no_403(service) -> None:
    # A missing id must NOT 403 -- the handler's own 404 / no-op runs instead,
    # so a typo never becomes a cross-repo existence oracle.
    await _seed_claim(service, repo=REPO_A)
    await _require_claim_in_scope(_request(REPO_A), "no-such-claim-id")


async def test_unscoped_token_never_scoped(service) -> None:
    cid = await _seed_claim(service, repo=REPO_A)
    # token_repo None (operator / shared token): every repo is in scope, so the
    # guard returns without consulting the store at all.
    await _require_claim_in_scope(_request(None), cid)
