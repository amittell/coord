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

import os
import subprocess
import sys
from pathlib import Path
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
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "fixed-test-session")
    await mcp_server.check_conflicts(files=files, engineer="alice")

    assert len(captured_params) == 1
    params = captured_params[0]
    # Must be a list (not a dict/mapping) to preserve duplicate `pattern` keys.
    assert isinstance(params, list)
    # Each entry must be a 2-tuple.
    assert all(isinstance(p, tuple) and len(p) == 2 for p in params)
    # Order: pattern entries first (preserved relative to input order), then
    # engineer, then session_id. Session_id is appended last as the
    # activity-ping signal.
    assert params == [
        ("pattern", "a.py"),
        ("pattern", "b.py"),
        ("pattern", "c.py"),
        ("engineer", "alice"),
        ("session_id", "fixed-test-session"),
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
async def test_claim_files_includes_repo_id_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setenv("COORD_REPO_ID", "amittell/coord")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["c"], "conflicts": [], "warnings": [], "options": []})
    )

    await mcp_server.claim_files(engineer="alice", patterns=["src/**"])

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["repo"] == "amittell/coord"


@pytest.mark.asyncio
async def test_claim_files_omits_repo_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.delenv("COORD_REPO_ID", raising=False)
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["c"], "conflicts": [], "warnings": [], "options": []})
    )

    await mcp_server.claim_files(engineer="alice", patterns=["src/**"])

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert "repo" not in body


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


def test_headers_omit_bearer_when_token_is_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracked .mcp.json ships ``COORD_AUTH_TOKEN=set-me`` so OSS users
    see the shape; sending ``Bearer set-me`` to a real server would 401
    and leave the user wondering whether the wrapper or the server is at
    fault. Treating the documented placeholder as "no token" yields a
    cleaner failure mode (no Authorization header at all)."""
    monkeypatch.setenv("COORD_AUTH_TOKEN", "set-me")
    assert "Authorization" not in mcp_server._headers()


# ---------------------------------------------------------------------------
# .coordination/local.env auto-load
# ---------------------------------------------------------------------------


def _seed_local_env(repo_root: Path, body: str) -> Path:
    coord_dir = repo_root / ".coordination"
    coord_dir.mkdir(parents=True, exist_ok=True)
    env_file = coord_dir / "local.env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def test_load_local_env_populates_unset_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_local_env(
        tmp_path,
        "COORD_API_URL=http://svc.example\nCOORD_AUTH_TOKEN=real-token\n",
    )
    for key in ("COORD_API_URL", "COORD_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    loaded = mcp_server._load_local_env(start=tmp_path)

    assert loaded == tmp_path / ".coordination" / "local.env"
    assert os.environ["COORD_API_URL"] == "http://svc.example"
    assert os.environ["COORD_AUTH_TOKEN"] == "real-token"


def test_load_local_env_overrides_placeholder_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the placeholder override: a tracked .mcp.json
    can keep ``COORD_AUTH_TOKEN=set-me`` and the wrapper still picks up
    the real token from local.env on startup. Without this, the
    placeholder in .mcp.json would shadow the real value and the
    sanitisation-vs-prod-config tension would force secrets into git."""
    _seed_local_env(tmp_path, "COORD_AUTH_TOKEN=real-token\n")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "set-me")

    mcp_server._load_local_env(start=tmp_path)

    assert os.environ["COORD_AUTH_TOKEN"] == "real-token"


def test_load_local_env_preserves_real_existing_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell exports or a real .mcp.json env block must win over
    local.env, so an operator's explicit override stays in effect."""
    _seed_local_env(tmp_path, "COORD_AUTH_TOKEN=from-file\n")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "from-shell")

    mcp_server._load_local_env(start=tmp_path)

    assert os.environ["COORD_AUTH_TOKEN"] == "from-shell"


