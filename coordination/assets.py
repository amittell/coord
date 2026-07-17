from __future__ import annotations

CLAUDE_SNIPPET = """## Coordination protocol (mandatory)

Multi-agent file coordination via the `coord` MCP server. Required calls every session:

1. `list_claims` at task start.
2. `claim_files` before editing.
3. `release_claims` (or `release_session` to drop everything this MCP session holds, v0.6+) when done.

If `claim_files` returns conflicts, stop and ask the user. No edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.

### Repo-scoped tokens (v0.42+)

On a shared coord service that fronts several repos, use a **repo-scoped token** so you only ever see and touch your own repo's claims. The server enforces the scope from the token, so `list_claims` / `check_conflicts` return just your repo no matter what the client sends.

If a tool result carries a `coord_notice` -- or `coord status` prints a `Token warning:` line -- your token is **unscoped** and deprecated: on a shared service it sees and can affect every repo's claims. It is still honored for now, but switch. Ask an operator for a scoped token and drop it in `.coordination/local.env`:

- `coord tokens create <engineer> --repo <owner/name>`

Operator-wide reads are opt-in with `all_repos=True` (a scoped token gets a 403, not a silent all-repo view). Rollout detail: `docs/deployment.md`.

### Sub-file (symbol-level) claims (v0.14+)

When two agents need different parts of the same file, claim symbols instead of whole files. Pass `symbols={"src/auth/login.ts": ["handleLogin"]}` to `claim_files` so the claim scopes to just that function. Two agents on disjoint symbols of the same file auto-coexist with no 409.

- `Foo::handleA` (v0.16): method-level notation. Sibling methods of the same class auto-coexist; the bare class blocks all of its methods.
- `Outer::Inner::method` (v0.17): recursive nesting works to any depth.
- Server-side validation (v0.17): when `COORD_REPO_ROOT` is set, the server rejects symbols that do not exist in the file with a hint listing what is parseable. The MCP wrapper pre-validates locally before POST so typos fail fast; disable with `COORD_DISABLE_CLIENT_VALIDATION=1`.

### Queueing instead of bouncing (v0.21+)

When a hot file is contested and your work is blocking the team, pass `wait_seconds=60` to `claim_files`. The request joins a FIFO queue behind the blocking holder; on release the next queued requester is auto-granted. Pair with `urgency='low' | 'normal' | 'high' | 'blocking'` (v0.25) to jump ahead of lower-priority waiters; long-waiting entries age-boost one level after `COORD_QUEUE_AGE_BOOST_SECONDS` (v0.26). Abandon a wait early via `cancel_queue_request(queue_id, engineer=...)` (v0.26). Inspect your own queue rows via `my_requests(queued=True)` (v0.22).

### Asking the holder directly (v0.6+ / v0.11+ decisions)

If queueing will not work either:
- `request_release` files an explicit ask against the holder's claim. The holder's TTL shortens; their decision lands back in your `my_requests` view.
- `respond_to_request` decisions (v0.11+): `approved` (release whole claim), `denied` (keep it), `narrowed` (close the claim, open a tighter one -- pass `narrowed_pattern`), `coexist` (let the requester have a sibling claim on the same scope -- pass `coexist_pattern`). `coexist` is cooperative not enforced; agents on the same file still handle imports and module-level state themselves.

### Operator visibility

Poll `pending_requests` between operations to see who is blocked on your scope and respond via `respond_to_request`. The dashboard surfaces hotspot files, an auto-resolution heatmap, and a pending-queue panel for ambient awareness.

**Sub-agents (Task tool):** include this protocol in the sub-agent task text; sub-agents do not inherit `CLAUDE.md`.
"""

