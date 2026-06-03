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

## MCP tools return `401` from a known-good service

Symptom: `coord` MCP tools (e.g. `list_claims`) return `401` even though `curl -H "Authorization: Bearer $COORD_AUTH_TOKEN" $COORD_API_URL/claims` from the same shell returns `200`.

Cause: the MCP child process (`coord-mcp`) was spawned with placeholder env values from a sanitised `.mcp.json` (`COORD_AUTH_TOKEN=set-me`, `COORD_API_URL=http://127.0.0.1:8080`) and `.coordination/local.env` either does not exist or also carries the placeholder. The wrapper treats `set-me`, `example-org/example-repo`, and `http://127.0.0.1:8080` as "unset" for the purpose of overriding from `local.env` and for the `Authorization` header, so a setup with placeholders everywhere reaches the service with no `Authorization` at all.

Confirm by simulating the loader against the affected repo:

```bash
cd /path/to/your-app
COORD_AUTH_TOKEN=set-me python -c "
import os
from coordination.mcp_server import _load_local_env, _headers, _base_url
print('loaded:', _load_local_env())
print('url:   ', _base_url())
print('auth:  ', _headers().get('Authorization', '<absent>')[:18])
"
```

If `loaded:` is `None`, there is no `.coordination/local.env` for the wrapper to find. Run `coord init` to generate one, then drop the real token in:

```bash
coord init --tool claude --mode local --yes
$EDITOR .coordination/local.env       # replace COORD_AUTH_TOKEN=set-me
```

If `loaded:` points at a real file but `auth:` is `<absent>`, the file carries the placeholder. Edit the same file and replace `COORD_AUTH_TOKEN=set-me` with the real token (`coord start` prints it; for a remote service ask the operator).

Restart the editor/CLI so the next `coord-mcp` child picks up the change. The committed `.mcp.json` does not need to be edited; placeholders are the right value for that file.

## MCP wrapper picks up the wrong service URL

Symptom: MCP tools connect to `http://127.0.0.1:8080` even though the team's `COORD_API_URL` is the cluster URL.

Cause: same as above. `.mcp.json` carries the placeholder `http://127.0.0.1:8080`, no `.coordination/local.env` exists, and no shell export overrides. The wrapper falls through to the built-in default.

Fix: run `coord init --mode remote --service-url <real-url>` or hand-edit `.coordination/local.env` to set `COORD_API_URL` and restart the editor.

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

Run `coord doctor` -- the v0.7.1+ `.coordination/hooks/pre-push exists` check catches the most common partial-install variant.

## `git stash -u` keeps wiping `.coordination/` files

Symptom: every few hours `.coordination/config.toml`, `owners.yaml`, and `hooks/pre-push` disappear, but `local.env` survives.

Cause (closed in v0.8.1): pre-v0.8.1 the managed `.gitignore` rule was just `.coordination/local.env` -- only `local.env` was ignored. The other files were untracked-but-not-ignored, so `git stash -u` (`--include-untracked`) swept them up. A stash conflict / drop / partial-pop then lost them; only `local.env` survived because it was actually ignored.

Fix: run `coord upgrade` to migrate the `.gitignore` block to `/.coordination/` (the wider rule). v0.8.1+ ignores the entire directory; nothing under it is ever stashed.

## Holder won't release a claim and my work is blocked (v0.9.0+)

File a release request with `request_release`. The holder's TTL shortens to ~5 min and they get notified on their next `pending_requests` poll. They can approve (claim released), deny (TTL restored), narrow (close + reopen on a tighter pattern, v0.11+), or coexist (sibling claim on the same scope, v0.11+). If they don't respond, the shortened TTL fires and your `claim_files` retry succeeds.

Use `urgency="blocking"` for incident work; the urgency is recorded for the operator audit even though it doesn't yet vary the TTL window per urgency.

Pass `requested_scope` (v0.11+) to tell the holder what you actually need -- often a sub-pattern of their claim. The holder uses it to decide between `approved` (release everything) and the narrower options.

## My push silently fails because my own subagent's claims look like conflicts (v0.10.0+)

Symptom: pre-v0.10 hooks passed only `git config user.name` as the engineer to `/conflicts`, so an agent's own subagent claims (under names like `codex-server-review` that don't match git's user) showed up as adversarial conflicts on the agent's own push. The agent had to pre-release defensively before pushing.

Fix in v0.10.0: `coord-mcp` writes its `session_id` to `<repo_root>/.coordination/sessions.live` on startup. The pre-push hook reads every line and forwards each as `&session_id=` to `/conflicts`, which now self-excludes claims matching any of them. Once you've upgraded coord and restarted the parent agent (Claude Code / Codex / Cursor), the hook stops false-positiving. Verify by checking `cat .coordination/sessions.live` -- a fresh hex id means the new MCP child registered itself.

## "Transport closed" error from coord MCP tool (v0.11.0 fix)

Symptom: an agent's tool call to coord (e.g. `coord.release_session`) fails with `tool call error ... Caused by: Transport closed`, and subsequent calls keep failing; the parent (Codex / Claude Code) has a stale stdio handle to a dead MCP child.

Cause (v0.10.0 only): the v0.10 implementation installed custom SIGTERM/SIGINT handlers that re-raised the signal under `SIG_DFL` after marker cleanup. That fought with FastMCP's own signal handling: any signal (including transient ones from parent watchdogs) aborted the MCP child before its stdio loop could drain.

Fix in v0.11.0: drops the explicit signal handlers entirely. Marker cleanup runs from `atexit` only, which fires for both clean exits and signal-driven shutdowns through the interpreter's normal path. FastMCP keeps full ownership of signal disposition.

If you're seeing this on a v0.10.0 client: upgrade to v0.11.0+ and restart the parent agent. As a one-off recovery, fall back to the `coord` CLI (`coord release ...`) which doesn't go through the MCP child.
