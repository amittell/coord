"""Audit regression tests for the pre-push hook script in assets.py.

Covers:

- the coord bearer token never appearing on curl's command line (argv is
  world-readable via ps for the duration of each request); it travels in
  a 0600 curl config file instead, and reaches python3 via the
  environment, not argv;
- the session liveness probe treating kill -0 EPERM (process alive but
  owned by another user) as live instead of pruning the session's
  /conflicts self-exclusion.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordination.assets import PRE_PUSH_SCRIPT


# These regressions specifically cover POSIX permissions/liveness and their
# Windows-safe fallbacks, so they belong in the targeted platform smoke set.
pytestmark = pytest.mark.platform


def _require_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this platform")
    return bash


def _require_python3() -> None:
    if not shutil.which("python3"):
        pytest.skip("python3 not available on this platform")


# ---------------------------------------------------------------------------
# Token never on argv
# ---------------------------------------------------------------------------


def test_token_is_not_inlined_on_curl_argv() -> None:
    # The pre-fix form put the bearer token straight into curl's argv.
    assert '-H "Authorization: Bearer ${TOKEN}"' not in PRE_PUSH_SCRIPT
    assert '-H "Authorization: Bearer ${COORD_TOKEN}"' not in PRE_PUSH_SCRIPT
    # The auth now rides in a private curl config file...
    assert 'CURL_AUTH=(--config "${CURL_AUTH_CFG}")' in PRE_PUSH_SCRIPT
    # ...that is created by mktemp and cleaned up on exit.
    assert "mktemp" in PRE_PUSH_SCRIPT
    assert 'chmod 600 "${CURL_AUTH_CFG}"' in PRE_PUSH_SCRIPT
    assert "trap 'rm -f \"${CURL_AUTH_CFG}\"' EXIT" in PRE_PUSH_SCRIPT
    # The token reaches python3 via the environment, never argv.
    assert 'COORD_HOOK_TOKEN="${TOKEN}" python3' in PRE_PUSH_SCRIPT
    # The set -u guard on the expansion site survives (bash 3.2 compat).
    assert "CURL_AUTH=()" in PRE_PUSH_SCRIPT
    assert '${CURL_AUTH[@]+"${CURL_AUTH[@]}"}' in PRE_PUSH_SCRIPT


def _extract_auth_block(script: str) -> str:
    start = script.index("CURL_AUTH=()")
    end = script.index("if ! command -v jq", start)
    return script[start:end]


def test_auth_block_extractable() -> None:
    block = _extract_auth_block(PRE_PUSH_SCRIPT)
    assert "CURL_AUTH_CFG" in block
    assert "--config" in block


def test_auth_block_e2e_writes_token_to_private_config_not_argv(
    tmp_path,
) -> None:
    """Run the real auth-block fragment with a token containing a double
    quote and a backslash. The curl argv (CURL_AUTH expansion) must not
    contain the token; the config file must hold the properly escaped
    header line. The script's chmod 600 contract is verified through mode
    bits on POSIX and statically on Windows, whose CRT synthesizes mode bits."""
    bash = _require_bash()
    _require_python3()

    block = _extract_auth_block(PRE_PUSH_SCRIPT)
    fragment = tmp_path / "fragment.sh"
    fragment.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "TOKEN='sekret\"quo\\te'\n"
        f"{block}"
        'printf "ARGV:%s\\n" ${CURL_AUTH[@]+"${CURL_AUTH[@]}"}\n'
        'printf "PERM:%s\\n" "$(python3 -c "import os,sys,stat; '
        'print(oct(os.stat(sys.argv[1]).st_mode & 0o777))" "${CURL_AUTH_CFG}")"\n'
        'printf "CFG:%s\\n" "$(cat "${CURL_AUTH_CFG}")"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [bash, str(fragment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    argv_lines = [ln for ln in lines if ln.startswith("ARGV:")]
    perm_lines = [ln for ln in lines if ln.startswith("PERM:")]
    cfg_lines = [ln for ln in lines if ln.startswith("CFG:")]
    # argv carries only --config and a temp path; never the token.
    assert argv_lines[0] == "ARGV:--config"
    assert not any("sekret" in ln for ln in argv_lines)
    # Config file is private to the pushing user. Windows' CRT reports
    # synthetic POSIX mode bits (typically 0666) even after Git Bash's chmod,
    # so st_mode can only verify the permission contract on POSIX. The static
    # assertion above keeps the chmod itself pinned on every platform.
    assert len(perm_lines) == 1
    if os.name != "nt":
        assert perm_lines == ["PERM:0o600"]
    # The header line is present with backslash and quote escaped per the
    # curl config quoting rules.
    assert cfg_lines == [
        'CFG:header = "Authorization: Bearer sekret\\"quo\\\\te"'
    ]


def test_auth_block_e2e_empty_token_leaves_curl_auth_empty(tmp_path) -> None:
    bash = _require_bash()

    block = _extract_auth_block(PRE_PUSH_SCRIPT)
    fragment = tmp_path / "fragment.sh"
    fragment.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'TOKEN=""\n'
        f"{block}"
        'printf "[%s]" ${CURL_AUTH[@]+"${CURL_AUTH[@]}"}\n'
        'echo "ok"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [bash, str(fragment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
    assert "--config" not in result.stdout


@pytest.mark.parametrize("token_key", ["COORD_TOKEN", "COORD_AUTH_TOKEN", None])
@pytest.mark.parametrize("scenario", ["batch", "legacy", "conflict", "unreachable", "old-server"])
def test_complete_hook_keeps_auth_private_and_cleans_up(tmp_path, token_key, scenario) -> None:
    """Hold each real hook invocation of a curl spy and inspect its live argv.

    The spy replaces transport only. Git, jq, the whole generated hook, its
    private config, token identity selection and exit trap execute normally.
    No host credentials, Git config, coord instance or network are used.
    """
    bash = _require_bash()
    _require_python3()
    for executable in ("git", "jq"):
        if not shutil.which(executable):
            pytest.skip(f"{executable} not available")
    spy_dir = tmp_path / "bin"
    spy_dir.mkdir()
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    private_tmp = tmp_path / "tmp"
    private_tmp.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    token = 'synthetic-coord71-quote"-slash\\-only' if token_key else ""
    env = {
        "PATH": str(spy_dir) + os.pathsep + os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path), "TMPDIR": str(private_tmp), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "COORD_API_URL": "https://coord.invalid", "COORD_REPO_ID": "fixture/repo",
        "COORD_PREPUSH_MODE": "enforce", "COORD71_TRACE": str(trace_dir),
        "COORD71_SCENARIO": scenario,
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    if token_key:
        env[token_key] = token

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=repo, env=env, text=True, stderr=subprocess.PIPE,
        ).strip()

    git("init", "-q")
    git("config", "user.name", "fixture committer")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "change.txt").write_text("base\n", encoding="utf-8")
    git("add", "change.txt")
    git("commit", "-qm", "fixture base")
    base = git("rev-parse", "HEAD")
    (repo / "change.txt").write_text("candidate\n", encoding="utf-8")
    git("commit", "-qam", "fixture change")
    head = git("rev-parse", "HEAD")
    hook = tmp_path / "pre-push"
    hook.write_text(PRE_PUSH_SCRIPT, encoding="utf-8")
    spy = spy_dir / "curl"
    spy.write_text('''#!/usr/bin/env python3
import json, os, stat, sys, time
from pathlib import Path
root = Path(os.environ["COORD71_TRACE"])
scenario = os.environ["COORD71_SCENARIO"]
url = sys.argv[-1]
kind = "whoami" if url.endswith("/whoami") else "batch" if url.endswith("/batch") else "legacy"
configs = [Path(sys.argv[i + 1]) for i, arg in enumerate(sys.argv[:-1]) if arg == "--config"]
record = {"pid": os.getpid(), "argv": sys.argv, "configs": [str(p) for p in configs],
          "modes": [stat.S_IMODE(p.stat().st_mode) for p in configs],
          "config_contents": [p.read_text() for p in configs],
          "payload": sys.stdin.read() if kind == "batch" else ""}
temporary = root / (kind + ".tmp")
temporary.write_text(json.dumps(record))
temporary.replace(root / (kind + ".json"))
deadline = time.monotonic() + 10
while not (root / (kind + ".release")).exists():
    if time.monotonic() > deadline:
        raise SystemExit(98)
    time.sleep(0.01)
if kind == "whoami":
    if scenario == "old-server":
        print("curl: fixture endpoint unavailable", file=sys.stderr)
        raise SystemExit(22)
    print(json.dumps({"engineer": "fixture token owner"}))
elif kind == "batch":
    if scenario == "unreachable":
        print("curl: fixture connection failed", file=sys.stderr)
        raise SystemExit(7)
    print(json.dumps({"has_conflicts": scenario == "conflict"}))
    print("404" if scenario == "legacy" else "200")
else:
    print(json.dumps({"has_conflicts": False}))
''', encoding="utf-8")
    spy.chmod(0o755)
    expected = (["whoami"] if token_key else []) + ["batch"]
    if scenario == "legacy":
        expected.append("legacy")
    records = []
    proc = subprocess.Popen(
        [bash, str(hook), "origin"], cwd=repo, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(f"refs/heads/topic {head} refs/heads/topic {base}\n")
    proc.stdin.close()
    proc.stdin = None
    try:
        for kind in expected:
            receipt = trace_dir / (kind + ".json")
            deadline = time.monotonic() + 15
            while not receipt.exists():
                assert proc.poll() is None, f"hook exited before {kind} request"
                assert time.monotonic() < deadline, f"hook never issued {kind}"
                time.sleep(0.01)
            record = json.loads(receipt.read_text())
            records.append(record)
            assert not token or all(token not in arg for arg in record["argv"])
            if os.name != "nt":
                # Inspect only the held fixture child, never unrelated processes.
                live_argv = subprocess.check_output(
                    ["ps", "-ww", "-p", str(record["pid"]), "-o", "command="], text=True,
                )
                assert str(spy) in live_argv
                assert not token or token not in live_argv
                if token_key:
                    assert record["modes"] == [0o600]
            if token_key:
                escaped = token.replace("\\", "\\\\").replace('"', '\\"')
                assert record["config_contents"] == [
                    f'header = "Authorization: Bearer {escaped}"\n'
                ]
            else:
                assert record["configs"] == []
                assert "Authorization" not in " ".join(record["argv"])
            if kind == "batch":
                payload = json.loads(record["payload"])
                expected_engineer = (
                    "fixture token owner" if token_key and scenario != "old-server"
                    else "fixture committer"
                )
                assert payload["engineer"] == expected_engineer
                assert payload["repo"] == "fixture/repo"
                assert payload["patterns"] == ["change.txt"]
            (trace_dir / (kind + ".release")).touch()
    finally:
        # Release any held spy before awaiting the hook, including assertion failures.
        for kind in ("whoami", "batch", "legacy"):
            (trace_dir / (kind + ".release")).touch()
        try:
            stdout, stderr = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=15)
            raise
    assert (proc.returncode != 0) == (scenario in {"conflict", "unreachable"}), stderr
    assert not token or token not in stdout + stderr
    assert all(not Path(path).exists() for record in records for path in record["configs"])
    assert not list(private_tmp.glob("coord-pre-push.*"))


# ---------------------------------------------------------------------------
# Liveness probe: EPERM means alive
# ---------------------------------------------------------------------------


def _extract_liveness_function(script: str) -> str:
    start = script.index("coord_pid_is_live() {")
    py_end = script.index("\nPY\n", start)
    close = script.index("\n  }\n", py_end)
    return script[start : close + len("\n  }\n")]


def test_liveness_function_extractable_and_handles_eperm() -> None:
    fn = _extract_liveness_function(PRE_PUSH_SCRIPT)
    assert "kill -0" in fn
    # POSIX re-probe distinguishing EPERM (live) from ESRCH (dead).
    assert "except PermissionError:" in fn
    assert "except ProcessLookupError:" in fn


@pytest.mark.parametrize(
    ("pid", "expect_live"),
    [
        ("SELF", True),  # own process: kill -0 succeeds directly
        ("1", True),  # pid 1 (root-owned init/launchd): EPERM but alive
        ("99999999", False),  # beyond pid_max everywhere: dead
    ],
)
def test_liveness_probe_e2e(tmp_path, pid: str, expect_live: bool) -> None:
    bash = _require_bash()
    _require_python3()
    if os.name == "nt":
        pytest.skip("POSIX liveness semantics only")

    fn = _extract_liveness_function(PRE_PUSH_SCRIPT)
    probe_pid = str(os.getpid()) if pid == "SELF" else pid
    fragment = tmp_path / "fragment.sh"
    fragment.write_text(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        f"{fn}\n"
        f'if coord_pid_is_live "{probe_pid}"; then\n'
        '  echo "LIVE"\n'
        "else\n"
        '  echo "DEAD"\n'
        "fi\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [bash, str(fragment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    verdict = "LIVE" if expect_live else "DEAD"
    assert verdict in result.stdout, (
        f"pid {probe_pid}: expected {verdict}, got {result.stdout!r} "
        f"(stderr: {result.stderr!r})"
    )
