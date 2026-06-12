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
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

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
from coordination.service import (
    CoordinationService,
    _tightest_enclosing_symbol,
)

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


def test_tightest_enclosing_symbol_end_position_is_exclusive() -> None:
    """LSP Range ends are exclusive: a reference whose identifier
    starts exactly at a symbol's end position (e.g. ``function a(){}a();``
    in one line) is OUTSIDE that symbol; the start position is
    inclusive. Lines are the pool's 1-based output, columns 0-based."""
    flat = [
        {
            "name": "fn",
            "parent_path": None,
            "start_line": 1,
            "start_col": 0,
            "end_line": 1,
            "end_col": 14,
        }
    ]
    # Start position is inclusive.
    assert _tightest_enclosing_symbol(flat, 1, 0) == "fn"
    # Last position covered by the (exclusive-end) range.
    assert _tightest_enclosing_symbol(flat, 1, 13) == "fn"
    # Exactly at the end position: outside.
    assert _tightest_enclosing_symbol(flat, 1, 14) is None
    # Before the start column on the start line: outside.
    flat[0]["start_col"] = 4
    assert _tightest_enclosing_symbol(flat, 1, 3) is None


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


async def test_ensure_open_reopens_when_file_changes_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool_factory
) -> None:
    """Per the LSP contract a server owns an open document's text and
    never re-reads disk, so an open-once client serves stale content
    for its whole lifetime -- which would silently defeat the rename
    sweep (the old symbol still 'exists' in the server's stale copy).
    Pin the re-open: a changed (mtime_ns, size) signature must produce
    didClose + a fresh didOpen; an unchanged file must not."""
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", json.dumps(NESTED_FIXTURE))
    repo = _make_repo(tmp_path)
    pool = pool_factory(_settings(tmp_path))

    assert await pool.document_symbols(repo, "python", "mod.py") is not None
    client = next(iter(pool._clients.values()))

    sent: list[str] = []
    real_notify = client.notify

    async def recording_notify(method: str, params: Any) -> None:
        sent.append(method)
        await real_notify(method, params)

    client.notify = recording_notify  # type: ignore[method-assign]

    # Unchanged file: no further didOpen traffic.
    assert await pool.document_symbols(repo, "python", "mod.py") is not None
    assert sent == []

    # Rewrite the file (content change alters size; bump mtime too so
    # coarse-mtime filesystems cannot make this flaky).
    target = repo / "mod.py"
    target.write_text(target.read_text() + "\n\ndef appended():\n    pass\n")
    os.utime(target, ns=(time.time_ns(), time.time_ns()))

    assert await pool.document_symbols(repo, "python", "mod.py") is not None
    assert sent == ["textDocument/didClose", "textDocument/didOpen"]


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
    # Drain wave-2 background enrichment before teardown closes the pool.
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)
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


# ---------------------------------------------------------------------------
# v0.31 wave 2: callsite enrichment
# ---------------------------------------------------------------------------


def _loc(path: Path, line0: int, char: int) -> dict[str, object]:
    """One raw LSP Location (0-based lines, file:// URI)."""
    return {
        "uri": path.resolve().as_uri(),
        "range": {
            "start": {"line": line0, "character": char},
            "end": {"line": line0, "character": char + 7},
        },
    }


CALLER_PY = (
    "def caller_fn():\n"
    "    a = handler()\n"
    "    b = handler()\n"
    "    return a + b\n"
)

# Variant with a second function that contains NO handler callsite, so a
# symbol-scope requester on ``unrelated_fn`` has a meaningful negative
# case (its span, lines 7-8, excludes the recorded callsites at 2-3).
CALLER_TWO_FN_PY = (
    "def caller_fn():\n"
    "    a = handler()\n"
    "    b = handler()\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def unrelated_fn():\n"
    "    return 42\n"
)


async def _claim_handler(
    svc: CoordinationService,
    *,
    engineer: str = "alice",
    session_id: str | None = None,
) -> str:
    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer=engineer,
            session_id=session_id,
            claims=[
                ClaimItem(type="file", pattern="mod.py", symbols=["handler"])
            ],
        )
    )
    assert result.claim_ids, f"claim rejected: {result.warnings!r}"
    return result.claim_ids[0]


