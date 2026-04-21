"""Tests for the MCP stdio bridge (coord-mcp).

The bridge is the primary integration surface for Claude Code / Codex / Cursor
agents. Each `@mcp.tool()` function proxies a single HTTP call to the
coordination service. These tests verify URL construction, auth header
handling, HTTP method / body shape, and success + error surfaces for every
tool. Network is replaced with `httpx.MockTransport` so the real httpx stack
(URL composition, query encoding, header merging) is exercised - only the
socket layer is mocked.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from coordination import mcp_server


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> list[httpx.Request]:
    """Replace httpx.AsyncClient with a version that uses MockTransport(handler).

    Returns a list that captures every request the code under test sends.
    The handler is wrapped so it always records the request before delegating.
    """
    captured: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording_handler)
        return real_client(**kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", factory)
    return captured


def _json_handler(status: int = 200, body: dict[str, Any] | None = None):
    payload = body if body is not None else {}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


# ---------------------------------------------------------------------------
# list_claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_claims_hits_claims_endpoint_with_default_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc.example:9000")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok-1")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claims": [], "count": 0})
    )

    result = await mcp_server.list_claims()

    assert result == {"claims": [], "count": 0}
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert str(req.url).startswith("http://svc.example:9000/claims")
    # Default filters: active_only=true; engineer/module omitted.
    assert req.url.params.get("active_only") == "true"
    assert "engineer" not in req.url.params
    assert "module" not in req.url.params


@pytest.mark.asyncio
async def test_list_claims_passes_engineer_and_module_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(monkeypatch, _json_handler(200, {"claims": []}))

    await mcp_server.list_claims(
        active_only=False, engineer="alex/claude/main", module="auth"
    )

    req = captured[0]
    assert req.url.params.get("active_only") == "false"
    assert req.url.params.get("engineer") == "alex/claude/main"
    assert req.url.params.get("module") == "auth"


@pytest.mark.asyncio
async def test_list_claims_sends_bearer_token_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "secret-123")
    captured = _install_mock_transport(monkeypatch, _json_handler(200, {"claims": []}))

    await mcp_server.list_claims()

    assert captured[0].headers.get("authorization") == "Bearer secret-123"


@pytest.mark.asyncio
async def test_list_claims_omits_auth_header_when_token_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    captured = _install_mock_transport(monkeypatch, _json_handler(200, {"claims": []}))

    await mcp_server.list_claims()

    assert "authorization" not in {k.lower() for k in captured[0].headers.keys()}


@pytest.mark.asyncio
async def test_list_claims_uses_default_url_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COORD_API_URL", raising=False)
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(monkeypatch, _json_handler(200, {"claims": []}))

    await mcp_server.list_claims()

    assert str(captured[0].url).startswith("http://127.0.0.1:8080/claims")


@pytest.mark.asyncio
async def test_list_claims_strips_trailing_slash_from_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080/")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(monkeypatch, _json_handler(200, {"claims": []}))

    await mcp_server.list_claims()

    # No double slash between host and /claims.
    assert str(captured[0].url).startswith("http://svc:8080/claims")
    assert "//claims" not in str(captured[0].url)


@pytest.mark.asyncio
async def test_list_claims_raises_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    _install_mock_transport(monkeypatch, _json_handler(500, {"detail": "boom"}))

    with pytest.raises(httpx.HTTPStatusError):
        await mcp_server.list_claims()


# ---------------------------------------------------------------------------
# check_conflicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_conflicts_sends_one_pattern_param_per_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(
            200,
            {"has_conflicts": False, "conflicts": [], "safe_to_proceed": True, "safe": True, "suggestion": None},
        ),
    )

    await mcp_server.check_conflicts(
        files=["src/auth/login.ts", "src/auth/logout.ts"], engineer="alice"
    )

    req = captured[0]
    assert req.method == "GET"
    assert str(req.url).startswith("http://svc:8080/conflicts")
    # httpx duplicates the pattern= key for each value.
    pattern_values = req.url.params.get_list("pattern")
    assert pattern_values == ["src/auth/login.ts", "src/auth/logout.ts"]
    assert req.url.params.get("engineer") == "alice"


@pytest.mark.asyncio
async def test_check_conflicts_returns_body_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "has_conflicts": True,
        "conflicts": [{"your_pattern": "src/auth/**", "engineer": "bob"}],
        "safe_to_proceed": False,
        "safe": False,
        "suggestion": "Wait for TTL",
    }
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    _install_mock_transport(monkeypatch, _json_handler(200, body))

    result = await mcp_server.check_conflicts(
        files=["src/auth/login.ts"], engineer="alice"
    )

    assert result == body


@pytest.mark.asyncio
async def test_check_conflicts_passes_params_as_list_of_tuples_preserving_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the local `params` list in check_conflicts is typed as
    list[tuple[str, str | int | float | bool | None]] (httpx's accepted type).
    Monkeypatch AsyncClient.get to capture the exact object passed so we can
    assert it is handed through as a list-of-tuples without mutation. This
    regression-guards the broader type annotation: if anyone retightens the
    local to list[tuple[str, str]], this test still passes (behavior is the
    same), but the mypy check IS the type regression test. What this test
    guards is that we never silently switch to a dict/mapping (which would
    collapse the duplicate `pattern` keys)."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")

    captured_params: list[Any] = []

    real_async_client = httpx.AsyncClient

    class _RecordingClient(real_async_client):  # type: ignore[misc,valid-type]
        async def get(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured_params.append(kwargs.get("params"))
            return await super().get(url, *args, **kwargs)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={
                    "has_conflicts": False,
                    "conflicts": [],
                    "safe_to_proceed": True,
                    "safe": True,
                    "suggestion": None,
                },
            )
        )
        return _RecordingClient(**kwargs)

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", factory)

    files = ["a.py", "b.py", "c.py"]
    await mcp_server.check_conflicts(files=files, engineer="alice")

    assert len(captured_params) == 1
    params = captured_params[0]
    # Must be a list (not a dict/mapping) to preserve duplicate `pattern` keys.
    assert isinstance(params, list)
    # Each entry must be a 2-tuple.
    assert all(isinstance(p, tuple) and len(p) == 2 for p in params)
    # Order and contents must match: one ("pattern", f) per file, then engineer.
    assert params == [
        ("pattern", "a.py"),
        ("pattern", "b.py"),
        ("pattern", "c.py"),
        ("engineer", "alice"),
    ]


@pytest.mark.asyncio
async def test_check_conflicts_raises_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    _install_mock_transport(
        monkeypatch, _json_handler(400, {"detail": "no patterns"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await mcp_server.check_conflicts(files=[], engineer="alice")


# ---------------------------------------------------------------------------
# claim_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_files_posts_correct_body_with_all_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["cid-1"], "conflicts": [], "warnings": [], "options": []})
    )

    await mcp_server.claim_files(
        engineer="alice",
        patterns=["src/auth/**", "src/billing/**"],
        description="refactor auth",
        branch="alice/auth",
        shared_files=["package-lock.json"],
        ttl_hours=6,
    )

    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "http://svc:8080/claims"
    assert req.headers.get("content-type", "").startswith("application/json")

    import json as _json

    body = _json.loads(req.content.decode("utf-8"))
    assert body["engineer"] == "alice"
    assert body["branch"] == "alice/auth"
    assert body["description"] == "refactor auth"
    assert body["ttl_hours"] == 6
    assert body["claims"] == [
        {"type": "file", "pattern": "src/auth/**"},
        {"type": "file", "pattern": "src/billing/**"},
        {"type": "shared_file", "pattern": "package-lock.json"},
    ]


@pytest.mark.asyncio
async def test_claim_files_omits_ttl_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["cid"], "conflicts": [], "warnings": [], "options": []})
    )

    await mcp_server.claim_files(engineer="alice", patterns=["src/**"])

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert "ttl_hours" not in body


@pytest.mark.asyncio
async def test_claim_files_returns_409_body_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 from /claims is meaningful data (conflict details), not an error.
    The MCP tool must surface the JSON body to the agent so it can reason
    about the conflict - raising would hide the conflict list."""
    conflict_body = {
        "claim_ids": [],
        "conflicts": [
            {
                "your_pattern": "src/auth/**",
                "conflicting_claim": {
                    "id": "x",
                    "engineer": "bob",
                    "pattern": "src/auth/login.ts",
                    "severity": "hard",
                    "description": "working on login",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
                "overlap": ["src/auth/login.ts"],
            }
        ],
        "warnings": [],
        "options": ["wait", "narrow_claim", "escalate", "override"],
    }
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    _install_mock_transport(monkeypatch, _json_handler(409, conflict_body))

    result = await mcp_server.claim_files(engineer="alice", patterns=["src/auth/**"])

    assert result == conflict_body
    assert result["conflicts"], "agent needs the conflicts list to make a decision"


@pytest.mark.asyncio
async def test_claim_files_returns_400_body_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 (scope validation failure, negation pattern, zero-match details)
    is also structured data the agent should see - not an unexpected error."""
    body = {
        "claim_ids": [],
        "conflicts": [],
        "warnings": [
            "Pattern '!src/auth/**' starts with '!' (gitignore negation). "
            "Negation patterns have no coherent overlap semantics and are rejected."
        ],
        "options": ["narrow_claim"],
    }
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    _install_mock_transport(monkeypatch, _json_handler(400, body))

    result = await mcp_server.claim_files(engineer="alice", patterns=["!src/auth/**"])

    assert result == body
    assert result["warnings"], "agent needs the warnings to understand the rejection"


@pytest.mark.asyncio
async def test_claim_files_raises_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx is a real error, not structured data. Raise so the agent sees it."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    _install_mock_transport(monkeypatch, _json_handler(503, {"detail": "overloaded"}))

    with pytest.raises(httpx.HTTPStatusError):
        await mcp_server.claim_files(engineer="alice", patterns=["src/auth/**"])


@pytest.mark.asyncio
async def test_claim_files_raises_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth failures should surface loudly - not silently return a 401 body
    that an agent might mistake for a conflict response."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "wrong")
    _install_mock_transport(
        monkeypatch, _json_handler(401, {"detail": "invalid token"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await mcp_server.claim_files(engineer="alice", patterns=["src/auth/**"])


# ---------------------------------------------------------------------------
# release_claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_claims_posts_ids_and_engineer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(monkeypatch, _json_handler(200, {"released": 2}))

    result = await mcp_server.release_claims(
        claim_ids=["cid-1", "cid-2"], engineer="alice"
    )

    import json as _json

    assert result == {"released": 2}
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "http://svc:8080/claims/release"
    body = _json.loads(req.content.decode("utf-8"))
    assert body == {"claim_ids": ["cid-1", "cid-2"], "engineer": "alice"}


@pytest.mark.asyncio
async def test_release_claims_allows_null_engineer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`engineer` is optional on the HTTP API; MCP must preserve that."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    captured = _install_mock_transport(monkeypatch, _json_handler(200, {"released": 1}))

    await mcp_server.release_claims(claim_ids=["cid-1"])

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["claim_ids"] == ["cid-1"]
    assert body["engineer"] is None


@pytest.mark.asyncio
async def test_release_claims_raises_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    _install_mock_transport(monkeypatch, _json_handler(500, {"detail": "boom"}))

    with pytest.raises(httpx.HTTPStatusError):
        await mcp_server.release_claims(claim_ids=["cid"], engineer="alice")


# ---------------------------------------------------------------------------
# env resolution
# ---------------------------------------------------------------------------


def test_base_url_strips_trailing_slashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080///")
    assert mcp_server._base_url() == "http://svc:8080"


def test_headers_include_bearer_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_AUTH_TOKEN", "abc")
    assert mcp_server._headers().get("Authorization") == "Bearer abc"


def test_headers_omit_bearer_when_token_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_AUTH_TOKEN", "")
    assert "Authorization" not in mcp_server._headers()


def test_headers_omit_bearer_when_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    assert "Authorization" not in mcp_server._headers()
