from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordination.assets import PRE_PUSH_SCRIPT
from coordination.cli_init import _install_hook


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


def test_script_passes_branch_to_conflicts_endpoint() -> None:
    # v0.34: the hook must forward the pushing branch as &branch=<branch>
    # on the /conflicts query so a bounced push can be surfaced as a
    # comment on the corresponding open PR. The branch is built into a
    # BRANCH_QS fragment (mirroring REPO_QS) that is only non-empty when
    # BRANCH is set, and is interpolated onto the curl URL after
    # ${SESSION_QS}. Without this, the server never learns which branch
    # bounced and cannot resolve the PR to comment on.
    assert 'BRANCH_QS=""' in PRE_PUSH_SCRIPT
    assert "&branch=" in PRE_PUSH_SCRIPT
    # The conditional must gate on a non-empty BRANCH and exclude the
    # detached-HEAD sentinel ("HEAD") so a detached checkout does not
    # append a useless "&branch=HEAD" (which the server cannot resolve a
    # PR for anyway).
    assert 'if [[ -n "${BRANCH}" && "${BRANCH}" != "HEAD" ]]; then' in PRE_PUSH_SCRIPT
    # The curl URL must interpolate ${BRANCH_QS} right after the session
    # query string so all three optional segments compose cleanly.
    assert "${REPO_QS}${SESSION_QS}${BRANCH_QS}" in PRE_PUSH_SCRIPT


def test_script_branch_qs_url_encoded_via_urllib() -> None:
    # The branch value must be URL-encoded with the same python3 -c
    # urllib.parse.quote idiom used for ENGINEER / REPO_ID / SESSION_ID
    # so a branch name with slashes or other reserved characters does
    # not corrupt the query string.
    assert (
        '&branch=$(python3 -c "import urllib.parse,sys; '
        'print(urllib.parse.quote(sys.argv[1]))" "${BRANCH}")'
    ) in PRE_PUSH_SCRIPT


def test_script_references_sessions_live_file() -> None:
    # v0.10.0: coord-mcp writes its session_id to .coordination/sessions.live
    # on startup so the pre-push hook can hand the active session ids to
    # /conflicts as self-exclusions. Without this, an agent's own subagent
    # claims (created under engineer names like 'codex-server-review' that
    # don't match `git config user.name`) blow up the agent's own push.
    assert ".coordination/sessions.live" in PRE_PUSH_SCRIPT


def test_script_initializes_session_qs_default_empty() -> None:
    # SESSION_QS must default to "" before the conditional file-read so a
    # repo without sessions.live still falls through to the existing
    # engineer-name self-exclusion path. Silently bailing out of the hook
    # when the file is missing would be a regression.
    assert 'SESSION_QS=""' in PRE_PUSH_SCRIPT
    # And the curl URL must always interpolate ${SESSION_QS} (it's a no-op
    # when empty but must be present so the missing-file path still
    # exercises the conflict check rather than skipping it).
    assert "${SESSION_QS}" in PRE_PUSH_SCRIPT
    assert "${REPO_QS}${SESSION_QS}" in PRE_PUSH_SCRIPT


def test_script_url_encodes_session_ids_via_urllib() -> None:
    # Each session id must be URL-encoded via the same python3 -c
    # urllib.parse.quote idiom the hook already uses for ENGINEER and
    # REPO_ID, so unusual characters in session ids (slashes, spaces) do
    # not corrupt the query string.
    quote_fragment = (
        'python3 -c "import urllib.parse,sys; '
        'print(urllib.parse.quote(sys.argv[1]))"'
    )
    # The fragment is used multiple times; just confirm it shows up at
    # least three times now (ENGINEER + REPO_ID + SESSION_ID).
    assert PRE_PUSH_SCRIPT.count(quote_fragment) >= 3
    # And the SESSION_QS build path must produce &session_id=<encoded>.
    assert "&session_id=" in PRE_PUSH_SCRIPT


def test_script_session_qs_skips_blank_and_comment_lines() -> None:
    # Sessions.live is a line-oriented file; blank lines and lines
    # starting with '#' must be ignored so future operator notes or
    # accidental trailing newlines don't poison the query string.
    # The script encodes this by checking each line for emptiness and a
    # leading '#'.
    assert "session_line" in PRE_PUSH_SCRIPT
    # A leading-# guard must be present in some form. Match the literal
    # case-pattern OR the [[ ... == \#* ]] form, whichever the
    # implementation uses.
    assert ("== \\#*" in PRE_PUSH_SCRIPT) or ("'#'*" in PRE_PUSH_SCRIPT)