async def test_enrichment_persists_callsites_and_replaces_wholesale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    (repo / "caller.py").write_text(CALLER_PY, encoding="utf-8")
    refs = [
        _loc(repo / "caller.py", 1, 8),
        _loc(repo / "caller.py", 2, 8),
        _loc(repo / "util.py", 0, 0),
    ]
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", _handler_fixture())
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", json.dumps(refs))
    svc = await _make_service(
        tmp_path, repo, lsp_enabled=True, lsp_command_python=FAKE_CMD
    )

    cid = await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks)

    rows = await svc.db.list_callsites_for_claims([cid])
    # file_path repo-root-relative, lines converted to 1-based.
    assert [(r["file_path"], r["line"], r["character"]) for r in rows] == [
        ("caller.py", 2, 8),
        ("caller.py", 3, 8),
        ("util.py", 1, 0),
    ]
    assert all(r["symbol_path"] == "handler" for r in rows)
    assert all(r["claim_id"] == cid for r in rows)

    # Re-enrichment with the same answer must not accrete duplicates.
    await svc._enrich_claim_callsites(cid)
    rows_again = await svc.db.list_callsites_for_claims([cid])
    assert len(rows_again) == 3

    # Wholesale replace: a fresh server answering differently leaves
    # ONLY the new callsites behind. Reset the pool so the new env-fed
    # fixture takes effect at spawn time.
    monkeypatch.setenv(
        "FAKE_LSP_REFERENCES_JSON",
        json.dumps([_loc(repo / "caller.py", 9, 4)]),
    )
    old_pool = lsp_module._POOL
    assert old_pool is not None
    await old_pool.shutdown_all()
    lsp_module._reset_pool()

    await svc._enrich_claim_callsites(cid)
    rows_replaced = await svc.db.list_callsites_for_claims([cid])
    assert [(r["file_path"], r["line"]) for r in rows_replaced] == [
        ("caller.py", 10)
    ]


async def test_enrichment_caps_stored_callsites_at_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    refs = [_loc(repo / "caller.py", i, 0) for i in range(205)]
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", _handler_fixture())
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", json.dumps(refs))
    svc = await _make_service(
        tmp_path, repo, lsp_enabled=True, lsp_command_python=FAKE_CMD
    )

    cid = await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks)

    rows = await svc.db.list_callsites_for_claims([cid])
    assert len(rows) == 200
    # Truncation keeps the head of the references list.
    assert max(r["line"] for r in rows) == 200


# ---------------------------------------------------------------------------
# v0.31 wave 2: advisory CALLSITE_OVERLAP on grant
# ---------------------------------------------------------------------------


async def _make_enriched_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    caller_source: str = CALLER_PY,
) -> tuple[CoordinationService, str]:
    """alice (session sess-a) claims mod.py::handler; enrichment lands
    callsites in caller.py at lines 2 and 3. ``caller_source`` swaps
    the caller.py content (callsite lines stay 2 and 3) so requester
    tests can claim symbols that do / do not contain the callsites."""
    repo = _make_repo(tmp_path)
    (repo / "caller.py").write_text(caller_source, encoding="utf-8")
    refs = [
        _loc(repo / "caller.py", 1, 8),
        _loc(repo / "caller.py", 2, 8),
    ]
    monkeypatch.setenv("FAKE_LSP_SYMBOLS_JSON", _handler_fixture())
    monkeypatch.setenv("FAKE_LSP_REFERENCES_JSON", json.dumps(refs))
    svc = await _make_service(
        tmp_path, repo, lsp_enabled=True, lsp_command_python=FAKE_CMD
    )
    holder_cid = await _claim_handler(svc, session_id="sess-a")
    await asyncio.gather(*svc._enrichment_tasks)
    rows = await svc.db.list_callsites_for_claims([holder_cid])
    assert len(rows) == 2, "enrichment must land before the advisory test"
    return svc, holder_cid


