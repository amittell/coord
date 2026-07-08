"""Audit regression tests for the webhook outbox delivery loop.

Covers:

- the stable ``X-Coord-Delivery-Id`` header (the outbox row id) so
  receivers can dedup at-least-once deliveries;
- unsigned rows (empty COORD_WEBHOOK_SECRET) omitting the
  ``X-Coord-Signature`` header entirely instead of sending an empty
  value that looks authentic, plus the once-per-process operator
  warning at emit time;
- delivery-time resolution of the target URL from current settings so
  rotating COORD_WEBHOOK_URL redirects rows already sitting in the
  outbox (with the stored row url as the fallback when the setting is
  cleared).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from coordination.config import Settings
from coordination.db import Database
from coordination.service import CoordinationService


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route ``httpx.AsyncClient(...)`` through MockTransport(handler),
    capturing every request. Mirrors tests/test_webhooks.py."""

    captured: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


def _ok_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


async def _make_service(
    tmp_path: Path, *, webhook_url: str, webhook_secret: str
) -> CoordinationService:
    db_path = tmp_path / "outbox.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        _env_file=None,
    )
    return CoordinationService(db=db, settings=settings)


async def test_delivery_carries_stable_delivery_id_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = await _make_service(
        tmp_path,
        webhook_url="https://receiver.example/hook",
        webhook_secret="s3cret",
    )
    captured = _install_mock_transport(monkeypatch, _ok_handler)

    outbox_id = await service.fire_webhook("claim_granted", {"claim_ids": ["c1"]})
    assert outbox_id is not None

    counts = await service.deliver_pending_webhooks()
    assert counts["delivered"] == 1
    assert len(captured) == 1
    req = captured[0]
    assert req.headers["X-Coord-Delivery-Id"] == outbox_id
    # Signed row keeps its signature header unchanged.
    assert req.headers["X-Coord-Signature"]


async def test_unsigned_rows_omit_signature_header_and_warn_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = await _make_service(
        tmp_path,
        webhook_url="https://receiver.example/hook",
        webhook_secret="",
    )
    captured = _install_mock_transport(monkeypatch, _ok_handler)

    with caplog.at_level(logging.WARNING, logger="coordination.service"):
        first = await service.fire_webhook("claim_granted", {"n": 1})
        second = await service.fire_webhook("claim_granted", {"n": 2})
    assert first is not None and second is not None

    unsigned_warnings = [
        rec for rec in caplog.records if "UNSIGNED" in rec.getMessage()
    ]
    assert len(unsigned_warnings) == 1, (
        "the unsigned-webhook warning must fire exactly once per process"
    )

    counts = await service.deliver_pending_webhooks()
    assert counts["delivered"] == 2
    for req in captured:
        assert "X-Coord-Signature" not in req.headers, (
            "unsigned delivery must omit the signature header entirely"
        )
        assert req.headers["X-Coord-Delivery-Id"]


async def test_delivery_follows_rotated_webhook_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending row enqueued against the old endpoint delivers to the
    CURRENT settings URL after rotation, mirroring the github path."""

    service = await _make_service(
        tmp_path,
        webhook_url="https://new-receiver.example/hook",
        webhook_secret="s3cret",
    )
    captured = _install_mock_transport(monkeypatch, _ok_handler)

    await service.db.enqueue_webhook(
        url="https://old-receiver.example/hook",
        event_type="claim_granted",
        payload_json='{"event_type":"claim_granted","detail":{}}',
        hmac_signature="sig-abc",
    )

    counts = await service.deliver_pending_webhooks()
    assert counts["delivered"] == 1
    assert str(captured[0].url) == "https://new-receiver.example/hook"


async def test_delivery_falls_back_to_row_url_when_setting_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = await _make_service(
        tmp_path, webhook_url="", webhook_secret="s3cret"
    )
    captured = _install_mock_transport(monkeypatch, _ok_handler)

    await service.db.enqueue_webhook(
        url="https://stored-receiver.example/hook",
        event_type="claim_granted",
        payload_json='{"event_type":"claim_granted","detail":{}}',
        hmac_signature="sig-abc",
    )

    counts = await service.deliver_pending_webhooks()
    assert counts["delivered"] == 1
    assert str(captured[0].url) == "https://stored-receiver.example/hook"
