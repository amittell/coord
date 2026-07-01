from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from coordination import cli_doctor, mcp_server
from coordination.repo_config import RepoConfig


def _config(tmp_path) -> RepoConfig:
    return RepoConfig(
        version=1,
        tool="claude",
        mode="remote",
        service_url="http://coord.example",
        ownership_file=".coordination/owners.yaml",
        local_env_file=".coordination/local.env",
    )


def _mock_transport(captured_requests: list[httpx.Request], status_by_path: dict[str, int]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_by_path.get(request.url.path, 200))

    return httpx.MockTransport(handler)


def test_check_service_with_token_sends_bearer(monkeypatch, tmp_path):
    captured: list[httpx.Request] = []
    transport = _mock_transport(captured, {"/readyz": 200, "/claims": 200})
    monkeypatch.setattr(
        cli_doctor.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )
    results = cli_doctor._check_service(_config(tmp_path), token="abc123")
    assert [r.label for r in results] == [
        "coordination service reachable",
        "auth token works",
    ]
    assert all(r.ok for r in results)
    claims_request = next(r for r in captured if r.url.path == "/claims")
    assert claims_request.headers["authorization"] == "Bearer abc123"


def test_load_token_strips_surrounding_quotes(tmp_path):
    """coord's local.env template ships COORD_AUTH_TOKEN="set-me" quoted, so a
    user who replaces only the value keeps the quotes. _load_token must strip
    them (matching the MCP reader at mcp_server.py and the bash `source` in
    the pre-push hook) -- otherwise the doctor sends a literal `"coordt_..."`
    and 401s on a token that works everywhere else."""
    coord = tmp_path / ".coordination"
    coord.mkdir()
    for raw in (
        '"coordt_abc123"',
        "'coordt_abc123'",
        "coordt_abc123",
        '  "coordt_abc123"  ',
    ):
        (coord / "local.env").write_text(
            f"COORD_API_URL=https://coord.example\nCOORD_AUTH_TOKEN={raw}\n",
            encoding="utf-8",
        )
        assert cli_doctor._load_token(tmp_path, _config(tmp_path)) == "coordt_abc123"


def test_check_service_without_token_omits_header(monkeypatch, tmp_path):
    captured: list[httpx.Request] = []
    transport = _mock_transport(captured, {"/readyz": 200, "/claims": 200})
    monkeypatch.setattr(
        cli_doctor.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )
    results = cli_doctor._check_service(_config(tmp_path), token="")
    # Second result is renamed when no token is configured.
    assert [r.label for r in results] == [
        "coordination service reachable",
        "unauthenticated access works",
    ]
    assert all(r.ok for r in results)
    claims_request = next(r for r in captured if r.url.path == "/claims")
    # httpx drops headers passed as empty dict, so Authorization must be absent.
    assert "authorization" not in {k.lower() for k in claims_request.headers}


def test_check_service_unauthenticated_failure_explains_insecure_flag(monkeypatch, tmp_path):
    captured: list[httpx.Request] = []
    transport = _mock_transport(captured, {"/readyz": 200, "/claims": 401})
    monkeypatch.setattr(
        cli_doctor.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )
    results = cli_doctor._check_service(_config(tmp_path), token="")
    auth_result = results[1]
    assert auth_result.label == "unauthenticated access works"
    assert auth_result.ok is False
    assert "COORD_ALLOW_INSECURE_NO_AUTH" in auth_result.hint


def test_check_service_unreachable_reports_both(monkeypatch, tmp_path):
    def raising_get(url, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(cli_doctor.httpx, "get", raising_get)
    results = cli_doctor._check_service(_config(tmp_path), token="abc123")
    assert len(results) == 2
    assert results[0].label == "coordination service reachable"
    assert results[0].ok is False
    assert results[1].label == "auth token works"
    assert results[1].ok is False


# --- asset drift checks ---------------------------------------------------


def _seed_drift_repo(repo: Path, *, hook: str, managed_block: str | None) -> None:
    coord = repo / ".coordination"
    (coord / "hooks").mkdir(parents=True, exist_ok=True)
    (coord / "hooks" / "pre-push").write_text(hook, encoding="utf-8")
    if managed_block is not None:
        (repo / "CLAUDE.md").write_text(
            f"# Project\n\n<!-- coord:begin -->\n{managed_block.strip()}\n<!-- coord:end -->\n",
            encoding="utf-8",
        )


def test_drift_check_ok_when_hook_and_block_match_current_assets(tmp_path):
    from coordination.assets import CLAUDE_SNIPPET, PRE_PUSH_SCRIPT

    _seed_drift_repo(tmp_path, hook=PRE_PUSH_SCRIPT, managed_block=CLAUDE_SNIPPET)
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "claude")
    results = cli_doctor._check_asset_drift(tmp_path, config)
    by_label = {r.label: r for r in results}
    assert by_label["pre-push hook is up to date"].ok is True
    assert by_label["CLAUDE.md managed block is up to date"].ok is True