def test_load_local_env_walks_up_to_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """coord-mcp may be spawned with cwd set anywhere under the repo
    (subdir of a monorepo, or a subagent's working dir). The loader has
    to walk up like git does."""
    _seed_local_env(tmp_path, "COORD_AUTH_TOKEN=root-token\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)

    loaded = mcp_server._load_local_env(start=nested)

    assert loaded == tmp_path / ".coordination" / "local.env"
    assert os.environ["COORD_AUTH_TOKEN"] == "root-token"


def test_load_local_env_noop_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_AUTH_TOKEN", "existing")
    assert mcp_server._load_local_env(start=tmp_path) is None
    assert os.environ["COORD_AUTH_TOKEN"] == "existing"


def test_load_local_env_ignores_comments_and_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_local_env(
        tmp_path,
        "# comment line\n"
        '\nCOORD_API_URL="http://svc.example"\n'
        "COORD_AUTH_TOKEN='quoted'\n"
        "INVALID LINE WITHOUT EQUALS\n",
    )
    for key in ("COORD_API_URL", "COORD_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    mcp_server._load_local_env(start=tmp_path)

    assert os.environ["COORD_API_URL"] == "http://svc.example"
    assert os.environ["COORD_AUTH_TOKEN"] == "quoted"


def test_load_local_env_ignores_unknown_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limiting the loader to a known COORD_* allowlist keeps a stray
    line in local.env (an unrelated tool's config, a debugging dump,
    etc.) from silently mutating the MCP wrapper's env."""
    _seed_local_env(
        tmp_path,
        "COORD_AUTH_TOKEN=ok\nPATH=/should/not/leak\nMALICIOUS=value\n",
    )
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MALICIOUS", "preserved")
    before_path = os.environ.get("PATH", "")

    mcp_server._load_local_env(start=tmp_path)

    assert os.environ["COORD_AUTH_TOKEN"] == "ok"
    assert os.environ.get("PATH", "") == before_path
    assert os.environ["MALICIOUS"] == "preserved"


# ---------------------------------------------------------------------------
# session_id (v0.5.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_files_includes_session_id_from_module_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each MCP process generates exactly one session_id at module load
    and reuses it for the lifetime of the process. claim_files must send
    that id on every POST so subagents share the same session."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "test-session-deadbeef")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["c"], "conflicts": [], "warnings": [], "options": []})
    )

    await mcp_server.claim_files(engineer="alice", patterns=["src/**"])

    import json as _json
    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["session_id"] == "test-session-deadbeef"


@pytest.mark.asyncio
async def test_claim_files_session_id_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can pin a stable session id by setting COORD_SESSION_ID
    before launching the MCP server. The module-level constant is
    re-evaluated through the helper so tests / advanced users can
    override it."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setenv("COORD_SESSION_ID", "explicit-session-7777")
    # Force re-resolution.
    monkeypatch.setattr(
        mcp_server, "_SESSION_ID", mcp_server._resolve_session_id()
    )
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["c"], "conflicts": [], "warnings": [], "options": []})
    )

    await mcp_server.claim_files(engineer="alice", patterns=["src/**"])

    import json as _json
    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["session_id"] == "explicit-session-7777"


