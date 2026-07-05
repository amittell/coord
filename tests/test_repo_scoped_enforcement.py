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


async def _seed_claim_id(client: AsyncClient, repo: str, pattern: str) -> str:
    await _seed_claim(client, repo, pattern)
    r = await client.get(
        f"/claims?repo={repo}", headers={"Authorization": f"Bearer {SHARED}"}
    )
    for c in r.json()["claims"]:
        if c["pattern"] == pattern:
            return c["id"]
    raise AssertionError("seeded claim not found")


async def _file_request(client: AsyncClient, claim_id: str) -> str:
    r = await client.post(
        "/requests",
        headers={"Authorization": f"Bearer {SHARED}"},
        json={"claim_id": claim_id, "requester": "req", "wait_seconds": 0},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


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


# ---------------------------------------------------------------------------
# id-addressed endpoints: a scoped token cannot read/mutate another repo's
# claims / requests / queue / session by id.
# ---------------------------------------------------------------------------


async def test_scoped_token_cannot_delete_other_repo_claim(client: AsyncClient) -> None:
    cid = await _seed_claim_id(client, REPO_B, "src/b.py")
    raw = await _mint(REPO_A)
    r = await client.delete(f"/claims/{cid}", headers=_auth(raw))
    assert r.status_code == 403, r.text


async def test_scoped_token_can_delete_own_repo_claim(client: AsyncClient) -> None:
    cid = await _seed_claim_id(client, REPO_A, "src/a.py")
    raw = await _mint(REPO_A)
    r = await client.delete(f"/claims/{cid}", headers=_auth(raw))
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1


async def test_scoped_token_batch_release_403_on_other_repo(client: AsyncClient) -> None:
    cid = await _seed_claim_id(client, REPO_B, "src/b.py")
    raw = await _mint(REPO_A)
    r = await client.post(
        "/claims/release",
        headers=_auth(raw),
        json={"engineer": "eng", "claim_ids": [cid]},
    )
    assert r.status_code == 403, r.text


async def test_scoped_token_extend_403_on_other_repo(client: AsyncClient) -> None:
    cid = await _seed_claim_id(client, REPO_B, "src/b.py")
    raw = await _mint(REPO_A)
    r = await client.post(
        f"/claims/{cid}/extend",
        headers=_auth(raw),
        json={"engineer": "seed", "ttl_hours": 2},
    )
    assert r.status_code == 403, r.text


async def test_scoped_session_release_only_touches_own_repo(client: AsyncClient) -> None:
    # One session id spanning two repos: a REPO_A-scoped token must release
    # only REPO_A's claim, leaving REPO_B's intact.
    from coordination.deps import get_service

    svc = get_service()
    await svc.db.insert_claims_batch(
        engineer="a", branch=None, description=None,
        items=[("ca", "file", "src/a.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="shared-sess", repo=REPO_A,
    )
    await svc.db.insert_claims_batch(
        engineer="b", branch=None, description=None,
        items=[("cb", "file", "src/b.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="shared-sess", repo=REPO_B,
    )
    raw = await _mint(REPO_A)
    r = await client.post("/sessions/shared-sess/release", headers=_auth(raw))
    assert r.status_code == 200, r.text
    assert r.json()["released"] == 1
    # REPO_B's claim survived.
    survivors = await client.get(
        f"/claims?repo={REPO_B}", headers={"Authorization": f"Bearer {SHARED}"}
    )
    assert {c["pattern"] for c in survivors.json()["claims"]} == {"src/b.py"}


async def test_scoped_token_file_request_403_on_other_repo_claim(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim_id(client, REPO_B, "src/b.py")
    raw = await _mint(REPO_A)
    r = await client.post(
        "/requests",
        headers=_auth(raw),
        json={"claim_id": cid, "requester": "eng", "wait_seconds": 0},
    )
    assert r.status_code == 403, r.text


async def test_scoped_token_respond_403_on_other_repo_request(
    client: AsyncClient,
) -> None:
    cid = await _seed_claim_id(client, REPO_B, "src/b.py")
    rid = await _file_request(client, cid)
    raw = await _mint(REPO_A)
    r = await client.post(
        f"/requests/{rid}/respond",
        headers=_auth(raw),
        json={"decision": "denied", "engineer": "eng"},
    )
    assert r.status_code == 403, r.text


async def test_scoped_token_get_request_403_on_other_repo(client: AsyncClient) -> None:
    cid = await _seed_claim_id(client, REPO_B, "src/b.py")
    rid = await _file_request(client, cid)
    raw = await _mint(REPO_A)
    r = await client.get(f"/requests/{rid}", headers=_auth(raw))
    assert r.status_code == 403, r.text
    ev = await client.get(f"/requests/{rid}/events", headers=_auth(raw))
    assert ev.status_code == 403, ev.text


async def test_scoped_token_list_requests_only_own_repo(client: AsyncClient) -> None:
    cid_a = await _seed_claim_id(client, REPO_A, "src/a.py")
    cid_b = await _seed_claim_id(client, REPO_B, "src/b.py")
    await _file_request(client, cid_a)
    await _file_request(client, cid_b)
    raw = await _mint(REPO_A)
    r = await client.get("/requests", headers=_auth(raw))
    assert r.status_code == 200, r.text
    claim_ids = {row["claim_id"] for row in r.json()["requests"]}
    assert claim_ids == {cid_a}, claim_ids


async def test_scoped_token_pending_requests_empty_on_other_repo_session(
    client: AsyncClient,
) -> None:
    # A session entirely in REPO_B, polled by a REPO_A token, yields an
    # empty inbox (200) -- row-level filtering, not a 403 existence
    # oracle that would confirm the session lives in another repo.
    from coordination.deps import get_service

    svc = get_service()
    await svc.db.insert_claims_batch(
        engineer="b", branch=None, description=None,
        items=[("cb", "file", "src/b.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="b-sess", repo=REPO_B,
    )
    raw = await _mint(REPO_A)
    r = await client.get("/sessions/b-sess/pending_requests", headers=_auth(raw))
    assert r.status_code == 200, r.text
    assert r.json() == {"pending": [], "count": 0}, r.text


async def test_scoped_token_pending_requests_filters_mixed_repo_session(
    client: AsyncClient,
) -> None:
    # The real bypass: ONE session id holds claims in BOTH repos, each
    # with a pending release request. A REPO_A token polling this shared
    # session must see only REPO_A's request -- and must NOT fire a
    # ``notified`` audit event against REPO_B's request (which would both
    # leak the row and forge evidence the B holder saw it).
    from coordination.deps import get_service

    svc = get_service()
    await svc.db.insert_claims_batch(
        engineer="dev", branch=None, description=None,
        items=[("ca", "file", "src/a.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="mix-sess", repo=REPO_A,
    )
    await svc.db.insert_claims_batch(
        engineer="dev", branch=None, description=None,
        items=[("cb", "file", "src/b.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="mix-sess", repo=REPO_B,
    )
    rid_a = await _file_request(client, "ca")
    rid_b = await _file_request(client, "cb")

    raw = await _mint(REPO_A)
    r = await client.get("/sessions/mix-sess/pending_requests", headers=_auth(raw))
    assert r.status_code == 200, r.text
    request_ids = {
        row.get("id") for row in r.json()["pending"] if row.get("kind") == "request"
    }
    assert request_ids == {rid_a}, r.json()

    # REPO_B's request was neither returned nor notified by A's poll.
    ev_b = await svc.db.list_request_events(rid_b)
    assert not any(e["event_type"] == "notified" for e in ev_b), ev_b
    # REPO_A's request WAS notified (the in-scope holder genuinely saw it).
    ev_a = await svc.db.list_request_events(rid_a)
    assert any(e["event_type"] == "notified" for e in ev_a), ev_a


async def test_scoped_token_cancel_queue_403_on_other_repo(client: AsyncClient) -> None:
    # Insert a claim_queue row tagged REPO_B directly, then a REPO_A token
    # must not be able to cancel it.
    import sqlite3

    from coordination.deps import get_service

    svc = get_service()
    cid = await _seed_claim_id(client, REPO_B, "src/b.py")
    with sqlite3.connect(svc.db.path) as conn:
        conn.execute(
            "INSERT INTO claim_queue (id, blocking_claim_id, requester_engineer, "
            "repo, claim_type, pattern, position, state, enqueued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("q-b", cid, "waiter", REPO_B, "file", "src/b.py", 1, "waiting",
             "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
        )
        conn.commit()
    raw = await _mint(REPO_A)
    r = await client.delete("/requests/q-b", headers=_auth(raw))
    assert r.status_code == 403, r.text


async def test_scoped_token_set_ownership_is_operator_only(client: AsyncClient) -> None:
    raw = await _mint(REPO_A)
    r = await client.post(
        "/config/ownership", headers=_auth(raw), content="shared_files:\n  - src/x.py\n"
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# COORD_REQUIRE_SCOPED_TOKEN: make repo scoping mandatory. An unscoped
# per-engineer token is rejected; the shared token stays the operator hatch.
# ---------------------------------------------------------------------------


async def test_require_scoped_token_rejects_unscoped_per_engineer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_REQUIRE_SCOPED_TOKEN", "true")
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        svc = deps.get_service()
        unscoped = "coordt_" + "a" * 64
        await svc.db.create_engineer_token("e", _sha256(unscoped))
        scoped = "coordt_" + "b" * 64
        await svc.db.create_engineer_token("e", _sha256(scoped), repo=REPO_A)

        # Unscoped per-engineer token -> 401 with an actionable hint.
        r1 = await ac.get("/claims", headers=_auth(unscoped))
        assert r1.status_code == 401, r1.text
        assert "repo-scoped token" in r1.json()["detail"].lower()

        # Scoped per-engineer token -> 200.
        r2 = await ac.get("/claims", headers=_auth(scoped))
        assert r2.status_code == 200, r2.text

        # Shared token stays the operator escape hatch -> 200.
        r3 = await ac.get(
            "/claims", headers={"Authorization": f"Bearer {SHARED}"}
        )
        assert r3.status_code == 200, r3.text


async def test_unscoped_per_engineer_ok_when_flag_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.delenv("COORD_REQUIRE_SCOPED_TOKEN", raising=False)
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        svc = deps.get_service()
        unscoped = "coordt_" + "d" * 64
        await svc.db.create_engineer_token("e", _sha256(unscoped))
        r = await ac.get("/claims", headers=_auth(unscoped))
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# v0.42 security-review hardening (post-review fixes)
# ---------------------------------------------------------------------------


async def test_touch_session_activity_repo_scoped(client: AsyncClient) -> None:
    # A repo-scoped warm-up must not refresh another repo's claims that
    # happen to share a session id. Pinned at the DB layer so it is
    # deterministic (no wall-clock dependence beyond "!= the old value").
    from coordination.deps import get_service

    svc = get_service()
    old = "2020-01-01T00:00:00Z"
    await svc.db.insert_claims_batch(
        engineer="dev", branch=None, description=None,
        items=[("ta", "file", "src/a.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="warm", repo=REPO_A, last_activity=old,
    )
    await svc.db.insert_claims_batch(
        engineer="dev", branch=None, description=None,
        items=[("tb", "file", "src/b.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="warm", repo=REPO_B, last_activity=old,
    )
    n = await svc.db.touch_session_activity("warm", repo=REPO_A)
    assert n == 1, n  # only the REPO_A claim was warmed
    rows = {r["id"]: r for r in await svc.db.list_active_claims_rows()}
    assert rows["ta"]["last_activity"] != old, rows["ta"]
    assert rows["tb"]["last_activity"] == old, rows["tb"]


async def test_scoped_token_all_repos_rejected(client: AsyncClient) -> None:
    # The advertised explicit-403 for all_repos, now actually wired.
    raw = await _mint(REPO_A)
    r = await client.get("/claims?all_repos=true", headers=_auth(raw))
    assert r.status_code == 403, r.text
    rc = await client.get(
        "/conflicts?engineer=e&pattern=src/x.py&all_repos=true",
        headers=_auth(raw),
    )
    assert rc.status_code == 403, rc.text
    rh = await client.get("/metrics/hotspots?all_repos=true", headers=_auth(raw))
    assert rh.status_code == 403, rh.text


async def test_operator_all_repos_ok(client: AsyncClient) -> None:
    await _seed_claim(client, REPO_A, "src/a.py")
    await _seed_claim(client, REPO_B, "src/b.py")
    r = await client.get(
        "/claims?all_repos=true",
        headers={"Authorization": f"Bearer {SHARED}"},
    )
    assert r.status_code == 200, r.text
    repos = {c.get("repo") for c in r.json()["claims"]}
    assert repos == {REPO_A, REPO_B}, repos


async def test_queue_depth_header_absent_without_auth(client: AsyncClient) -> None:
    # Before v0.42 the backpressure middleware stamped queue depth from a
    # client-supplied ``engineer`` with no credential -- anyone could read
    # any engineer's depth. Now an unauthenticated request gets no header.
    r = await client.get("/claims?engineer=victim")
    assert r.status_code == 401, r.text
    assert "X-Coord-Queue-Depth" not in r.headers


async def test_queue_depth_header_present_for_authed(client: AsyncClient) -> None:
    raw = await _mint(REPO_A, engineer="eng")
    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    assert "X-Coord-Queue-Depth" in r.headers


async def test_list_stale_engineers_repo_scoped(client: AsyncClient) -> None:
    # A repo-scoped dashboard viewer must not see other repos' stale
    # holders, their counts, or their repo names.
    from coordination.deps import get_service

    svc = get_service()
    old = "2020-01-01T00:00:00Z"
    await svc.db.insert_claims_batch(
        engineer="alice", branch=None, description=None,
        items=[("sa", "file", "src/a.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="sa", repo=REPO_A, last_activity=old,
    )
    await svc.db.insert_claims_batch(
        engineer="bob", branch=None, description=None,
        items=[("sb", "file", "src/b.py", "soft", "2099-01-01T00:00:00Z")],
        session_id="sb", repo=REPO_B, last_activity=old,
    )
    scoped = await svc.db.list_stale_engineers(days=1, repo=REPO_A)
    assert {row["engineer"] for row in scoped} == {"alice"}, scoped
    assert all(row["repos"] == [REPO_A] for row in scoped), scoped
    unscoped = await svc.db.list_stale_engineers(days=1)
    assert {row["engineer"] for row in unscoped} == {"alice", "bob"}, unscoped


@pytest.fixture()
async def client_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    # Auto-promote enabled so a single bounce crosses the threshold.
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_AUTO_PROMOTE_THRESHOLD", "1")
    monkeypatch.setenv("COORD_AUTO_PROMOTE_WINDOW_DAYS", "7")

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    deps.get_service.cache_clear()


async def _bounce(ac: AsyncClient, headers: dict[str, str], repo: str | None) -> None:
    # Seed a holder (operator token) then bounce a second claim off it so a
    # conflict lands in conflict_log and _maybe_auto_promote is reached.
    rh = await ac.post(
        "/claims",
        headers={"Authorization": f"Bearer {SHARED}"},
        json={
            "engineer": "holder",
            "repo": REPO_A,
            "claims": [{"type": "file", "pattern": "src/hot.py"}],
        },
    )
    assert rh.status_code == 200, rh.text
    body: dict = {
        "engineer": "bouncer",
        "claims": [{"type": "file", "pattern": "src/hot.py"}],
    }
    if repo is not None:
        body["repo"] = repo
    rb = await ac.post("/claims", headers=headers, json=body)
    assert rb.status_code in (200, 409), rb.text
    assert rb.json().get("claim_ids") == [], rb.text


async def test_scoped_token_conflict_does_not_auto_promote(
    client_promote: AsyncClient,
) -> None:
    # A repo-scoped caller's conflict must not rewrite the GLOBAL
    # ownership YAML (auto-promote is operator-only).
    raw = await _mint(REPO_A, engineer="bouncer")
    await _bounce(client_promote, _auth(raw), repo=None)
    g = await client_promote.get(
        "/config/ownership", headers={"Authorization": f"Bearer {SHARED}"}
    )
    # 204 (no ownership YAML at all) or 200 without the pattern -- either
    # way the scoped caller promoted nothing.
    assert g.status_code in (200, 204), g.text
    if g.status_code == 200:
        assert "src/hot.py" not in g.text, g.text


async def test_operator_conflict_still_auto_promotes(
    client_promote: AsyncClient,
) -> None:
    # The operator (unscoped) path is unchanged: it still promotes.
    await _bounce(
        client_promote,
        {"Authorization": f"Bearer {SHARED}"},
        repo=REPO_A,
    )
    g = await client_promote.get(
        "/config/ownership", headers={"Authorization": f"Bearer {SHARED}"}
    )
    assert g.status_code == 200, g.text
    assert "src/hot.py" in g.text, g.text


async def test_count_queued_for_repo_scoped(client: AsyncClient) -> None:
    # A repo-scoped token must not read an engineer's cross-repo queue
    # depth via the X-Coord-Queue-Depth backpressure header.
    from coordination.deps import get_service

    svc = get_service()
    await svc.db.insert_claims_batch(
        engineer="holder", branch=None, description=None,
        items=[("qha", "file", "src/a.py", "soft", "2099-01-01T00:00:00Z")],
        repo=REPO_A,
    )
    await svc.db.insert_claims_batch(
        engineer="holder", branch=None, description=None,
        items=[("qhb", "file", "src/b.py", "soft", "2099-01-01T00:00:00Z")],
        repo=REPO_B,
    )
    for repo, blk, pat in ((REPO_A, "qha", "src/a.py"), (REPO_B, "qhb", "src/b.py")):
        await svc.db.enqueue_claim_request(
            blocking_claim_id=blk,
            requester_engineer="alice",
            requester_session_id=None,
            requester_branch=None,
            requester_description=None,
            repo=repo,
            claim_type="file",
            pattern=pat,
            symbols=None,
            narrowable=None,
            ttl_hours=None,
            wait_seconds=0,
        )
    assert await svc.count_queued_for("alice", repo=REPO_A) == 1
    assert await svc.count_queued_for("alice", repo=REPO_B) == 1
    assert await svc.count_queued_for("alice") == 2  # operator sees all repos


async def test_scoped_token_cannot_read_ownership_yaml(client: AsyncClient) -> None:
    # GET /config/ownership exposes deployment-wide (cross-repo) config, so
    # it is operator-only like its POST sibling.
    raw = await _mint(REPO_A)
    r = await client.get("/config/ownership", headers=_auth(raw))
    assert r.status_code == 403, r.text
    ro = await client.get(
        "/config/ownership", headers={"Authorization": f"Bearer {SHARED}"}
    )
    assert ro.status_code in (200, 204), ro.text


async def test_operator_all_repos_conflicts_span_repos(client: AsyncClient) -> None:
    # v0.42: an operator's /conflicts?all_repos=true actually checks against
    # every repo's claims, instead of resolving to repo=None and comparing
    # only the (here empty) legacy NULL bucket.
    await _seed_claim(client, REPO_A, "src/shared.py")
    await _seed_claim(client, REPO_B, "src/shared.py")
    op = {"Authorization": f"Bearer {SHARED}"}

    r = await client.get(
        "/conflicts?engineer=other&pattern=src/shared.py&all_repos=true", headers=op
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_conflicts"] is True, body
    assert len(body["conflicts"]) == 2, body  # one per repo

    # Operator without all_repos (repo=None) only sees the legacy NULL bucket,
    # which is empty here because both claims are repo-tagged.
    r2 = await client.get(
        "/conflicts?engineer=other&pattern=src/shared.py", headers=op
    )
    assert r2.json()["has_conflicts"] is False, r2.json()

    # A scoped REPO_A token cannot request all_repos...
    raw = await _mint(REPO_A)
    r3 = await client.get(
        "/conflicts?engineer=other&pattern=src/shared.py&all_repos=true",
        headers=_auth(raw),
    )
    assert r3.status_code == 403, r3.text
    # ...and scoped to its own repo it sees only REPO_A's claim.
    r4 = await client.get(
        "/conflicts?engineer=other&pattern=src/shared.py", headers=_auth(raw)
    )
    assert r4.status_code == 200, r4.text
    assert len(r4.json()["conflicts"]) == 1, r4.json()


@pytest.fixture()
async def client_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_MAX_CLAIMS_PER_ENGINEER", "2")

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    deps.get_service.cache_clear()


async def test_active_claim_cap_is_per_repo(client_capped: AsyncClient) -> None:
    # v0.42: the per-engineer active-claim cap is per-repo on a repo-tagged
    # deployment, so alice's REPO_B usage neither blocks nor is disclosed to
    # her REPO_A work.
    ac = client_capped
    op = {"Authorization": f"Bearer {SHARED}"}
    for i in range(2):  # fill alice's cap (2) in REPO_B
        r = await ac.post(
            "/claims",
            headers=op,
            json={
                "engineer": "alice",
                "repo": REPO_B,
                "claims": [{"type": "file", "pattern": f"src/b{i}.py"}],
            },
        )
        assert r.status_code == 200, r.text

    # A REPO_A-scoped alice token still has a full budget in REPO_A.
    raw = await _mint(REPO_A, engineer="alice")
    r = await ac.post(
        "/claims",
        headers=_auth(raw),
        json={"engineer": "alice", "claims": [{"type": "file", "pattern": "src/a.py"}]},
    )
    assert r.status_code == 200, r.text

    # A third REPO_B claim is over the per-repo cap -> 429.
    r2 = await ac.post(
        "/claims",
        headers=op,
        json={
            "engineer": "alice",
            "repo": REPO_B,
            "claims": [{"type": "file", "pattern": "src/b2.py"}],
        },
    )
    assert r2.status_code == 429, r2.text


# ---------------------------------------------------------------------------
# v0.43 soft-deprecation warning for unscoped per-engineer tokens
# ---------------------------------------------------------------------------


async def test_unscoped_per_engineer_token_gets_deprecation_header(
    client: AsyncClient,
) -> None:
    raw = await _mint(None, engineer="dana")  # unscoped per-engineer token
    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    warning = r.headers.get("X-Coord-Token-Warning")
    assert warning is not None, r.headers
    assert "coord tokens create dana --repo" in warning
    assert "AGENTS.md" in warning


async def test_scoped_token_no_deprecation_header(client: AsyncClient) -> None:
    raw = await _mint(REPO_A)
    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    assert "X-Coord-Token-Warning" not in r.headers
    # scoped tokens advertise their scope instead
    assert r.headers.get("X-Coord-Repo-Scope") == REPO_A


async def test_shared_operator_token_no_deprecation_header(
    client: AsyncClient,
) -> None:
    # The shared operator token is unscoped by design -- it is the escape
    # hatch, not a deprecated per-engineer token, so it is never warned.
    r = await client.get(
        "/claims", headers={"Authorization": f"Bearer {SHARED}"}
    )
    assert r.status_code == 200, r.text
    assert "X-Coord-Token-Warning" not in r.headers


@pytest.fixture()
async def client_no_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", SHARED)
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.setenv("COORD_WARN_UNSCOPED_TOKEN", "false")

    from coordination import deps

    deps.get_service.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    deps.get_service.cache_clear()


async def test_deprecation_header_silenced_by_flag(
    client_no_warn: AsyncClient,
) -> None:
    raw = await _mint(None, engineer="dana")
    r = await client_no_warn.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    assert "X-Coord-Token-Warning" not in r.headers


async def test_deprecation_header_sanitizes_engineer(client: AsyncClient) -> None:
    # Engineer ids are stored verbatim, so a CR/LF in the name must not break
    # the X-Coord-Token-Warning header or inject a second header (Copilot
    # review, PR #62).
    # Includes CR/LF (header injection) and a non-ASCII char (Starlette
    # encodes header values as latin-1, so a Unicode id would 500).
    raw = await _mint(None, engineer="dana\r\nX-Injected: pwned☃")
    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    warning = r.headers.get("X-Coord-Token-Warning")
    assert warning is not None
    assert "\r" not in warning and "\n" not in warning, repr(warning)
    assert "X-Injected" not in r.headers  # no header injection
    assert "☃" not in warning  # non-ASCII stripped
    warning.encode("ascii")  # must be pure ASCII (latin-1 header-safe)


async def test_deprecation_header_caps_engineer_length(client: AsyncClient) -> None:
    # An oversized engineer id must not bloat the header past receiver limits
    # (Copilot review round 3, PR #62). "Z" is absent from the fixed template,
    # so its count in the warning is exactly the cap.
    raw = await _mint(None, engineer="Z" * 500)
    r = await client.get("/claims", headers=_auth(raw))
    assert r.status_code == 200, r.text
    warning = r.headers["X-Coord-Token-Warning"]
    assert warning.count("Z") == 64, warning.count("Z")
