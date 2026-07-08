"""Audit regression tests for the CLI surface (doctor / init / upgrade /
start / ops).

Covers the audit-fix campaign findings:

- cli_doctor._check_service must not raise UnboundLocalError when the
  /claims probe itself fails after /readyz succeeded.
- coord upgrade reads COORD_AUTH_TOKEN via the shared envfile parser
  (export prefix tolerated, last assignment wins) and preserves unmanaged
  keys and comments when rewriting local.env.
- coord init re-runs preserve an existing non-placeholder token in
  .coordination/local.env unless --force is passed, and preserve unmanaged
  keys and comments.
- cli_init._update_mcp_json refuses to clobber an invalid-JSON or
  non-object .mcp.json instead of silently replacing it or crashing.
- coord start --background reaps the spawned process on readiness failure
  and never unlinks a PID file it did not write.
- CLI token precedence matches the MCP wrapper (real env wins, placeholders
  lose) and coord status names the token source and exits non-zero when the
  claims probe fails.
- envfile.update_env_file semantics (the shared writer behind the above).
"""

from __future__ import annotations

import json
import subprocess
import threading
import socketserver
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import httpx
import pytest

from coordination import cli, cli_doctor, cli_init, cli_ops, cli_start, cli_upgrade
from coordination.envfile import read_env_file, update_env_file
from coordination.repo_config import RepoConfig


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_repo(root: Path) -> Path:
    repo = root / "app"
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "app"}\n', encoding="utf-8")
    return repo


def _seed_initialised_repo(
    repo: Path,
    service_url: str = "http://coord.team.local",
    local_env_body: str | None = None,
) -> None:
    coord = repo / ".coordination"
    coord.mkdir(parents=True, exist_ok=True)
    (coord / "config.toml").write_text(
        "version = 1\n"
        'tool = "claude"\n'
        'mode = "remote"\n'
        f'service_url = "{service_url}"\n'
        'ownership_file = ".coordination/owners.yaml"\n'
        'local_env_file = ".coordination/local.env"\n',
        encoding="utf-8",
    )
    if local_env_body is not None:
        (coord / "local.env").write_text(local_env_body, encoding="utf-8")
    (coord / "owners.yaml").write_text(
        'areas:\n  src:\n    paths: ["src/**"]\n    owners: [team]\n',
        encoding="utf-8",
    )


class _MockServer:
    def __init__(self, handler_class: type[BaseHTTPRequestHandler]) -> None:
        self.server = socketserver.TCPServer(("127.0.0.1", 0), handler_class)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockServer":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