AGENTS_SNIPPET = """## Coordination protocol (mandatory)

Multi-agent file coordination via the `coord` MCP server. Required calls every session:

1. `list_claims` at task start.
2. `claim_files` before editing.
3. `release_claims` (or `release_session` to drop everything this MCP session holds, v0.6+) when done.

If `claim_files` returns conflicts, stop and ask the user. No edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.

### Repo-scoped tokens (v0.42+)

On a shared coord service that fronts several repos, use a **repo-scoped token** so you only ever see and touch your own repo's claims. The server enforces the scope from the token, so `list_claims` / `check_conflicts` return just your repo no matter what the client sends.

If a tool result carries a `coord_notice` -- or `coord status` prints a `Token warning:` line -- your token is **unscoped** and deprecated: on a shared service it sees and can affect every repo's claims. It is still honored for now, but switch. Ask an operator for a scoped token and drop it in `.coordination/local.env`:

- `coord tokens create <engineer> --repo <owner/name>`

Operator-wide reads are opt-in with `all_repos=True` (a scoped token gets a 403, not a silent all-repo view). Rollout detail: `docs/deployment.md`.

### Sub-file (symbol-level) claims (v0.14+)

When two agents need different parts of the same file, claim symbols instead of whole files. Pass `symbols={"src/auth/login.ts": ["handleLogin"]}` to `claim_files` so the claim scopes to just that function. Two agents on disjoint symbols of the same file auto-coexist with no 409.

- `Foo::handleA` (v0.16): method-level notation. Sibling methods of the same class auto-coexist; the bare class blocks all of its methods.
- `Outer::Inner::method` (v0.17): recursive nesting works to any depth.
- Server-side validation (v0.17): when `COORD_REPO_ROOT` is set, the server rejects symbols that do not exist in the file with a hint listing what is parseable. The MCP wrapper pre-validates locally before POST so typos fail fast; disable with `COORD_DISABLE_CLIENT_VALIDATION=1`.

### Queueing instead of bouncing (v0.21+)

When a hot file is contested and your work is blocking the team, pass `wait_seconds=60` to `claim_files`. The request joins a FIFO queue behind the blocking holder; on release the next queued requester is auto-granted. Pair with `urgency='low' | 'normal' | 'high' | 'blocking'` (v0.25) to jump ahead of lower-priority waiters; long-waiting entries age-boost one level after `COORD_QUEUE_AGE_BOOST_SECONDS` (v0.26). Abandon a wait early via `cancel_queue_request(queue_id, engineer=...)` (v0.26). Inspect your own queue rows via `my_requests(queued=True)` (v0.22).

### Asking the holder directly (v0.6+ / v0.11+ decisions)

If queueing will not work either:
- `request_release` files an explicit ask against the holder's claim. The holder's TTL shortens; their decision lands back in your `my_requests` view.
- `respond_to_request` decisions (v0.11+): `approved` (release whole claim), `denied` (keep it), `narrowed` (close the claim, open a tighter one -- pass `narrowed_pattern`), `coexist` (let the requester have a sibling claim on the same scope -- pass `coexist_pattern`). `coexist` is cooperative not enforced; agents on the same file still handle imports and module-level state themselves.

### Operator visibility

Poll `pending_requests` between operations to see who is blocked on your scope and respond via `respond_to_request`. The dashboard surfaces hotspot files, an auto-resolution heatmap, and a pending-queue panel for ambient awareness.
"""

CURSOR_RULE = """---
description: Coordination protocol for shared repos
alwaysApply: false
---

Multi-agent file coordination via the `coord` MCP server. Required calls every session:

1. `list_claims` at task start.
2. `claim_files` before editing.
3. `release_claims` (or `release_session` to drop everything this MCP session holds, v0.6+) when done.

If `claim_files` returns conflicts, stop and ask the user.

Use `symbols={"path.ts": ["handleLogin"]}` on `claim_files` for sub-file scope (v0.14+, supports method/nested notation via `Foo::handleA` and `Outer::Inner::m`). Pass `wait_seconds=60` to queue instead of bouncing (v0.21+, optional `urgency='high'`). Use `request_release` when blocked and urgent (v0.9+); the holder can respond `approved | denied | narrowed | coexist` (v0.11+).
"""

