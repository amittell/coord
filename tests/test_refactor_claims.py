"""v0.31 wave 2: POST /claims/refactor + the claim_refactor MCP tool.

``create_refactor_claims`` expands a (file, symbol) refactor intent into
a normal claims batch using the language server's references answer.
These tests drive the full HTTP surface against the stdlib fake LSP
server (tests/fake_lsp_server.py) spawned via the absolute
sys.executable; fixture payloads ride in through environment variables
the subprocess inherits.

All cross-task waiting is done by POLLING for observable state (never
bare sleeps) -- Windows CI timers are too coarse for sleep-based
synchronisation.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import coordination.lsp as lsp_module
from coordination import deps
from coordination.main import app

FAKE_SERVER = Path(__file__).resolve().parent / "fake_lsp_server.py"
FAKE_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_SERVER))}"

_AUTH = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    from coordination.engine import _clear_ls_files_cache

    _clear_ls_files_cache()
    yield
    _clear_ls_files_cache()


@pytest.fixture(autouse=True)
async def _fresh_singleton_pool():
    """Reset the module-level LSP pool around every test so each test's
    fake-server env vars take effect at spawn time, and reap any
    subprocesses the singleton spawned."""

    lsp_module._reset_pool()
    yield
    pool = lsp_module._POOL
    if pool is not None:
        await pool.shutdown_all()
    lsp_module._reset_pool()


@pytest.fixture(autouse=True)
def _service_cache_reset():
    yield
    deps.get_service.cache_clear()


# ---------------------------------------------------------------------------
# Repo + LSP fixture payloads
# ---------------------------------------------------------------------------

# mod.py defines the refactor target (handler) plus a same-file caller
# (local_caller) so one reference resolves to an enclosing symbol in the
# DEFINING file. 0-based LSP lines: handler 0-1, local_caller 4-5.
MOD_PY = (
    "def handler():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def local_caller():\n"
    "    return handler()\n"
)

# caller.py holds two references inside ONE enclosing function so the
# dedupe rule (one claim per enclosing symbol) is observable.
CALLER_PY = (
    "def caller_fn():\n"
    "    a = handler()\n"
    "    b = handler()\n"
    "    return a + b\n"
)

# modlevel.py references handler at module level: no enclosing symbol,
# so the expansion must fall back to a whole-file claim.
MODLEVEL_PY = "value = handler()\n"


def _sym(
    name: str, start_line: int, start_char: int, end_line: int, end_char: int
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": 12,
        "range": {
            "start": {"line": start_line, "character": start_char},
            "end": {"line": end_line, "character": end_char},
        },
        "children": [],
    }


MOD_SYMBOLS = [_sym("handler", 0, 0, 1, 12), _sym("local_caller", 4, 0, 5, 20)]
CALLER_SYMBOLS = [_sym("caller_fn", 0, 0, 3, 16)]


def _make_repo(tmp_path: Path) -> Path:
    """Three-file git repo. git init + add makes ``git ls-files`` see
    the files so the claim-scope ratio guard has a real denominator (a
    refactor batch can legitimately cover several files)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "mod.py").write_text(MOD_PY, encoding="utf-8")
    (repo / "caller.py").write_text(CALLER_PY, encoding="utf-8")
    (repo / "modlevel.py").write_text(MODLEVEL_PY, encoding="utf-8")
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _loc(path: Path, line0: int, char: int) -> dict[str, Any]:
    """One raw LSP Location (0-based lines, file:// URI)."""
    return {
        "uri": path.resolve().as_uri(),
        "range": {
            "start": {"line": line0, "character": char},
            "end": {"line": line0, "character": char + 7},
        },
    }


def _refs_fixture(repo: Path) -> list[dict[str, Any]]:
    """References for handler: two inside caller.py::caller_fn (dedupe),
    one at modlevel.py module level (file scope), one inside
    mod.py::local_caller (enclosing symbol in the defining file)."""
    return [
        _loc(repo / "caller.py", 1, 8),
        _loc(repo / "caller.py", 2, 8),
        _loc(repo / "modlevel.py", 0, 8),
        _loc(repo / "mod.py", 5, 11),
    ]


