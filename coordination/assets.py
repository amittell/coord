from __future__ import annotations

CLAUDE_SNIPPET = """## Coordination protocol (mandatory)

This repo uses a shared coordination service to prevent multi-agent / multi-engineer file collisions.

Use the `coord` MCP tools:

1. `list_claims` then `claim_files` at task start.
2. If conflicts are returned: stop and ask the user.
3. `release_claims` when done.

Hard rules: no edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.

**Claude Code sub-agents:** when using the Task tool, include the protocol above in the sub-agent task text (sub-agents may not inherit full `CLAUDE.md` context).
"""

AGENTS_SNIPPET = """## Coordination protocol (mandatory)

This repo uses a shared coordination service to prevent multi-agent / multi-engineer file collisions.

Use the `coord` MCP tools:

1. `list_claims` then `claim_files` at task start.
2. If conflicts are returned: stop and ask the user.
3. `release_claims` when done.

Hard rules: no edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.
"""

CURSOR_RULE = """---
description: Coordination protocol for shared repos
alwaysApply: false
---

This repo uses the `coord` MCP server for multi-agent coordination.

Before edits:
1. Use `list_claims` or `check_conflicts`.
2. Use `claim_files` before changing files.
3. If conflicts are returned, stop and ask the user.
4. Use `release_claims` when done.
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

# When the service is in COORD_ALLOW_INSECURE_NO_AUTH mode the token is
# deliberately empty. In that case we still want to run the conflict check,
# just without an Authorization header. Only skip auth -- never skip the
# check itself -- so the hook keeps protecting pushes.
CURL_AUTH=()
if [[ -n "${TOKEN}" ]]; then
  CURL_AUTH=(-H "Authorization: Bearer ${TOKEN}")
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "coordination pre-push: jq not installed; skipping" >&2
  exit 0
fi

ENGINEER="$(git config user.name 2>/dev/null || echo unknown)"
UPSTREAM="${1:-origin}"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"

if git rev-parse "${UPSTREAM}/HEAD" >/dev/null 2>&1; then
  BASE="$(git merge-base HEAD "${UPSTREAM}/HEAD")"
else
  BASE="$(git hash-object -t tree /dev/null 2>/dev/null || true)"
  if [[ -z "${BASE}" ]]; then
    echo "coordination pre-push: could not determine diff base; skipping" >&2
    exit 0
  fi
fi

MODIFIED="$(git diff --name-only "${BASE}"...HEAD || true)"
if [[ -z "${MODIFIED//[$'\\t\\r\\n ']/}" ]]; then
  exit 0
fi

while IFS= read -r file; do
  [[ -z "${file}" ]] && continue
  enc="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${file}")"
  resp="$(curl -fsS \\
    "${CURL_AUTH[@]}" \\
    "${COORD_URL}/conflicts?pattern=${enc}&engineer=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${ENGINEER}")" \\
    || true)"
  has="$(printf '%s' "${resp}" | jq -r '.has_conflicts // empty' 2>/dev/null || true)"
  if [[ "${has}" == "true" ]]; then
    echo "coordination pre-push: conflict reported for ${file}" >&2
    printf '%s\\n' "${resp}" | jq . >&2 || printf '%s\\n' "${resp}" >&2
    exit 1
  fi
done <<< "${MODIFIED}"

exit 0
"""