def _status_handler(seen_auth: list[str], claims_status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
                return
            if self.path == "/meta":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"version":"0.1.0","repo_root_configured":false}')
                return
            if self.path.startswith("/claims"):
                seen_auth.append(self.headers.get("Authorization", ""))
                self.send_response(claims_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if claims_status == 200:
                    self.wfile.write(b'{"claims":[],"count":0}')
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    return Handler


# ---------------------------------------------------------------------------
# cli_doctor: /claims probe failure after /readyz success
# ---------------------------------------------------------------------------


def _doctor_config() -> RepoConfig:
    return RepoConfig(
        version=1,
        tool="claude",
        mode="remote",
        service_url="http://coord.example",
        ownership_file=".coordination/owners.yaml",
        local_env_file=".coordination/local.env",
    )


def test_check_service_claims_probe_httpx_error_does_not_crash(monkeypatch):
    """/readyz answers 200 but the /claims GET itself raises (timeout,
    connection reset mid-request). The old code referenced a ``hint`` local
    that was only bound after the /claims response returned, so this path
    died with UnboundLocalError instead of reporting the failed check."""

    def fake_get(url: str, **kw: object) -> httpx.Response:
        if url.endswith("/readyz"):
            return httpx.Response(200, request=httpx.Request("GET", url))
        raise httpx.ConnectTimeout("connection timed out mid-request")

    monkeypatch.setattr(cli_doctor.httpx, "get", fake_get)

    results = cli_doctor._check_service(_doctor_config(), token="abc123")

    assert [r.label for r in results] == [
        "coordination service reachable",
        "auth token works",
    ]
    assert results[0].ok
    assert not results[1].ok
    assert "timed out" in results[1].detail
    assert results[1].hint  # a concrete hint, not an unbound local


# ---------------------------------------------------------------------------
# envfile.update_env_file
# ---------------------------------------------------------------------------


def test_update_env_file_preserves_comments_and_unknown_keys(tmp_path: Path):
    path = tmp_path / "local.env"
    path.write_text(
        "# team notes: rotated 2026-07-01\n"
        "COORD_API_URL=http://stale.example\n"
        "\n"
        "COORD_USER=alice\n"
        "COORD_BRANCH=feature/x\n"
        "COORD_AUTH_TOKEN=old\n",
        encoding="utf-8",
    )
    update_env_file(
        path,
        {
            "COORD_API_URL": "http://fresh.example",
            "COORD_AUTH_TOKEN": "new-token",
        },
    )
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# team notes: rotated 2026-07-01"
    assert "COORD_USER=alice" in lines
    assert "COORD_BRANCH=feature/x" in lines
    assert "COORD_API_URL=http://fresh.example" in lines
    assert "COORD_AUTH_TOKEN=new-token" in lines
    assert "stale.example" not in text
    assert "" in lines  # blank line kept


def test_update_env_file_collapses_duplicates_and_appends_missing(tmp_path: Path):
    path = tmp_path / "local.env"
    path.write_text(
        "COORD_AUTH_TOKEN=stale\n"
        "export COORD_AUTH_TOKEN=fresh\n",
        encoding="utf-8",
    )
    update_env_file(
        path,
        {"COORD_AUTH_TOKEN": "fresh", "COORD_SERVICE_URL": "http://svc"},
    )
    text = path.read_text(encoding="utf-8")
    assert text.count("COORD_AUTH_TOKEN") == 1
    assert "COORD_AUTH_TOKEN=fresh" in text
    # Missing managed key appended.
    assert text.rstrip().endswith("COORD_SERVICE_URL=http://svc")


def test_update_env_file_creates_missing_file(tmp_path: Path):
    path = tmp_path / "sub" / "local.env"
    update_env_file(path, {"COORD_API_URL": "http://svc", "COORD_AUTH_TOKEN": "t"})
    assert read_env_file(path) == {
        "COORD_API_URL": "http://svc",
        "COORD_AUTH_TOKEN": "t",
    }


# ---------------------------------------------------------------------------
# coord upgrade: token read via shared parser + unmanaged keys preserved
# ---------------------------------------------------------------------------


def _upgrade_args(repo: Path):
    class _Args:
        root = str(repo)

    return _Args()


def test_upgrade_preserves_export_prefixed_token(tmp_path: Path):
    """`export COORD_AUTH_TOKEN=...` is valid for every other local.env
    reader (bash source, MCP wrapper, doctor). The old first-match
    startswith() reader returned "" for it and upgrade rewrote the file
    with an empty token, 401ing every client."""
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(
        repo,
        local_env_body=(
            "COORD_API_URL=http://stale.example\n"
            "export COORD_AUTH_TOKEN=real-scoped-token\n"
        ),
    )
    assert cli_upgrade.run_upgrade(_upgrade_args(repo)) == 0
    env = read_env_file(repo / ".coordination" / "local.env")
    assert env["COORD_AUTH_TOKEN"] == "real-scoped-token"


def test_upgrade_keeps_last_assignment_of_rotated_token(tmp_path: Path):
    """envfile.py's documented rotation pattern: a fresh token appended
    below a stale one wins. Upgrade must not regress to the stale first
    match."""
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(
        repo,
        local_env_body=(
            "COORD_AUTH_TOKEN=stale-old-token\n"
            "COORD_AUTH_TOKEN=fresh-rotated-token\n"
        ),
    )
    assert cli_upgrade.run_upgrade(_upgrade_args(repo)) == 0
    text = (repo / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert "fresh-rotated-token" in text
    assert "stale-old-token" not in text
    assert text.count("COORD_AUTH_TOKEN=") == 1


def test_upgrade_preserves_unmanaged_keys_and_comments(tmp_path: Path):
    """local.env is the documented home for every key in
    mcp_server._LOCAL_ENV_KEYS; upgrade must not silently delete them."""
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(
        repo,
        local_env_body=(
            "# do not commit this file\n"
            "COORD_API_URL=http://stale.example\n"
            "COORD_AUTH_TOKEN=keep-me\n"
            "COORD_USER=alice\n"
            "COORD_REPO_ROOT=/srv/checkout\n"
            "COORD_DISABLE_CLIENT_VALIDATION=1\n"
        ),
    )
    assert cli_upgrade.run_upgrade(_upgrade_args(repo)) == 0
    text = (repo / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert "# do not commit this file" in text
    assert "COORD_USER=alice" in text
    assert "COORD_REPO_ROOT=/srv/checkout" in text
    assert "COORD_DISABLE_CLIENT_VALIDATION=1" in text
    assert "COORD_AUTH_TOKEN=keep-me" in text
    assert "stale.example" not in text


def test_upgrade_warns_when_token_resolves_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(
        repo,
        local_env_body="COORD_API_URL=http://stale.example\n",
    )
    assert cli_upgrade.run_upgrade(_upgrade_args(repo)) == 0
    err = capsys.readouterr().err
    assert "no COORD_AUTH_TOKEN found" in err


# ---------------------------------------------------------------------------
# coord init: re-run must not clobber a real token
# ---------------------------------------------------------------------------


def test_reinit_remote_mode_preserves_real_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Re-init is a supported additive flow. In remote mode without
    COORD_AUTH_TOKEN exported, the old code silently replaced a working
    pasted scoped token with the literal placeholder."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)

    assert cli.main(["init", "--tool", "claude", "--mode", "remote", "--yes"]) == 0
    local_env = repo / ".coordination" / "local.env"
    # Operator pastes a repo-scoped token (the documented v0.42 flow).
    text = local_env.read_text(encoding="utf-8")
    text = text.replace("COORD_AUTH_TOKEN=set-me", "COORD_AUTH_TOKEN=coordt_real")
    local_env.write_text(text, encoding="utf-8")

    assert cli.main(["init", "--tool", "codex", "--mode", "remote", "--yes"]) == 0
    env = read_env_file(local_env)
    assert env["COORD_AUTH_TOKEN"] == "coordt_real"


def test_reinit_local_mode_preserves_operator_token_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli_init, "_probe_service", lambda url: True)

    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0
    local_env = repo / ".coordination" / "local.env"
    text = local_env.read_text(encoding="utf-8")
    minted = read_env_file(local_env)["COORD_AUTH_TOKEN"]
    text = text.replace(
        f"COORD_AUTH_TOKEN={minted}", "COORD_AUTH_TOKEN=coordt_operator_scoped"
    )
    local_env.write_text(text, encoding="utf-8")

    # Re-init without --force keeps the operator-minted token.
    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0
    assert (
        read_env_file(local_env)["COORD_AUTH_TOKEN"] == "coordt_operator_scoped"
    )

    # --force explicitly re-mints from the shared ~/.coord/token file.
    assert (
        cli.main(["init", "--tool", "claude", "--mode", "local", "--yes", "--force"])
        == 0
    )
    assert read_env_file(local_env)["COORD_AUTH_TOKEN"] == minted


def test_reinit_preserves_unmanaged_local_env_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli_init, "_probe_service", lambda url: True)

    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0
    local_env = repo / ".coordination" / "local.env"
    with local_env.open("a", encoding="utf-8") as fh:
        fh.write("# per-user identity\nCOORD_USER=alice\n")

    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0
    text = local_env.read_text(encoding="utf-8")
    assert "COORD_USER=alice" in text
    assert "# per-user identity" in text


def test_init_replaces_placeholder_token_on_reinit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A leftover ``set-me`` placeholder is NOT a real token; re-init in
    local mode must still backfill the minted token over it."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(cli_init, "_probe_service", lambda url: True)
    _seed_initialised_repo(
        repo, local_env_body="COORD_AUTH_TOKEN=set-me\n"
    )

    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0
    token = read_env_file(repo / ".coordination" / "local.env")["COORD_AUTH_TOKEN"]
    assert token and token != "set-me"


# ---------------------------------------------------------------------------
# cli_init._update_mcp_json: invalid JSON / non-object handling
# ---------------------------------------------------------------------------


def test_init_refuses_to_clobber_invalid_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    monkeypatch.setattr(cli_init, "_probe_service", lambda url: True)
    broken = '{"mcpServers": {"github": {"command": "npx"},}}\n'  # trailing comma
    (repo / ".mcp.json").write_text(broken, encoding="utf-8")

    exit_code = cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"])

    assert exit_code == 1
    # The user's file is untouched -- their other MCP servers survive.
    assert (repo / ".mcp.json").read_text(encoding="utf-8") == broken
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    assert "refusing to overwrite" in err


def test_init_refuses_non_object_mcp_json_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A top-level array parses but has no .setdefault; the old code crashed
    with AttributeError."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    monkeypatch.setattr(cli_init, "_probe_service", lambda url: True)
    (repo / ".mcp.json").write_text("[]\n", encoding="utf-8")

    exit_code = cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"])

    assert exit_code == 1
    assert (repo / ".mcp.json").read_text(encoding="utf-8") == "[]\n"
    assert "does not contain a JSON object" in capsys.readouterr().err


def test_update_mcp_json_refuses_non_object_mcp_servers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = tmp_path / ".mcp.json"
    path.write_text('{"mcpServers": ["not", "a", "dict"]}\n', encoding="utf-8")
    assert cli_init._update_mcp_json(path) is False
    assert '"mcpServers"' in capsys.readouterr().err
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "mcpServers": ["not", "a", "dict"]
    }