def test_drift_check_flags_stale_hook(tmp_path):
    from coordination.assets import CLAUDE_SNIPPET

    _seed_drift_repo(
        tmp_path,
        hook="#!/usr/bin/env bash\n# OLD VERSION\nexit 0\n",
        managed_block=CLAUDE_SNIPPET,
    )
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "claude")
    results = cli_doctor._check_asset_drift(tmp_path, config)
    hook_result = next(r for r in results if r.label == "pre-push hook is up to date")
    assert hook_result.ok is False
    assert "coord upgrade" in hook_result.hint


def test_drift_check_flags_stale_claude_block(tmp_path):
    from coordination.assets import PRE_PUSH_SCRIPT

    _seed_drift_repo(
        tmp_path,
        hook=PRE_PUSH_SCRIPT,
        managed_block="## stale managed content from a previous coord version",
    )
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "claude")
    results = cli_doctor._check_asset_drift(tmp_path, config)
    block_result = next(
        r for r in results if r.label == "CLAUDE.md managed block is up to date"
    )
    assert block_result.ok is False
    assert "coord upgrade" in block_result.hint


def test_drift_check_flags_each_tool_block_independently(tmp_path):
    """Multi-tool repos: when both .mcp.json and .codex/config.toml are
    wired, doctor must check CLAUDE.md AND AGENTS.md drift, not just the
    one named in config.toml. Otherwise stale snippets in the secondary
    tool's docs slip past upgrade reminders."""
    from coordination.assets import (
        AGENTS_SNIPPET,
        CLAUDE_SNIPPET,
        PRE_PUSH_SCRIPT,
    )

    _seed_drift_repo(
        tmp_path, hook=PRE_PUSH_SCRIPT, managed_block=CLAUDE_SNIPPET
    )
    # AGENTS.md exists with a stale block, simulating a repo that has
    # codex wired alongside claude.
    (tmp_path / "AGENTS.md").write_text(
        "# AGENTS\n\n<!-- coord:begin -->\nstale agents block\n<!-- coord:end -->\n",
        encoding="utf-8",
    )
    # Also drop a stub .codex/config.toml so doctor knows codex is wired.
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        "[mcp_servers.coord]\ncommand = \"coord-mcp\"\n", encoding="utf-8"
    )
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "claude")  # primary

    results = cli_doctor._check_asset_drift(tmp_path, config)
    by_label = {r.label: r for r in results}
    assert by_label["CLAUDE.md managed block is up to date"].ok is True
    # AGENTS.md drift must surface even though config.tool says claude.
    assert "AGENTS.md managed block is up to date" in by_label, (
        f"expected AGENTS.md drift check; got labels: {list(by_label)}"
    )
    assert by_label["AGENTS.md managed block is up to date"].ok is False
    assert AGENTS_SNIPPET  # used to silence import unused warning


def test_token_consistency_check_flags_stale_mcp_token(tmp_path):
    """If the user rotates COORD_AUTH_TOKEN in .coordination/local.env
    but forgets to run `coord upgrade`, the embedded copy of the token
    in .mcp.json (and .codex/config.toml) goes stale. Doctor must flag
    this so the user doesn't silently authenticate as a previous
    rotation key."""
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir(parents=True)
    (coord_dir / "local.env").write_text(
        "COORD_API_URL=http://coord.example\n"
        "COORD_AUTH_TOKEN=fresh-token-2026-05\n",
        encoding="utf-8",
    )
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers":{"coord":{"command":"coord-mcp","args":[],'
        '"env":{"COORD_API_URL":"http://coord.example",'
        '"COORD_AUTH_TOKEN":"stale-token-from-q1"}}}}',
        encoding="utf-8",
    )
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "claude")

    results = cli_doctor._check_token_consistency(
        tmp_path, config, token="fresh-token-2026-05"
    )
    by_label = {r.label: r for r in results}
    assert ".mcp.json token matches local.env" in by_label
    drift = by_label[".mcp.json token matches local.env"]
    assert drift.ok is False
    assert "coord upgrade" in drift.hint


