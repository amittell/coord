#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/worktree-add.sh <engineer_slug> [branch_name]
# Example: scripts/worktree-add.sh alice alice/current-task
#
# Requires a clean main working tree; creates ../$(basename $PWD)-<slug>

ROOT="$(git rev-parse --show-toplevel)"
SLUG="${1:?engineer slug required}"
BRANCH="${2:-"${SLUG}/work"}"
NAME="$(basename "${ROOT}")"
TARGET="$(dirname "${ROOT}")/${NAME}-${SLUG}"

if [[ -e "${TARGET}" ]]; then
  echo "Target already exists: ${TARGET}" >&2
  exit 1
fi

git fetch origin main 2>/dev/null || git fetch origin master 2>/dev/null || true

git worktree add -b "${BRANCH}" "${TARGET}" origin/main 2>/dev/null \
  || git worktree add -b "${BRANCH}" "${TARGET}" origin/master 2>/dev/null \
  || git worktree add -b "${BRANCH}" "${TARGET}" HEAD

echo "Created worktree at: ${TARGET} (branch ${BRANCH})"