@pytest.mark.asyncio
async def test_release_session_tool_posts_to_sessions_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """release_session releases every claim with the caller's current
    session_id. Default form takes no arguments and uses the module-level
    session id - the typical 'wrap up the session' call at end of work."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "wrap-this-up")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"released": 5})
    )

    result = await mcp_server.release_session()

    assert result == {"released": 5}
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "http://svc:8080/sessions/wrap-this-up/release"


def test_resolve_session_id_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_SESSION_ID", "from-env")
    assert mcp_server._resolve_session_id() == "from-env"


def test_resolve_session_id_generates_hex_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COORD_SESSION_ID", raising=False)
    sid = mcp_server._resolve_session_id()
    assert len(sid) == 16
    int(sid, 16)  # parses as hex


# ---------------------------------------------------------------------------
# Release-request tools (v0.9.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_release_posts_with_session_and_returns_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setenv("COORD_REQUESTER", "bob")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "requester-session-aaa")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(
            200,
            {
                "id": "req-1",
                "decision": "pending",
                "claim_id": "c-1",
                "urgency": "high",
            },
        ),
    )

    result = await mcp_server.request_release(
        claim_id="c-1",
        reason="hot fix",
        urgency="high",
        wait_seconds=0,
    )

    assert result["id"] == "req-1"
    assert result["decision"] == "pending"
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "http://svc:8080/requests"
    import json as _json

    body = _json.loads(req.content.decode("utf-8"))
    assert body["claim_id"] == "c-1"
    assert body["session_id"] == "requester-session-aaa"
    assert body["requester"] == "bob"
    assert body["wait_seconds"] == 0


@pytest.mark.asyncio
async def test_respond_to_request_posts_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "holder-session-bbb")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"id": "req-1", "decision": "approved"}),
    )

    result = await mcp_server.respond_to_request(
        request_id="req-1",
        decision="approved",
        note="ok",
    )

    assert result["decision"] == "approved"
    req = captured[0]
    assert req.method == "POST"
    assert str(req.url) == "http://svc:8080/requests/req-1/respond"
    import json as _json

    body = _json.loads(req.content.decode("utf-8"))
    assert body["decision"] == "approved"
    assert body["session_id"] == "holder-session-bbb"


@pytest.mark.asyncio
async def test_my_requests_filters_by_requester_and_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setenv("COORD_REQUESTER", "alice")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"requests": [], "count": 0})
    )

    await mcp_server.my_requests()

    req = captured[0]
    assert req.url.params.get("requester") == "alice"
    assert req.url.params.get("decision") == "pending"


# ---------------------------------------------------------------------------
# v0.11.0 -- requested_scope on file_request, narrowed/coexist on respond
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_release_includes_requested_scope_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-empty ``requested_scope`` arg must surface on the body so
    the server can record what the requester actually wanted (often a
    sub-pattern of the holder's claim pattern)."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "scope-session-zzz")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"id": "req-2", "decision": "pending"}),
    )

    await mcp_server.request_release(
        claim_id="c-2",
        reason="need login.py only",
        wait_seconds=0,
        requested_scope="src/auth/login.py",
    )

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["requested_scope"] == "src/auth/login.py"


@pytest.mark.asyncio
async def test_request_release_omits_requested_scope_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty default is the legacy behaviour: do not put a
    ``requested_scope`` key on the body. Server treats absence and
    NULL as the same thing, but a stray empty string would clutter
    the audit log."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "scope-session-yyy")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"id": "req-3", "decision": "pending"}),
    )

    await mcp_server.request_release(
        claim_id="c-3",
        reason="full claim please",
        wait_seconds=0,
    )

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert "requested_scope" not in body


@pytest.mark.asyncio
async def test_respond_to_request_includes_narrowed_pattern_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``decision='narrowed'`` carries a ``narrowed_pattern`` that the
    server uses to open the holder's replacement claim. Bridge must
    pass it through verbatim."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "holder-session-narrow")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"id": "req-4", "decision": "narrowed"}),
    )

    await mcp_server.respond_to_request(
        request_id="req-4",
        decision="narrowed",
        narrowed_pattern="src/auth/utils.py",
    )

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["decision"] == "narrowed"
    assert body["narrowed_pattern"] == "src/auth/utils.py"
    # coexist_pattern must not leak in.
    assert "coexist_pattern" not in body


@pytest.mark.asyncio
async def test_respond_to_request_includes_coexist_pattern_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``decision='coexist'`` carries a ``coexist_pattern`` that the
    server uses to mint the requester's sibling claim."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "holder-session-coex")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"id": "req-5", "decision": "coexist"}),
    )

    await mcp_server.respond_to_request(
        request_id="req-5",
        decision="coexist",
        coexist_pattern="src/auth/login.py",
    )

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["decision"] == "coexist"
    assert body["coexist_pattern"] == "src/auth/login.py"
    # narrowed_pattern must not leak in.
    assert "narrowed_pattern" not in body


@pytest.mark.asyncio
async def test_respond_to_request_omits_unused_pattern_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For the legacy ``approved`` decision neither pattern field
    belongs on the body. Sending empty strings would force the server
    into pattern-validation paths that don't apply here."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "holder-session-plain")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"id": "req-6", "decision": "approved"}),
    )

    await mcp_server.respond_to_request(
        request_id="req-6",
        decision="approved",
    )

    import json as _json

    body = _json.loads(captured[0].content.decode("utf-8"))
    assert body["decision"] == "approved"
    assert "narrowed_pattern" not in body
    assert "coexist_pattern" not in body


# ---------------------------------------------------------------------------
# pending_requests + session id propagation (v0.6.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_requests_tool_uses_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "my-session-aaaa")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"pending": [], "count": 0})
    )

    result = await mcp_server.pending_requests()

    assert result == {"pending": [], "count": 0}
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert (
        str(req.url)
        == "http://svc:8080/sessions/my-session-aaaa/pending_requests"
    )


@pytest.mark.asyncio
async def test_check_conflicts_passes_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activity-touch on the server side keys off session_id, so
    coord-mcp must include it on every check_conflicts call too -- not
    just on claim_files."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "live-session-bbbb")
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"has_conflicts": False, "conflicts": [], "safe_to_proceed": True, "safe": True, "suggestion": None}),
    )

    await mcp_server.check_conflicts(files=["src/x.py"], engineer="alice")

    req = captured[0]
    assert req.url.params.get("session_id") == "live-session-bbbb"


@pytest.mark.asyncio
async def test_list_claims_passes_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Likewise list_claims is an activity signal; the server uses the
    session_id query param to refresh last_activity for the caller's
    held claims."""
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "live-session-cccc")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claims": [], "count": 0})
    )

    await mcp_server.list_claims()

    req = captured[0]
    assert req.url.params.get("session_id") == "live-session-cccc"