def test_token_consistency_check_passes_when_in_sync(tmp_path):
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir(parents=True)
    (coord_dir / "local.env").write_text(
        "COORD_AUTH_TOKEN=match-me\n", encoding="utf-8"
    )
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers":{"coord":{"command":"coord-mcp","args":[],'
        '"env":{"COORD_AUTH_TOKEN":"match-me"}}}}',
        encoding="utf-8",
    )
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "claude")

    results = cli_doctor._check_token_consistency(
        tmp_path, config, token="match-me"
    )
    assert all(r.ok for r in results)


def test_token_consistency_check_covers_codex_too(tmp_path):
    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir(parents=True)
    (coord_dir / "local.env").write_text(
        "COORD_AUTH_TOKEN=fresh\n", encoding="utf-8"
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        "[mcp_servers.coord]\n"
        'command = "coord-mcp"\n'
        "[mcp_servers.coord.env]\n"
        'COORD_AUTH_TOKEN = "stale"\n',
        encoding="utf-8",
    )
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "codex")

    results = cli_doctor._check_token_consistency(tmp_path, config, token="fresh")
    by_label = {r.label: r for r in results}
    assert ".codex/config.toml token matches local.env" in by_label
    assert by_label[".codex/config.toml token matches local.env"].ok is False


def test_drift_check_skips_block_when_file_missing(tmp_path):
    # No CLAUDE.md at all -- the existing 'managed block found' check
    # already reports that. Drift check must not double-report failure.
    from coordination.assets import PRE_PUSH_SCRIPT

    _seed_drift_repo(tmp_path, hook=PRE_PUSH_SCRIPT, managed_block=None)
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "claude")
    results = cli_doctor._check_asset_drift(tmp_path, config)
    labels = [r.label for r in results]
    assert "pre-push hook is up to date" in labels
    assert "CLAUDE.md managed block is up to date" not in labels


def test_version_check_ok_when_versions_match(monkeypatch, tmp_path):
    def get(url, **kw):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"name": "multi-agent-coordination", "version": "0.1.0"})
            if r.url.path == "/meta" else httpx.Response(200)
        ))
        return client.get(url, **kw)

    monkeypatch.setattr(cli_doctor.httpx, "get", get)
    result = cli_doctor._check_server_version(_config(tmp_path), client_version="0.1.0")
    assert result is not None
    assert result.ok is True
    assert "0.1.0" in result.label


def test_version_check_flags_when_server_is_newer(monkeypatch, tmp_path):
    def get(url, **kw):
        return httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"version": "0.2.0"})
        )).get(url, **kw)

    monkeypatch.setattr(cli_doctor.httpx, "get", get)
    result = cli_doctor._check_server_version(_config(tmp_path), client_version="0.1.0")
    assert result is not None
    assert result.ok is False
    # Hint must direct them to update the CLI, not the server.
    assert "upgrade" in result.hint.lower() or "newer" in result.detail.lower()
    assert "0.2.0" in result.detail


def test_version_check_flags_when_client_is_newer(monkeypatch, tmp_path):
    def get(url, **kw):
        return httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"version": "0.1.0"})
        )).get(url, **kw)

    monkeypatch.setattr(cli_doctor.httpx, "get", get)
    result = cli_doctor._check_server_version(_config(tmp_path), client_version="0.5.0")
    assert result is not None
    assert result.ok is False
    # Hint must direct them to bump the cluster image, not upgrade the client.
    combined = (result.detail + " " + result.hint).lower()
    assert "older" in combined or "image tag" in combined


def test_version_check_skips_when_meta_lacks_version(monkeypatch, tmp_path):
    def get(url, **kw):
        return httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"name": "x"})
        )).get(url, **kw)

    monkeypatch.setattr(cli_doctor.httpx, "get", get)
    result = cli_doctor._check_server_version(_config(tmp_path), client_version="0.1.0")
    # No actionable signal -- skip silently rather than spuriously fail.
    assert result is None


def test_version_check_skips_when_meta_unreachable(monkeypatch, tmp_path):
    def raising_get(url, **kw):
        raise httpx.ConnectError("dead")

    monkeypatch.setattr(cli_doctor.httpx, "get", raising_get)
    result = cli_doctor._check_server_version(_config(tmp_path), client_version="0.1.0")
    # Existing 'service reachable' check already reports unreachability.
    # Don't double-report here.
    assert result is None