def _env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    *,
    lsp_enabled: bool = True,
    lsp_command: str = FAKE_CMD,
    **extra: str,
) -> None:
    """Standard env for the ASGI app (mirrors test_rate_limits.py) with
    the LSP knobs the refactor endpoint needs."""
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.setenv("COORD_REPO_ROOT", str(repo))
    monkeypatch.setenv("COORD_MAX_CLAIM_RATIO", "1.0")
    if lsp_enabled:
        monkeypatch.setenv("COORD_LSP_ENABLED", "true")
    else:
        monkeypatch.delenv("COORD_LSP_ENABLED", raising=False)
    monkeypatch.setenv("COORD_LSP_COMMAND_PYTHON", lsp_command)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    deps.get_service.cache_clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain_enrichment() -> None:
    """Await any fire-and-forget callsite-enrichment tasks so the test
    never tears down the event loop (or the pool) under them."""
    svc = deps.get_service()
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)


async def _wait_for_queue_id(
    client: AsyncClient, requester: str, timeout: float = 5.0
) -> str:
    """Poll GET /requests?queued=true until a waiting row appears for
    the given requester and return its queue id (test_api.py pattern)."""
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
        f"queue row for requester {requester!r} did not appear within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Happy path: expansion shape, description default, dedupe
# ---------------------------------------------------------------------------


async def test_refactor_happy_path_expands_definition_and_callsites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv(
        "FAKE_LSP_SYMBOLS_BY_FILE_JSON",
        json.dumps(
            {
                "mod.py": MOD_SYMBOLS,
                "caller.py": CALLER_SYMBOLS,
                "modlevel.py": [],
            }
        ),
    )
    monkeypatch.setenv(
        "FAKE_LSP_REFERENCES_JSON", json.dumps(_refs_fixture(repo))
    )
    _env(tmp_path, monkeypatch, repo)

    async with _client() as client:
        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={
                "engineer": "alice",
                "file": "mod.py",
                "symbol": "handler",
                "new_name": "renamed_handler",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["conflicts"] == []
        # One claim per file: symbol claims on mod.py and caller.py,
        # file claim on modlevel.py (module-level reference).
        assert len(body["claim_ids"]) == 3

        lr = await client.get("/claims", headers=_AUTH)
        assert lr.status_code == 200, lr.text
        rows = lr.json()["claims"]
        by_pattern = {row["pattern"]: row for row in rows}
        assert set(by_pattern) == {"mod.py", "caller.py", "modlevel.py"}
        assert by_pattern["modlevel.py"]["scope_type"] == "file"
        assert by_pattern["mod.py"]["scope_type"] == "symbol"
        assert by_pattern["caller.py"]["scope_type"] == "symbol"
        for row in rows:
            assert (
                row["description"]
                == "refactor: rename handler -> renamed_handler"
            )

        db = deps.get_service().db
        mod_syms = await db.get_claim_symbols(str(by_pattern["mod.py"]["id"]))
        # Definition symbol + the enclosing function of the same-file
        # reference, both as symbol rows on the defining file.
        assert sorted(s["symbol_name"] for s in mod_syms) == [
            "handler",
            "local_caller",
        ]
        caller_syms = await db.get_claim_symbols(
            str(by_pattern["caller.py"]["id"])
        )
        # Dedupe: TWO references inside caller_fn produce ONE claim row.
        assert [s["symbol_name"] for s in caller_syms] == ["caller_fn"]
        modlevel_syms = await db.get_claim_symbols(
            str(by_pattern["modlevel.py"]["id"])
        )
        assert modlevel_syms == []
        await _drain_enrichment()


async def test_refactor_default_description_without_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(MOD_SYMBOLS))
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", "[]")
    _env(tmp_path, monkeypatch, repo)

    async with _client() as client:
        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={"engineer": "alice", "file": "mod.py", "symbol": "handler"},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["claim_ids"]) == 1

        lr = await client.get("/claims", headers=_AUTH)
        rows = lr.json()["claims"]
        assert len(rows) == 1
        assert rows[0]["description"] == "refactor: handler"
        assert rows[0]["pattern"] == "mod.py"
        await _drain_enrichment()


# ---------------------------------------------------------------------------
# Conflict + queue
# ---------------------------------------------------------------------------


async def test_refactor_conflict_returns_409_with_standard_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(MOD_SYMBOLS))
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", "[]")
    _env(tmp_path, monkeypatch, repo)

    async with _client() as client:
        # Bob pre-holds the exact symbol the refactor batch will claim,
        # which routes symbol/symbol same-symbol to a 409.
        rb = await client.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": "bob",
                "claims": [
                    {
                        "type": "file",
                        "pattern": "mod.py",
                        "symbols": ["handler"],
                    }
                ],
            },
        )
        assert rb.status_code == 200, rb.text

        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={
                "engineer": "alice",
                "file": "mod.py",
                "symbol": "handler",
                "new_name": "renamed_handler",
            },
        )
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["claim_ids"] == []
        assert len(body["conflicts"]) == 1
        conflict = body["conflicts"][0]
        assert conflict["your_pattern"] == "mod.py"
        assert conflict["your_symbols"] == ["handler"]
        assert conflict["conflicting_claim"]["engineer"] == "bob"
        assert "wait" in body["options"]
        await _drain_enrichment()


