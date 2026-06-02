from __future__ import annotations

CLAUDE_SNIPPET = """## Coordination protocol (mandatory)

Multi-agent file coordination via the `coord` MCP server. Required calls (every coord-mcp version):

1. `list_claims` at task start.
2. `claim_files` before editing.
3. `release_claims` when done.

If `claim_files` returns conflicts, stop and ask the user. No edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.

If your `coord` MCP exposes them, additionally prefer:
- `pending_requests` (v0.6+) between operations — see who is blocked on your scope; if a holder, approve or deny pending release requests via `respond_to_request`.
- `release_session` (v0.6+) instead of `release_claims` at end-of-work — releases every claim from this MCP session in one call, including those made by subagents under different engineer names.
- `request_release` (v0.9+) when `claim_files` 409'd and your work is urgent — files an explicit ask against the holder's claim. The holder's TTL shortens, they get notified on their next `pending_requests` poll, and the decision lands back in your `my_requests` view.
- `respond_to_request` decisions in v0.11+: in addition to `approved` (release whole claim) and `denied` (keep it), the holder can respond with `narrowed` (close the claim, open a tighter one — pass `narrowed_pattern`) or `coexist` (let the requester have a sibling claim on the same scope — pass `coexist_pattern`). `coexist` is cooperative not enforced; agents on the same file still need to handle imports and module-level state themselves.

**Sub-agents (Task tool):** include this protocol in the sub-agent task text; sub-agents do not inherit `CLAUDE.md`.
"""

AGENTS_SNIPPET = """## Coordination protocol (mandatory)

Multi-agent file coordination via the `coord` MCP server. Required calls (every coord-mcp version):

1. `list_claims` at task start.
2. `claim_files` before editing.
3. `release_claims` when done.

If `claim_files` returns conflicts, stop and ask the user. No edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.

If your `coord` MCP exposes them, additionally prefer:
- `pending_requests` (v0.6+) between operations — see who is blocked on your scope; if a holder, approve or deny pending release requests via `respond_to_request`.
- `release_session` (v0.6+) instead of `release_claims` at end-of-work — releases every claim from this MCP session in one call, including those made by subagents under different engineer names.
- `request_release` (v0.9+) when `claim_files` 409'd and your work is urgent — files an explicit ask against the holder's claim. The holder's TTL shortens, they get notified on their next `pending_requests` poll, and the decision lands back in your `my_requests` view.
- `respond_to_request` decisions in v0.11+: in addition to `approved` (release whole claim) and `denied` (keep it), the holder can respond with `narrowed` (close the claim, open a tighter one — pass `narrowed_pattern`) or `coexist` (let the requester have a sibling claim on the same scope — pass `coexist_pattern`). `coexist` is cooperative not enforced; agents on the same file still need to handle imports and module-level state themselves.
"""

CURSOR_RULE = """---
description: Coordination protocol for shared repos
alwaysApply: false
---

Multi-agent file coordination via the `coord` MCP server. Required calls (every coord-mcp version):

1. `list_claims` at task start.
2. `claim_files` before editing.
3. `release_claims` when done.

If `claim_files` returns conflicts, stop and ask the user.

If your `coord` MCP exposes them, prefer `pending_requests` between operations and `release_session` at end-of-work over `release_claims` (v0.6+). When work is urgent and a `claim_files` was blocked, file `request_release` against the holder's claim and check `my_requests` for the decision (v0.9+).
"""

PRE_PUSH_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

# Source .coordination/local.env so the hook picks up the service URL and
# token written by `coord init`, not whichever stale values happen to be
# in the pushing shell's environment. Without this, a remote-mode repo
# would silently fall back to http://127.0.0.1:8080 and quietly soft-pass
# every push.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "${REPO_ROOT}" && -f "${REPO_ROOT}/.coordination/local.env" ]]; then
  set -a
  # shellcheck disable=SC1090,SC1091
  source "${REPO_ROOT}/.coordination/local.env"
  set +a