async def test_callsite_advisory_warns_cross_engineer_file_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite

    svc, holder_cid = await _make_enriched_holder(tmp_path, monkeypatch)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="sess-b",
            claims=[ClaimItem(type="file", pattern="caller.py")],
        )
    )

    # Advisory, not blocking: the grant must succeed.
    assert result.claim_ids, f"grant blocked: {result.warnings!r}"
    advisories = [w for w in result.warnings if w.startswith("advisory:")]
    assert len(advisories) == 1
    advisory = advisories[0]
    assert "alice" in advisory
    assert holder_cid in advisory
    assert "'mod.py'" in advisory
    assert "2 recorded callsite(s)" in advisory
    assert "'caller.py'" in advisory
    assert "lines 2, 3" in advisory

    # Each finding also lands as a callsite-advisory audit event.
    async with aiosqlite.connect(svc.db.path) as conn:
        cur = await conn.execute(
            "SELECT detail FROM request_events "
            "WHERE event_type = 'callsite-advisory'"
        )
        events = await cur.fetchall()
    assert len(events) == 1
    detail = json.loads(events[0][0])
    assert detail["holder_claim_id"] == holder_cid
    assert detail["requester_claim_id"] == result.claim_ids[0]
    assert detail["file"] == "caller.py"
    assert detail["lines"] == [2, 3]


async def test_callsite_advisory_not_emitted_for_same_engineer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _holder_cid = await _make_enriched_holder(tmp_path, monkeypatch)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            session_id="sess-a2",
            claims=[ClaimItem(type="file", pattern="caller.py")],
        )
    )

    assert result.claim_ids, f"grant blocked: {result.warnings!r}"
    assert not [w for w in result.warnings if w.startswith("advisory:")]


async def test_callsite_advisory_not_emitted_for_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _holder_cid = await _make_enriched_holder(tmp_path, monkeypatch)

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="sess-a",
            claims=[ClaimItem(type="file", pattern="caller.py")],
        )
    )

    assert result.claim_ids, f"grant blocked: {result.warnings!r}"
    assert not [w for w in result.warnings if w.startswith("advisory:")]


async def test_callsite_advisory_for_symbol_requester_span_containing_callsite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symbol-scope requester (the ``item.symbols`` branch of
    ``_callsite_advisories``): bob claims caller.py::caller_fn, whose
    persisted span (lines 1-4) contains alice's recorded callsites at
    lines 2-3, so the advisory fires on the grant."""
    svc, holder_cid = await _make_enriched_holder(
        tmp_path, monkeypatch, caller_source=CALLER_TWO_FN_PY
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="sess-b",
            claims=[
                ClaimItem(
                    type="file",
                    pattern="caller.py",
                    symbols=["caller_fn"],
                )
            ],
        )
    )
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    assert result.claim_ids, f"grant blocked: {result.warnings!r}"
    advisories = [w for w in result.warnings if w.startswith("advisory:")]
    assert len(advisories) == 1
    advisory = advisories[0]
    assert "alice" in advisory
    assert holder_cid in advisory
    assert "2 recorded callsite(s)" in advisory
    assert "lines 2, 3" in advisory


async def test_callsite_advisory_not_emitted_for_disjoint_symbol_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same file, different symbol: bob claims
    caller.py::unrelated_fn, whose persisted span (lines 7-8) contains
    none of alice's recorded callsites (lines 2-3), so no advisory."""
    svc, _holder_cid = await _make_enriched_holder(
        tmp_path, monkeypatch, caller_source=CALLER_TWO_FN_PY
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="sess-b",
            claims=[
                ClaimItem(
                    type="file",
                    pattern="caller.py",
                    symbols=["unrelated_fn"],
                )
            ],
        )
    )
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    assert result.claim_ids, f"grant blocked: {result.warnings!r}"
    assert not [w for w in result.warnings if w.startswith("advisory:")]


