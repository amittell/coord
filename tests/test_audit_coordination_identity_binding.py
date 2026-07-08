"""Audit: bind the client-supplied ``engineer`` on mutating endpoints to
the authenticated per-engineer token identity (main.py delete_claim /
release_claims / cancel_request / respond_to_request).

Covers COORD_ENFORCE_ENGINEER_IDENTITY=warn (default: honored, logged,
``X-Coord-Identity-Warning`` response header) and =enforce (403 on
mismatch, omitted engineer defaulted to the token identity), plus the
immediate holder-authorization check on POST /requests/{id}/respond
(a requester must not be able to self-approve a request it filed against
someone else's claim). Shared operator tokens stay exempt throughout.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination.main import app

SHARED = "shared-test-token"


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
    monkeypatch.delenv("COORD_ENFORCE_ENGINEER_IDENTITY", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


async def _mint(engineer: str) -> str:
    """Mint an unscoped per-engineer token and return the raw value."""
    from coordination.deps import get_service

    raw = "coordt_" + hashlib.sha256(f"identity-{engineer}".encode()).hexdigest()
    await get_service().db.create_engineer_token(engineer, _sha256(raw))
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


_SHARED_AUTH = {"Authorization": f"Bearer {SHARED}"}


async def _seed_claim(
    client: AsyncClient, engineer: str, pattern: str
) -> str:
    r = await client.post(
        "/claims",
        headers=_SHARED_AUTH,
        json={
            "engineer": engineer,
            "claims": [{"type": "file", "pattern": pattern}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["claim_ids"][0]


async def _claim_active(client: AsyncClient, claim_id: str) -> bool:
    r = await client.get("/claims", headers=_SHARED_AUTH)
    assert r.status_code == 200, r.text
    return any(c["id"] == claim_id for c in r.json()["claims"])


async def _file_request(client: AsyncClient, claim_id: str, requester: str) -> str:
    r = await client.post(
        "/requests",
        headers=_SHARED_AUTH,
        json={"claim_id": claim_id, "requester": requester, "wait_seconds": 0},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# warn mode (default)
# ---------------------------------------------------------------------------


async def test_warn_mode_mismatch_is_honored_with_warning_header(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim(client, "bob", "src/warn-mismatch.py")
    alice = await _mint("alice")
    r = await client.delete(f"/claims/{cid}?engineer=bob", headers=_auth(alice))
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1
    warning = r.headers.get("X-Coord-Identity-Warning", "")
    assert "alice" in warning and "bob" in warning


async def test_warn_mode_omitted_engineer_is_honored_with_warning_header(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim(client, "bob", "src/warn-omitted.py")
    alice = await _mint("alice")
    r = await client.delete(f"/claims/{cid}", headers=_auth(alice))
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1
    warning = r.headers.get("X-Coord-Identity-Warning", "")
    assert "<omitted>" in warning and "alice" in warning


async def test_warn_mode_matching_engineer_has_no_warning(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim(client, "alice", "src/warn-match.py")
    alice = await _mint("alice")
    r = await client.delete(f"/claims/{cid}?engineer=alice", headers=_auth(alice))
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1
    assert "X-Coord-Identity-Warning" not in r.headers


async def test_warn_mode_shared_token_never_warns(client: AsyncClient) -> None:
    cid = await _seed_claim(client, "bob", "src/warn-shared.py")
    r = await client.delete(f"/claims/{cid}?engineer=bob", headers=_SHARED_AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1
    assert "X-Coord-Identity-Warning" not in r.headers


# ---------------------------------------------------------------------------
# enforce mode
# ---------------------------------------------------------------------------


async def test_enforce_mode_mismatch_is_403(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_ENFORCE_ENGINEER_IDENTITY", "enforce")
    cid = await _seed_claim(client, "bob", "src/enforce-mismatch.py")
    alice = await _mint("alice")
    r = await client.delete(f"/claims/{cid}?engineer=bob", headers=_auth(alice))
    assert r.status_code == 403, r.text
    assert await _claim_active(client, cid)


async def test_enforce_mode_omitted_engineer_scopes_to_token_identity(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_ENFORCE_ENGINEER_IDENTITY", "enforce")
    cid = await _seed_claim(client, "bob", "src/enforce-omitted.py")
    alice = await _mint("alice")
    # The omitted engineer defaults to 'alice', so the ownership
    # predicate no longer matches bob's claim: nothing is released.
    r = await client.delete(f"/claims/{cid}", headers=_auth(alice))
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 0
    assert await _claim_active(client, cid)


async def test_enforce_mode_own_claim_with_omitted_engineer_releases(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_ENFORCE_ENGINEER_IDENTITY", "enforce")
    cid = await _seed_claim(client, "alice", "src/enforce-own.py")
    alice = await _mint("alice")
    r = await client.delete(f"/claims/{cid}", headers=_auth(alice))
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1


async def test_enforce_mode_shared_token_stays_exempt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_ENFORCE_ENGINEER_IDENTITY", "enforce")
    cid = await _seed_claim(client, "bob", "src/enforce-shared.py")
    r = await client.delete(f"/claims/{cid}?engineer=bob", headers=_SHARED_AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1


async def test_enforce_mode_bulk_release_body_engineer_mismatch_is_403(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_ENFORCE_ENGINEER_IDENTITY", "enforce")
    cid = await _seed_claim(client, "bob", "src/enforce-bulk.py")
    alice = await _mint("alice")
    r = await client.post(
        "/claims/release",
        headers=_auth(alice),
        json={"claim_ids": [cid], "engineer": "bob"},
    )
    assert r.status_code == 403, r.text
    assert await _claim_active(client, cid)


async def test_enforce_mode_queue_cancel_engineer_mismatch_is_403(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_ENFORCE_ENGINEER_IDENTITY", "enforce")
    alice = await _mint("alice")
    r = await client.delete(
        "/requests/some-queue-id?engineer=bob", headers=_auth(alice)
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# holder authorization on respond (immediate, per-engineer tokens only)
# ---------------------------------------------------------------------------


async def test_respond_requester_cannot_self_approve(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim(client, "bob", "src/respond-selfapprove.py")
    rid = await _file_request(client, cid, requester="mallory")
    mallory = await _mint("mallory")
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_auth(mallory),
        json={"decision": "approved", "engineer": "mallory"},
    )
    assert r.status_code == 403, r.text
    assert "bob" in r.json()["detail"]
    assert await _claim_active(client, cid)


async def test_respond_omitted_engineer_defaults_to_token_identity(
    client: AsyncClient,
) -> None:
    # An omitted actor defaults to the authenticated identity: the
    # requester's token is not the holder, so the self-approve is 403.
    cid = await _seed_claim(client, "bob", "src/respond-omitted.py")
    rid = await _file_request(client, cid, requester="mallory")
    mallory = await _mint("mallory")
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_auth(mallory),
        json={"decision": "approved"},
    )
    assert r.status_code == 403, r.text
    assert await _claim_active(client, cid)


async def test_respond_holder_token_with_omitted_engineer_succeeds(
    client: AsyncClient,
) -> None:
    # The standard MCP holder flow sends no ``engineer`` on respond; the
    # actor defaults to the token identity, which IS the holder, so the
    # decision lands cleanly with no identity warning.
    cid = await _seed_claim(client, "bob", "src/respond-mcp-flow.py")
    rid = await _file_request(client, cid, requester="mallory")
    bob = await _mint("bob")
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_auth(bob),
        json={"decision": "approved"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "approved"
    assert r.json()["decided_by_engineer"] == "bob"
    assert "X-Coord-Identity-Warning" not in r.headers
    assert not await _claim_active(client, cid)


async def test_respond_naming_the_holder_is_allowed(
    client: AsyncClient,
) -> None:
    # Warn mode: the named actor (the holder) is what is authorized; the
    # token/engineer mismatch only adds the identity warning header.
    cid = await _seed_claim(client, "bob", "src/respond-holder-named.py")
    rid = await _file_request(client, cid, requester="mallory")
    bobs_agent = await _mint("bob-agent")
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_auth(bobs_agent),
        json={"decision": "approved", "engineer": "bob"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "approved"
    assert "X-Coord-Identity-Warning" in r.headers
    assert not await _claim_active(client, cid)


async def test_respond_holder_with_own_token_succeeds(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim(client, "bob", "src/respond-holder-token.py")
    rid = await _file_request(client, cid, requester="mallory")
    bob = await _mint("bob")
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_auth(bob),
        json={"decision": "denied", "engineer": "bob"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "denied"
    assert "X-Coord-Identity-Warning" not in r.headers
    assert await _claim_active(client, cid)


async def test_respond_shared_operator_token_stays_exempt(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim(client, "bob", "src/respond-operator.py")
    rid = await _file_request(client, cid, requester="mallory")
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_SHARED_AUTH,
        json={"decision": "approved", "engineer": "operator"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "approved"
    assert not await _claim_active(client, cid)


async def test_respond_enforce_mode_binds_actor_before_holder_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With enforce on, a requester cannot even NAME the holder: the
    # actor is bound to the token identity first, so the lie is a 403.
    monkeypatch.setenv("COORD_ENFORCE_ENGINEER_IDENTITY", "enforce")
    cid = await _seed_claim(client, "bob", "src/respond-enforce.py")
    rid = await _file_request(client, cid, requester="mallory")
    mallory = await _mint("mallory")
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_auth(mallory),
        json={"decision": "approved", "engineer": "bob"},
    )
    assert r.status_code == 403, r.text
    assert await _claim_active(client, cid)
