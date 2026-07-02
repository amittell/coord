"""Server-side repo-scope enforcement for repo-bound tokens (#30 slice 2/3).

The foundation (schema v19 + auth threading) binds a token to a repo; these
tests pin the ENFORCEMENT: a scoped token may only read/write its own repo,
regardless of what the client sends, while an unscoped (operator / shared)
token is unaffected. This file covers the query/body layer (claims list,
conflicts, metrics, /repos, create, promote). Id-addressed endpoints
(claims-by-id, requests, queue, sessions) are covered separately.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination.main import app

SHARED = "shared-test-token"
REPO_A = "amittell/repo-a"
REPO_B = "amittell/repo-b"


def _sha256(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


@pytest.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _mint(repo: str | None, engineer: str = "eng") -> str:
    """Mint a token (scoped when repo is set) and return the raw value."""
    from coordination.deps import get_service

    raw = "coordt_" + hashlib.sha256((repo or "none").encode()).hexdigest()
    await get_service().db.create_engineer_token(engineer, _sha256(raw), repo=repo)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _seed_claim(client: AsyncClient, repo: str, pattern: str) -> None:
    # Seed via the unscoped shared token so the claim is tagged to `repo`.
    r = await client.post(
        "/claims",
        headers={"Authorization": f"Bearer {SHARED}"},
        json={
            "engineer": "seed",
            "repo": repo,
            "claims": [{"type": "file", "pattern": pattern}],
        },
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def test_scoped_token_list_claims_sees_only_its_repo(client: AsyncClient) -> None:
    await _seed_claim(client, REPO_A, "src/a.py")
    await _seed_claim(client, REPO_B, "src/b.py")
    raw = await _mint(REPO_A)

    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    repos = {c["repo"] for c in r.json()["claims"]}
    assert repos == {REPO_A}, repos


async def test_scoped_token_list_claims_403_on_explicit_other_repo(
    client: AsyncClient,
) -> None:
    raw = await _mint(REPO_A)
    r = await client.get(f"/claims?repo={REPO_B}", headers=_auth(raw))
    assert r.status_code == 403, r.text


async def test_scoped_token_list_claims_absent_repo_is_silently_scoped(
    client: AsyncClient,
) -> None:
    await _seed_claim(client, REPO_B, "src/b.py")
    raw = await _mint(REPO_A)
    # No repo param -> silently scoped to REPO_A (stale clients still enforced).
    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    assert r.json()["claims"] == []


async def test_scoped_token_repos_lists_only_its_repo(client: AsyncClient) -> None:
    await _seed_claim(client, REPO_A, "src/a.py")
    await _seed_claim(client, REPO_B, "src/b.py")
    raw = await _mint(REPO_A)
    r = await client.get("/repos", headers=_auth(raw))
    assert r.status_code == 200, r.text
    repos = {row["repo"] for row in r.json()["repos"]}
    assert repos == {REPO_A}, repos


async def test_scope_response_header_present_for_scoped_token(
    client: AsyncClient,
) -> None:
    raw = await _mint(REPO_A)
    r = await client.get("/claims", headers=_auth(raw))
    assert r.headers.get("X-Coord-Repo-Scope") == REPO_A


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


async def test_scoped_token_create_defaults_repo_to_token_repo(
    client: AsyncClient,
) -> None:
    raw = await _mint(REPO_A)
    r = await client.post(
        "/claims",
        headers=_auth(raw),
        json={"engineer": "eng", "claims": [{"type": "file", "pattern": "src/x.py"}]},
    )
    assert r.status_code == 200, r.text
    # The claim is visible to the scoped token, meaning it was tagged REPO_A.
    listed = await client.get("/claims", headers=_auth(raw))
    patterns = {c["pattern"] for c in listed.json()["claims"]}
    assert "src/x.py" in patterns


async def test_scoped_token_create_403_on_repo_mismatch(client: AsyncClient) -> None:
    raw = await _mint(REPO_A)
    r = await client.post(
        "/claims",
        headers=_auth(raw),
        json={
            "engineer": "eng",
            "repo": REPO_B,
            "claims": [{"type": "file", "pattern": "src/x.py"}],
        },
    )
    assert r.status_code == 403, r.text


async def test_scoped_token_promote_hotspot_is_operator_only(
    client: AsyncClient,
) -> None:
    raw = await _mint(REPO_A)
    r = await client.post(
        "/metrics/hotspots/promote",
        headers=_auth(raw),
        json={"action": "shared_file", "pattern": "src/x.py", "repo": REPO_A},
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# back-compat: unscoped / shared tokens are unaffected
# ---------------------------------------------------------------------------


async def test_unscoped_token_sees_all_repos(client: AsyncClient) -> None:
    await _seed_claim(client, REPO_A, "src/a.py")
    await _seed_claim(client, REPO_B, "src/b.py")
    raw = await _mint(None, engineer="operator")  # NULL repo = unscoped
    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    repos = {c["repo"] for c in r.json()["claims"]}
    assert repos == {REPO_A, REPO_B}, repos


async def test_shared_token_can_still_target_a_specific_repo(
    client: AsyncClient,
) -> None:
    await _seed_claim(client, REPO_A, "src/a.py")
    await _seed_claim(client, REPO_B, "src/b.py")
    # Unscoped operator may still filter by an explicit repo, and promote.
    r = await client.get(
        f"/claims?repo={REPO_B}", headers={"Authorization": f"Bearer {SHARED}"}
    )
    assert r.status_code == 200, r.text
    assert {c["repo"] for c in r.json()["claims"]} == {REPO_B}