def test_drift_check_uses_agents_md_for_codex(tmp_path):
    from coordination.assets import AGENTS_SNIPPET, PRE_PUSH_SCRIPT

    coord = tmp_path / ".coordination" / "hooks"
    coord.mkdir(parents=True)
    (coord / "pre-push").write_text(PRE_PUSH_SCRIPT, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        f"<!-- coord:begin -->\n{AGENTS_SNIPPET.strip()}\n<!-- coord:end -->\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    object.__setattr__(config, "tool", "codex")
    results = cli_doctor._check_asset_drift(tmp_path, config)
    by_label = {r.label: r for r in results}
    assert by_label["AGENTS.md managed block is up to date"].ok is True
    assert "CLAUDE.md managed block is up to date" not in by_label


# --- symbol-parser-backend check (v0.15) ---------------------------------


def test_symbol_parser_check_ok_when_treesitter_available(monkeypatch):
    """The dev extra ships tree-sitter for every registered extension, so
    the default install path should resolve to tree-sitter end-to-end and
    report OK with no fallback list."""
    # Ensure auto mode (no forced backend) so probe sees the real install state.
    monkeypatch.delenv("COORD_SYMBOL_PARSER", raising=False)
    # Force every probe call to look like tree-sitter is present, so the
    # test is independent of the host machine's wheel install state.
    from coordination import symbols

    monkeypatch.setattr(
        symbols,
        "probe_backend",
        lambda ext: ("treesitter", "ok"),
    )

    result = cli_doctor._check_symbol_parser_backend()
    assert result.label == "symbol parser backend"
    assert result.ok is True
    assert result.level == "fail"  # default; ok=True so level is irrelevant
    assert "tree-sitter" in result.detail


def test_symbol_parser_check_warns_when_forced_to_regex(monkeypatch):
    """`COORD_SYMBOL_PARSER=regex` flips every supported extension to the
    regex backend. That's a documented degraded mode, not a failure, so
    the check must report WARN and list every fallback extension."""
    monkeypatch.setenv("COORD_SYMBOL_PARSER", "regex")
    from coordination import symbols

    monkeypatch.setattr(
        symbols,
        "probe_backend",
        lambda ext: ("regex", "forced via COORD_SYMBOL_PARSER=regex"),
    )

    result = cli_doctor._check_symbol_parser_backend()
    assert result.label == "symbol parser backend"
    assert result.ok is False
    assert result.level == "warn"
    # Every registered extension should appear in the fallback list.
    for ext in symbols.supported_extensions():
        assert ext in result.detail
    assert "regex" in result.detail.lower()
    assert "symbols" in result.hint  # nudges toward the install extra
    assert "coord-mcp-server[symbols]" in result.hint


def test_symbol_parser_check_fails_when_treesitter_forced_and_missing(monkeypatch):
    """`COORD_SYMBOL_PARSER=treesitter` with a missing native grammar would
    crash `extract_symbols` at call time, so doctor must surface it as a
    hard fail (not a warn) with a hint pointing at the `symbols` extra."""
    monkeypatch.setenv("COORD_SYMBOL_PARSER", "treesitter")
    from coordination import symbols

    monkeypatch.setattr(
        symbols,
        "probe_backend",
        lambda ext: (
            "none",
            f"COORD_SYMBOL_PARSER=treesitter but grammar for {ext} not installed",
        ),
    )

    result = cli_doctor._check_symbol_parser_backend()
    assert result.label == "symbol parser backend"
    assert result.ok is False
    assert result.level == "fail"
    assert "COORD_SYMBOL_PARSER=treesitter" in result.detail
    # The hint must mention how to fix it.
    assert "symbols" in result.hint
    assert "coord-mcp-server[symbols]" in result.hint


# --- v0.32.2: doctor robustness under the user-scoped MCP model ---


def _write_claude_json(home: Path, mcp_servers: dict) -> None:
    import json

    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": mcp_servers}), encoding="utf-8"
    )