PRE_PUSH_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail

# Read .coordination/local.env as inert data so the hook picks up the service
# URL and token written by `coord init` without executing operator-provided
# shell syntax. The parser intentionally mirrors coordination.envfile:
# whitespace/export prefixes are tolerated, one layer of matching outer
# quotes is removed, and the last assignment wins. Only keys this hook uses
# are imported; every other line remains inert.
load_coord_local_env() {
  local env_file="$1"
  local coord_env_records=""
  local coord_env_record=""
  local coord_env_key=""
  local coord_env_value=""

  if ! coord_env_records="$(python3 - "${env_file}" <<'PY'
import sys
from pathlib import Path

wanted = (
    "COORD_API_URL",
    "COORD_SERVICE_URL",
    "COORD_URL",
    "COORD_TOKEN",
    "COORD_AUTH_TOKEN",
    "COORD_REPO_ID",
    "COORD_PUSH_BASE_REF",
)

try:
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
except (OSError, UnicodeError) as exc:
    print(f"unable to read local.env: {exc}", file=sys.stderr)
    raise SystemExit(1)

values = {}
for raw in text.splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    key, sep, value = line.partition("=")
    if sep != "=":
        continue
    key = key.strip()
    if key not in wanted:
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if "\\x00" in value:
        print(f"NUL byte in {key}", file=sys.stderr)
        raise SystemExit(1)
    values[key] = value

records = "".join(
    f"{key}={values[key]}\\n" for key in wanted if key in values
)
# Binary stdout prevents Windows Python from translating LF to CRLF. The Git
# Bash read builtin deliberately preserves a trailing CR, which would otherwise
# become part of each imported URL/token/repo value.
sys.stdout.buffer.write(records.encode("utf-8"))
PY
)"; then
    echo "coordination pre-push: could not parse ${env_file}; refusing to push" >&2
    return 1
  fi

  # Do not eval or source these records. Quoted assignment keeps spaces,
  # semicolons, dollar signs, and command-substitution text literal on bash
  # 3.2 and newer.
  while IFS= read -r coord_env_record || [[ -n "${coord_env_record}" ]]; do
    [[ -z "${coord_env_record}" ]] && continue
    coord_env_key="${coord_env_record%%=*}"
    coord_env_value="${coord_env_record#*=}"
    case "${coord_env_key}" in
      COORD_API_URL) COORD_API_URL="${coord_env_value}" ;;
      COORD_SERVICE_URL) COORD_SERVICE_URL="${coord_env_value}" ;;
      COORD_URL) COORD_URL="${coord_env_value}" ;;
      COORD_TOKEN) COORD_TOKEN="${coord_env_value}" ;;
      COORD_AUTH_TOKEN) COORD_AUTH_TOKEN="${coord_env_value}" ;;
      COORD_REPO_ID) COORD_REPO_ID="${coord_env_value}" ;;
      COORD_PUSH_BASE_REF) COORD_PUSH_BASE_REF="${coord_env_value}" ;;
    esac
  done <<< "${coord_env_records}"
}

# Resolve the MAIN worktree root via the common git dir's parent so the
# hook still finds .coordination/local.env when invoked from a linked git
# worktree (where --show-toplevel points at the linked root, which has no
# .coordination/). This stays fail-soft: a missing local.env just means we
# fall back to env vars and defaults below. The conflict check further
# down remains fail-closed on real conflicts.
COORD_COMMON_DIR="$(git rev-parse --git-common-dir 2>/dev/null || true)"
REPO_ROOT=""
if [[ -n "${COORD_COMMON_DIR}" ]]; then
  REPO_ROOT="$(cd "${COORD_COMMON_DIR}/.." 2>/dev/null && pwd || true)"