# ---------------------------------------------------------------------------
# sessions.live marker (v0.10.0)
#
# coord-mcp publishes its own session id into <repo>/.coordination/sessions.live
# at startup so the pre-push hook can self-exclude live sessions when checking
# for blocking claims. The marker is removed on graceful shutdown.
# ---------------------------------------------------------------------------


def _read_marker_session_ids(path: Path) -> list[str]:
    """Read just the session_id column from a sessions.live file.

    v0.12 format: each non-comment line is "<session_id> <pid> <start_time_ns>"
    space-separated. Tests usually only care about the session_id list.
    """
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.split()[0])
    return out


def _seed_live_marker_line(session_id: str) -> str:
    """Compose a sessions.live entry whose PID is the current process,
    so the v0.12 sweep treats it as a 'live' peer when seeding test
    fixtures. The current pid + its real start_time pass _is_live_pid
    on any platform."""
    pid = os.getpid()
    start = mcp_server._process_start_time_ns(pid)
    return f"{session_id} {pid} {start}"


def test_register_session_marker_creates_file_with_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "abc123def456")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._register_session_marker()

    marker = coord_dir / "sessions.live"
    assert marker.exists()
    assert _read_marker_session_ids(marker) == ["abc123def456"]


def test_register_session_marker_writes_pid_and_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.12 format records PID alongside session_id so the sweep can
    later check liveness via os.kill(pid, 0). The line MUST have at
    least 2 whitespace-separated fields and the second must be our
    own running PID (so subsequent sweeps see us as live)."""
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "v12-fmt-test")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._register_session_marker()

    marker = coord_dir / "sessions.live"
    raw = marker.read_text(encoding="utf-8").splitlines()
    body = [ln for ln in raw if ln.strip() and not ln.strip().startswith("#")]
    assert len(body) == 1
    parts = body[0].split()
    assert parts[0] == "v12-fmt-test"
    assert int(parts[1]) == os.getpid()
    # Field 2 (start_time_ns) is platform-dependent; it's 0 when /proc
    # isn't available. Just assert it parses as an int.
    assert int(parts[2]) >= 0


def test_register_session_marker_appends_when_other_sessions_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    marker = coord_dir / "sessions.live"
    # Pre-existing live session belonging to a different "process":
    # we tag it with our own PID so the v0.12 sweep keeps it as live.
    other = _seed_live_marker_line("other-session-aaa")
    marker.write_text(f"# header comment\n{other}\n\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "my-session-bbb")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._register_session_marker()

    ids = _read_marker_session_ids(marker)
    assert "other-session-aaa" in ids
    assert "my-session-bbb" in ids
    assert len(ids) == 2


def test_register_session_marker_appends_own_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_register_session_marker is append-only: each call adds one line
    to sessions.live without reading or rewriting the file. Duplicate
    entries from multiple calls are harmless -- the hook uses kill -0 to
    filter, and _remove_session_marker rewrites with only live entries on
    graceful shutdown."""
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "dup-test-sid")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._register_session_marker()
    mcp_server._register_session_marker()
    mcp_server._register_session_marker()

    marker = coord_dir / "sessions.live"
    ids = _read_marker_session_ids(marker)
    assert len(ids) == 3, f"three appends should produce three lines; got {ids!r}"
    assert all(sid == "dup-test-sid" for sid in ids)


