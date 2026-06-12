"""v0.31 wave 1: LSP client pool + service integration.

The pool tests drive a stdlib fake language server
(tests/fake_lsp_server.py) spawned with the absolute sys.executable, so
they are in-process-fast asyncio subprocess tests -- no external server
install, no integration marker needed. Fixture payloads ride in via
environment variables the subprocess inherits.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

import coordination.lsp as lsp_module
from coordination.config import Settings
from coordination.db import Database
from coordination.lsp import (
    LspClientPool,
    _encode_message,
    _flatten_document_symbols,
    _read_message,
    get_lsp_pool,
    language_for_path,
)
from coordination.schemas import ClaimItem, CreateClaimsRequest
from coordination.service import CoordinationService

FAKE_SERVER = Path(__file__).resolve().parent / "fake_lsp_server.py"
FAKE_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_SERVER))}"


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
    """Reset the module-level pool around every test so service-level
    tests get a pool built from their own settings, and shut down any
    subprocesses the singleton spawned."""

    lsp_module._reset_pool()
    yield
    pool = lsp_module._POOL
    if pool is not None:
        await pool.shutdown_all()
    lsp_module._reset_pool()


@pytest.fixture()
async def pool_factory():
    """Construct pools and guarantee their subprocesses are reaped even
    when an assertion fails mid-test."""

    pools: list[LspClientPool] = []

    def make(settings: Settings) -> LspClientPool:
        pool = LspClientPool(settings)
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        await pool.shutdown_all()


def _settings(tmp_path: Path, **overrides) -> Settings:
    defaults = dict(
        database_path=tmp_path / "db.sqlite",
        allow_insecure_no_auth=True,
        lsp_enabled=True,
        lsp_command_python=FAKE_CMD,
        lsp_request_timeout_sec=5.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    (repo / "mod.py").write_text(
        "def handler():\n    return 1\n", encoding="utf-8"
    )
    return repo


# DocumentSymbol fixture: Outer class with a nested method, plus the
# top-level handler that matches the on-disk mod.py. Ranges use LSP's
# 0-based lines so the tests prove the 0 -> 1 conversion.
NESTED_FIXTURE = [
    {
        "name": "handler",
        "kind": 12,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 1, "character": 12},
        },
        "selectionRange": {
            "start": {"line": 0, "character": 4},
            "end": {"line": 0, "character": 11},
        },
        "children": [],
    },
    {
        "name": "Outer",
        "kind": 5,
        "range": {
            "start": {"line": 4, "character": 0},
            "end": {"line": 9, "character": 1},
        },
        "selectionRange": {
            "start": {"line": 4, "character": 6},
            "end": {"line": 4, "character": 11},
        },
        "children": [
            {
                "name": "method",
                "kind": 6,
                "range": {
                    "start": {"line": 6, "character": 4},
                    "end": {"line": 8, "character": 8},
                },
                "selectionRange": {
                    "start": {"line": 6, "character": 8},
                    "end": {"line": 6, "character": 14},
                },
                "children": [],
            }
        ],
    },
]


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


async def test_framing_round_trip_two_messages_then_eof() -> None:
    first = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"a": "ü"}}
    second = {"jsonrpc": "2.0", "id": 2, "result": None}
    reader = asyncio.StreamReader()
    reader.feed_data(_encode_message(first) + _encode_message(second))
    reader.feed_eof()

    assert await _read_message(reader) == first
    assert await _read_message(reader) == second
    assert await _read_message(reader) is None


async def test_framing_tolerates_content_type_header() -> None:
    body = json.dumps({"id": 7}).encode("utf-8")
    raw = (
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
        "\r\n"
    ).encode("ascii") + body
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()

    assert await _read_message(reader) == {"id": 7}


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def test_flatten_nested_children_with_one_based_lines() -> None:
    flat = _flatten_document_symbols(NESTED_FIXTURE)
    assert flat is not None
    by_name = {f["name"]: f for f in flat}
    assert set(by_name) == {"handler", "Outer", "method"}

    handler = by_name["handler"]
    assert handler["parent_path"] is None
    assert handler["start_line"] == 1  # LSP line 0 -> 1-based
    assert handler["start_col"] == 0  # columns stay 0-based
    assert handler["end_line"] == 2
    assert handler["end_col"] == 12

    method = by_name["method"]
    assert method["parent_path"] == "Outer"
    assert method["start_line"] == 7
    assert method["end_line"] == 9


def test_flatten_deeply_nested_joins_ancestors_with_double_colon() -> None:
    deep = [
        {
            "name": "A",
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 9, "character": 0},
            },
            "children": [
                {
                    "name": "B",
                    "range": {
                        "start": {"line": 1, "character": 0},
                        "end": {"line": 8, "character": 0},
                    },
                    "children": [
                        {
                            "name": "leaf",
                            "range": {
                                "start": {"line": 2, "character": 4},
                                "end": {"line": 3, "character": 8},
                            },
                        }
                    ],
                }
            ],
        }
    ]
    flat = _flatten_document_symbols(deep)
    assert flat is not None
    leaf = next(f for f in flat if f["name"] == "leaf")
    assert leaf["parent_path"] == "A::B"


def test_flatten_symbolinformation_flat_shape() -> None:
    flat_shape = [
        {
            "name": "handleAuth",
            "kind": 12,
            "containerName": "Router",
            "location": {
                "uri": "file:///x.ts",
                "range": {
                    "start": {"line": 4, "character": 2},
                    "end": {"line": 8, "character": 3},
                },
            },
        }
    ]
    flat = _flatten_document_symbols(flat_shape)
    assert flat is not None
    assert flat[0]["parent_path"] == "Router"
    assert flat[0]["start_line"] == 5
    assert flat[0]["start_col"] == 2
    assert flat[0]["end_line"] == 9
    assert flat[0]["end_col"] == 3


def test_flatten_rejects_garbage() -> None:
    assert _flatten_document_symbols({"not": "a list"}) is None
    assert _flatten_document_symbols("garbage") is None
    assert _flatten_document_symbols([{"no_name": True}]) is None
    assert _flatten_document_symbols(None) is None
    assert _flatten_document_symbols([]) == []


def test_language_detection_by_extension() -> None:
    assert language_for_path("src/a.py") == "python"
    assert language_for_path("src/a.ts") == "typescript"
    assert language_for_path("src/a.TSX") == "typescript"
    assert language_for_path("src/a.js") == "typescript"
    assert language_for_path("src/a.jsx") == "typescript"
    assert language_for_path("src/a.go") == "go"
    assert language_for_path("src/a.rs") is None
    assert language_for_path("Makefile") is None


# ---------------------------------------------------------------------------
# Pool lifecycle against the fake server
# ---------------------------------------------------------------------------


async def test_pool_spawns_once_per_key_and_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(NESTED_FIXTURE))
    repo = _make_repo(tmp_path)
    pool = pool_factory(_settings(tmp_path))

    first = await pool.document_symbols(repo, "python", "mod.py")
    second = await pool.document_symbols(repo, "python", "mod.py")

    assert first is not None and second is not None
    assert {f["name"] for f in first} == {"handler", "Outer", "method"}
    assert pool.spawn_count == 1


async def test_timeout_returns_none_and_counts_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    monkeypatch.setenv("FAKE_LSP_DELAY_SEC", "0.5")
    repo = _make_repo(tmp_path)
    pool = pool_factory(
        _settings(
            tmp_path,
            lsp_request_timeout_sec=0.1,
            lsp_circuit_failure_threshold=10,
        )
    )

    result = await pool.document_symbols(repo, "python", "mod.py")

    assert result is None
    key = ("python", str(repo.resolve()))
    assert pool._circuits[key].consecutive_failures == 1
    assert pool._circuits[key].open_until == 0.0, "circuit must not open yet"


async def test_circuit_opens_after_threshold_and_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    monkeypatch.setenv("FAKE_LSP_DELAY_SEC", "0.5")
    repo = _make_repo(tmp_path)
    pool = pool_factory(
        _settings(
            tmp_path,
            lsp_request_timeout_sec=0.1,
            lsp_circuit_failure_threshold=2,
            lsp_circuit_cooldown_sec=600,
        )
    )

    assert await pool.document_symbols(repo, "python", "mod.py") is None
    assert await pool.document_symbols(repo, "python", "mod.py") is None
    spawns_before_open = pool.spawn_count
    assert spawns_before_open == 2

    # Circuit is open: the next call must short-circuit without
    # touching a subprocess.
    started = time.monotonic()
    assert await pool.document_symbols(repo, "python", "mod.py") is None
    assert pool.spawn_count == spawns_before_open
    assert time.monotonic() - started < 0.1


async def test_spawn_failure_opens_circuit_immediately(
    tmp_path: Path, pool_factory
) -> None:
    repo = _make_repo(tmp_path)
    pool = pool_factory(
        _settings(
            tmp_path,
            lsp_command_python="/nonexistent/coord-test-lsp-binary",
            lsp_circuit_failure_threshold=5,
        )
    )

    assert await pool.document_symbols(repo, "python", "mod.py") is None
    assert pool.spawn_count == 1
    key = ("python", str(repo.resolve()))
    assert pool._circuits[key].open_until > time.monotonic()

    # While open: returns None instantly, no second spawn attempt.
    assert await pool.document_symbols(repo, "python", "mod.py") is None
    assert pool.spawn_count == 1


async def test_crash_mid_stream_returns_none_then_circuit_handles_later_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    # The fake answers initialize (response 1) then dies on the first
    # documentSymbol -- the classic mid-request crash.
    monkeypatch.setenv("FAKE_LSP_DIE_AFTER", "1")
    repo = _make_repo(tmp_path)
    pool = pool_factory(
        _settings(tmp_path, lsp_circuit_failure_threshold=2)
    )

    assert await pool.document_symbols(repo, "python", "mod.py") is None
    key = ("python", str(repo.resolve()))
    assert pool._circuits[key].consecutive_failures == 1
    # The crashed client must have been discarded, not left pooled.
    assert key not in pool._clients

    # Second call respawns (the replacement also dies) and trips the
    # threshold; third call is short-circuited by the open circuit.
    assert await pool.document_symbols(repo, "python", "mod.py") is None
    spawns = pool.spawn_count
    assert await pool.document_symbols(repo, "python", "mod.py") is None
    assert pool.spawn_count == spawns


async def test_idle_shutdown_reaps_client_past_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(NESTED_FIXTURE))
    repo = _make_repo(tmp_path)
    pool = pool_factory(_settings(tmp_path, lsp_idle_shutdown_sec=300))

    assert await pool.document_symbols(repo, "python", "mod.py") is not None
    key = ("python", str(repo.resolve()))
    client = pool._clients[key]

    # Inject a future "now" instead of sleeping: the client has been
    # idle for far longer than the threshold from that vantage point.
    await pool.shutdown_idle(now=time.monotonic() + 10_000)

    assert key not in pool._clients
    assert client.process is not None
    assert client.process.returncode is not None, "process must be reaped"

    # And a fresh call respawns on demand.
    assert await pool.document_symbols(repo, "python", "mod.py") is not None
    assert pool.spawn_count == 2


async def test_shutdown_all_terminates_processes_without_zombies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(NESTED_FIXTURE))
    repo = _make_repo(tmp_path)
    pool = pool_factory(_settings(tmp_path))

    assert await pool.document_symbols(repo, "python", "mod.py") is not None
    clients = list(pool._clients.values())
    assert clients

    await pool.shutdown_all()

    assert pool._clients == {}
    for client in clients:
        assert client.process is not None
        # returncode is only set once the event loop has wait()ed on
        # the child, so a non-None value proves no zombie remains.
        assert client.process.returncode is not None


async def test_missing_file_returns_none_without_charging_circuit(
    tmp_path: Path, pool_factory
) -> None:
    repo = _make_repo(tmp_path)
    pool = pool_factory(_settings(tmp_path))

    assert await pool.document_symbols(repo, "python", "nope.py") is None
    key = ("python", str(repo.resolve()))
    assert pool._circuits[key].consecutive_failures == 0
    assert pool.spawn_count == 0


async def test_garbage_document_symbol_result_counts_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", '{"bogus": true}')
    repo = _make_repo(tmp_path)
    pool = pool_factory(_settings(tmp_path))

    assert await pool.document_symbols(repo, "python", "mod.py") is None
    key = ("python", str(repo.resolve()))
    assert pool._circuits[key].consecutive_failures == 1


# ---------------------------------------------------------------------------
# Service integration: span persistence + validation fallback
# ---------------------------------------------------------------------------


async def _make_service(
    tmp_path: Path, repo_root: Path | None, **settings_overrides
) -> CoordinationService:
    db_path = tmp_path / "svc.sqlite"
    db = Database(db_path)
    await db.init()
    defaults = dict(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=repo_root,
        max_claim_ratio=1.0,
    )
    defaults.update(settings_overrides)
    return CoordinationService(db=db, settings=Settings(**defaults))


def _handler_fixture() -> str:
    """LSP view of repo/mod.py's handler with full column precision so
    a persisted row is unambiguously LSP-resolved (parser rows always
    have NULL columns)."""

    return json.dumps(
        [
            {
                "name": "handler",
                "kind": 12,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 12},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 11},
                },
                "children": [],
            }
        ]
    )


async def test_service_persists_lsp_spans_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", _handler_fixture())
    repo = _make_repo(tmp_path)
    svc = await _make_service(
        tmp_path, repo, lsp_enabled=True, lsp_command_python=FAKE_CMD
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )

    assert result.claim_ids, f"claim rejected: {result.warnings!r}"
    rows = await svc.db.get_claim_symbols(result.claim_ids[0])
    assert len(rows) == 1
    row = rows[0]
    assert row["resolved_by"] == "lsp"
    assert row["start_line"] == 1
    assert row["start_col"] == 0
    assert row["end_line"] == 2
    assert row["end_col"] == 12


async def test_service_falls_back_to_parser_spans_on_lsp_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", '{"bogus": true}')
    repo = _make_repo(tmp_path)
    svc = await _make_service(
        tmp_path, repo, lsp_enabled=True, lsp_command_python=FAKE_CMD
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )

    assert result.claim_ids, f"claim rejected: {result.warnings!r}"
    rows = await svc.db.get_claim_symbols(result.claim_ids[0])
    assert len(rows) == 1
    row = rows[0]
    assert row["resolved_by"] == "parser"
    assert row["start_line"] == 1
    assert row["start_col"] is None
    assert row["end_line"] is not None
    assert row["end_col"] is None


async def test_validation_fallback_accepts_symbol_only_lsp_can_see(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parser cannot see ghost_handler (it is not in the file's
    parseable surface at all), so v0.17 validation would reject the
    claim. With LSP enabled and the server reporting the symbol, the
    claim goes through and the span is LSP-resolved."""

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cond.py").write_text(
        "def visible():\n    return 1\n", encoding="utf-8"
    )
    monkeypatch.setenv(
        "FAKE_LSP_SYMBOLS_JSON",
        json.dumps(
            [
                {
                    "name": "ghost_handler",
                    "kind": 12,
                    "range": {
                        "start": {"line": 3, "character": 0},
                        "end": {"line": 5, "character": 10},
                    },
                    "children": [],
                }
            ]
        ),
    )
    svc = await _make_service(
        tmp_path, repo, lsp_enabled=True, lsp_command_python=FAKE_CMD
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[
                ClaimItem(
                    type="file", pattern="cond.py", symbols=["ghost_handler"]
                )
            ],
        )
    )

    assert result.claim_ids, f"claim rejected: {result.warnings!r}"
    rows = await svc.db.get_claim_symbols(result.claim_ids[0])
    assert rows[0]["resolved_by"] == "lsp"
    assert rows[0]["start_line"] == 4
    assert rows[0]["end_line"] == 6