def _extract_session_qs_block(script: str) -> str:
    # Pull the SESSION_QS-build fragment out of PRE_PUSH_SCRIPT so the
    # e2e test stays in lockstep with whatever the script actually does
    # (no copy-paste drift).
    start_marker = 'SESSION_QS=""'
    end_marker = "while IFS= read -r file; do"
    start = script.index(start_marker)
    end = script.index(end_marker, start)
    return script[start:end]


def test_script_session_qs_block_extractable() -> None:
    # Sanity check that the e2e fixture below will be able to find the
    # block; if these markers ever drift, fail loudly here rather than
    # via a confusing bash error in the e2e test.
    block = _extract_session_qs_block(PRE_PUSH_SCRIPT)
    assert "sessions.live" in block
    assert "session_id=" in block


def test_script_session_qs_e2e_builds_expected_string(tmp_path) -> None:
    # End-to-end: seed a fake .coordination/sessions.live with two known
    # session ids (one plain, one with a slash to exercise URL-encoding)
    # plus a comment line and a blank line, then run just the SESSION_QS
    # build fragment under bash and assert SESSION_QS came out as
    # '&session_id=<id1>&session_id=<id2>' with the slash %-encoded.
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this platform")
    if not shutil.which("python3"):
        pytest.skip("python3 not available on this platform")

    coord_dir = tmp_path / ".coordination"
    coord_dir.mkdir()
    sessions_live = coord_dir / "sessions.live"
    # Use a session id with a character that the default
    # urllib.parse.quote (safe='/') will encode -- '#' becomes %23 -- so
    # the test verifies encoding actually fires. Keep the second id
    # plain to verify pass-through too.
    # v0.12 format: "<session_id> <pid> <start_time_ns>". Use the current
    # process PID (alive for the duration of the test) so the hook's
    # kill -0 liveness probe passes and both entries make it into SESSION_QS.
    live_pid = os.getpid()
    sessions_live.write_text(
        "# this is a comment\n"
        "\n"
        f"session-one {live_pid} 0\n"
        f"mcp#session-two {live_pid} 0\n",
        encoding="utf-8",
    )

    block = _extract_session_qs_block(PRE_PUSH_SCRIPT)

    fragment = tmp_path / "fragment.sh"
    fragment.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'REPO_ROOT="{tmp_path}"\n'
        f"{block}"
        'printf "%s" "${SESSION_QS}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(fragment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # '#' in 'mcp#session-two' must be %-encoded to %23. Default
    # urllib.parse.quote leaves '/' alone (safe='/'), so we exercise '#'
    # to confirm the encoding path actually fires.
    assert (
        result.stdout
        == "&session_id=session-one&session_id=mcp%23session-two"
    ), result.stdout


def test_script_session_qs_e2e_empty_when_file_missing(tmp_path) -> None:
    # When .coordination/sessions.live does not exist, SESSION_QS must
    # come out empty (not an error). This is the no-active-MCP-sessions
    # baseline path -- the hook still runs the conflict check, just with
    # only the engineer-name self-exclusion.
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this platform")

    block = _extract_session_qs_block(PRE_PUSH_SCRIPT)

    fragment = tmp_path / "fragment.sh"
    fragment.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'REPO_ROOT="{tmp_path}"\n'
        f"{block}"
        'printf "[%s]" "${SESSION_QS}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [bash, str(fragment)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "[]", result.stdout


