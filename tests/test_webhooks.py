"""Tests for the v0.27 webhook delivery loop.

Cover the service-level ``deliver_pending_webhooks`` sweep that drains
the ``webhook_outbox`` table populated by ``fire_webhook``. Network is
replaced via :class:`httpx.MockTransport` installed on
``httpx.AsyncClient`` so we exercise the real URL / header / body
plumbing and only fake the socket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import aiosqlite
import httpx
import pytest

from coordination.config import Settings
from coordination.db import Database, _configure_sqlite
from coordination.service import CoordinationService


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Make ``httpx.AsyncClient(...)`` route through MockTransport(handler).

    Returns a list that captures every request the code under test
    sends. The handler is wrapped so it always records the request
    before delegating, matching the pattern in ``tests/test_mcp_server.py``
    so anyone debugging webhook tests recognises it immediately.
    """

    captured: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording_handler)
        return real_client(*args, **kwargs)

    # The delivery loop imports httpx locally; patching the top-level
    # ``httpx.AsyncClient`` symbol catches the call regardless of where
    # the import lives in the module under test.
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


@pytest.fixture()
async def service(tmp_path: Path) -> CoordinationService:
    db_path = tmp_path / "webhooks.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        webhook_url="https://receiver.example/hook",
        webhook_secret="s3cret",
        webhook_max_retries=3,
        # Short backoff keeps the retry-window assertions inside test
        # latency budgets without making the production default less
        # conservative.
        webhook_retry_backoff_sec=5,
    )
    return CoordinationService(db=db, settings=settings)