fi
if [[ -n "${REPO_ROOT}" && -f "${REPO_ROOT}/.coordination/local.env" ]]; then
  if ! load_coord_local_env "${REPO_ROOT}/.coordination/local.env"; then
    exit 1
  fi
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
# The Authorization header travels via a private curl config file (mktemp
# creates it 0600), never on the curl command line: argv is world-readable
# through `ps` for the duration of each request -- and the hook runs one
# curl per pushed file -- so a bearer token passed as
# `-H "Authorization: Bearer ..."` could be harvested by any local user on
# a shared host. The token reaches python3 via the environment (also not
# visible to other users), not argv, for the same reason.
CURL_AUTH=()
CURL_AUTH_CFG=""
if [[ -n "${TOKEN}" ]]; then
  CURL_AUTH_CFG="$(mktemp "${TMPDIR:-/tmp}/coord-pre-push.XXXXXX")"
  chmod 600 "${CURL_AUTH_CFG}"
  trap 'rm -f "${CURL_AUTH_CFG}"' EXIT
  COORD_HOOK_TOKEN="${TOKEN}" python3 - > "${CURL_AUTH_CFG}" <<'PY'
import os

token = os.environ["COORD_HOOK_TOKEN"]
# curl config quoting: escape backslash and double quote so a pathological
# token cannot break out of the quoted parameter value.
token = token.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"')
print('header = "Authorization: Bearer %s"' % token)
PY
  CURL_AUTH=(--config "${CURL_AUTH_CFG}")
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "coordination pre-push: jq not installed; refusing to push without conflict check" >&2
  echo "  install jq, or override with 'git push --no-verify' for a one-off bypass." >&2
  exit 1
fi

# v20: the claims-and-conflicts identity is the TOKEN's engineer, not the git
# committer name -- the two diverging is how a pusher's own claims read as
# someone else's and block the push (found live 2026-07-17). Ask the service
# who the token belongs to; fall back to git config only when unauthenticated
# or the server predates /whoami.
ENGINEER=""
if [ -n "${COORD_TOKEN:-}" ]; then
  ENGINEER="$(curl -fsS --max-time 3 -H "Authorization: Bearer ${COORD_TOKEN}" \
    "${COORD_URL}/whoami" 2>/dev/null | jq -r '.engineer // empty' 2>/dev/null || true)"
fi
if [ -z "${ENGINEER}" ]; then
  ENGINEER="$(git config user.name 2>/dev/null || echo unknown)"
fi
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

new_branch_base_ref() {
  local local_ref="$1"
  local short_ref="${local_ref#refs/heads/}"
  local configured="${COORD_PUSH_BASE_REF:-}"
  local merge_ref=""
  local merge_remote=""
  local inferred=""

  # Explicit override wins. It may be supplied by .coordination/local.env
  # for integration-branch repos, or per branch in git config:
  #   git config branch.<name>.coordPushBase origin/pre-prod
  if [[ -z "${configured}" && "${short_ref}" != "${local_ref}" ]]; then
    configured="$(git config --get "branch.${short_ref}.coordPushBase" 2>/dev/null || true)"
  fi
  if [[ -n "${configured}" ]]; then
    if ! git rev-parse "${configured}" >/dev/null 2>&1; then
      echo "coordination pre-push: configured push base does not resolve: ${configured}" >&2
      return 1
    fi
    printf '%s\\n' "${configured}"
    return 0
  fi

  # A preconfigured tracking base is the next-best signal. This is useful
  # when a new topic branch intentionally tracks a long-lived integration
  # branch. Ignore an inferred ref that is not present locally and continue
  # to the backwards-compatible remote-HEAD fallback.
  if [[ "${short_ref}" != "${local_ref}" ]]; then
    merge_ref="$(git config --get "branch.${short_ref}.merge" 2>/dev/null || true)"
    merge_remote="$(git config --get "branch.${short_ref}.remote" 2>/dev/null || true)"
    if [[ -n "${merge_ref}" ]]; then
      if [[ -z "${merge_remote}" || "${merge_remote}" == "." ]]; then
        inferred="${merge_ref#refs/heads/}"
      else
        inferred="${merge_remote}/${merge_ref#refs/heads/}"
      fi
      if git rev-parse "${inferred}" >/dev/null 2>&1; then
        printf '%s\\n' "${inferred}"
        return 0
      fi
    fi
  fi

  printf '%s\\n' "${UPSTREAM}/HEAD"
}

