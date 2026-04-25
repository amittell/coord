from __future__ import annotations

from pathlib import Path

import httpx

from coordination import cli_doctor
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
