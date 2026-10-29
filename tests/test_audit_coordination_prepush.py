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


def _wait_for_spy_receipt(receipt, proc, deadline, *, clock=time.monotonic, sleep=time.sleep):
    """Wait for a readable receipt while both fixture deadlines remain valid."""
    while True:
        assert proc.poll() is None, f"hook exited before {receipt.stem} request"
        assert clock() < deadline, f"{receipt.stem} receipt not readable before deadline"
        try:
            text = receipt.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError):
            sleep(0.01)
            continue
        # A published receipt is complete. Malformed JSON must fail immediately.
        record = json.loads(text)
        assert proc.poll() is None, f"hook exited while reading {receipt.stem} receipt"
        now = clock()
        assert now < deadline, f"{receipt.stem} receipt read exceeded deadline"
        # The hook can outlive a timed-out /whoami spy, so hook.poll() alone
        # does not prove this observation was made during the spy's hold.
        assert now < record["hold_until"], f"{receipt.stem} spy hold expired"
        return record


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
deadline = time.monotonic() + 10
record["hold_until"] = deadline
temporary.write_text(json.dumps(record))
temporary.replace(root / (kind + ".json"))
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
            record = _wait_for_spy_receipt(receipt, proc, deadline)
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


def test_complete_hook_retries_one_receipt_permission_error(tmp_path, monkeypatch) -> None:
    receipt = tmp_path / "trace" / "whoami.json"
    read_text = Path.read_text
    denied = []
    successful_reads = []

    def read(path, *args, **kwargs):
        if path == receipt and path.exists() and not denied:
            denied.append(path)
            raise PermissionError("synthetic receipt denial")
        text = read_text(path, *args, **kwargs)
        if path == receipt:
            successful_reads.append(path)
        return text

    monkeypatch.setattr(Path, "read_text", read)
    test_complete_hook_keeps_auth_private_and_cleans_up(tmp_path, "COORD_TOKEN", "batch")
    assert denied == [receipt]
    assert successful_reads == [receipt]


def test_complete_hook_persistent_receipt_denial_fails_and_cleans_up(tmp_path, monkeypatch) -> None:
    receipt = tmp_path / "trace" / "whoami.json"
    read_text = Path.read_text
    wait = _wait_for_spy_receipt
    denied = []

    def read(path, *args, **kwargs):
        if path == receipt and path.exists():
            denied.append(path)
            raise PermissionError("synthetic persistent receipt denial")
        return read_text(path, *args, **kwargs)

    def bounded_wait(path, proc, deadline):
        # Advance only this helper's clock after two actual receipt attempts;
        # allow the real hook/spy to start normally, without a 15-second test.
        def clock():
            return deadline if len(denied) >= 2 else time.monotonic()

        return wait(path, proc, deadline, clock=clock)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setitem(globals(), "_wait_for_spy_receipt", bounded_wait)
    with pytest.raises(AssertionError, match="receipt not readable before deadline"):
        test_complete_hook_keeps_auth_private_and_cleans_up(tmp_path, "COORD_TOKEN", "batch")
    assert denied == [receipt, receipt]
    record = json.loads(read_text(receipt))
    assert all(not Path(path).exists() for path in record["configs"])
    assert not list((tmp_path / "tmp").glob("coord-pre-push.*"))
    assert all(
        (tmp_path / "trace" / f"{kind}.release").exists()
        for kind in ("whoami", "batch", "legacy")
    )


class _ReceiptHook:
    returncode = None

    def poll(self):
        return self.returncode


class _ReceiptClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.parametrize("denial", [FileNotFoundError, PermissionError])
def test_receipt_retries_only_readiness_errors(tmp_path, monkeypatch, denial) -> None:
    receipt = tmp_path / "whoami.json"
    expected = {"hold_until": 10, "observed": "synthetic"}
    receipt.write_text(json.dumps(expected), encoding="utf-8")
    read_text = Path.read_text
    attempts = []

    def read(path, *args, **kwargs):
        if path == receipt:
            attempts.append(path)
            if len(attempts) == 1:
                raise denial("synthetic readiness failure")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    clock = _ReceiptClock()
    assert _wait_for_spy_receipt(receipt, _ReceiptHook(), 15, clock=clock, sleep=clock.sleep) == expected
    assert attempts == [receipt, receipt]
    assert clock.sleeps == [0.01]