async def test_validation_still_rejects_when_lsp_disabled(
    tmp_path: Path,
) -> None:
    """Contrast case for the fallback: with LSP off, an unparseable
    symbol gets exactly the v0.17 rejection."""

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "cond.py").write_text(
        "def visible():\n    return 1\n", encoding="utf-8"
    )
    svc = await _make_service(tmp_path, repo, lsp_enabled=False)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[
                ClaimItem(
                    type="file", pattern="cond.py", symbols=["ghost_handler"]
                )
            ],
        )
    )

    assert result.claim_ids == []
    assert any("ghost_handler" in w for w in result.warnings)


async def test_lsp_disabled_never_touches_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lsp_enabled=False posture must be byte-identical to v0.30:
    zero subprocess spawns, zero pool lookups. Booby-trap the pool
    accessor inside the service module to prove it is never reached."""

    import coordination.service as service_module

    def _boom(_settings: Settings) -> LspClientPool:
        raise AssertionError("get_lsp_pool must not be called when disabled")

    monkeypatch.setattr(service_module, "get_lsp_pool", _boom)

    repo = _make_repo(tmp_path)
    svc = await _make_service(
        tmp_path,
        repo,
        lsp_enabled=False,
        lsp_command_python="/nonexistent/coord-test-lsp-binary",
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )

    assert result.claim_ids, f"claim rejected: {result.warnings!r}"
    rows = await svc.db.get_claim_symbols(result.claim_ids[0])
    assert rows[0]["resolved_by"] == "parser"


async def test_get_lsp_pool_singleton_and_reset_hook(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pool_a = get_lsp_pool(settings)
    pool_b = get_lsp_pool(settings)
    assert pool_a is pool_b
    lsp_module._reset_pool()
    pool_c = get_lsp_pool(settings)
    assert pool_c is not pool_a
