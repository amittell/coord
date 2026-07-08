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

import os
import shutil
import subprocess

import pytest

from coordination.assets import PRE_PUSH_SCRIPT


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
    # The auth now rides in a private curl config file...
    assert 'CURL_AUTH=(--config "${CURL_AUTH_CFG}")' in PRE_PUSH_SCRIPT
    # ...that is created by mktemp and cleaned up on exit.
    assert "mktemp" in PRE_PUSH_SCRIPT
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
    header line and be chmod 600."""
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
    # Config file is private to the pushing user.
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