@pytest.mark.parametrize("content", ["not json", "{"])
def test_receipt_malformed_json_fails_without_retry(tmp_path, content) -> None:
    receipt = tmp_path / "whoami.json"
    receipt.write_text(content, encoding="utf-8")
    clock = _ReceiptClock()
    with pytest.raises(json.JSONDecodeError):
        _wait_for_spy_receipt(receipt, _ReceiptHook(), 15, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == []


def test_receipt_unrelated_read_error_is_not_retried(tmp_path, monkeypatch) -> None:
    receipt = tmp_path / "whoami.json"
    failure = OSError("synthetic unrelated read error")
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == receipt:
            raise failure
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    clock = _ReceiptClock()
    with pytest.raises(OSError) as caught:
        _wait_for_spy_receipt(receipt, _ReceiptHook(), 15, clock=clock, sleep=clock.sleep)
    assert caught.value is failure
    assert clock.sleeps == []


@pytest.mark.parametrize(
    "expired",
    ["hook-before", "consumer-before", "spy-before",
     "hook-during-read", "consumer-during-read", "spy-during-read"],
)
def test_receipt_rejects_expired_lifetimes(tmp_path, monkeypatch, expired) -> None:
    receipt = tmp_path / "whoami.json"
    receipt.write_text(json.dumps({"hold_until": 10}), encoding="utf-8")
    hook = _ReceiptHook()
    clock = _ReceiptClock()
    if expired == "hook-before":
        hook.returncode = 0
    elif expired == "consumer-before":
        clock.now = 15
    elif expired == "spy-before":
        clock.now = 10
    read_text = Path.read_text
    attempts = []

    def read(path, *args, **kwargs):
        text = read_text(path, *args, **kwargs)
        if path == receipt:
            attempts.append(path)
            if expired == "hook-during-read":
                hook.returncode = 0
            elif expired == "consumer-during-read":
                clock.now = 15
            elif expired == "spy-during-read":
                clock.now = 10
        return text

    monkeypatch.setattr(Path, "read_text", read)
    if expired.startswith("hook"):
        message = "hook exited"
    elif expired.startswith("consumer"):
        message = "deadline"
    else:
        message = "spy hold expired"
    with pytest.raises(AssertionError, match=message):
        _wait_for_spy_receipt(receipt, hook, 15, clock=clock, sleep=clock.sleep)
    assert attempts == ([] if expired in {"hook-before", "consumer-before"} else [receipt])
    assert clock.sleeps == []


@pytest.mark.skipif(os.name != "nt", reason="requires Windows CreateFileW sharing semantics")
def test_receipt_recovers_after_native_windows_sharing_denial(tmp_path, monkeypatch) -> None:
    import ctypes
    import errno
    from ctypes import wintypes

    # Models the documented shared-open condition, not the historical CI cause:
    # https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    receipt = tmp_path / "whoami.json"
    expected = {"hold_until": time.monotonic() + 10, "observed": "synthetic"}
    receipt.write_text(json.dumps(expected), encoding="utf-8")
    # GENERIC_READ, no sharing, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL.
    handle = create(str(receipt), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    read_text = Path.read_text
    denials = []

    def release():
        nonlocal handle
        if handle is not None:
            if not close(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None

    def read(path, *args, **kwargs):
        try:
            return read_text(path, *args, **kwargs)
        except PermissionError as error:
            if path == receipt:
                assert error.errno == errno.EACCES
                # FileIO's CRT open may report errno13 without winerror.
                # Require a native sharing-violation code from a real open
                # while the same exclusive handle is still held.
                assert getattr(error, "winerror", None) in (None, 32)
                probe = create(str(receipt), 0x80000000, 7, None, 3, 0x80, None)
                code = ctypes.get_last_error()
                if probe != ctypes.c_void_p(-1).value:
                    try:
                        raise AssertionError("exclusive fixture file allowed a second open")
                    finally:
                        if not close(probe):
                            raise ctypes.WinError(ctypes.get_last_error())
                assert code == 32  # ERROR_SHARING_VIOLATION
                denials.append(code)
                # Release only after the actual read was denied. The next
                # helper attempt reaches the real filesystem again.
                release()
            raise

    try:
        monkeypatch.setattr(Path, "read_text", read)
        actual = _wait_for_spy_receipt(receipt, _ReceiptHook(), time.monotonic() + 15)
        assert actual == expected
        assert denials == [32]
        assert handle is None
    finally:
        release()


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
