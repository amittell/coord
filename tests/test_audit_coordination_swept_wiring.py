"""Audit: coverage for the cross-package wiring landed by the fixups pass.

- The background cleanup loop garbage-collects symbol child rows of
  long-released claims via ``db.purge_released_symbol_rows``, gated on
  ``COORD_SYMBOL_PURGE_AFTER_SEC`` (0 disables).
- ``_enqueue_and_wait`` picks the queue target item by pattern AND
  symbol set, so a batch with duplicate patterns enqueues the item that
  actually conflicted instead of the first pattern match.
- The MCP ``respond_to_request`` tool accepts an optional ``engineer``
  override that lands in both the request body and the
  ``X-Coord-Engineer`` header (for fleets whose token identity differs
  from the claim-holder engineer name), and stays absent when omitted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
import pytest

from coordination import mcp_server
from coordination.config import Settings
from coordination.db import Database
from coordination.main import app
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Lifespan wiring: released-claim symbol-row purge
# ---------------------------------------------------------------------------


@pytest.fixture()
def lifespan_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_DISABLE_BACKGROUND_CLEANUP", raising=False)
    monkeypatch.delenv("COORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("COORD_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    yield monkeypatch
    deps.get_service.cache_clear()


async def test_cleanup_loop_purges_released_symbol_rows(
    lifespan_env: pytest.MonkeyPatch,
) -> None:
    lifespan_env.setenv("COORD_SYMBOL_PURGE_AFTER_SEC", "1234")

    from coordination.deps import get_service

    svc = get_service()
    called = asyncio.Event()
    seen: list[int] = []

    async def rec_purge(*, older_than_sec: int) -> dict[str, int]:
        seen.append(older_than_sec)
        called.set()
        return {"claim_symbols": 0, "callsites": 0, "renames": 0}

    lifespan_env.setattr(svc.db, "purge_released_symbol_rows", rec_purge)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(called.wait(), timeout=2.0)

    assert seen and seen[0] == 1234


async def test_symbol_purge_disabled_at_zero(
    lifespan_env: pytest.MonkeyPatch,
) -> None:
    lifespan_env.setenv("COORD_SYMBOL_PURGE_AFTER_SEC", "0")

    from coordination.deps import get_service

    svc = get_service()
    purge_calls: list[int] = []
    queue_reaped = asyncio.Event()

    async def rec_purge(*, older_than_sec: int) -> dict[str, int]:
        purge_calls.append(older_than_sec)
        return {"claim_symbols": 0, "callsites": 0, "renames": 0}

    async def rec_expire(now_iso: str | None = None) -> int:
        queue_reaped.set()
        return 0

    lifespan_env.setattr(svc.db, "purge_released_symbol_rows", rec_purge)
    lifespan_env.setattr(svc.db, "expire_stale_queue_entries", rec_expire)

    async with app.router.lifespan_context(app):
        # The queue reap runs immediately before the purge gate in the
        # same cleanup iteration, so once it has fired (plus a beat for
        # the same iteration to finish) an empty purge_calls proves the
        # 0 setting disabled the purge rather than the loop not having
        # run yet.
        await asyncio.wait_for(queue_reaped.wait(), timeout=2.0)
        await asyncio.sleep(0.1)

    assert purge_calls == []


# ---------------------------------------------------------------------------
# Queue target selection: duplicate patterns pick the conflicting symbols
# ---------------------------------------------------------------------------


async def test_enqueue_picks_item_by_pattern_and_symbols(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    svc = CoordinationService(
        db=db,
        settings=Settings(
            database_path=db_path,
            allow_insecure_no_auth=True,
            max_claim_ratio=1.0,
        ),
    )

    holder = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            session_id="sess-a",
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )
    assert holder.claim_ids, holder.warnings

    # Two requester items share the pattern; only the SECOND conflicts
    # (same symbol as the holder). A pattern-only first-match would
    # enqueue the first item's symbols.
    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="sess-b",
            wait_seconds=1,
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["other"]),
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"]),
            ],
        )
    )
    # The wait times out against the live holder; either response shape
    # is fine -- the assertion is on what was enqueued.
    assert not result.claim_ids or result.warnings

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT pattern, symbols FROM claim_queue "
            "WHERE requester_engineer = 'bob'"
        )
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["pattern"] == "mod.py"
    assert json.loads(rows[0]["symbols"]) == ["handler"]


# ---------------------------------------------------------------------------
# MCP respond_to_request: optional engineer override
# ---------------------------------------------------------------------------


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> list[httpx.Request]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"id": "req-1", "decision": "approved"}
        )

    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", factory)
    return captured


async def test_respond_engineer_override_sent_in_body_and_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "holder-session")
    captured = _install_mock_transport(monkeypatch)

    result = await mcp_server.respond_to_request(
        request_id="req-1",
        decision="approved",
        engineer="alice-holder",
    )

    assert result["decision"] == "approved"
    req = captured[0]
    body = json.loads(req.content.decode("utf-8"))
    assert body["engineer"] == "alice-holder"
    assert req.headers["X-Coord-Engineer"] == "alice-holder"


async def test_respond_engineer_omitted_stays_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "holder-session")
    captured = _install_mock_transport(monkeypatch)

    await mcp_server.respond_to_request(
        request_id="req-1",
        decision="denied",
    )

    req = captured[0]
    body = json.loads(req.content.decode("utf-8"))
    assert "engineer" not in body
    assert "X-Coord-Engineer" not in req.headers
