"""Tests for the v0.34 GitHub PR-comment delivery adapter.

Cover ``coordination.github_adapter.post_bounce_comment`` and the
``deliver_pending_webhooks`` routing that hands a ``kind='github'`` row
to the adapter while keeping ``kind='webhook'`` rows on the HTTP POST
path. Network is replaced via :class:`httpx.MockTransport` installed on
``httpx.AsyncClient`` so we exercise the real URL / header / body
plumbing and only fake the socket, mirroring ``tests/test_webhooks.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from coordination.config import Settings
from coordination.db import Database
from coordination.service import CoordinationService
from coordination import github_adapter
from coordination.github_adapter import MARKER, post_bounce_comment


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Make ``httpx.AsyncClient(...)`` route through MockTransport(handler).

    Returns a list that captures every request the code under test
    sends. The handler is wrapped so it always records the request
    before delegating, matching ``tests/test_webhooks.py`` so anyone
    debugging adapter tests recognises it immediately.
    """

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


def _settings() -> Settings:
    return Settings(
        database_path=Path("unused.sqlite"),
        allow_insecure_no_auth=True,
        github_token="ghp_testtoken",
        github_api_base="https://api.github.com",
    )


def _detail() -> dict[str, Any]:
    return {
        "repo": "octo/widgets",
        "pushing_engineer": "alice",
        "pushing_branch": "feature/x",
        "bounced": [
            {
                "files": ["src/auth/login.ts", "src/auth/session.ts"],
                "holder_engineer": "bob",
                "holder_branch": "feature/y",
                "holder_pattern": "src/auth/**",
                "holder_description": "auth refactor",
            }
        ],
    }


@pytest.mark.asyncio
async def test_resolves_pr_and_posts_new_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No existing marker comment: the adapter resolves the open PR by
    head branch, lists the (empty) comment thread, and POSTs a fresh
    comment carrying the marker and the rendered body."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/octo/widgets/pulls":
            # PR resolution: filtered by head=<owner>:<branch>, open state.
            assert request.url.params["head"] == "octo:feature/x"
            assert request.url.params["state"] == "open"
            return httpx.Response(200, json=[{"number": 42}])
        if (
            request.method == "GET"
            and path == "/repos/octo/widgets/issues/42/comments"
        ):
            return httpx.Response(200, json=[])
        if (
            request.method == "POST"
            and path == "/repos/octo/widgets/issues/42/comments"
        ):
            body = json.loads(request.content)["body"]
            assert MARKER in body
            assert "alice" in body
            assert "feature/x" in body
            assert "bob" in body
            assert "src/auth/login.ts" in body
            return httpx.Response(201, json={"id": 1001})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    captured = _install_mock_transport(monkeypatch, handler)

    await post_bounce_comment(_settings(), _detail())

    # PR resolve, comment list, comment post.
    assert [r.method for r in captured] == ["GET", "GET", "POST"]
    # Bearer auth header present on every call.
    for req in captured:
        assert req.headers["Authorization"] == "Bearer ghp_testtoken"


@pytest.mark.asyncio
async def test_updates_existing_marker_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing comment carrying the marker is PATCHed in place
    (dedup) -- no new comment is POSTed."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/octo/widgets/pulls":
            return httpx.Response(200, json=[{"number": 7}])
        if (
            request.method == "GET"
            and path == "/repos/octo/widgets/issues/7/comments"
        ):
            return httpx.Response(
                200,
                json=[
                    {"id": 500, "body": "unrelated chatter"},
                    {"id": 501, "body": f"{MARKER}\nold bounce body"},
                ],
            )
        if (
            request.method == "PATCH"
            and path == "/repos/octo/widgets/issues/comments/501"
        ):
            body = json.loads(request.content)["body"]
            assert MARKER in body
            assert "alice" in body
            return httpx.Response(200, json={"id": 501})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    captured = _install_mock_transport(monkeypatch, handler)

    await post_bounce_comment(_settings(), _detail())

    # PR resolve, comment list, comment PATCH -- never a POST.
    assert [r.method for r in captured] == ["GET", "GET", "PATCH"]
    assert not any(r.method == "POST" for r in captured)


@pytest.mark.asyncio
async def test_no_open_pr_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No open PR for the head branch: the adapter logs and returns
    without listing comments or posting anything (not an error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/repos/octo/widgets/pulls":
            return httpx.Response(200, json=[])
        raise AssertionError(
            f"no further calls expected after empty PR list: "
            f"{request.method} {path}"
        )

    captured = _install_mock_transport(monkeypatch, handler)

    await post_bounce_comment(_settings(), _detail())

    assert [r.method for r in captured] == ["GET"]


@pytest.mark.asyncio
async def test_http_error_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx response from GitHub raises so the outbox retry/backoff
    path applies (the adapter does not swallow API failures)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "server error"})

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(httpx.HTTPStatusError):
        await post_bounce_comment(_settings(), _detail())