diff_for_ref() {
  local local_ref="$1"
  local local_sha="$2"
  local remote_ref="$3"
  local remote_sha="$4"
  local base=""
  local base_ref=""

  # Deleted refs have no local tree to inspect; nothing to claim against.
  if [[ -z "${local_sha}" || "${local_sha}" == "${ZERO_SHA}" ]]; then
    return 0
  fi

  # Branch already exists on the remote: diff exactly what's being added.
  if [[ -n "${remote_sha}" && "${remote_sha}" != "${ZERO_SHA}" ]]; then
    diff_names "${remote_sha}" "${local_sha}"
    return
  fi

  # First push of this branch: prefer an explicit/configured integration
  # base, then a resolvable tracking base, and only then remote HEAD. This
  # prevents a one-file branch cut from pre-prod from checking the entire
  # pre-prod-vs-main delta (#68). Fall back to the empty tree if no remote
  # reference exists yet.
  # The empty tree is required for the very first push; triple-dot
  # diffs fail there because triple-dot needs a commit on each side.
  if ! base_ref="$(new_branch_base_ref "${local_ref}")"; then
    return 1
  fi
  if git rev-parse "${base_ref}" >/dev/null 2>&1; then
    if ! base="$(git merge-base "${local_sha}" "${base_ref}")"; then
      echo "coordination pre-push: could not find merge base for ${local_ref} and ${base_ref}" >&2
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

# v0.34: forward the pushing branch to /conflicts so a bounced push can
# be surfaced as a comment on the corresponding open PR. BRANCH is the
# current ref (resolved above); only sent when non-empty so a detached
# HEAD (BRANCH="HEAD") still leaves a clean URL without a literal
# trailing "&branch=".
BRANCH_QS=""
if [[ -n "${BRANCH}" && "${BRANCH}" != "HEAD" ]]; then
  BRANCH_QS="&branch=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${BRANCH}")"
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
SESSION_IDS=""
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
if pid <= 0:
    sys.exit(1)

if os.name != "nt":
    # The shell's `kill -0` already returned success for any process this
    # user may signal; landing here means it failed, which conflates ESRCH
    # (no such process) with EPERM (alive but owned by another user).
    # Re-probe and treat EPERM as live so a coord-mcp session started
    # under a different UID (service account, containerized agent sharing
    # the worktree) keeps its /conflicts self-exclusion instead of being
    # pruned as dead -- pruning it would false-positive the push against
    # the agent's own claims.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        sys.exit(1)
    except PermissionError:
        sys.exit(0)
    except OSError:
        sys.exit(1)
    sys.exit(0)

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
    # Liveness probe. `kill -0` answers directly for processes this user
    # may signal; the python3 fallback distinguishes EPERM (alive, owned
    # by another user -> live) from ESRCH (gone -> dead) on POSIX, and on
    # Git Bash for Windows -- which cannot reliably resolve native Python
    # PIDs -- uses a tiny Win32 process-exit-code check instead.
    if ! coord_pid_is_live "${pid_field}"; then
      continue
    fi
    SESSION_IDS+="${session_field}"$'\\n'
    enc_sid="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${session_field}")"
    SESSION_QS+="&session_id=${enc_sid}"
  done < "${REPO_ROOT}/.coordination/sessions.live"
fi