def test_user_scoped_coord_mcp_detected_by_command(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Named "coord"
    _write_claude_json(tmp_path, {"coord": {"command": "coord-mcp", "args": []}})
    assert cli_doctor._user_scoped_coord_mcp() is True
    # Matched by command even under a different entry name.
    _write_claude_json(tmp_path, {"whatever": {"command": "coord-mcp"}})
    assert cli_doctor._user_scoped_coord_mcp() is True


def test_user_scoped_coord_mcp_absent_and_malformed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # No file at all.
    assert cli_doctor._user_scoped_coord_mcp() is False
    # File present but no coord server.
    _write_claude_json(tmp_path, {"other": {"command": "other-mcp"}})
    assert cli_doctor._user_scoped_coord_mcp() is False
    # Malformed JSON does not raise.
    (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
    assert cli_doctor._user_scoped_coord_mcp() is False


def test_managed_block_doc_checks_both_docs(tmp_path):
    from coordination.cli_shared import MANAGED_BEGIN

    assert cli_doctor._managed_block_doc(tmp_path) is None
    # Block in AGENTS.md (not CLAUDE.md) is still found.
    (tmp_path / "AGENTS.md").write_text(
        f"# Agents\n{MANAGED_BEGIN}\nproto\n", encoding="utf-8"
    )
    assert cli_doctor._managed_block_doc(tmp_path) == "AGENTS.md"
    # CLAUDE.md takes precedence in the check order.
    (tmp_path / "CLAUDE.md").write_text(
        f"# Claude\n{MANAGED_BEGIN}\nproto\n", encoding="utf-8"
    )
    assert cli_doctor._managed_block_doc(tmp_path) == "CLAUDE.md"


def test_sessions_live_stale_entries_are_pruned(tmp_path):
    """Stale dead-PID entries are self-healing noise; doctor should prune
    them from the local runtime file instead of leaving an operator warning."""
    coord = tmp_path / ".coordination"
    coord.mkdir()
    live_pid = os.getpid()
    live_start = mcp_server._process_start_time_ns(live_pid)
    live_line = f"sess-a {live_pid} {live_start}"
    (coord / "sessions.live").write_text(
        f"{live_line}\nsess-b 2147480000 0\n", encoding="utf-8"
    )
    results = cli_doctor._check_sessions_live(tmp_path)
    assert results, "expected a sessions.live result"
    pruned = results[0]
    assert pruned.ok is True
    assert "pruned 1/2 stale entries" in pruned.detail
    assert (coord / "sessions.live").read_text(encoding="utf-8") == f"{live_line}\n"


def test_sessions_live_busy_lock_warns_without_pruning(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Doctor must not rewrite sessions.live while coord-mcp owns the
    compaction lock."""
    coord = tmp_path / ".coordination"
    coord.mkdir()
    marker = coord / "sessions.live"
    marker.write_text("sess-b 2147480000 0\n", encoding="utf-8")
    (coord / ".sessions.live.lock").mkdir()
    monkeypatch.setattr(mcp_server, "_MARKER_LOCK_TIMEOUT_SECONDS", 0)

    results = cli_doctor._check_sessions_live(tmp_path)

    assert results, "expected a sessions.live result"
    busy = results[0]
    assert busy.ok is False
    assert busy.level == "warn"
    assert "could not acquire" in busy.detail
    assert marker.read_text(encoding="utf-8") == "sess-b 2147480000 0\n"


def test_installed_pre_push_hook_detected_in_worktree(tmp_path: Path) -> None:
    """The pre-push hook check must resolve the shared hooks dir in a git
    worktree (where ``.git`` is a file), not false-negative on it."""
    import subprocess

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
        )

    main = tmp_path / "main"
    main.mkdir()
    git("init", "-q", cwd=main)
    (main / "README.md").write_text("x", encoding="utf-8")
    git("add", ".", cwd=main)
    git("commit", "-qm", "init", cwd=main)

    # Install a pre-push hook in the (shared) hooks dir.
    hooks = main / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    # A linked worktree shares that hooks dir; its own .git is a file.
    wt = tmp_path / "wt"
    git("worktree", "add", "-q", str(wt), "-b", "feat", cwd=main)
    assert (wt / ".git").is_file()
    # The naive check the bug used would fail here...
    assert not (wt / ".git" / "hooks" / "pre-push").exists()
    # ...but the resolver finds the shared, installed hook.
    assert cli_doctor._installed_pre_push_hook(wt) is not None
    assert cli_doctor._installed_pre_push_hook(main) is not None


def test_installed_pre_push_hook_absent(tmp_path: Path) -> None:
    """No hook installed -> None (plain repo)."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    assert cli_doctor._installed_pre_push_hook(repo) is None