async def test_callsite_advisory_machinery_skipped_when_lsp_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With lsp_enabled=False the advisory pass must never run, even
    when callsite rows exist in the table (e.g. recorded before the
    operator turned LSP off)."""
    import coordination.service as service_module

    repo = _make_repo(tmp_path)
    (repo / "caller.py").write_text(CALLER_PY, encoding="utf-8")
    svc = await _make_service(tmp_path, repo, lsp_enabled=False)

    holder = await svc.create_claims(
        CreateClaimsRequest(
            engineer="alice",
            session_id="sess-a",
            claims=[ClaimItem(type="file", pattern="mod.py")],
        )
    )
    assert holder.claim_ids
    await svc.db.insert_claim_callsites(
        holder.claim_ids[0], [("caller.py", 2, 4, "handler")]
    )

    calls: list[int] = []
    real_group = service_module.group_callsite_overlaps

    def recording_group(*args, **kwargs):
        calls.append(1)
        return real_group(*args, **kwargs)

    monkeypatch.setattr(
        service_module, "group_callsite_overlaps", recording_group
    )

    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="sess-b",
            claims=[ClaimItem(type="file", pattern="caller.py")],
        )
    )

    assert result.claim_ids
    assert not [w for w in result.warnings if w.startswith("advisory:")]
    assert calls == [], "advisory grouping must not run with LSP disabled"


# ---------------------------------------------------------------------------
# v0.31 wave 2: rename auto-follow sweep
# ---------------------------------------------------------------------------


async def _make_sweep_service(
    tmp_path: Path, repo: Path
) -> CoordinationService:
    """Sweep-oriented service: LSP nominally enabled (the sweep is
    gated on it) but pointing at a nonexistent binary, so claim-time
    spans and the sweep's re-extraction both ride the parser path.
    A webhook URL is configured so symbol_renamed emits an outbox row.
    """
    return await _make_service(
        tmp_path,
        repo,
        lsp_enabled=True,
        lsp_command_python="/nonexistent/coord-test-lsp-binary",
        webhook_url="https://hooks.example/sink",
    )


async def test_rename_sweep_follows_unambiguous_rename(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(tmp_path, repo)
    cid = await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    # Rename on disk: same kind, same (absent) parent, same span.
    (repo / "mod.py").write_text(
        "def handle_event():\n    return 1\n", encoding="utf-8"
    )

    applied = await svc.rename_sweep()
    assert applied == 1

    rows = await svc.db.get_claim_symbols(cid)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol_name"] == "handle_event"
    assert row["resolved_by"] == "parser"
    assert row["start_line"] == 1
    assert row["end_line"] == 2
    assert row["start_col"] is None

    renames = await svc.db.list_symbol_renames_for_claims([cid])
    assert len(renames) == 1
    audit = renames[0]
    assert audit["old_symbol_name"] == "handler"
    assert audit["new_symbol_name"] == "handle_event"
    assert audit["old_symbol_path"] == "handler"
    assert audit["new_symbol_path"] == "handle_event"
    assert audit["resolved_by"] == "parser"
    assert audit["old_start_line"] == 1
    assert audit["old_end_line"] == 2

    # claims.pattern holds the FILE pattern for symbol claims, so a
    # symbol rename never rewrites it (the sweep passes
    # new_pattern=None by design).
    claim_rows = await svc.list_claims(active_only=True)
    assert [r["pattern"] for r in claim_rows] == ["mod.py"]

    # One symbol_renamed webhook outbox row.
    outbox = await svc.db.list_pending_webhooks()
    renamed_rows = [
        r for r in outbox if r["event_type"] == "symbol_renamed"
    ]
    assert len(renamed_rows) == 1
    payload = json.loads(renamed_rows[0]["payload_json"])
    assert payload["event_type"] == "symbol_renamed"
    assert payload["detail"]["claim_id"] == cid
    assert payload["detail"]["old"] == "handler"
    assert payload["detail"]["new"] == "handle_event"
    assert payload["detail"]["file"] == "mod.py"

    # Idempotence: the renamed symbol is now present, so a second
    # sweep does nothing.
    assert await svc.rename_sweep() == 0
    assert len(await svc.db.list_symbol_renames_for_claims([cid])) == 1


async def test_rename_sweep_leaves_ambiguous_rename_untouched(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(tmp_path, repo)
    cid = await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    # TWO same-kind, same-parent candidates overlap the stored span
    # within the +/- 5 line tolerance: ambiguity means hands off.
    (repo / "mod.py").write_text(
        "def handler_a():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def handler_b():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    assert await svc.rename_sweep() == 0
    rows = await svc.db.get_claim_symbols(cid)
    assert rows[0]["symbol_name"] == "handler"
    assert await svc.db.list_symbol_renames_for_claims([cid]) == []
    outbox = await svc.db.list_pending_webhooks()
    assert not [r for r in outbox if r["event_type"] == "symbol_renamed"]


async def test_rename_sweep_noop_when_symbol_still_present(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(tmp_path, repo)
    cid = await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    assert await svc.rename_sweep() == 0
    rows = await svc.db.get_claim_symbols(cid)
    assert rows[0]["symbol_name"] == "handler"
    assert await svc.db.list_symbol_renames_for_claims([cid]) == []


async def test_rename_sweep_skips_deleted_file(tmp_path: Path) -> None:
    """A deleted claimed file is a rename the sweep cannot follow:
    nothing is rewritten, no audit row lands, no webhook fires."""
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(tmp_path, repo)
    cid = await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    (repo / "mod.py").unlink()

    assert await svc.rename_sweep() == 0
    rows = await svc.db.get_claim_symbols(cid)
    assert len(rows) == 1
    assert rows[0]["symbol_name"] == "handler"
    assert await svc.db.list_symbol_renames_for_claims([cid]) == []
    outbox = await svc.db.list_pending_webhooks()
    assert not [r for r in outbox if r["event_type"] == "symbol_renamed"]


async def test_rename_sweep_skips_when_new_symbol_already_claimed(
    tmp_path: Path,
) -> None:
    """The sweep's rewrite bypasses the conflict pipeline, so it must
    not auto-follow a rename onto a symbol path another active claim
    already holds -- that would silently manufacture the same-symbol
    overlap claim-time enforcement rejects."""
    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(tmp_path, repo)
    cid = await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    # handler is renamed on disk...
    (repo / "mod.py").write_text(
        "def handle_event():\n    return 1\n", encoding="utf-8"
    )
    # ...and bob claims the post-rename symbol BEFORE the sweep runs
    # (disjoint from alice's 'handler' symbol claim, so it coexists).
    result = await svc.create_claims(
        CreateClaimsRequest(
            engineer="bob",
            session_id="sess-b",
            claims=[
                ClaimItem(
                    type="file",
                    pattern="mod.py",
                    symbols=["handle_event"],
                )
            ],
        )
    )
    assert result.claim_ids, f"claim rejected: {result.warnings!r}"
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    assert await svc.rename_sweep() == 0
    rows = await svc.db.get_claim_symbols(cid)
    assert rows[0]["symbol_name"] == "handler"
    assert await svc.db.list_symbol_renames_for_claims([cid]) == []
    outbox = await svc.db.list_pending_webhooks()
    assert not [r for r in outbox if r["event_type"] == "symbol_renamed"]


async def test_rename_sweep_respects_max_claims_bound(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod_a.py").write_text(
        "def fn_a():\n    return 1\n", encoding="utf-8"
    )
    (repo / "mod_b.py").write_text(
        "def fn_b():\n    return 2\n", encoding="utf-8"
    )
    svc = await _make_sweep_service(tmp_path, repo)

    # One claim per call: the combined-scope ratio guard counts a
    # two-file batch against this two-file repo as 100% of the tree.
    claim_ids: list[str] = []
    for pattern, symbol in (("mod_a.py", "fn_a"), ("mod_b.py", "fn_b")):
        result = await svc.create_claims(
            CreateClaimsRequest(
                engineer="alice",
                claims=[
                    ClaimItem(type="file", pattern=pattern, symbols=[symbol])
                ],
            )
        )
        assert result.claim_ids, f"rejected: {result.warnings!r}"
        claim_ids.extend(result.claim_ids)
    assert len(claim_ids) == 2
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    (repo / "mod_a.py").write_text(
        "def fn_a2():\n    return 1\n", encoding="utf-8"
    )
    (repo / "mod_b.py").write_text(
        "def fn_b2():\n    return 2\n", encoding="utf-8"
    )

    # Bounded pass: only one claim is inspected, so only one rename
    # can apply; the next pass picks up the remainder.
    assert await svc.rename_sweep(max_claims=1) == 1
    assert await svc.rename_sweep() == 1
    renames = await svc.db.list_symbol_renames_for_claims(claim_ids)
    assert len(renames) == 2
    assert sorted(r["new_symbol_name"] for r in renames) == ["fn_a2", "fn_b2"]


async def test_dashboard_shows_rename_note_after_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import coordination.dashboard as dashboard_mod
    from coordination.dashboard import render_dashboard

    repo = _make_repo(tmp_path)
    svc = await _make_sweep_service(tmp_path, repo)
    await _claim_handler(svc)
    await asyncio.gather(*svc._enrichment_tasks, return_exceptions=True)

    (repo / "mod.py").write_text(
        "def handle_event():\n    return 1\n", encoding="utf-8"
    )
    assert await svc.rename_sweep() == 1

    monkeypatch.setattr(dashboard_mod, "get_service", lambda: svc)
    html = await render_dashboard()
    assert "renamed: handler -&gt; handle_event" in html
