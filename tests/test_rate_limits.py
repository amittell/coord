"""Tests for v0.30 per-engineer rate limiting + per-repo queue-depth quota.

Three independent knobs, all defaulting to 0 (disabled):

- ``COORD_MAX_CLAIMS_PER_ENGINEER``: active-claim cap, enforced at
  insert time (the conflict-free path AND drain-time queue grants).
- ``COORD_MAX_QUEUED_PER_ENGINEER``: live queue-entry cap, enforced at
  enqueue time only.
- ``COORD_MAX_QUEUE_DEPTH_PER_REPO``: per-repo waiting-queue depth cap,
  also enqueue-time admission control only.

A breach maps to HTTP 429 with a ``Retry-After`` header and a
``{detail, scope, retry_after}`` body; the MCP wrapper surfaces that as
a structured ``{error, scope, retry_after}`` result instead of raising.

All waiting in these tests is done by POLLING (never bare sleeps for a
fixed outcome) -- Windows CI timers are too coarse for sleep-based
synchronisation.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from coordination.db import Database
from coordination.main import app

_AUTH = {"Authorization": "Bearer test-token"}


def _iso(*, hours: float = 0, seconds: float = 0) -> str:
    """ISO-8601 Z-suffixed timestamp offset from now, matching the
    format the service layer writes into ``claims.expires_at``."""
    dt = datetime.now(UTC) + timedelta(hours=hours, seconds=seconds)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _base_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: str
) -> None:
    """Standard test env (mirrors the test_api.py client fixture) plus
    any rate-limit knobs the individual fixture wants."""
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)

    from coordination import deps

    deps.get_service.cache_clear()


async def _make_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture()
async def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """All three knobs at their 0 defaults: rate limiting disabled."""
    _base_env(tmp_path, monkeypatch)
    async with await _make_client() as ac:
        yield ac
    from coordination import deps

    deps.get_service.cache_clear()


@pytest.fixture()
async def client_claims_cap_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    _base_env(tmp_path, monkeypatch, COORD_MAX_CLAIMS_PER_ENGINEER="2")
    async with await _make_client() as ac:
        yield ac
    from coordination import deps

    deps.get_service.cache_clear()


@pytest.fixture()
async def client_claims_cap_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    _base_env(tmp_path, monkeypatch, COORD_MAX_CLAIMS_PER_ENGINEER="1")
    async with await _make_client() as ac:
        yield ac
    from coordination import deps

    deps.get_service.cache_clear()


@pytest.fixture()
async def client_queue_cap_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    _base_env(tmp_path, monkeypatch, COORD_MAX_QUEUED_PER_ENGINEER="1")
    async with await _make_client() as ac:
        yield ac
    from coordination import deps

    deps.get_service.cache_clear()


@pytest.fixture()
async def client_repo_cap_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    _base_env(tmp_path, monkeypatch, COORD_MAX_QUEUE_DEPTH_PER_REPO="1")
    async with await _make_client() as ac:
        yield ac
    from coordination import deps

    deps.get_service.cache_clear()


def _db() -> Database:
    return Database(Path(os.environ["COORD_DATABASE_PATH"]))


async def _wait_for_queue_id(
    client: AsyncClient, requester: str, timeout: float = 2.0
) -> str:
    """Poll GET /requests?queued=true until a waiting row appears for
    the given requester and return its queue id. Local copy of the
    test_api.py helper (the tests directory is not a package, so
    cross-module imports are off the table)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(
            f"/requests?queued=true&requester={requester}", headers=_AUTH
        )
        if r.status_code == 200:
            for row in r.json().get("requests", []):
                if (
                    row.get("requester_engineer") == requester
                    and row.get("state") == "waiting"
                ):
                    return row["queue_id"]
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"queue row for requester {requester!r} did not appear within "
        f"{timeout}s"
    )


