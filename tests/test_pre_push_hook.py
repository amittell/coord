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


def test_script_still_skips_on_missing_jq_and_unreachable_upstream() -> None:
    # These remain intentional soft-fail paths -- document via assertion so
    # future edits don't silently remove them.
    assert "jq not installed; skipping" in PRE_PUSH_SCRIPT
    assert "could not determine diff base; skipping" in PRE_PUSH_SCRIPT


def test_script_sources_local_env_before_reading_config() -> None:
    # The hook must source .coordination/local.env before falling back
    # to env vars and defaults. Without this, a remote-mode repo would
    # silently hit http://127.0.0.1:8080 whenever COORD_API_URL is not
    # set in the pushing shell.
    assert 'source "${REPO_ROOT}/.coordination/local.env"' in PRE_PUSH_SCRIPT
    # URL precedence must prefer COORD_API_URL (written by `coord init`)
    # over the legacy COORD_SERVICE_URL / COORD_URL names.
    assert '"${COORD_API_URL:-${COORD_SERVICE_URL:-${COORD_URL:-' in PRE_PUSH_SCRIPT