async def _backdate_next_attempt(db: Database, outbox_id: str) -> None:
    """Move a webhook_outbox row's next_attempt_at into the past so the
    next ``list_pending_webhooks`` poll picks it up immediately. Used by
    the retry tests so we can drive multiple attempts without sleeping
    through the production backoff."""

    past = (
        datetime.now(UTC) - timedelta(hours=1)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    async with aiosqlite.connect(db.path) as conn:
        await _configure_sqlite(conn)
        await conn.execute(
            "UPDATE webhook_outbox SET next_attempt_at = ? WHERE id = ?",
            (past, outbox_id),
        )
        await conn.commit()


async def _get_outbox_row(db: Database, outbox_id: str) -> dict[str, Any]:
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        await _configure_sqlite(conn)
        cur = await conn.execute(
            "SELECT * FROM webhook_outbox WHERE id = ?", (outbox_id,)
        )
        row = await cur.fetchone()
    assert row is not None, f"outbox row {outbox_id!r} disappeared"
    return dict(row)


@pytest.mark.asyncio
async def test_webhook_delivery_posts_pending_rows(
    service: CoordinationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: a pending outbox row gets POSTed with the right
    HMAC + event-type headers, the row body is forwarded verbatim,
    and the row flips to ``delivered`` after a 2xx response."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    captured = _install_mock_transport(monkeypatch, handler)

    outbox_id = await service.db.enqueue_webhook(
        url=service.settings.webhook_url,
        event_type="auto-coexist",
        payload_json='{"event_type":"auto-coexist","detail":{}}',
        hmac_signature="sig-abc",
    )

    counts = await service.deliver_pending_webhooks()

    assert counts == {"delivered": 1, "failed": 0, "exhausted": 0}
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == service.settings.webhook_url
    assert req.headers["X-Coord-Signature"] == "sig-abc"
    assert req.headers["X-Coord-Event-Type"] == "auto-coexist"
    assert req.headers["Content-Type"] == "application/json"
    assert req.content == b'{"event_type":"auto-coexist","detail":{}}'

    row = await _get_outbox_row(service.db, outbox_id)
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    assert row["last_attempt_at"] is not None


@pytest.mark.asyncio
async def test_webhook_delivery_retries_on_failure(
    service: CoordinationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 response advances retry_count and schedules the next
    attempt at ``now + backoff * 2**retry_count``. A second sweep
    before that timestamp is a no-op (rate-limited); backdating the
    timestamp lets us drive the second attempt."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "overloaded"})

    captured = _install_mock_transport(monkeypatch, handler)

    outbox_id = await service.db.enqueue_webhook(
        url=service.settings.webhook_url,
        event_type="auto-narrow",
        payload_json="{}",
        hmac_signature="sig-1",
    )

    before_first = datetime.now(UTC)
    counts = await service.deliver_pending_webhooks()
    assert counts == {"delivered": 0, "failed": 1, "exhausted": 0}
    assert len(captured) == 1

    row = await _get_outbox_row(service.db, outbox_id)
    assert row["status"] == "failed"
    assert row["retry_count"] == 1
    assert row["last_error"] == "HTTP 503"
    backoff = service.settings.webhook_retry_backoff_sec
    next_dt = datetime.fromisoformat(
        row["next_attempt_at"].replace("Z", "+00:00")
    )
    # First failure (retry_count was 0 going in) schedules at +backoff.
    expected = before_first + timedelta(seconds=backoff)
    assert next_dt >= expected - timedelta(seconds=2)
    assert next_dt <= expected + timedelta(seconds=5)

    # Second sweep BEFORE next_attempt_at fires: rate-limited, no POST.
    counts = await service.deliver_pending_webhooks()
    assert counts == {"delivered": 0, "failed": 0, "exhausted": 0}
    assert len(captured) == 1
    row = await _get_outbox_row(service.db, outbox_id)
    assert row["retry_count"] == 1

    # Backdate next_attempt_at so the third sweep picks the row up.
    await _backdate_next_attempt(service.db, outbox_id)

    before_second = datetime.now(UTC)
    counts = await service.deliver_pending_webhooks()
    assert counts == {"delivered": 0, "failed": 1, "exhausted": 0}
    assert len(captured) == 2

    row = await _get_outbox_row(service.db, outbox_id)
    assert row["status"] == "failed"
    assert row["retry_count"] == 2
    # Second failure (retry_count was 1 going in) schedules at +backoff*2.
    next_dt = datetime.fromisoformat(
        row["next_attempt_at"].replace("Z", "+00:00")
    )
    expected = before_second + timedelta(seconds=backoff * 2)
    assert next_dt >= expected - timedelta(seconds=2)
    assert next_dt <= expected + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_webhook_exhausts_after_max_retries(
    service: CoordinationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive ``webhook_max_retries`` failed attempts and confirm the
    row flips to ``exhausted`` on the final attempt and is then
    skipped by the next sweep."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "broken"})

    _install_mock_transport(monkeypatch, handler)

    max_retries = service.settings.webhook_max_retries
    outbox_id = await service.db.enqueue_webhook(
        url=service.settings.webhook_url,
        event_type="claim_granted",
        payload_json="{}",
        hmac_signature="sig-x",
    )

    for attempt in range(max_retries):
        if attempt > 0:
            await _backdate_next_attempt(service.db, outbox_id)
        counts = await service.deliver_pending_webhooks()
        if attempt + 1 < max_retries:
            assert counts["failed"] == 1
            assert counts["exhausted"] == 0
        else:
            assert counts["failed"] == 0
            assert counts["exhausted"] == 1

    row = await _get_outbox_row(service.db, outbox_id)
    assert row["status"] == "exhausted"
    assert row["retry_count"] == max_retries
    assert row["last_error"] == "HTTP 500"

    # Once exhausted, list_pending_webhooks must filter it out so the
    # loop never re-tries it -- enforce that here by running another
    # sweep and confirming counts are all zero.
    counts = await service.deliver_pending_webhooks()
    assert counts == {"delivered": 0, "failed": 0, "exhausted": 0}


@pytest.mark.asyncio
async def test_webhook_disabled_when_url_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``webhook_url`` is empty, ``fire_webhook`` is a no-op
    (no outbox row is inserted) and ``deliver_pending_webhooks``
    returns zero counts without contacting the network."""

    db_path = tmp_path / "webhooks-off.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        webhook_url="",
    )
    svc = CoordinationService(db=db, settings=settings)

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("network must not be touched when webhook_url is empty")

    captured = _install_mock_transport(monkeypatch, handler)

    result = await svc.fire_webhook("auto-coexist", {"foo": "bar"})
    assert result is None

    async with aiosqlite.connect(db.path) as conn:
        await _configure_sqlite(conn)
        cur = await conn.execute("SELECT COUNT(*) FROM webhook_outbox")
        (n,) = await cur.fetchone()
    assert n == 0

    counts = await svc.deliver_pending_webhooks()
    assert counts == {"delivered": 0, "failed": 0, "exhausted": 0}
    assert captured == []