async def test_refactor_partial_conflict_blocks_whole_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-or-nothing batch semantics: when only ONE of the three
    expanded patterns conflicts (bob pre-holds the modlevel.py file
    claim), the whole refactor 409s and alice ends up holding ZERO
    claims -- no partial grant of the non-conflicting patterns."""
    repo = _make_repo(tmp_path)
    monkeypatch.setenv(
        "FAKE_LSP_SYMBOLS_BY_FILE_JSON",
        json.dumps(
            {
                "mod.py": MOD_SYMBOLS,
                "caller.py": CALLER_SYMBOLS,
                "modlevel.py": [],
            }
        ),
    )
    monkeypatch.setenv(
        "FAKE_LSP_REFERENCES_JSON", json.dumps(_refs_fixture(repo))
    )
    _env(tmp_path, monkeypatch, repo)

    async with _client() as client:
        # Bob pre-holds only ONE of the three patterns the refactor
        # will expand to: the whole-file claim on modlevel.py.
        rb = await client.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": "bob",
                "claims": [{"type": "file", "pattern": "modlevel.py"}],
            },
        )
        assert rb.status_code == 200, rb.text

        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={
                "engineer": "alice",
                "file": "mod.py",
                "symbol": "handler",
                "new_name": "renamed_handler",
            },
        )
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["claim_ids"] == []
        assert len(body["conflicts"]) == 1
        conflict = body["conflicts"][0]
        assert conflict["your_pattern"] == "modlevel.py"
        assert conflict["conflicting_claim"]["engineer"] == "bob"

        # All-or-nothing: alice holds nothing; bob's claim is the only
        # active row.
        lr = await client.get("/claims", headers=_AUTH)
        assert lr.status_code == 200, lr.text
        rows = lr.json()["claims"]
        assert [row["engineer"] for row in rows] == ["bob"]
        await _drain_enrichment()


async def test_refactor_wait_seconds_queues_then_grants_on_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(MOD_SYMBOLS))
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", "[]")
    _env(tmp_path, monkeypatch, repo)

    async with _client() as client:
        rb = await client.post(
            "/claims",
            headers=_AUTH,
            json={
                "engineer": "bob",
                "claims": [
                    {
                        "type": "file",
                        "pattern": "mod.py",
                        "symbols": ["handler"],
                    }
                ],
            },
        )
        assert rb.status_code == 200, rb.text
        holder_cid = rb.json()["claim_ids"][0]

        async def refactor_call() -> httpx.Response:
            return await client.post(
                "/claims/refactor",
                headers=_AUTH,
                json={
                    "engineer": "alice",
                    "file": "mod.py",
                    "symbol": "handler",
                    "wait_seconds": 30,
                },
            )

        waiter = asyncio.create_task(refactor_call())
        # Poll for the observable queue row instead of sleeping.
        await _wait_for_queue_id(client, "alice")

        rel = await client.post(
            "/claims/release",
            headers=_AUTH,
            json={"claim_ids": [holder_cid], "engineer": "bob"},
        )
        assert rel.status_code == 200, rel.text

        r = await asyncio.wait_for(waiter, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["claim_ids"], r.text
        await _drain_enrichment()


# ---------------------------------------------------------------------------
# 503: no language server to be had
# ---------------------------------------------------------------------------


async def test_refactor_503_when_lsp_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    _env(tmp_path, monkeypatch, repo, lsp_enabled=False)

    async with _client() as client:
        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={"engineer": "alice", "file": "mod.py", "symbol": "handler"},
        )
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert "COORD_LSP_ENABLED" in detail
        assert "LSP" in detail


async def test_refactor_503_when_language_server_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LSP enabled but the configured server binary does not exist: the
    pool's spawn failure opens the circuit and returns None, which must
    surface as 503, not as a degraded single-file claim."""
    repo = _make_repo(tmp_path)
    _env(
        tmp_path,
        monkeypatch,
        repo,
        lsp_command="/nonexistent/coord-test-lsp-binary",
    )

    async with _client() as client:
        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={"engineer": "alice", "file": "mod.py", "symbol": "handler"},
        )
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert "COORD_LSP_ENABLED" in detail
        assert "language server" in detail

        # Nothing was inserted on the failure path.
        lr = await client.get("/claims", headers=_AUTH)
        assert lr.json()["count"] == 0