fi

COORD_URL="${COORD_API_URL:-${COORD_SERVICE_URL:-${COORD_URL:-http://127.0.0.1:8080}}}"
COORD_URL="${COORD_URL%/}"
TOKEN="${COORD_TOKEN:-${COORD_AUTH_TOKEN:-}}"
# COORD_REPO_ID is written by `coord init` / `coord upgrade` into
# .coordination/local.env. When set, the hook scopes the conflict check
# to that repo so cross-repo path collisions don't false-positive against
# unrelated services on the same coord instance.
REPO_ID="${COORD_REPO_ID:-}"

# When the service is in COORD_ALLOW_INSECURE_NO_AUTH mode the token is
# deliberately empty. In that case we still want to run the conflict check,
# just without an Authorization header. Only skip auth -- never skip the
# check itself -- so the hook keeps protecting pushes.
# Empty-array expansion under `set -u` is unbound on bash 3.2 (the macOS
# system bash), so guard with the ${var+...} fallback whenever we expand
# CURL_AUTH below.
CURL_AUTH=()
if [[ -n "${TOKEN}" ]]; then
  CURL_AUTH=(-H "Authorization: Bearer ${TOKEN}")
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "coordination pre-push: jq not installed; refusing to push without conflict check" >&2
  echo "  install jq, or override with 'git push --no-verify' for a one-off bypass." >&2
  exit 1
fi

ENGINEER="$(git config user.name 2>/dev/null || echo unknown)"
UPSTREAM="${1:-origin}"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
ZERO_SHA="0000000000000000000000000000000000000000"
EMPTY_TREE="$(git hash-object -t tree /dev/null)"

# Determine the file set being pushed. git invokes this hook with the
# update list on stdin: one line per ref-update, with fields
# `<local_ref> <local_sha> <remote_ref> <remote_sha>`. Honouring stdin
# means we cover non-HEAD pushes, multi-ref pushes, and deleted-branch
# pushes correctly. The pre-v0.7 hook always diffed HEAD vs origin/HEAD
# regardless of what was being pushed, which silently missed those cases.
diff_names() {
  local base="$1"
  local head="$2"

  if ! git diff --name-only "${base}" "${head}"; then
    echo "coordination pre-push: git diff failed for ${base}..${head}" >&2
    return 1
  fi
}

diff_for_ref() {
  local local_ref="$1"
  local local_sha="$2"
  local remote_ref="$3"
  local remote_sha="$4"
  local base=""

  # Deleted refs have no local tree to inspect; nothing to claim against.
  if [[ -z "${local_sha}" || "${local_sha}" == "${ZERO_SHA}" ]]; then
    return 0
  fi

  # Branch already exists on the remote: diff exactly what's being added.
  if [[ -n "${remote_sha}" && "${remote_sha}" != "${ZERO_SHA}" ]]; then
    diff_names "${remote_sha}" "${local_sha}"
    return
  fi

  # First push of this branch: fall back to merge-base against the
  # default branch, or against the empty tree if no remote HEAD yet.
  # The empty tree is required for the very first push; triple-dot
  # diffs fail there because triple-dot needs a commit on each side.
  if git rev-parse "${UPSTREAM}/HEAD" >/dev/null 2>&1; then
    if ! base="$(git merge-base "${local_sha}" "${UPSTREAM}/HEAD")"; then
      echo "coordination pre-push: could not find merge base for ${local_ref} and ${UPSTREAM}/HEAD" >&2
      return 1
    fi
  else
    base="${EMPTY_TREE}"
  fi

  diff_names "${base}" "${local_sha}"
}

PUSH_INPUT=""
STDIN_IS_TTY=0
if [[ -t 0 ]]; then
  STDIN_IS_TTY=1
else
  PUSH_INPUT="$(cat || true)"
fi