def test_upgrade_fails_cleanly_on_invalid_mcp_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(repo, local_env_body="COORD_AUTH_TOKEN=tok\n")
    broken = "{not json}\n"
    (repo / ".mcp.json").write_text(broken, encoding="utf-8")

    assert cli_upgrade.run_upgrade(_upgrade_args(repo)) == 1
    assert (repo / ".mcp.json").read_text(encoding="utf-8") == broken
    err = capsys.readouterr().err
    assert "not valid JSON" in err


# ---------------------------------------------------------------------------
# coord start --background failure path
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, wait_timeouts: int = 0) -> None:
        self.pid = 43210
        self.terminated = False
        self.killed = False
        self._wait_timeouts = wait_timeouts

    def poll(self) -> int | None:
        return 0 if (self.terminated or self.killed) else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_timeouts > 0:
            self._wait_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="coord _serve", timeout=timeout or 0)
        return 0


def _start_args(port: int):
    class _Args:
        host = "127.0.0.1"
        background = True
        json = False
        open_dashboard = False

    _Args.port = port
    return _Args()


def test_start_failure_terminates_spawned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    proc = _FakeProc()
    monkeypatch.setattr(
        cli_start.subprocess, "Popen", lambda *a, **k: proc
    )
    monkeypatch.setattr(cli_start, "_service_already_running", lambda url: False)

    def fake_wait(*_: object, **__: object) -> None:
        raise RuntimeError("service did not become ready")

    monkeypatch.setattr(cli_start, "_wait_for_http_ready", fake_wait)

    assert cli_start.run_start(_start_args(8321)) == 1
    assert proc.terminated
    assert "service did not become ready" in capsys.readouterr().out