# ---------------------------------------------------------------------------
# Guardrails: batch cap + unknown symbol
# ---------------------------------------------------------------------------


async def test_refactor_cap_rejects_oversized_batch_without_inserting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    for name in ("ref_a.py", "ref_b.py", "ref_c.py"):
        (repo / name).write_text("handler()\n", encoding="utf-8")
    monkeypatch.setenv(
        "FAKE_LSP_SYMBOLS_BY_FILE_JSON", json.dumps({"mod.py": MOD_SYMBOLS})
    )
    # Three module-level references in three files -> three whole-file
    # claims, plus the definition symbol claim on mod.py = 4 > cap 2.
    refs = [
        _loc(repo / "ref_a.py", 0, 0),
        _loc(repo / "ref_b.py", 0, 0),
        _loc(repo / "ref_c.py", 0, 0),
    ]
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", json.dumps(refs))
    _env(tmp_path, monkeypatch, repo, COORD_MAX_CLAIM_FILES="2")

    async with _client() as client:
        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={"engineer": "alice", "file": "mod.py", "symbol": "handler"},
        )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["claim_ids"] == []
        assert len(body["warnings"]) == 1
        assert "4 claims" in body["warnings"][0]
        assert "max is 2" in body["warnings"][0]
        assert "COORD_MAX_CLAIM_FILES" in body["warnings"][0]

        lr = await client.get("/claims", headers=_AUTH)
        assert lr.json()["count"] == 0, "cap rejection must insert nothing"


async def test_refactor_unknown_symbol_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(MOD_SYMBOLS))
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", "[]")
    _env(tmp_path, monkeypatch, repo)

    async with _client() as client:
        r = await client.post(
            "/claims/refactor",
            headers=_AUTH,
            json={"engineer": "alice", "file": "mod.py", "symbol": "ghost"},
        )
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["claim_ids"] == []
        assert "Unknown symbol" in body["warnings"][0]
        assert "ghost" in body["warnings"][0]

        lr = await client.get("/claims", headers=_AUTH)
        assert lr.json()["count"] == 0


# ---------------------------------------------------------------------------
# MCP wrapper: structured 503 + happy-path passthrough
# ---------------------------------------------------------------------------


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler
) -> list[httpx.Request]:
    """httpx.MockTransport substitution (test_mcp_server.py pattern)."""
    from coordination import mcp_server

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


async def test_mcp_claim_refactor_surfaces_503_as_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 (LSP disabled/unavailable server-side) must come back as
    data the agent can branch on, never as a raised exception."""
    from coordination import mcp_server

    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.delenv("COORD_REPO_ID", raising=False)
    detail = (
        "refactor claims require COORD_LSP_ENABLED=true and a configured "
        "COORD_REPO_ROOT. LSP integration is disabled or unavailable "
        "(COORD_LSP_ENABLED); refactor claims need a live language server."
    )
    _install_mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(503, json={"detail": detail}),
    )

    result = await mcp_server.claim_refactor(
        engineer="alice", file="mod.py", symbol="handler"
    )
    assert result == {"error": detail, "status": 503}


async def test_mcp_claim_refactor_happy_path_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordination import mcp_server

    monkeypatch.setenv("COORD_API_URL", "http://svc:8080")
    monkeypatch.setenv("COORD_AUTH_TOKEN", "tok")
    monkeypatch.delenv("COORD_REPO_ID", raising=False)
    payload = {
        "claim_ids": ["c-1", "c-2"],
        "conflicts": [],
        "warnings": [],
        "options": [],
    }
    captured = _install_mock_transport(
        monkeypatch, lambda _request: httpx.Response(200, json=payload)
    )

    result = await mcp_server.claim_refactor(
        engineer="alice",
        file="mod.py",
        symbol="handler",
        new_name="renamed_handler",
        wait_seconds=30,
        urgency="high",
    )
    assert result == payload

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/claims/refactor"
    assert request.headers["Authorization"] == "Bearer tok"
    body = json.loads(request.content)
    assert body["engineer"] == "alice"
    assert body["file"] == "mod.py"
    assert body["symbol"] == "handler"
    assert body["new_name"] == "renamed_handler"
    assert body["wait_seconds"] == 30
    assert body["urgency"] == "high"
    assert body["session_id"] == mcp_server._SESSION_ID
    assert "repo" not in body