async def _wait_for_queue_state(
    db: Database, queue_id: str, state: str, timeout: float = 3.0
) -> dict[str, Any]:
    """Poll the claim_queue row until it reaches ``state``."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] | None = None
    while asyncio.get_event_loop().time() < deadline:
        last = await db.get_queue_entry(queue_id)
        if last is not None and last.get("state") == state:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"queue {queue_id} never reached state {state!r}; last seen {last}"
    )


async def _claim(
    client: AsyncClient,
    engineer: str,
    patterns: list[str],
    *,
    repo: str | None = None,
    wait_seconds: int | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {
        "engineer": engineer,
        "claims": [{"type": "file", "pattern": p} for p in patterns],
    }
    if repo is not None:
        body["repo"] = repo
    if wait_seconds is not None:
        body["wait_seconds"] = wait_seconds
    return await client.post("/claims", headers=_AUTH, json=body)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_active_claims_excludes_released_and_ttl_expired(
    tmp_path: Path,
) -> None:
    """Released rows and rows whose expires_at is in the past must not
    count. TTL filtering happens in Python (mirroring
    list_active_claims_rows), so a past-expiry row that is still
    unreleased in the table is the interesting case."""
    db = Database(tmp_path / "db.sqlite")
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[
            ("c-live", "file", "a.ts", "soft", _iso(hours=2)),
            ("c-released", "file", "b.ts", "soft", _iso(hours=4)),
            ("c-expired", "file", "c.ts", "soft", _iso(hours=-1)),
        ],
    )
    # Another engineer's claim must never count against alice.
    await db.insert_claims_batch(
        engineer="bob",
        branch=None,
        description=None,
        items=[("c-bob", "file", "d.ts", "soft", _iso(hours=2))],
    )
    await db.release_claims(["c-released"], "alice")

    count, soonest = await db.count_active_claims_for_engineer("alice")
    assert count == 1
    rows = await db.list_active_claims_rows()
    live = next(r for r in rows if r["id"] == "c-live")
    assert soonest == live["expires_at"]


@pytest.mark.asyncio
async def test_count_active_claims_soonest_expiry_and_zero_case(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "db.sqlite")
    near = _iso(hours=1)
    far = _iso(hours=8)
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[
            ("c-far", "file", "a.ts", "soft", far),
            ("c-near", "file", "b.ts", "soft", near),
        ],
    )
    count, soonest = await db.count_active_claims_for_engineer("alice")
    assert count == 2
    assert soonest == near

    # Engineer with no claims at all: (0, None).
    count_none, soonest_none = await db.count_active_claims_for_engineer(
        "nobody"
    )
    assert count_none == 0
    assert soonest_none is None


@pytest.mark.asyncio
async def test_count_queue_entries_by_state(tmp_path: Path) -> None:
    """Default states count waiting + in_progress; terminal states
    (expired here) never count; an explicit states tuple narrows."""
    db = Database(tmp_path / "db.sqlite")
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("c-holder", "file", "hot.ts", "soft", _iso(hours=2))],
    )

    async def enqueue() -> dict[str, Any]:
        return await db.enqueue_claim_request(
            blocking_claim_id="c-holder",
            requester_engineer="bob",
            requester_session_id=None,
            requester_branch=None,
            requester_description=None,
            repo=None,
            claim_type="file",
            pattern="hot.ts",
            symbols=None,
            narrowable=None,
            ttl_hours=None,
            wait_seconds=120,
        )

    e1 = await enqueue()
    await enqueue()
    e3 = await enqueue()

    # Flip the head entry to in_progress via the real pop path, and a
    # third to a terminal state.
    popped = await db.pop_next_waiting_queue_entry("c-holder")
    assert popped is not None and popped["id"] == e1["id"]
    await db.mark_queue_expired(e3["id"])

    assert await db.count_queue_entries_for_engineer("bob") == 2
    assert (
        await db.count_queue_entries_for_engineer("bob", states=("waiting",))
        == 1
    )
    assert await db.count_queue_entries_for_engineer("carol") == 0


@pytest.mark.asyncio
async def test_queue_depth_for_repo_buckets(tmp_path: Path) -> None:
    """Named repos count independently and the NULL bucket only matches
    NULL-repo requests; only state='waiting' rows count."""
    db = Database(tmp_path / "db.sqlite")
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("c-holder", "file", "hot.ts", "soft", _iso(hours=2))],
    )

    async def enqueue(repo: str | None) -> dict[str, Any]:
        return await db.enqueue_claim_request(
            blocking_claim_id="c-holder",
            requester_engineer="bob",
            requester_session_id=None,
            requester_branch=None,
            requester_description=None,
            repo=repo,
            claim_type="file",
            pattern="hot.ts",
            symbols=None,
            narrowable=None,
            ttl_hours=None,
            wait_seconds=120,
        )

    await enqueue("repo-one")
    e2 = await enqueue("repo-one")
    await enqueue(None)

    assert await db.queue_depth_for_repo("repo-one") == 2
    assert await db.queue_depth_for_repo("repo-two") == 0
    assert await db.queue_depth_for_repo(None) == 1

    # Terminal states leave the waiting count.
    await db.mark_queue_expired(e2["id"])
    assert await db.queue_depth_for_repo("repo-one") == 1


# ---------------------------------------------------------------------------
# Active-claim cap (COORD_MAX_CLAIMS_PER_ENGINEER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_cap_boundary_and_429_shape(
    client_claims_cap_two: AsyncClient,
) -> None:
    """count + requested == limit passes; one more 429s with the
    documented body shape and a sane Retry-After header."""
    client = client_claims_cap_two
    # Exactly-at-limit batch is allowed (0 + 2 <= 2).
    r = await _claim(client, "alice", ["src/a.ts", "src/b.ts"])
    assert r.status_code == 200, r.text

    # One more would exceed (2 + 1 > 2).
    r2 = await _claim(client, "alice", ["src/c.ts"])
    assert r2.status_code == 429, r2.text
    body = r2.json()
    assert body["scope"] == "claims"
    assert "limit is 2" in body["detail"]
    # alice is at cap so she always has active claims: Retry-After is
    # expiry-anchored and clamped into [5, 3600].
    retry_after = body["retry_after"]
    assert isinstance(retry_after, int)
    assert 5 <= retry_after <= 3600
    assert r2.headers.get("Retry-After") == str(retry_after)
    # A single extra claim does not exceed the limit by itself, so the
    # reduce-batch hint must NOT appear.
    assert "batch" not in body["detail"]

    # The cap is per engineer: bob is unaffected by alice's bucket.
    r3 = await _claim(client, "bob", ["src/d.ts"])
    assert r3.status_code == 200, r3.text


@pytest.mark.asyncio
async def test_active_cap_batch_alone_exceeds_limit(
    client_claims_cap_two: AsyncClient,
) -> None:
    """A fresh engineer whose single batch is bigger than the limit gets
    the reduce-batch hint, and (holding zero claims) the flat-60
    Retry-After fallback since there is no expiry to anchor on."""
    r = await _claim(
        client_claims_cap_two, "carol", ["x.ts", "y.ts", "z.ts"]
    )
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["scope"] == "claims"
    assert "batch" in body["detail"]
    assert "reduce" in body["detail"]
    assert body["retry_after"] == 60
    assert r.headers.get("Retry-After") == "60"


@pytest.mark.asyncio
async def test_active_cap_retry_after_clamped_at_low_end(
    client_claims_cap_one: AsyncClient,
) -> None:
    """A claim expiring in ~3s yields a raw retry hint below the floor;
    the clamp must lift it to exactly 5."""
    db = _db()
    await db.insert_claims_batch(
        engineer="alice",
        branch=None,
        description=None,
        items=[("c-near", "file", "near.ts", "soft", _iso(seconds=3))],
    )
    r = await _claim(client_claims_cap_one, "alice", ["other.ts"])
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["retry_after"] == 5
    assert r.headers.get("Retry-After") == "5"


@pytest.mark.asyncio
async def test_active_cap_release_frees_capacity(
    client_claims_cap_one: AsyncClient,
) -> None:
    client = client_claims_cap_one
    r = await _claim(client, "alice", ["a.ts"])
    assert r.status_code == 200, r.text
    cid = r.json()["claim_ids"][0]

    r2 = await _claim(client, "alice", ["b.ts"])
    assert r2.status_code == 429, r2.text

    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text

    r3 = await _claim(client, "alice", ["b.ts"])
    assert r3.status_code == 200, r3.text


# ---------------------------------------------------------------------------
# Per-engineer queue cap (COORD_MAX_QUEUED_PER_ENGINEER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_cap_second_wait_429s_first_still_drains(
    client_queue_cap_one: AsyncClient,
) -> None:
    client = client_queue_cap_one
    rh = await _claim(client, "alice", ["hot.ts"])
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    async def waiter() -> dict[str, Any]:
        return (
            await _claim(client, "bob", ["hot.ts"], wait_seconds=10)
        ).json()

    task = asyncio.create_task(waiter())
    await _wait_for_queue_id(client, "bob")

    # Bob already occupies his one queue slot: the second wait_seconds
    # request is refused at the door.
    r2 = await _claim(client, "bob", ["hot.ts"], wait_seconds=5)
    assert r2.status_code == 429, r2.text
    body = r2.json()
    assert body["scope"] == "queue"
    assert body["retry_after"] == 60
    assert r2.headers.get("Retry-After") == "60"

    # The cap never touches entries that were already admitted: bob's
    # first wait drains normally when the holder releases.
    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [holder_cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result.get("claim_ids"), result


# ---------------------------------------------------------------------------
# Per-repo queue depth cap (COORD_MAX_QUEUE_DEPTH_PER_REPO)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_queue_depth_cap_isolated_per_bucket(
    client_repo_cap_one: AsyncClient,
) -> None:
    """Depth 1 reached in repo-one: the next wait there 429s, while a
    different repo and the NULL-repo bucket admit normally."""
    client = client_repo_cap_one
    holder_ids: list[str] = []
    for pattern, repo in (
        ("a.ts", "repo-one"),
        ("b.ts", "repo-two"),
        ("c.ts", None),
    ):
        r = await _claim(client, "alice", [pattern], repo=repo)
        assert r.status_code == 200, r.text
        holder_ids.extend(r.json()["claim_ids"])

    async def waiter(
        engineer: str, pattern: str, repo: str | None
    ) -> dict[str, Any]:
        return (
            await _claim(
                client, engineer, [pattern], repo=repo, wait_seconds=10
            )
        ).json()

    bob_task = asyncio.create_task(waiter("bob", "a.ts", "repo-one"))
    await _wait_for_queue_id(client, "bob")

    # repo-one is now at depth 1 == cap: carol is refused.
    rc = await _claim(client, "carol", ["a.ts"], repo="repo-one", wait_seconds=5)
    assert rc.status_code == 429, rc.text
    body = rc.json()
    assert body["scope"] == "repo_queue"
    assert "at capacity" in body["detail"]
    assert "wait_seconds" in body["detail"]
    assert rc.headers.get("Retry-After") == "60"

    # A different repo bucket is unaffected...
    dan_task = asyncio.create_task(waiter("dan", "b.ts", "repo-two"))
    await _wait_for_queue_id(client, "dan")
    # ...and the NULL bucket is independent of every named bucket.
    eve_task = asyncio.create_task(waiter("eve", "c.ts", None))
    await _wait_for_queue_id(client, "eve")

    # Drain everything so the admitted waiters all resolve as grants.
    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": holder_ids, "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text
    for task in (bob_task, dan_task, eve_task):
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result.get("claim_ids"), result


# ---------------------------------------------------------------------------
# Drain-at-cap: a queue grant must not blast through the active cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_at_cap_expires_entry_and_grants_next_waiter(
    client_claims_cap_one: AsyncClient,
) -> None:
    """Holder releases while the first waiter's engineer is at the
    active cap: that waiter's entry must be expired (not granted, not
    left in_progress) and the next under-cap waiter granted instead.
    Also proves an at-cap engineer can still ENQUEUE -- the cap blocks
    inserts, never queueing."""
    client = client_claims_cap_one
    rh = await _claim(client, "alice", ["hot.ts"])
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    # Bob fills his one-claim allowance elsewhere: he is now at cap.
    rb = await _claim(client, "bob", ["other.ts"])
    assert rb.status_code == 200, rb.text

    async def waiter(engineer: str) -> dict[str, Any]:
        return (
            await _claim(client, engineer, ["hot.ts"], wait_seconds=10)
        ).json()

    # At-cap bob may still join the queue (no 429 here; the row appears).
    bob_task = asyncio.create_task(waiter("bob"))
    bob_qid = await _wait_for_queue_id(client, "bob")
    # Carol (zero claims, under cap) queues behind bob.
    carol_task = asyncio.create_task(waiter("carol"))
    await _wait_for_queue_id(client, "carol")

    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [holder_cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text

    # Drain pops bob first (FIFO), hits his active cap, and must mark
    # the entry expired via the queue-expiry machinery -- poll the row.
    db = _db()
    await _wait_for_queue_state(db, bob_qid, "expired")

    # Bob's long-poll surfaces the legacy 409 conflict shape (no grant).
    bob_result = await asyncio.wait_for(bob_task, timeout=5.0)
    assert bob_result.get("claim_ids") == [], bob_result
    assert bob_result.get("conflicts"), bob_result

    # Carol, under cap, is granted by the same drain pass.
    carol_result = await asyncio.wait_for(carol_task, timeout=5.0)
    assert carol_result.get("claim_ids"), carol_result
    assert carol_result.get("conflicts") == [], carol_result


# ---------------------------------------------------------------------------
# Disabled by default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_knobs_zero_leave_claim_and_queue_flow_untouched(
    client: AsyncClient,
) -> None:
    """With every limit at 0, an engineer can stack claims freely and
    the v0.21 queue flow behaves exactly as before."""
    r = await _claim(
        client, "alice", [f"src/f{i}.ts" for i in range(5)]
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["claim_ids"]) == 5

    rh = await _claim(client, "alice", ["hot.ts"], repo="amittell/coord")
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    async def waiter() -> dict[str, Any]:
        return (
            await _claim(
                client,
                "bob",
                ["hot.ts"],
                repo="amittell/coord",
                wait_seconds=10,
            )
        ).json()

    task = asyncio.create_task(waiter())
    await _wait_for_queue_id(client, "bob")

    rel = await client.post(
        "/claims/release",
        headers=_AUTH,
        json={"claim_ids": [holder_cid], "engineer": "alice"},
    )
    assert rel.status_code == 200, rel.text
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result.get("claim_ids"), result
    assert result.get("conflicts") == [], result


# ---------------------------------------------------------------------------
# Backpressure header regression pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_header_still_counts_waiting_only(
    client: AsyncClient,
) -> None:
    """v0.30 added new queue counters but X-Coord-Queue-Depth keeps its
    v0.28 semantics: waiting rows only. Pin it with an in_progress row
    present alongside a waiting one."""
    rh = await _claim(client, "alice", ["bp.ts"])
    assert rh.status_code == 200, rh.text
    holder_cid = rh.json()["claim_ids"][0]

    db = _db()
    for _ in range(2):
        await db.enqueue_claim_request(
            blocking_claim_id=holder_cid,
            requester_engineer="bob",
            requester_session_id=None,
            requester_branch=None,
            requester_description=None,
            repo=None,
            claim_type="file",
            pattern="bp.ts",
            symbols=None,
            narrowable=None,
            ttl_hours=None,
            wait_seconds=120,
        )
    popped = await db.pop_next_waiting_queue_entry(holder_cid)
    assert popped is not None and popped["state"] == "waiting"
    row = await db.get_queue_entry(popped["id"])
    assert row is not None and row["state"] == "in_progress"

    r = await client.get("/claims?engineer=bob", headers=_AUTH)
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Coord-Queue-Depth") == "1"


# ---------------------------------------------------------------------------
# MCP wrapper surfaces 429 as a structured result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_claim_files_surfaces_429_as_structured_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim_files tool must hand the agent the rate-limit verdict
    as data (mirroring how 409 conflict payloads are surfaced) rather
    than raising. Uses the httpx.MockTransport substitution pattern
    from test_mcp_server.py."""
    from coordination import mcp_server

    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    body = {
        "detail": (
            "engineer 'alice' holds 5 active claims and this request "
            "would insert 1 more; the limit is 5"
        ),
        "scope": "claims",
        "retry_after": 42,
    }
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(
            lambda _request: httpx.Response(429, json=body)
        )
        return real_client(**kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", factory)

    result = await mcp_server.claim_files(
        engineer="alice", patterns=["src/x.ts"]
    )
    assert result == {
        "error": body["detail"],
        "scope": "claims",
        "retry_after": 42,
    }


# ---------------------------------------------------------------------------
# Concurrency: the cap must hold under simultaneous requests
# ---------------------------------------------------------------------------


async def test_active_cap_holds_under_concurrent_requests(
    client_claims_cap_two: AsyncClient,
) -> None:
    """Count-then-insert without serialization is a TOCTOU hole: two
    concurrent requests both observe an under-cap count before either
    insert lands, and the engineer ends up over the limit. The service
    serializes the check+insert pair under a quota lock; this pins it.
    With one claim held and a cap of 2, exactly one of two concurrent
    single-claim requests may win."""
    client = client_claims_cap_two
    first = await _claim(client, "alice", ["src/seed.ts"])
    assert first.status_code == 200

    r1, r2 = await asyncio.gather(
        _claim(client, "alice", ["src/a.ts"]),
        _claim(client, "alice", ["src/b.ts"]),
    )
    assert sorted([r1.status_code, r2.status_code]) == [200, 429]

    count, _ = await _db().count_active_claims_for_engineer("alice")
    assert count == 2