def test_start_failure_escalates_to_kill_when_terminate_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    proc = _FakeProc(wait_timeouts=1)
    monkeypatch.setattr(cli_start.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(cli_start, "_service_already_running", lambda url: False)
    monkeypatch.setattr(
        cli_start,
        "_wait_for_http_ready",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timed out")),
    )

    assert cli_start.run_start(_start_args(8322)) == 1
    assert proc.terminated
    assert proc.killed


def test_start_failure_does_not_unlink_foreign_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Any coord.pid present at failure time was written by a DIFFERENT,
    possibly healthy background service (this run only writes it after
    readiness). Unlinking it would orphan that service's stop record."""
    home = tmp_path / ".coord-home"
    monkeypatch.setenv("COORD_HOME", str(home))
    pid_file = home / "coord.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    foreign = json.dumps(
        {"pid": 999, "start_time": "2026-07-08T00:00:00Z", "marker": "coordination.cli _serve"}
    )
    pid_file.write_text(foreign + "\n", encoding="utf-8")

    proc = _FakeProc()
    monkeypatch.setattr(cli_start.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(cli_start, "_service_already_running", lambda url: False)
    monkeypatch.setattr(
        cli_start,
        "_wait_for_http_ready",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("not ready")),
    )

    assert cli_start.run_start(_start_args(8323)) == 1
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8").strip() == foreign


