"""Audit: assorted API-layer hardening.

- POST /metrics/hotspots/promote no longer accepts/echoes a ``repo``
  field (the ownership write is deployment-global).
- POST /config/ownership bounds the request body at 1 MiB (declared
  Content-Length fast path and streamed enforcement for chunked bodies).
- Requests that raise unhandled exceptions are counted in
  ``http_requests_total`` (status 500) and get an access-log line.
- GET /requests?queued=true pushes the scoped token's repo filter into
  the SQL query so the LIMIT window is per-repo, and
  db.list_recent_claims applies the repo filter before its LIMIT.
- db.request_claim_holder resolves a request to its target claim holder.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from conftest import seam_connection
from coordination import metrics
from coordination.main import OWNERSHIP_MAX_BODY_BYTES, app

SHARED = "shared-test-token"
REPO_A = "amittell/repo-a"
REPO_B = "amittell/repo-b"

_SHARED_AUTH = {"Authorization": f"Bearer {SHARED}"}


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
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


async def _mint(repo: str | None, engineer: str = "eng") -> str:
    from coordination.deps import get_service

    raw = "coordt_" + hashlib.sha256(f"misc-{repo}-{engineer}".encode()).hexdigest()
    await get_service().db.create_engineer_token(engineer, _sha256(raw), repo=repo)
    return raw


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _seed_claim(client: AsyncClient, repo: str, pattern: str) -> None:
    r = await client.post(
        "/claims",
        headers=_SHARED_AUTH,
        json={
            "engineer": "seed",
            "repo": repo,
            "claims": [{"type": "file", "pattern": pattern}],
        },
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# promote_hotspot: no repo in request/response
# ---------------------------------------------------------------------------


async def test_promote_response_has_no_repo_field(client: AsyncClient) -> None:
    r = await client.post(
        "/metrics/hotspots/promote",
        headers=_SHARED_AUTH,
        json={"action": "shared_file", "pattern": "src/hot.py"},
    )
    assert r.status_code == 200, r.text
    assert "repo" not in r.json()


async def test_promote_ignores_retired_repo_field(client: AsyncClient) -> None:
    # An old client still sending ``repo`` is not broken (pydantic
    # ignores the extra field), but the value is neither used nor echoed
    # back implying a repo-scoped write.
    r = await client.post(
        "/metrics/hotspots/promote",
        headers=_SHARED_AUTH,
        json={
            "action": "shared_file",
            "pattern": "src/hot2.py",
            "repo": "not a valid id!!",
        },
    )
    assert r.status_code == 200, r.text
    assert "repo" not in r.json()


# ---------------------------------------------------------------------------
# /config/ownership body cap
# ---------------------------------------------------------------------------


async def test_ownership_small_body_still_works(client: AsyncClient) -> None:
    r = await client.post(
        "/config/ownership",
        headers=_SHARED_AUTH,
        content=(
            b"modules:\n"
            b"  docs:\n"
            b"    paths:\n"
            b"      - docs/**\n"
            b"    owners:\n"
            b"      - seed\n"
        ),
    )
    assert r.status_code == 200, r.text


async def test_ownership_oversized_body_is_413(client: AsyncClient) -> None:
    body = b"#" * (OWNERSHIP_MAX_BODY_BYTES + 1)
    r = await client.post("/config/ownership", headers=_SHARED_AUTH, content=body)
    assert r.status_code == 413, r.text


async def test_ownership_oversized_chunked_body_is_413(
    client: AsyncClient,
) -> None:
    # No Content-Length (generator body -> chunked transfer): the cap is
    # enforced while streaming instead.
    async def big_chunks():
        chunk = b"#" * 65536
        for _ in range((OWNERSHIP_MAX_BODY_BYTES // len(chunk)) + 2):
            yield chunk

    r = await client.post(
        "/config/ownership", headers=_SHARED_AUTH, content=big_chunks()
    )
    assert r.status_code == 413, r.text


async def test_ownership_non_utf8_body_is_400(client: AsyncClient) -> None:
    r = await client.post(
        "/config/ownership", headers=_SHARED_AUTH, content=b"\xff\xfe\xfa"
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# unhandled exceptions are observable
# ---------------------------------------------------------------------------

_BOOM_PATH = "/_audit_boom"


@app.get(_BOOM_PATH, include_in_schema=False)
async def _audit_boom() -> dict:
    raise RuntimeError("intentional test crash")


async def test_unhandled_exception_counts_in_http_requests_total(
    client: AsyncClient,
) -> None:
    key = ("GET", _BOOM_PATH, "500")
    before = metrics.http_requests_total.values.get(key, 0.0)
    r = await client.get(_BOOM_PATH)
    assert r.status_code == 500
    after = metrics.http_requests_total.values.get(key, 0.0)
    assert after == before + 1


async def test_unhandled_exception_emits_access_log_line(
    client: AsyncClient,
) -> None:
    records: list[logging.LogRecord] = []

    class _Recorder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    access = logging.getLogger("coordination.access")
    handler = _Recorder(level=logging.DEBUG)
    prior_level = access.level
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    try:
        r = await client.get(_BOOM_PATH)
        assert r.status_code == 500
    finally:
        access.removeHandler(handler)
        access.setLevel(prior_level)
    crash_lines = [
        rec
        for rec in records
        if getattr(rec, "path", "") == _BOOM_PATH
        and getattr(rec, "status", None) == 500
    ]
    assert crash_lines, "crash 500 produced no access-log line"
    assert getattr(crash_lines[0], "duration_ms", None) is not None


# ---------------------------------------------------------------------------
# repo scope applied before the DB LIMIT
# ---------------------------------------------------------------------------


async def test_queued_listing_pushes_token_repo_into_query(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordination.deps import get_service

    svc = get_service()
    seen: list[dict] = []

    async def rec_list(
        *, engineer=None, session_id=None, state="waiting", repo=None, limit=100
    ):
        seen.append(
            {
                "engineer": engineer,
                "session_id": session_id,
                "state": state,
                "repo": repo,
            }
        )
        return []

    monkeypatch.setattr(svc.db, "list_queued_with_holder", rec_list)

    scoped = await _mint(REPO_A, engineer="scoped-eng")
    r = await client.get("/requests?queued=true", headers=_auth(scoped))
    assert r.status_code == 200, r.text
    assert seen and seen[-1]["repo"] == REPO_A

    r = await client.get("/requests?queued=true", headers=_SHARED_AUTH)
    assert r.status_code == 200, r.text
    assert seen[-1]["repo"] is None


async def test_list_recent_claims_filters_repo_before_limit(
    client: AsyncClient, tmp_path: Path
) -> None:
    from coordination.deps import get_service

    await _seed_claim(client, REPO_A, "src/a.py")
    await _seed_claim(client, REPO_B, "src/b.py")
    db = get_service().db
    # Make the repo-B claim strictly newer so an unscoped limit-1 window
    # is filled entirely by repo B.
    async with seam_connection(db) as conn:
        await conn.execute(
            "UPDATE claims SET created_at = datetime(created_at, '+1 hour') "
            "WHERE repo = ?",
            (REPO_B,),
        )

    newest = await db.list_recent_claims(1)
    assert [r["repo"] for r in newest] == [REPO_B]
    # With the filter pushed into SQL, repo A's row survives a window of
    # 1 instead of being crowded out and post-filtered to nothing.
    scoped = await db.list_recent_claims(1, repo=REPO_A)
    assert [r["repo"] for r in scoped] == [REPO_A]


# ---------------------------------------------------------------------------
# request_claim_holder
# ---------------------------------------------------------------------------


async def test_request_claim_holder_resolves_target_claim_engineer(
    client: AsyncClient,
) -> None:
    from coordination.deps import get_service

    await _seed_claim(client, REPO_A, "src/holder.py")
    r = await client.get(f"/claims?repo={REPO_A}", headers=_SHARED_AUTH)
    claim_id = next(
        c["id"] for c in r.json()["claims"] if c["pattern"] == "src/holder.py"
    )
    fr = await client.post(
        "/requests",
        headers=_SHARED_AUTH,
        json={"claim_id": claim_id, "requester": "req", "wait_seconds": 0},
    )
    assert fr.status_code == 200, fr.text
    request_id = fr.json()["id"]

    db = get_service().db
    assert await db.request_claim_holder(request_id) == (True, "seed")
    assert await db.request_claim_holder("nope") == (False, None)