def _git(repo: Path, *args: str) -> None:
    env = dict(os.environ)
    # Deterministic identity so commits succeed in a clean CI sandbox.
    env.update(
        {
            "GIT_AUTHOR_NAME": "coord test",
            "GIT_AUTHOR_EMAIL": "coord-test@example.com",
            "GIT_COMMITTER_NAME": "coord test",
            "GIT_COMMITTER_EMAIL": "coord-test@example.com",
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_shim_resolves_helper_from_main_root_in_linked_worktree(tmp_path) -> None:
    # Regression test for #28: the installed .git/hooks/pre-push shim must
    # resolve coord's managed helper relative to the MAIN worktree root,
    # not whichever (linked) worktree the push was launched from. A linked
    # worktree has no .coordination/ of its own, so a show-toplevel-based
    # shim used to exec a nonexistent helper and hard-block the push. The
    # git-common-dir-based shim resolves the main root instead.
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this platform")
    if not shutil.which("git"):
        pytest.skip("git not available on this platform")

    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(main_repo, "init", "-q")
    (main_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(main_repo, "add", "seed.txt")
    _git(main_repo, "commit", "-q", "-m", "seed")

    # Install the real shim (and the managed helper) into the main repo.
    _install_hook(main_repo, force=False)

    shim = main_repo / ".git" / "hooks" / "pre-push"
    assert shim.is_file()
    # The shim must no longer hard-code show-toplevel; it derives the main
    # root from the common git dir's parent.
    shim_text = shim.read_text(encoding="utf-8")
    assert "git rev-parse --git-common-dir" in shim_text
    # The shim must not actually *invoke* show-toplevel any more (a mention
    # in the explanatory comment is fine, the executable resolution is not).
    assert "rev-parse --show-toplevel)" not in shim_text

    # Replace the managed helper with a marker that reveals the path it was
    # exec'd as ($0) so we can prove which worktree's helper ran. exec in
    # the shim sets $0 to "$repo_root/.coordination/hooks/pre-push".
    helper = main_repo / ".coordination" / "hooks" / "pre-push"
    marker = (
        "#!/usr/bin/env bash\n"
        'echo "MARKER_HELPER_RAN"\n'
        'echo "$0"\n'
        "exit 0\n"
    )
    helper.write_text(marker, encoding="utf-8")
    helper.chmod(0o755)

    # Add a linked worktree and commit in it so a push from there is real.
    linked = tmp_path / "linked"
    _git(main_repo, "worktree", "add", "-q", "-b", "feature", str(linked))
    (linked / "work.txt").write_text("work\n", encoding="utf-8")
    _git(linked, "add", "work.txt")
    _git(linked, "commit", "-q", "-m", "work in linked worktree")

    # Invoke the shim with cwd = the LINKED worktree. git would run the
    # shared hook (main/.git/hooks/pre-push) from this directory on push,
    # so `git rev-parse --git-common-dir` resolves to main/.git here.
    result = subprocess.run(
        [bash, str(shim)],
        cwd=str(linked),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    # The marker helper must have actually run...
    assert "MARKER_HELPER_RAN" in result.stdout, result.stdout
    # ...and it must be the helper under the MAIN worktree root, never the
    # linked worktree root (which has no .coordination/ at all).
    # $0 is reported in the invoking shell's own path style: a native path on
    # POSIX, but an MSYS POSIX path (/c/Users/...) under Git-bash on Windows,
    # which never matches a str(Path) like C:\\...\\. Compare on a
    # forward-slash-normalised path tail relative to the worktree directory,
    # which is stable across both shells and drive-prefix styles.
    norm_stdout = result.stdout.replace("\\", "/")
    main_tail = f"/{main_repo.name}/.coordination/hooks/pre-push"
    linked_tail = f"/{linked.name}/.coordination/hooks/pre-push"
    assert main_tail in norm_stdout, result.stdout
    assert linked_tail not in norm_stdout, result.stdout


def test_shim_exits_zero_when_helper_missing(tmp_path) -> None:
    # The shim must FAIL OPEN: if coord's managed helper is absent (e.g.
    # .coordination/ was never installed in this checkout, or was removed),
    # the shim exits 0 and lets the push proceed rather than hard-blocking
    # it. Only the helper itself is fail-closed on real conflicts; the shim
    # never blocks a push just because it cannot find the helper.
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this platform")
    if not shutil.which("git"):
        pytest.skip("git not available on this platform")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")

    _install_hook(repo, force=False)

    shim = repo / ".git" / "hooks" / "pre-push"
    helper = repo / ".coordination" / "hooks" / "pre-push"
    assert shim.is_file()
    assert helper.is_file()

    # Remove the managed helper to simulate a checkout without it.
    helper.unlink()
    assert not helper.exists()

    result = subprocess.run(
        [bash, str(shim)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )

    # Push allowed: the shim resolves the (now missing) helper and exits 0
    # instead of erroring on a nonexistent exec target.
    assert result.returncode == 0, result.stderr
