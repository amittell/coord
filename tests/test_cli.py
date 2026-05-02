from __future__ import annotations

import json
from pathlib import Path
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler

import pytest

import coordination
from coordination import cli


def _make_repo(root: Path) -> Path:
    repo = root / "app"
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / "src" / "auth").mkdir(parents=True)
    (repo / "src" / "auth" / "login.ts").write_text("export {};\n", encoding="utf-8")
    (repo / "package.json").write_text('{"name": "app"}\n', encoding="utf-8")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    return repo


class _FakeProcess:
    def __init__(self, command: list[str], env: dict[str, str], returncode: int | None = None) -> None:
        self.command = command
        self.env = env
        self.pid = 43210
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_start_background_bootstraps_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    seen: dict[str, object] = {}

    def fake_popen(command: list[str], env: dict[str, str] | None = None, **_: object) -> _FakeProcess:
        assert env is not None
        seen["command"] = command
        seen["env"] = env
        return _FakeProcess(command, env)

    monkeypatch.setattr("coordination.cli_start.subprocess.Popen", fake_popen)
    monkeypatch.setattr("coordination.cli_start._wait_for_http_ready", lambda *args, **kwargs: None)

    exit_code = cli.main(["start", "--background", "--port", "8123", "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["api_url"] == "http://127.0.0.1:8123"
    assert output["dashboard_url"] == "http://127.0.0.1:8123/dashboard"
    assert Path(output["token_file"]).exists()
    assert Path(output["database_path"]).parent.exists()
    assert seen["command"] == [sys.executable, "-m", "coordination.cli", "_serve"]
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["COORD_PORT"] == "8123"
    assert env["COORD_AUTH_TOKEN"]


def test_start_background_fails_if_service_never_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    def fake_popen(command: list[str], env: dict[str, str] | None = None, **_: object) -> _FakeProcess:
        assert env is not None
        return _FakeProcess(command, env, returncode=1)

    monkeypatch.setattr("coordination.cli_start.subprocess.Popen", fake_popen)

    class _Boom(Exception):
        pass

    def fake_wait(*_: object, **__: object) -> None:
        raise RuntimeError("service did not become ready")

    monkeypatch.setattr("coordination.cli_start._wait_for_http_ready", fake_wait)

    exit_code = cli.main(["start", "--background", "--port", "8124"])

    assert exit_code == 1
    assert "service did not become ready" in capsys.readouterr().out


def test_init_claude_creates_repo_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    exit_code = cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"])

    assert exit_code == 0
    assert (repo / ".coordination" / "config.toml").exists()
    assert (repo / ".coordination" / "owners.yaml").exists()
    assert (repo / ".coordination" / "local.env").exists()
    assert (repo / ".mcp.json").exists()
    assert (repo / "CLAUDE.md").exists()
    assert (repo / ".git" / "hooks" / "pre-push").exists()

    mcp = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
    assert "coord" in mcp["mcpServers"]
    assert mcp["mcpServers"]["coord"]["command"] == "coord-mcp"

    config_text = (repo / ".coordination" / "config.toml").read_text(encoding="utf-8")
    assert 'tool = "claude"' in config_text
    assert 'mode = "local"' in config_text

    claude_text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "<!-- coord:begin -->" in claude_text
    assert "Coordination protocol" in claude_text

    gitignore_text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".coordination/local.env" in gitignore_text

    # local.env must carry the service URL (under both names) and the token.
    # Without the URL, the pre-push hook silently falls back to localhost.
    local_env_text = (repo / ".coordination" / "local.env").read_text(encoding="utf-8")
    assert local_env_text.count("COORD_API_URL=") == 1
    assert local_env_text.count("COORD_SERVICE_URL=") == 1
    assert local_env_text.count("COORD_AUTH_TOKEN=") == 1
    assert "COORD_API_URL=http://127.0.0.1:8080" in local_env_text


def test_init_does_not_clobber_pre_push_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: the user's `.git/hooks/pre-push` may be a symlink to
    a tracked repo file (e.g. `scripts/git-hooks/pre-push`) carrying
    real CI logic. coord init must NEVER write through that symlink --
    `pathlib.Path.write_text` follows symlinks, which silently
    overwrites the target file. The previously-tracked hook is then
    lost (until restored from git) and the user's CI / lint / deploy
    guardrails stop running."""
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    # Set up a symlinked .git/hooks/pre-push, where the target carries
    # the user's real (non-coord) hook content.
    (repo / "scripts" / "git-hooks").mkdir(parents=True)
    real_hook = repo / "scripts" / "git-hooks" / "pre-push"
    real_hook.write_text(
        "#!/usr/bin/env bash\n# user's CI lint hook\nset -e\necho ci-checks\n",
        encoding="utf-8",
    )
    git_hook = repo / ".git" / "hooks" / "pre-push"
    git_hook.parent.mkdir(parents=True, exist_ok=True)
    git_hook.symlink_to(real_hook)

    pre_init_content = real_hook.read_text(encoding="utf-8")
    exit_code = cli.main(
        ["init", "--tool", "claude", "--mode", "local", "--yes", "--force"]
    )

    assert exit_code == 0, "init should not fail when .git/hooks/pre-push is a symlink"
    # The user's tracked hook content is untouched.
    assert real_hook.read_text(encoding="utf-8") == pre_init_content, (
        "coord init clobbered the symlink target; tracked hook content was overwritten"
    )
    # Coord still printed actionable guidance so the user can wire the
    # chain themselves.
    err = capsys.readouterr().err
    assert ".git/hooks/pre-push is a symlink" in err
    assert ".coordination/hooks/pre-push" in err


def test_init_is_idempotent_for_managed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0
    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0

    claude_text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude_text.count("<!-- coord:begin -->") == 1


def test_init_preserves_existing_mcp_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"command": "npx", "args": ["-y", "@anthropic/mcp-github"]},
                }
            }
        ),
        encoding="utf-8",
    )

    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0

    mcp = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
    assert "github" in mcp["mcpServers"]
    assert "coord" in mcp["mcpServers"]


def test_doctor_reports_healthy_repo_and_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))
    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            auth = self.headers.get("Authorization")
            if self.path == "/readyz":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    b'{"status":"ready","version":"0.1.0","auth_mode":"bearer","database_path":"x"}'
                )
                return
            if self.path == "/claims" and auth and auth.startswith("Bearer "):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"claims":[],"count":0}')
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = (repo / ".coordination" / "config.toml").read_text(encoding="utf-8")
            config = config.replace("http://127.0.0.1:8080", f"http://127.0.0.1:{port}")
            (repo / ".coordination" / "config.toml").write_text(config, encoding="utf-8")

            exit_code = cli.main(["doctor"])
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "OK  repo is initialized" in output
    assert "OK  coordination service reachable" in output
    assert "OK  auth token works" in output


class _StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_start_background_skips_spawn_when_service_already_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    popen_called = {"n": 0}

    def fake_popen(*_: object, **__: object) -> _FakeProcess:
        popen_called["n"] += 1
        return _FakeProcess([], {})

    def fake_get(url: str, *_: object, **__: object) -> _StubResponse:
        assert url.endswith("/readyz")
        return _StubResponse(200)

    monkeypatch.setattr("coordination.cli_start.subprocess.Popen", fake_popen)
    monkeypatch.setattr("coordination.cli_start.httpx.get", fake_get)

    exit_code = cli.main(["start", "--background", "--port", "8125", "--json"])

    assert exit_code == 0
    assert popen_called["n"] == 0
    output = json.loads(capsys.readouterr().out)
    assert output["api_url"] == "http://127.0.0.1:8125"


def test_start_background_spawns_when_port_is_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx as _httpx

    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    seen: dict[str, object] = {}

    def fake_popen(command: list[str], env: dict[str, str] | None = None, **_: object) -> _FakeProcess:
        assert env is not None
        seen["command"] = command
        return _FakeProcess(command, env)

    def fake_get(url: str, *_: object, **__: object) -> _StubResponse:
        raise _httpx.ConnectError("nothing listening")

    monkeypatch.setattr("coordination.cli_start.subprocess.Popen", fake_popen)
    monkeypatch.setattr("coordination.cli_start.httpx.get", fake_get)
    monkeypatch.setattr("coordination.cli_start._wait_for_http_ready", lambda *a, **k: None)

    exit_code = cli.main(["start", "--background", "--port", "8126", "--json"])

    assert exit_code == 0
    assert seen["command"] == [sys.executable, "-m", "coordination.cli", "_serve"]


def test_init_warns_when_service_unreachable_in_local_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import httpx as _httpx

    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    def fake_get(*_: object, **__: object) -> _StubResponse:
        raise _httpx.ConnectError("nothing listening")

    monkeypatch.setattr("coordination.cli_init.httpx.get", fake_get)

    exit_code = cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"])

    assert exit_code == 0
    assert (repo / ".coordination" / "config.toml").exists()
    err = capsys.readouterr().err
    assert "coord start" in err


def test_init_does_not_warn_when_service_reachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    def fake_get(*_: object, **__: object) -> _StubResponse:
        return _StubResponse(200)

    monkeypatch.setattr("coordination.cli_init.httpx.get", fake_get)

    exit_code = cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "coord start" not in err


def test_doctor_hints_remediation_when_service_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import httpx as _httpx

    repo = _make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    def fake_init_get(*_: object, **__: object) -> _StubResponse:
        return _StubResponse(200)

    monkeypatch.setattr("coordination.cli_init.httpx.get", fake_init_get)

    assert cli.main(["init", "--tool", "claude", "--mode", "local", "--yes"]) == 0

    def fake_doctor_get(*_: object, **__: object) -> _StubResponse:
        raise _httpx.ConnectError("connection refused")

    monkeypatch.setattr("coordination.cli_doctor.httpx.get", fake_doctor_get)

    capsys.readouterr()
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "coord start" in out


def test_init_with_root_flag_writes_to_subpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    (repo / "apps" / "web").mkdir(parents=True)
    (repo / "apps" / "web" / "package.json").write_text('{"name": "web"}\n', encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    exit_code = cli.main(
        ["init", "--tool", "claude", "--mode", "local", "--yes", "--root", "apps/web"]
    )

    assert exit_code == 0
    assert (repo / "apps" / "web" / ".coordination" / "config.toml").exists()
    assert not (repo / ".coordination" / "config.toml").exists()

    claude_text = (repo / "apps" / "web" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "<!-- coord:begin -->" in claude_text


def test_init_with_root_flag_rejects_path_outside_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Tmp dir with no .git in any ancestor.
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    bogus_target = outside / "nowhere"

    exit_code = cli.main(
        [
            "init",
            "--tool",
            "claude",
            "--mode",
            "local",
            "--yes",
            "--root",
            str(bogus_target),
        ]
    )

    assert exit_code != 0
    combined = capsys.readouterr()
    message = combined.out + combined.err
    # The message should clearly point at the --root path or at "git".
    assert "--root" in message or "git" in message.lower()


def test_init_with_root_flag_respects_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    service = repo / "services" / "foo"
    service.mkdir(parents=True)
    (service / "pyproject.toml").write_text("[project]\nname = 'foo'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORD_HOME", str(tmp_path / ".coord-home"))

    exit_code = cli.main(
        [
            "init",
            "--tool",
            "claude",
            "--mode",
            "local",
            "--yes",
            "--root",
            str(service.resolve()),
        ]
    )

    assert exit_code == 0
    assert (service / ".coordination" / "config.toml").exists()
    assert not (repo / ".coordination" / "config.toml").exists()


def test_version_flag_prints_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    # Banner is decorative; the machine-readable part must contain the
    # literal string "coord <version>" somewhere on its own line and the
    # output must still end with the version number for scripts that
    # tail-read it.
    assert f"coord {coordination.__version__}" in out
    assert out.rstrip("\n").endswith(coordination.__version__)
    # Banner sanity: the figlet-style letterforms use slashes and
    # underscores; at least one of each should appear.
    assert "/" in out and "_" in out


def test_version_in_parser_does_not_conflict_with_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["start", "--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--host" in out
    assert "--port" in out
