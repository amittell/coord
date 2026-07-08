"""Audit regression tests for coordination/mcp_server.py (the MCP bridge).

Findings covered:

- mcp_server.py:1014 -- ``my_requests`` had no ``engineer`` parameter, so
  queue rows created by ``claim_files(engineer=...)`` were invisible
  whenever that name differed from COORD_REQUESTER / COORD_USER.
- mcp_server.py:762 -- ``claim_refactor`` read COORD_REPO_ID raw,
  bypassing the placeholder filter every other tool applies via
  ``_repo_id()``.
- mcp_server.py:490 -- client-side symbol pre-validation resolved paths
  against ``Path.cwd()`` instead of COORD_REPO_ROOT / the git toplevel.
- mcp_server.py:400 -- 403 (scoped token + all_repos) and 422 (schema
  bounds) surfaced as opaque ``raise_for_status`` exceptions with the
  server detail stripped.
- mcp_server.py:810 -- ``coord_notice`` (X-Coord-Token-Warning) was
  dropped by most tools.
- mcp_server.py:1363 -- stale-lock removal in ``_acquire_marker_lock``
  was a TOCTOU that let two contenders both hold the sessions.live lock.
- mcp_server.py:972 -- ``wait_for_request`` re-imported asyncio and used
  the deprecated ``get_event_loop().time()`` accessor for its deadline.
- mcp_server.py:66 -- ``_load_local_env`` aborted the parent-directory
  walk on an unreadable local.env instead of continuing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from coordination import mcp_server


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> list[httpx.Request]:
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


def _json_handler(
    status: int = 200,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
):
    payload = body if body is not None else {}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=headers or {})

    return handler


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.delenv("COORD_REPO_ID", raising=False)
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)
    monkeypatch.delenv("COORD_REQUESTER", raising=False)
    monkeypatch.delenv("COORD_USER", raising=False)
    monkeypatch.delenv("COORD_DISABLE_CLIENT_VALIDATION", raising=False)
    yield


# ---------------------------------------------------------------------------
# my_requests: engineer parameter (queue rows filter by claim-time engineer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_requests_engineer_param_overrides_env_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_REQUESTER", "env-requester")
    monkeypatch.setenv("COORD_USER", "env-user")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"requests": [], "count": 0})
    )

    await mcp_server.my_requests(queued=True, engineer="claim-bot")

    req = captured[0]
    assert req.url.params.get("requester") == "claim-bot"
    assert req.url.params.get("queued") == "true"
    assert req.headers.get("x-coord-engineer") == "claim-bot"


@pytest.mark.asyncio
async def test_my_requests_falls_back_to_env_then_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"requests": [], "count": 0})
    )

    await mcp_server.my_requests()
    assert captured[-1].url.params.get("requester") == "agent"

    monkeypatch.setenv("COORD_USER", "user-name")
    await mcp_server.my_requests()
    assert captured[-1].url.params.get("requester") == "user-name"

    monkeypatch.setenv("COORD_REQUESTER", "requester-name")
    await mcp_server.my_requests()
    assert captured[-1].url.params.get("requester") == "requester-name"


@pytest.mark.asyncio
async def test_my_requests_blank_engineer_uses_env_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_REQUESTER", "env-requester")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"requests": [], "count": 0})
    )

    await mcp_server.my_requests(engineer="   ")

    assert captured[0].url.params.get("requester") == "env-requester"


@pytest.mark.asyncio
async def test_my_requests_sends_session_id_for_server_side_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_SESSION_ID", "sess-abc123")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"requests": [], "count": 0})
    )

    await mcp_server.my_requests(queued=True, engineer="claim-bot")

    assert captured[0].url.params.get("session_id") == "sess-abc123"


# ---------------------------------------------------------------------------
# claim_refactor: placeholder COORD_REPO_ID must be treated as unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_refactor_ignores_placeholder_repo_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_REPO_ID", "example-org/example-repo")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["c1"]})
    )

    await mcp_server.claim_refactor(
        engineer="alice", file="src/auth.py", symbol="handleLogin"
    )

    body = json.loads(captured[0].content.decode("utf-8"))
    assert "repo" not in body


@pytest.mark.asyncio
async def test_claim_refactor_sends_real_repo_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_REPO_ID", "acme/widgets")
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["c1"]})
    )

    await mcp_server.claim_refactor(
        engineer="alice", file="src/auth.py", symbol="handleLogin"
    )

    body = json.loads(captured[0].content.decode("utf-8"))
    assert body["repo"] == "acme/widgets"


# ---------------------------------------------------------------------------
# client-side symbol validation: resolve against COORD_REPO_ROOT, not cwd
# ---------------------------------------------------------------------------


def _seed_checkout(root: Path, source: str) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "mod.py").write_text(source, encoding="utf-8")


@pytest.mark.asyncio
async def test_symbol_validation_uses_coord_repo_root_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd is a DIFFERENT checkout missing the symbol; COORD_REPO_ROOT has
    it. The claim must pass local validation and reach the server."""
    repo_root = tmp_path / "server-root"
    repo_root.mkdir()
    _seed_checkout(repo_root, "def handleLogin():\n    return 1\n")
    stale = tmp_path / "stale-checkout"
    stale.mkdir()
    _seed_checkout(stale, "def somethingElse():\n    return 2\n")
    monkeypatch.chdir(stale)
    monkeypatch.setenv("COORD_REPO_ROOT", str(repo_root))
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"claim_ids": ["c1"], "conflicts": []}),
    )

    result = await mcp_server.claim_files(
        engineer="alice",
        patterns=[],
        symbols={"src/mod.py": ["handleLogin"]},
    )

    assert result["claim_ids"] == ["c1"]
    assert len(captured) == 1, "claim must reach the server"