# ---------------------------------------------------------------------------
# cli_ops: token precedence, token source line, status exit code
# ---------------------------------------------------------------------------


def test_resolve_service_env_token_beats_local_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(
        repo, local_env_body="COORD_AUTH_TOKEN=file-token\n"
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_AUTH_TOKEN", "env-token")

    ctx = cli_ops._resolve_service()
    assert ctx is not None
    assert ctx.auth_token == "env-token"
    assert ctx.token_source == "from environment"


def test_resolve_service_placeholder_env_token_loses_to_local_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(
        repo, local_env_body="COORD_AUTH_TOKEN=file-token\n"
    )
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_AUTH_TOKEN", "set-me")

    ctx = cli_ops._resolve_service()
    assert ctx is not None
    assert ctx.auth_token == "file-token"
    assert ctx.token_source == "from .coordination/local.env"


def test_resolve_service_placeholder_everywhere_means_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _make_repo(tmp_path)
    _seed_initialised_repo(
        repo, local_env_body="COORD_AUTH_TOKEN=set-me\n"
    )
    monkeypatch.chdir(repo)
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)

    ctx = cli_ops._resolve_service()
    assert ctx is not None
    assert ctx.auth_token == ""
    assert ctx.token_source == "not set"


def test_status_authenticates_with_env_token_and_names_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    seen_auth: list[str] = []
    with _MockServer(_status_handler(seen_auth)) as mock:
        repo = _make_repo(tmp_path)
        _seed_initialised_repo(
            repo,
            service_url=f"http://127.0.0.1:{mock.port}",
            local_env_body="COORD_AUTH_TOKEN=file-token\n",
        )
        monkeypatch.chdir(repo)
        monkeypatch.setenv("COORD_AUTH_TOKEN", "env-token")
        monkeypatch.delenv("COORD_REPO_ID", raising=False)
        exit_code = cli.main(["status"])

    assert exit_code == 0
    assert seen_auth == ["Bearer env-token"]
    out = capsys.readouterr().out
    assert "Token: from environment" in out


def test_status_names_local_env_token_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    seen_auth: list[str] = []
    with _MockServer(_status_handler(seen_auth)) as mock:
        repo = _make_repo(tmp_path)
        _seed_initialised_repo(
            repo,
            service_url=f"http://127.0.0.1:{mock.port}",
            local_env_body="COORD_AUTH_TOKEN=file-token\n",
        )
        monkeypatch.chdir(repo)
        monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("COORD_REPO_ID", raising=False)
        exit_code = cli.main(["status"])

    assert exit_code == 0
    assert seen_auth == ["Bearer file-token"]
    assert "Token: from .coordination/local.env" in capsys.readouterr().out


def test_status_exits_nonzero_when_claims_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A dead token must not exit 0 just because /readyz answered; scripts
    gate on this exit code as a wiring check."""
    seen_auth: list[str] = []
    with _MockServer(_status_handler(seen_auth, claims_status=401)) as mock:
        repo = _make_repo(tmp_path)
        _seed_initialised_repo(
            repo,
            service_url=f"http://127.0.0.1:{mock.port}",
            local_env_body="COORD_AUTH_TOKEN=revoked-token\n",
        )
        monkeypatch.chdir(repo)
        monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("COORD_REPO_ID", raising=False)
        exit_code = cli.main(["status"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "Active claims: status 401" in out