# Redirected-but-empty stdin is the signature of an outer wrapper hook
# that backgrounded us (e.g. astrowars's run_child function pre-fix) or
# otherwise dropped git's pre-push ref-update stream. The pre-v0.7.2
# hook silently fell through to a HEAD-vs-origin/HEAD diff here, which
# misses non-HEAD pushes, multi-ref pushes, new-branch pushes, and
# deletions -- so we refuse rather than soft-checking the wrong file
# set. A TTY stdin means "hand-run for testing"; the fallback below
# only runs in that case.
if [[ ${STDIN_IS_TTY} -eq 0 && -z "${PUSH_INPUT//[$'\\t\\r\\n ']/}" ]]; then
  echo "coordination pre-push: stdin was redirected but empty;" >&2
  echo "  this normally means an outer wrapper hook backgrounded us or did" >&2
  echo "  not forward git's ref-update stream. Refusing rather than" >&2
  echo "  silently checking only HEAD vs ${UPSTREAM}/HEAD (would miss" >&2
  echo "  non-HEAD, new-branch, multi-ref, and deletion pushes)." >&2
  echo "  Outer-hook fix: cache stdin once into a tempfile and redirect" >&2
  echo "  the coord call from it, e.g.:" >&2
  echo "    PUSH_REFS=\\"\\$(mktemp)\\"" >&2
  echo "    [ ! -t 0 ] && cat > \\"\\$PUSH_REFS\\"" >&2
  echo "    bash \\"\\$COORD_HOOK\\" \\"\\$@\\" < \\"\\$PUSH_REFS\\"" >&2
  exit 1
fi

MODIFIED=""
if [[ -n "${PUSH_INPUT//[$'\\t\\r\\n ']/}" ]]; then
  while read -r local_ref local_sha remote_ref remote_sha; do
    [[ -z "${local_ref}" ]] && continue
    files="$(diff_for_ref "${local_ref}" "${local_sha}" "${remote_ref}" "${remote_sha}")" || exit 1
    MODIFIED+="${files}"$'\\n'
  done <<< "${PUSH_INPUT}"
else
  # Hand-run from a terminal (stdin is a TTY). git push always pipes
  # ref-updates via stdin, so this branch only triggers when a human is
  # exercising the hook manually for testing. Best-effort HEAD-based
  # fallback with a noisy heads-up so the operator knows it's not the
  # real push code-path.
  echo "coordination pre-push: hand-run mode (stdin is a TTY); falling back" >&2
  echo "  to HEAD vs ${UPSTREAM}/HEAD diff. Real pushes use the ref-update" >&2
  echo "  stream from stdin -- this path is for testing only." >&2
  if git rev-parse "${UPSTREAM}/HEAD" >/dev/null 2>&1; then
    if ! BASE="$(git merge-base HEAD "${UPSTREAM}/HEAD")"; then
      echo "coordination pre-push: could not find merge base for ${BRANCH} and ${UPSTREAM}/HEAD" >&2
      exit 1
    fi
  else
    BASE="${EMPTY_TREE}"
  fi
  MODIFIED="$(diff_names "${BASE}" HEAD)" || exit 1
fi
MODIFIED="$(printf '%s\\n' "${MODIFIED}" | sed '/^[[:space:]]*$/d' | sort -u)"
if [[ -z "${MODIFIED//[$'\\t\\r\\n ']/}" ]]; then
  exit 0
fi

REPO_QS=""
if [[ -n "${REPO_ID}" ]]; then
  REPO_QS="&repo=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${REPO_ID}")"
fi