@pytest.mark.asyncio
async def test_symbol_validation_rejects_typo_against_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "server-root"
    repo_root.mkdir()
    _seed_checkout(repo_root, "def handleLogin():\n    return 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORD_REPO_ROOT", str(repo_root))
    captured = _install_mock_transport(
        monkeypatch, _json_handler(200, {"claim_ids": ["c1"]})
    )

    result = await mcp_server.claim_files(
        engineer="alice",
        patterns=[],
        symbols={"src/mod.py": ["missingFn"]},
    )

    assert result["claim_ids"] == []
    assert any("missingFn" in w for w in result["warnings"])
    assert captured == [], "typo short-circuits without a round-trip"


def test_client_repo_root_prefers_configured_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_REPO_ROOT", str(tmp_path))
    assert mcp_server._client_repo_root() == tmp_path


def test_client_repo_root_treats_placeholder_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_REPO_ROOT", "set-me")
    monkeypatch.chdir(tmp_path)
    root = mcp_server._client_repo_root()
    # tmp_path is outside any git checkout, so the fallback is cwd.
    assert root.resolve() == tmp_path.resolve()


# ---------------------------------------------------------------------------
# 403 / 422: structured error surface instead of opaque raise_for_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_claims_403_returns_server_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "This token is scoped to repo 'acme/widgets'; it cannot access all repos."
    _install_mock_transport(monkeypatch, _json_handler(403, {"detail": detail}))

    result = await mcp_server.list_claims(all_repos=True)

    assert result == {"error": detail, "status": 403}


@pytest.mark.asyncio
async def test_check_conflicts_403_returns_server_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "This token is scoped to repo 'acme/widgets'; it cannot access all repos."
    _install_mock_transport(monkeypatch, _json_handler(403, {"detail": detail}))

    result = await mcp_server.check_conflicts(
        files=["src/a.py"], engineer="alice", all_repos=True
    )

    assert result == {"error": detail, "status": 403}


@pytest.mark.asyncio
async def test_claim_files_422_returns_validation_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = [
        {
            "loc": ["body", "wait_seconds"],
            "msg": "ensure this value is less than or equal to 600",
            "type": "value_error.number.not_le",
        }
    ]
    _install_mock_transport(monkeypatch, _json_handler(422, {"detail": detail}))

    result = await mcp_server.claim_files(
        engineer="alice", patterns=["src/**"], wait_seconds=900
    )

    assert result["status"] == 422
    assert result["error"] == detail


@pytest.mark.asyncio
async def test_request_release_422_returns_validation_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = [
        {
            "loc": ["body", "wait_seconds"],
            "msg": "ensure this value is less than or equal to 600",
            "type": "value_error.number.not_le",
        }
    ]
    _install_mock_transport(monkeypatch, _json_handler(422, {"detail": detail}))

    result = await mcp_server.request_release(
        claim_id="cid-1", reason="need it", wait_seconds=900
    )

    assert result["status"] == 422
    assert result["error"] == detail


