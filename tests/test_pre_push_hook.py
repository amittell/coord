from __future__ import annotations

import shutil
import subprocess

import pytest

from coordination.assets import PRE_PUSH_SCRIPT


def test_script_runs_conflict_check_when_token_is_empty() -> None:
    # The old script exited 0 with "COORD_TOKEN (or COORD_AUTH_TOKEN) not set;
    # skipping" whenever the token was empty, which silently disabled the
    # hook for services running with COORD_ALLOW_INSECURE_NO_AUTH=true.
    assert "not set; skipping" not in PRE_PUSH_SCRIPT
    # The fix wraps the Authorization header in a conditional array. The
    # expansion site MUST use the ${var[@]+"${var[@]}"} form so empty
    # arrays don't trip `set -u` on bash 3.2 (macOS system bash).
    assert 'CURL_AUTH=()' in PRE_PUSH_SCRIPT
    assert '${CURL_AUTH[@]+"${CURL_AUTH[@]}"}' in PRE_PUSH_SCRIPT


def test_script_runs_under_bash_3_2_set_u(tmp_path) -> None:
    # Regression test for a bug where ${CURL_AUTH[@]} expanded under
    # `set -u` on bash 3.2 raised "unbound variable" and broke pushes on
    # stock macOS. We can't easily install bash 3.2 in CI, but we can
    # exercise the relevant fragment under whichever bash we have plus
    # the ${var[@]+...} guard, which is what makes 3.2 happy too.
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this platform")
    fragment = tmp_path / "fragment.sh"
    fragment.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "CURL_AUTH=()\n"
        "TOKEN=\"\"\n"
        'if [[ -n "${TOKEN}" ]]; then\n'
        '  CURL_AUTH=(-H "Authorization: Bearer ${TOKEN}")\n'
        "fi\n"
        # Use the same expansion form the real hook does.
        'printf "%s\\n" ${CURL_AUTH[@]+"${CURL_AUTH[@]}"}\n'
        'echo "ok"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [bash, str(fragment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"empty-array expansion broke under set -u: {result.stderr}"
    )
    assert "ok" in result.stdout


def test_script_is_syntactically_valid_bash(tmp_path) -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this platform")
    script = tmp_path / "pre-push"
    script.write_text(PRE_PUSH_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [bash, "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_script_fails_closed_on_missing_jq() -> None:
    """v0.7 inverted the missing-jq path. Pre-v0.7 silently exited 0 when
    jq wasn't installed, which let a developer push without the conflict
    check ever running. v0.7 refuses the push so the operator must
    install jq or pass --no-verify deliberately."""
    assert "jq not installed; refusing to push" in PRE_PUSH_SCRIPT
    assert "jq not installed; skipping" not in PRE_PUSH_SCRIPT


def test_script_fails_closed_on_curl_error() -> None:
    """v0.7 also closed the silent-bypass on transport errors. Pre-v0.7
    wrapped the curl call in '|| true', so a transient network glitch
    produced an empty response and the check passed by default. v0.7
    explicitly checks curl's exit code and refuses on failure."""
    assert "conflict check failed for ${file}; refusing to push" in PRE_PUSH_SCRIPT
    # The blanket '|| true' on the curl invocation is gone.
    assert "|| true)\"\n  has=" not in PRE_PUSH_SCRIPT


def test_script_refuses_when_stdin_redirected_but_empty() -> None:
    """v0.7.2: when an outer wrapper hook backgrounds us (or otherwise
    drops git's pre-push ref-update stream), stdin is redirected (not
    a TTY) but PUSH_INPUT comes back empty. The pre-v0.7.2 hook
    silently fell through to a HEAD-vs-origin/HEAD diff in this case,
    which misses non-HEAD pushes, multi-ref pushes, and new-branch
    pushes -- exactly the failure mode astrowars's run_child wrapper
    introduced. Refuse loudly with actionable guidance instead."""
    # The script must contain the strict refusal message and the
    # actionable hint about forwarding stdin.
    assert "stdin was redirected but empty" in PRE_PUSH_SCRIPT
    assert "Refusing rather than" in PRE_PUSH_SCRIPT
    # The fallback HEAD-based path is gated behind a TTY check now
    # (hand-running for tests is fine).
    assert "[[ -t 0 ]]" in PRE_PUSH_SCRIPT or "[ -t 0 ]" in PRE_PUSH_SCRIPT


def test_script_consumes_push_stdin_for_per_ref_diffs() -> None:
    """git push hands the hook ref-update info on stdin, one line per
    ref in the form '<local_ref> <local_sha> <remote_ref> <remote_sha>'.
    The hook must read this so non-HEAD pushes, multi-ref pushes, and
    deleted-branch pushes all get the right diff base."""
    assert "PUSH_INPUT" in PRE_PUSH_SCRIPT
    assert "while read -r local_ref local_sha remote_ref remote_sha" in PRE_PUSH_SCRIPT
    # Empty-tree fallback for first-push scenarios where triple-dot fails.
    assert "EMPTY_TREE" in PRE_PUSH_SCRIPT


def test_script_sources_local_env_before_reading_config() -> None:
    # The hook must source .coordination/local.env before falling back
    # to env vars and defaults. Without this, a remote-mode repo would
    # silently hit http://127.0.0.1:8080 whenever COORD_API_URL is not
    # set in the pushing shell.
    assert 'source "${REPO_ROOT}/.coordination/local.env"' in PRE_PUSH_SCRIPT
    # URL precedence must prefer COORD_API_URL (written by `coord init`)
    # over the legacy COORD_SERVICE_URL / COORD_URL names.
    assert '"${COORD_API_URL:-${COORD_SERVICE_URL:-${COORD_URL:-' in PRE_PUSH_SCRIPT


def test_script_passes_repo_id_to_conflicts_endpoint() -> None:
    # v0.4.0: when COORD_REPO_ID is set (sourced from local.env), the hook
    # must forward it as &repo=<id> on the /conflicts query so the server
    # scopes the conflict check to claims from the same repo. Without
    # this, cross-repo path collisions false-positive.
    assert "COORD_REPO_ID" in PRE_PUSH_SCRIPT
    # The query string must include &repo=... only when repo id is
    # non-empty; the hook must not send a literal "&repo=" trailing the URL.
    assert "&repo=" in PRE_PUSH_SCRIPT