@pytest.mark.asyncio
async def test_custom_api_base_is_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-hosted ``github_api_base`` (GitHub Enterprise) is used for
    every call, with a trailing slash trimmed."""

    settings = Settings(
        database_path=Path("unused.sqlite"),
        allow_insecure_no_auth=True,
        github_token="ghp_testtoken",
        github_api_base="https://github.example.com/api/v3/",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "github.example.com"
        assert request.url.path.startswith("/api/v3/")
        path = request.url.path
        if path == "/api/v3/repos/octo/widgets/pulls":
            return httpx.Response(200, json=[{"number": 9}])
        if path == "/api/v3/repos/octo/widgets/issues/9/comments":
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(201, json={"id": 1})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    captured = _install_mock_transport(monkeypatch, handler)

    await post_bounce_comment(settings, _detail())

    assert [r.method for r in captured] == ["GET", "GET", "POST"]


@pytest.fixture()
async def gh_service(tmp_path: Path) -> CoordinationService:
    """Service with both webhook and github transports enabled so the
    routing test can enqueue rows of each kind."""

    db_path = tmp_path / "gh.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        webhook_url="https://receiver.example/hook",
        webhook_secret="s3cret",
        webhook_max_retries=3,
        webhook_retry_backoff_sec=5,
        github_token="ghp_testtoken",
        github_api_base="https://api.github.com",
    )
    return CoordinationService(db=db, settings=settings)


@pytest.mark.asyncio
async def test_delivery_loop_routes_github_and_webhook_rows(
    gh_service: CoordinationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``kind='github'`` row drives the GitHub adapter (PR resolve +
    comment) while a ``kind='webhook'`` row drives the HTTP POST path.
    Both flip to delivered in a single sweep."""

    posted_webhook = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted_webhook
        # Webhook transport: POST to the receiver URL.
        if request.url.host == "receiver.example":
            posted_webhook = True
            return httpx.Response(200, json={"ok": True})
        # GitHub transport: REST calls to api.github.com.
        path = request.url.path
        if path == "/repos/octo/widgets/pulls":
            return httpx.Response(200, json=[{"number": 3}])
        if path == "/repos/octo/widgets/issues/3/comments":
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(201, json={"id": 77})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    _install_mock_transport(monkeypatch, handler)

    webhook_id = await gh_service.db.enqueue_webhook(
        url=gh_service.settings.webhook_url,
        event_type="auto-coexist",
        payload_json='{"event_type":"auto-coexist","detail":{}}',
        hmac_signature="sig-abc",
    )
    github_id = await gh_service.db.enqueue_webhook(
        url=gh_service.settings.webhook_url,
        event_type="push_bounced",
        payload_json=json.dumps({"event_type": "push_bounced", "detail": _detail()}),
        hmac_signature="",
        kind="github",
    )

    counts = await gh_service.deliver_pending_webhooks()

    assert counts == {"delivered": 2, "failed": 0, "exhausted": 0}
    assert posted_webhook is True

    async def _status(outbox_id: str) -> str:
        rows = await gh_service.db.list_pending_webhooks()
        # Delivered rows must no longer be pending.
        assert outbox_id not in {r["id"] for r in rows}
        return "delivered"

    assert await _status(webhook_id) == "delivered"
    assert await _status(github_id) == "delivered"


@pytest.mark.asyncio
async def test_github_row_skipped_when_token_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A github row with no github_token configured is marked delivered
    (skipped) and never hits the network -- so it is not retried forever."""

    db_path = tmp_path / "gh-off.sqlite"
    db = Database(db_path)
    await db.init()
    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        webhook_url="https://receiver.example/hook",
        github_token="",
    )
    svc = CoordinationService(db=db, settings=settings)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("network must not be touched when github_token empty")

    monkeypatch.setattr(
        github_adapter,
        "post_bounce_comment",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("adapter must not run without a token")
        ),
    )
    captured = _install_mock_transport(monkeypatch, handler)

    github_id = await svc.db.enqueue_webhook(
        url=svc.settings.webhook_url,
        event_type="push_bounced",
        payload_json=json.dumps({"event_type": "push_bounced", "detail": _detail()}),
        hmac_signature="",
        kind="github",
    )

    counts = await svc.deliver_pending_webhooks()

    assert counts == {"delivered": 1, "failed": 0, "exhausted": 0}
    assert captured == []
    rows = await svc.db.list_pending_webhooks()
    assert github_id not in {r["id"] for r in rows}