@pytest.mark.asyncio
async def test_claim_files_401_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth failures must keep surfacing loudly; only 403/422 became
    structured data."""
    _install_mock_transport(
        monkeypatch, _json_handler(401, {"detail": "invalid token"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await mcp_server.claim_files(engineer="alice", patterns=["src/**"])


# ---------------------------------------------------------------------------
# coord_notice consistency: every dict-returning tool surfaces the warning
# ---------------------------------------------------------------------------

_WARNING = "unscoped token deprecated; ask an operator for a repo-scoped token"
_WARN_HEADERS = {"X-Coord-Token-Warning": _WARNING}


@pytest.mark.asyncio
async def test_release_claims_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch, _json_handler(200, {"released": 1}, headers=_WARN_HEADERS)
    )
    result = await mcp_server.release_claims(claim_ids=["c1"], engineer="alice")
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_pending_requests_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"requests": [], "count": 0}, headers=_WARN_HEADERS),
    )
    result = await mcp_server.pending_requests()
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_request_release_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch,
        _json_handler(
            200, {"request_id": "r1", "decision": "pending"}, headers=_WARN_HEADERS
        ),
    )
    result = await mcp_server.request_release(
        claim_id="c1", reason="need it", wait_seconds=0
    )
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_respond_to_request_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"decision": "approved"}, headers=_WARN_HEADERS),
    )
    result = await mcp_server.respond_to_request(
        request_id="r1", decision="approved"
    )
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_wait_for_request_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"decision": "approved"}, headers=_WARN_HEADERS),
    )
    result = await mcp_server.wait_for_request(request_id="r1", timeout=5)
    assert result["decision"] == "approved"
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_my_requests_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"requests": [], "count": 0}, headers=_WARN_HEADERS),
    )
    result = await mcp_server.my_requests()
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_release_session_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch, _json_handler(200, {"released": 3}, headers=_WARN_HEADERS)
    )
    result = await mcp_server.release_session()
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_cancel_queue_request_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch, _json_handler(200, {"cancelled": True}, headers=_WARN_HEADERS)
    )
    result = await mcp_server.cancel_queue_request(queue_id="q1", engineer="alice")
    assert result["coord_notice"] == _WARNING


@pytest.mark.asyncio
async def test_claim_refactor_503_surfaces_token_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock_transport(
        monkeypatch,
        _json_handler(503, {"detail": "LSP disabled"}, headers=_WARN_HEADERS),
    )
    result = await mcp_server.claim_refactor(
        engineer="alice", file="src/a.py", symbol="f"
    )
    assert result["status"] == 503
    assert result["error"] == "LSP disabled"
    assert result["coord_notice"] == _WARNING


# ---------------------------------------------------------------------------
# wait_for_request: monotonic deadline, no event-loop time accessor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_request_returns_pending_row_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_mock_transport(
        monkeypatch,
        _json_handler(200, {"request_id": "r1", "decision": "pending"}),
    )

    start = time.monotonic()
    result = await mcp_server.wait_for_request(request_id="r1", timeout=0)
    elapsed = time.monotonic() - start

    assert result["decision"] == "pending"
    assert len(captured) == 1, "timeout=0 means exactly one poll"
    assert elapsed < 5.0


# ---------------------------------------------------------------------------
# marker lock: stale break must not evict a freshly re-created lock
# ---------------------------------------------------------------------------


def _leftover_break_dirs(parent: Path) -> list[Path]:
    return list(parent.glob(".sessions.live.lock.break.*"))


def test_break_stale_marker_lock_removes_genuinely_stale_lock(
    tmp_path: Path,
) -> None:
    lock = tmp_path / ".sessions.live.lock"
    lock.mkdir()
    old = time.time() - 3600
    os.utime(lock, (old, old))

    mcp_server._break_stale_marker_lock(lock)

    assert not lock.exists()
    assert _leftover_break_dirs(tmp_path) == []


def test_break_stale_marker_lock_restores_fresh_lock(tmp_path: Path) -> None:
    """A contender that stat'd a stale lock which was then broken and
    re-created by a peer must put the peer's FRESH lock back instead of
    evicting it -- the TOCTOU that let two processes hold the lock."""
    lock = tmp_path / ".sessions.live.lock"
    lock.mkdir()  # fresh mtime: simulates the peer's just-created lock

    mcp_server._break_stale_marker_lock(lock)

    assert lock.exists(), "fresh lock must survive a racing stale-breaker"
    assert _leftover_break_dirs(tmp_path) == []


def test_acquire_marker_lock_still_reclaims_stale_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".sessions.live.lock"
    lock.mkdir()
    old = time.time() - 3600
    os.utime(lock, (old, old))

    acquired = mcp_server._acquire_marker_lock(tmp_path)

    assert acquired == lock
    assert lock.is_dir()
    mcp_server._release_marker_lock(acquired)


def test_acquire_marker_lock_respects_fresh_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / ".sessions.live.lock"
    lock.mkdir()
    monkeypatch.setattr(mcp_server, "_MARKER_LOCK_TIMEOUT_SECONDS", 0.1)

    assert mcp_server._acquire_marker_lock(tmp_path) is None
    assert lock.is_dir(), "fresh holder's lock untouched"


# ---------------------------------------------------------------------------
# _load_local_env: unreadable nearest file continues the walk
# ---------------------------------------------------------------------------


def _seed_local_env(repo_root: Path, body: str) -> Path:
    coord_dir = repo_root / ".coordination"
    coord_dir.mkdir(parents=True, exist_ok=True)
    env_file = coord_dir / "local.env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="permission-based unreadability requires a non-root POSIX user",
)
def test_load_local_env_skips_unreadable_file_and_continues_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    parent_env = _seed_local_env(tmp_path, "COORD_AUTH_TOKEN=parent-token\n")
    child = tmp_path / "packages" / "svc-a"
    child.mkdir(parents=True)
    unreadable = _seed_local_env(child, "COORD_AUTH_TOKEN=child-token\n")
    unreadable.chmod(0o000)
    try:
        loaded = mcp_server._load_local_env(start=child)
    finally:
        unreadable.chmod(0o600)

    assert loaded == parent_env
    assert os.environ["COORD_AUTH_TOKEN"] == "parent-token"