def test_register_session_marker_does_not_sweep_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registration no longer sweeps dead entries -- that responsibility
    moved to _remove_session_marker so the registration path is a single
    non-blocking append (no read-modify-write race). Dead entries remain
    until the next graceful shutdown rewrites the file."""
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    marker = coord_dir / "sessions.live"

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid

    marker.write_text(f"ghost-session {dead_pid} 0\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "fresh-startup")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._register_session_marker()

    ids = _read_marker_session_ids(marker)
    assert "fresh-startup" in ids, "our session must be registered"
    assert "ghost-session" in ids, (
        "dead entries are NOT swept at registration; they wait for removal"
    )


def test_remove_session_marker_sweeps_dead_pid_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_remove_session_marker sweeps dead-PID entries when it rewrites
    the file. This is the lazy cleanup path for SIGKILL/OOM predecessors
    whose atexit handler never fired."""
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    marker = coord_dir / "sessions.live"

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid

    # Seed the file with a dead entry plus our own live entry.
    live_line = _seed_live_marker_line("our-session")
    marker.write_text(
        f"ghost-session {dead_pid} 0\n{live_line}\n", encoding="utf-8"
    )
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "our-session")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._remove_session_marker()

    assert not marker.exists(), (
        "file should be unlinked when only dead entries + our own remain"
    )