validate_conflict_response() {
  local resp="$1"
  local context="$2"
  local has=""
  if ! has="$(printf '%s' "${resp}" | jq -r \\
    'if (.has_conflicts | type) == "boolean" then .has_conflicts else error("missing boolean has_conflicts") end' \\
    2>/dev/null)"; then
    echo "coordination pre-push: invalid conflict-check response for ${context}; refusing to push" >&2
    printf '%s\\n' "${resp}" >&2
    return 1
  fi
  if [[ "${has}" == "true" ]]; then
    echo "coordination pre-push: conflict reported for ${context}" >&2
    printf '%s\\n' "${resp}" | jq . >&2 || printf '%s\\n' "${resp}" >&2
    return 2
  fi
  if [[ "${has}" != "false" ]]; then
    echo "coordination pre-push: unexpected conflict-check value for ${context}: ${has}" >&2
    printf '%s\\n' "${resp}" | jq . >&2 || printf '%s\\n' "${resp}" >&2
    return 1
  fi
  return 0
}

legacy_conflict_check() {
  local file=""
  local enc=""
  local resp=""
  while IFS= read -r file; do
    [[ -z "${file}" ]] && continue
    enc="$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${file}")"
    # Fail-closed on transport errors. Pre-v0.7 used '|| true' here, which
    # made network glitches silently bypass the conflict check.
    if ! resp="$(curl -fsS \\
      ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \\
      "${COORD_URL}/conflicts?pattern=${enc}&engineer=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "${ENGINEER}")${REPO_QS}${SESSION_QS}${BRANCH_QS}")"; then
      echo "coordination pre-push: conflict check failed for ${file}; refusing to push" >&2
      return 1
    fi
    validate_conflict_response "${resp}" "${file}" || return $?
  done <<< "${MODIFIED}"
}

# Modern servers accept the complete push as JSON, cutting a 300-file push
# from 300 HTTP round-trips (plus per-file interpreter spawns) to one (#67).
# A 404/405 means an older coord server; only that case falls back to the
# legacy per-file GET contract. Every other error remains fail-closed.
PATTERNS_JSON="$(printf '%s\\n' "${MODIFIED}" | jq -Rsc 'split("\\n") | map(select(length > 0))')"
SESSION_IDS_JSON="$(printf '%s' "${SESSION_IDS}" | jq -Rsc 'split("\\n") | map(select(length > 0))')"
BATCH_PAYLOAD="$(jq -cn \\
  --argjson patterns "${PATTERNS_JSON}" \\
  --arg engineer "${ENGINEER}" \\
  --arg repo "${REPO_ID}" \\
  --arg branch "${BRANCH}" \\
  --argjson session_ids "${SESSION_IDS_JSON}" \\
  '{patterns: $patterns, engineer: $engineer, session_ids: $session_ids}
   + (if $repo == "" then {} else {repo: $repo} end)
   + (if $branch == "" or $branch == "HEAD" then {} else {branch: $branch} end)')"

if ! batch_raw="$(printf '%s' "${BATCH_PAYLOAD}" | curl -sS \\
  ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \\
  -H "Content-Type: application/json" \\
  --data-binary @- \\
  -w $'\\n%{http_code}' \\
  "${COORD_URL}/conflicts/batch")"; then
  echo "coordination pre-push: batch conflict check failed; refusing to push" >&2
  exit 1
fi

batch_status="${batch_raw##*$'\\n'}"
batch_resp="${batch_raw%$'\\n'*}"
if [[ "${batch_status}" == "404" || "${batch_status}" == "405" ]]; then
  legacy_conflict_check || exit $?
elif [[ "${batch_status}" == 2* ]]; then
  validate_conflict_response "${batch_resp}" "pushed file batch" || exit $?
else
  echo "coordination pre-push: batch conflict check returned HTTP ${batch_status}; refusing to push" >&2
  printf '%s\\n' "${batch_resp}" >&2
  exit 1
fi

exit 0
"""

