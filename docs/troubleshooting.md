# Troubleshooting

## `coord start` exits immediately

Cause: `COORD_AUTH_TOKEN` is missing and insecure mode is not enabled.

Fix:

```bash
coord start
```

For an explicit local-only bypass:

```bash
export COORD_ALLOW_INSECURE_NO_AUTH=true
coord-api
```

## `/claims` returns `401`

Cause: the bearer token is missing or wrong.

Fix:

```bash
curl -H "Authorization: Bearer $COORD_AUTH_TOKEN" http://127.0.0.1:8080/claims
```

## Ownership upload fails

Cause: the YAML shape is invalid.

Fixes:

- ensure the top level is a mapping
- ensure `modules` or `areas` exists
- ensure every block has a non-empty `paths` list
- use only `soft` or `hard` for `severity`

Start from `templates/.coordination/owners.example.yaml`.

## Agents still collide even though the service is running

Most likely causes:

- the agent never called `check_conflicts` or `claim_files`
- multiple workers are reusing the same `engineer` value
- the claim was too broad or too vague to match accurately

Use unique worker IDs such as `alex/claude/main` and `alex/claude/reviewer`.

## Overlap results seem weak or surprising

Cause: `COORD_REPO_ROOT` is not configured, so overlap uses heuristic matching.

Fix: if possible, point `COORD_REPO_ROOT` at a checkout of the application repo. If that is not possible, use more exact file claims.

## `coord-mcp` starts but the tools fail

Check:

1. `COORD_API_URL` points at the running API
2. `COORD_AUTH_TOKEN` matches the API token
3. `curl $COORD_API_URL/readyz` works from the same machine

## `coord doctor` fails on repo checks

Check:

1. you are inside the git repo you initialized
2. `.coordination/config.toml` exists
3. `.coordination/local.env` contains a token
4. the tool-specific file exists, such as `.mcp.json` for Claude Code
5. `CLAUDE.md` or `AGENTS.md` still contains the managed coordination block

## Stale claims remain visible

Claims expire on TTL and are also swept by a background cleanup loop. If you want them gone immediately, release them explicitly.

You can also reduce `COORD_CLEANUP_INTERVAL_SEC`.

## Docker container is unhealthy

Check:

1. the container logs for startup errors
2. `COORD_AUTH_TOKEN` is set
3. port `8080` is reachable inside the container
4. the `/data` volume is writable

## Subagents in one session block each other (v0.5.0+)

Symptom: a parent agent spawns subagents under engineer names like `codex-server-review`, `codex-render-review`, etc. They start blocking each other on overlapping patterns.

Cause: the running `coord-mcp` child is pre-v0.5 and so doesn't tag claims with a `session_id`. Without a session_id, the conflict check only self-excludes by exact engineer name, and distinct subagent names look adversarial to each other.

Fix: restart the parent Claude / Codex / Cursor process so it spawns a fresh `coord-mcp` child against your locally-installed `coord` package (verify with `coord --version` matches what the cluster reports at `/meta`). New claims will carry `session_id`, the conflict check will self-exclude across subagent names, and `pending_requests` / `release_session` / `request_release` start working.

Verify by inspecting an active claim:

```bash
curl "http://127.0.0.1:8080/claims?active_only=true" -H "Authorization: Bearer $COORD_AUTH_TOKEN" | jq '.claims[].session_id'
```

A non-null session id means the v0.5+ flow is live.

## Pre-push hook silently skips and a deploy commit goes missing

Symptom: `git push` exits zero but ArgoCD or the remote never sees your commit. Local logs show the pre-push hook ran but didn't actually check anything.

Most common causes (all closed in v0.7.x):

1. `jq` is not installed → pre-v0.7.0 hooks silently skipped. v0.7.0+ refuses with a clear message.
2. The hook chain delegates to `.coordination/hooks/pre-push` and that file is missing → "partial install" state. v0.7.1+ doctor adds an explicit `.coordination/hooks/pre-push exists` check; fix with `coord upgrade` (or `coord init --force` if `config.toml` is also missing).
3. An outer wrapper hook backgrounded the coord call (e.g. `"$@" &`), severing stdin → coord can't see git's ref-update stream. v0.7.2+ refuses loudly when stdin is redirected but empty. Wire the outer hook to forward stdin: cache `cat > $TMPFILE` once at the top, then `bash $COORD_HOOK "$@" < $TMPFILE`.

Run `coord doctor` — the v0.7.1+ `.coordination/hooks/pre-push exists` check catches the most common partial-install variant.

## `git stash -u` keeps wiping `.coordination/` files

Symptom: every few hours `.coordination/config.toml`, `owners.yaml`, and `hooks/pre-push` disappear, but `local.env` survives.

Cause (closed in v0.8.1): pre-v0.8.1 the managed `.gitignore` rule was just `.coordination/local.env` — only `local.env` was ignored. The other files were untracked-but-not-ignored, so `git stash -u` (`--include-untracked`) swept them up. A stash conflict / drop / partial-pop then lost them; only `local.env` survived because it was actually ignored.

Fix: run `coord upgrade` to migrate the `.gitignore` block to `/.coordination/` (the wider rule). v0.8.1+ ignores the entire directory; nothing under it is ever stashed.

## Holder won't release a claim and my work is blocked (v0.9.0+)

File a release request with `request_release`. The holder's TTL shortens to ~5 min and they get notified on their next `pending_requests` poll. They can approve (claim released) or deny (TTL restored). If they don't respond, the shortened TTL fires and your `claim_files` retry succeeds.

Use `urgency="blocking"` for incident work; the urgency is recorded for the operator audit even though v0.9.0 doesn't yet vary the TTL window per urgency.
