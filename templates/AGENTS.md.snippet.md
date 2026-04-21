## Coordination protocol (mandatory)

This repo uses a shared coordination service to prevent multi-agent / multi-engineer file collisions.

Use the `coord` MCP tools:

1. `list_claims` then `claim_files` at task start.
2. If conflicts are returned: stop and ask the user.
3. `release_claims` when done.

Hard rules: no edits outside claimed scope; no opportunistic refactors; shared config edits only with explicit user approval.
