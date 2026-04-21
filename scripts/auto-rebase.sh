#!/usr/bin/env bash
set -euo pipefail

# Usage: run inside a git worktree; polls upstream and rebases.
#   COORD_UPSTREAM=origin/main ./scripts/auto-rebase.sh
#
# Stop with Ctrl+C.

UPSTREAM="${COORD_UPSTREAM:-origin/main}"
SLEEP_SEC="${COORD_REBASE_INTERVAL_SEC:-1800}"

while true; do
  git fetch origin "$(echo "${UPSTREAM}" | cut -d/ -f2-)" 2>/dev/null || git fetch "${UPSTREAM%%/*}" "$(echo "${UPSTREAM}" | cut -d/ -f2-)" || true
  if git rebase "${UPSTREAM}" --autostash; then
    echo "$(date -Iseconds) rebase ok"
  else
    echo "$(date -Iseconds) REBASE CONFLICT: resolve manually in this worktree" >&2
    exit 1
  fi
  sleep "${SLEEP_SEC}"
done