# v0.10: hand the active coord-mcp session ids to /conflicts so the
# server can self-exclude claims that originated from this very repo's
# in-flight MCP sessions. Without this, an agent's own subagent claims
# (created under engineer names like 'codex-server-review' that don't
# match `git config user.name`) false-positive the agent's push.
# coord-mcp writes one session_id per line into .coordination/sessions.live
# on startup and removes it on graceful shutdown; missing file just means
# no live MCP sessions for this repo, in which case we fall through to
# the existing engineer-name self-exclusion.
SESSION_QS=""
if [[ -f "${REPO_ROOT}/.coordination/sessions.live" ]]; then
  coord_pid_is_live() {
    local pid="$1"
    if kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    python3 - "${pid}" <<'PY'
import os
import sys

pid = int(sys.argv[1])
if pid <= 0 or os.name != "nt":
    sys.exit(1)

try:
    import ctypes
    import ctypes.wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        sys.exit(1)
    exit_code = ctypes.wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            sys.exit(1)
        sys.exit(0 if exit_code.value == STILL_ACTIVE else 1)
    finally:
        kernel32.CloseHandle(handle)
except Exception:
    sys.exit(1)
PY
  }

  # v0.12 format per line: "<session_id> <pid> <start_time_ns>". Older
  # entries with just a session_id (no PID) are pruned by the next
  # coord-mcp startup; we also skip them here as a defense-in-depth so
  # legacy entries don't cause /conflicts to over-exclude. Any entry
  # whose PID is no longer alive is also skipped -- that's the v0.12
  # cleanup mechanism on the read side: bash's "kill -0 <pid>" is the
  # POSIX-portable existence probe (signal 0 sends nothing, only the
  # permission/existence checks fire).
  while IFS= read -r session_line || [[ -n "${session_line}" ]]; do
    case "${session_line}" in
      ''|'#'*) continue ;;
    esac
    # Split into session_id, pid, and the rest (which we don't use).
    read -r session_field pid_field _rest <<< "${session_line}"
    [[ -z "${session_field}" ]] && continue
    # Legacy entry (no PID) -> stale, skip.
    [[ -z "${pid_field}" ]] && continue
    # Numeric guard. A non-numeric "pid" is corruption; skip.
    if ! [[ "${pid_field}" =~ ^[0-9]+$ ]]; then
      continue
    fi
    # Liveness probe. POSIX shells can use kill -0 directly; Git Bash on
    # Windows cannot reliably resolve native Python PIDs, so fall back to
    # a tiny Win32 process-exit-code check there.
    if ! coord_pid_is_live "${pid_field}"; then
      continue
    fi
    enc_sid="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${session_field}")"
    SESSION_QS+="&session_id=${enc_sid}"
  done < "${REPO_ROOT}/.coordination/sessions.live"
fi

while IFS= read -r file; do
  [[ -z "${file}" ]] && continue
  enc="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${file}")"
  # Fail-closed on transport errors. Pre-v0.7 used '|| true' here, which
  # made network glitches silently bypass the conflict check.
  if ! resp="$(curl -fsS \\
    ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \\
    "${COORD_URL}/conflicts?pattern=${enc}&engineer=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${ENGINEER}")${REPO_QS}${SESSION_QS}")"; then
    echo "coordination pre-push: conflict check failed for ${file}; refusing to push" >&2
    exit 1
  fi
  if ! has="$(printf '%s' "${resp}" | jq -r '.has_conflicts // false' 2>/dev/null)"; then
    echo "coordination pre-push: invalid conflict-check response for ${file}; refusing to push" >&2
    printf '%s\\n' "${resp}" >&2
    exit 1
  fi
  if [[ "${has}" == "true" ]]; then
    echo "coordination pre-push: conflict reported for ${file}" >&2
    printf '%s\\n' "${resp}" | jq . >&2 || printf '%s\\n' "${resp}" >&2
    exit 1
  fi
  if [[ "${has}" != "false" ]]; then
    echo "coordination pre-push: unexpected conflict-check value for ${file}: ${has}" >&2
    printf '%s\\n' "${resp}" | jq . >&2 || printf '%s\\n' "${resp}" >&2
    exit 1
  fi
done <<< "${MODIFIED}"

exit 0
"""

