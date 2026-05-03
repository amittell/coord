from __future__ import annotations

CLAUDE_SNIPPET = """## Coordination protocol (mandatory)

Multi-agent file coordination via the `coord` MCP server. Required calls:

1. `claim_files` before editing.
2. `pending_requests` periodically (someone else may be blocked on your scope; release if yes).
3. `release_session` when done.

If `claim_files` returns conflicts, stop and ask the user. No edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.

**Sub-agents (Task tool):** include this protocol in the sub-agent task text; sub-agents do not inherit `CLAUDE.md`.
"""

AGENTS_SNIPPET = """## Coordination protocol (mandatory)

Multi-agent file coordination via the `coord` MCP server. Required calls:

1. `claim_files` before editing.
2. `pending_requests` periodically (someone else may be blocked on your scope; release if yes).
3. `release_session` when done.

If `claim_files` returns conflicts, stop and ask the user. No edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.
"""

CURSOR_RULE = """---
description: Coordination protocol for shared repos
alwaysApply: false
---

Multi-agent file coordination via the `coord` MCP server. Required calls:

1. `claim_files` before editing.
2. `pending_requests` periodically (someone else may be blocked on your scope; release if yes).
3. `release_session` when done.

If `claim_files` returns conflicts, stop and ask the user.
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

while IFS= read -r file; do
  [[ -z "${file}" ]] && continue
  enc="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${file}")"
  # Fail-closed on transport errors. Pre-v0.7 used '|| true' here, which
  # made network glitches silently bypass the conflict check.
  if ! resp="$(curl -fsS \\
    ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \\
    "${COORD_URL}/conflicts?pattern=${enc}&engineer=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${ENGINEER}")${REPO_QS}")"; then
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