def test_remove_session_marker_drops_legacy_format_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_remove_session_marker rewrites the file with only live v0.12
    entries. Legacy entries (no PID) cannot be verified and are dropped."""
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    marker = coord_dir / "sessions.live"

    # Seed with a legacy entry plus our own live entry.
    live_line = _seed_live_marker_line("current-session")
    marker.write_text(f"legacy-no-pid\n{live_line}\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "current-session")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._remove_session_marker()

    # Our session is removed; legacy entry is also gone (can't verify liveness).
    assert not marker.exists(), (
        "file should be unlinked when only unverifiable legacy entries remain"
    )


def test_remove_session_marker_unlinks_file_when_only_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    marker = coord_dir / "sessions.live"
    marker.write_text(_seed_live_marker_line("solo-session") + "\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "solo-session")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._remove_session_marker()

    assert not marker.exists()


def test_remove_session_marker_leaves_other_sessions_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    marker = coord_dir / "sessions.live"
    marker.write_text(
        "\n".join(
            [
                _seed_live_marker_line("first-session"),
                _seed_live_marker_line("me-session"),
                _seed_live_marker_line("third-session"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "me-session")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    mcp_server._remove_session_marker()

    ids = _read_marker_session_ids(marker)
    assert "me-session" not in ids
    assert "first-session" in ids
    assert "third-session" in ids
    assert len(ids) == 2


def test_is_live_pid_true_for_current_process() -> None:
    assert mcp_server._is_live_pid(os.getpid()) is True


def test_is_live_pid_false_for_dead_process() -> None:
    """Spawn a real subprocess, wait for it to exit, then verify
    _is_live_pid returns False for its PID. POSIX guarantees no PID
    reuse until the parent reaps, which subprocess.wait() does."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert mcp_server._is_live_pid(proc.pid) is False


def test_is_live_pid_false_for_nonpositive_pid() -> None:
    """Defense: pid 0 and negative pids must short-circuit to False
    rather than fall into os.kill semantics (which would signal the
    whole process group / every process you can signal)."""
    assert mcp_server._is_live_pid(0) is False
    assert mcp_server._is_live_pid(-1) is False


def test_register_skips_silently_when_coordination_dir_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents may run from non-coord repos. _repo_root_for_marker returning
    None means we have nowhere to write -- and that is fine, not an error."""
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "no-home-sid")
    monkeypatch.setattr(mcp_server, "_repo_root_for_marker", lambda: None)

    # Must not raise.
    mcp_server._register_session_marker()
    mcp_server._remove_session_marker()


def test_register_skips_silently_on_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only or otherwise hostile .coordination/ must never break the
    MCP startup path: the marker is best-effort, not a hard prerequisite."""
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "write-fail-sid")
    monkeypatch.setattr(
        mcp_server, "_repo_root_for_marker", lambda: coord_dir
    )

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated permission denied")

    monkeypatch.setattr(mcp_server, "_atomic_write_lines", _boom)

    # Must not raise even though the underlying write would explode.
    mcp_server._register_session_marker()


def test_repo_root_for_marker_returns_none_outside_git_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_repo_root_for_marker shells out to `git rev-parse --show-toplevel`.
    Running it from a directory that is not inside any git repo must return
    None rather than raising or guessing. (If pytest's tmp_path happens to
    sit inside an outer repo on the dev machine, that repo is highly
    unlikely to have its own ``.coordination/`` directory, so the helper
    still returns None.)"""
    monkeypatch.chdir(tmp_path)
    result = mcp_server._repo_root_for_marker()
    assert result is None


def test_repo_root_for_marker_returns_none_when_coordination_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even inside a git repo, if there's no .coordination/ subdir (because
    the user has not run `coord init`), the function must return None and
    not create the directory."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    monkeypatch.chdir(tmp_path)

    assert mcp_server._repo_root_for_marker() is None
    assert not (tmp_path / ".coordination").exists()


def test_repo_root_for_marker_returns_coord_dir_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    result = mcp_server._repo_root_for_marker()
    assert result is not None
    assert result.resolve() == coord_dir.resolve()


def test_atomic_write_lines_replaces_destination_atomically(
    tmp_path: Path,
) -> None:
    """The atomic write helper must use a same-directory tempfile + replace
    so that a crash mid-write cannot leave the destination half-written."""
    import os as _os

    target = tmp_path / "sessions.live"
    target.write_text("old-content\n", encoding="utf-8")

    mcp_server._atomic_write_lines(target, ["a", "b", "c"])

    assert target.read_text(encoding="utf-8") == "a\nb\nc\n"
    # No leftover temp files in the same directory.
    leftovers = [p for p in _os.listdir(tmp_path) if p != "sessions.live"]
    assert leftovers == []
