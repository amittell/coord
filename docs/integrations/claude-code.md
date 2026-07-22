# Claude Code Integration

This is the primary integration path for teams using Claude Code.

## 0. Recommended: register coord once, user-scoped

The cleanest setup registers the coord MCP server once for all your repos, so no per-repo `.mcp.json` is needed:

```bash
claude mcp add --scope user coord coord-mcp
```

`coord-mcp` resolves each repo's service URL, token, and repo id from that repo's gitignored `.coordination/local.env` at startup, so a single user-scoped server works everywhere. Because there is no tracked `.mcp.json`, coord's machine config can never be swept into a contributor PR.

You still run `coord init` per repo (below) to create `.coordination/` and patch the protocol docs; from v0.32 it gitignores `.mcp.json` rather than tracking it.

## 1. Easiest path

Inside the application repo:

```bash
coord init --tool claude --mode local --yes
coord doctor
```

That will patch `CLAUDE.md`, create `.coordination/config.toml`, create `.coordination/local.env`, write a (gitignored) `.mcp.json`, install the pre-push hook, and merge coord's enforcement hooks into `.claude/settings.json`. From v0.32, `.mcp.json` is added to the managed `.gitignore` block and any previously-committed `.mcp.json` is untracked on `coord upgrade` -- machine config stays local; only the protocol docs (`CLAUDE.md`) and `.gitignore` block are tracked.

### Enforcement hook lifecycle

Claude's hook payload normally supplies a stable `session_id`. The managed PreToolUse hook attaches that ID and the configured `COORD_REPO_ID` to auto-claims and conflict checks. Its local freshness cache is partitioned by `(session_id, repo)` and records a separate timestamp per path, so the same relative path in two repos gets two claims and claiming one path cannot make an expired sibling look live. The cache lives in a private per-user temporary directory; lock files reject symlinks and are protected by a kernel advisory lock. If a malformed or older payload has no stable ID, conflict enforcement still runs but the hook does not auto-claim: an unscoped claim could not be safely deduplicated or bulk-released.

SessionEnd calls `POST /sessions/{session_id}/release`, which atomically marks that session/repo lifecycle closed, journals follow-up cleanup, and releases its claims. Every claim-producing path—including deferred narrowed/coexist decisions—takes the same repo→session lock and rejects a closed lifecycle with `409 session_closed`. Even a fresh local cache hit calls `/sessions/{session_id}/check`; the cache can suppress duplicate claims but can never override the server's terminal state. The lifecycle is owned by the authenticated engineer that first opens or admits it; another repo-scoped principal receives 403, while an unscoped operator retains recovery authority. Relationship repair and FIFO queue grants are durable, stage-checkpointed work. Cleanup rows are leased across replicas (`SKIP LOCKED` on PostgreSQL), failures back off without starving later rows, and the background leader retries unfinished stages. SessionStart calls the idempotent `/open` endpoint before resuming auto-claims; a SessionStart without an intervening SessionEnd preserves the live per-path cache. During a rolling upgrade, a 404 from an older server's missing lifecycle endpoints preserves the legacy fail-open behavior until the server is upgraded. If the service is unavailable, hooks still fail open and claim TTL remains the cleanup backstop. The generated SessionEnd command is asynchronous with a 15-second command timeout because Claude Code otherwise allows only a short shutdown window; cleanup never delays or changes Claude's response. An upgraded, already-running v0.48 session atomically moves its private legacy claim-ID file to a drain snapshot before releasing it and repeats boundedly if an older hook recreates the source during the request.

Run `coord upgrade` after updating coord. The settings merger reconciles an existing coord SessionEnd entry in place (adding `async` and the managed timeout) while preserving unrelated Claude settings and hooks.

## 2. Install the MCP bridge locally

Each engineer needs `coord-mcp` available in their shell. From a checkout of this repo (or by installing the published package):

```bash
source .venv/bin/activate
pip install -e .
```

## 3. Add project MCP config manually

Copy `templates/.mcp.json.example` into the application repo as `.mcp.json` and leave the placeholder values (`set-me`, `http://127.0.0.1:8080`, `example-org/example-repo`) exactly as they are: the MCP wrapper recognizes them as unset and falls back to the real values in the gitignored `.coordination/local.env`. Putting real tokens or URLs into `.mcp.json` both commits a secret and shadows every engineer's per-machine `local.env` configuration.

Example:

```json
{
  "mcpServers": {
    "coord": {
      "command": "coord-mcp",
      "args": [],
      "env": {
        "COORD_API_URL": "https://coordination.internal.example",
        "COORD_AUTH_TOKEN": "replace-me"
      }
    }
  }
}
```

## 4. Add the coordination rules

Append `templates/CLAUDE.md.snippet.md` to the application repo's `CLAUDE.md`.

That snippet tells Claude Code to:

1. check claims at task start
2. stop when conflicts are returned
3. release claims when done

## 5. Use session-scoped engineer IDs

Recommended values when calling the tools:

- `alex/claude/main`
- `alex/claude/reviewer`
- `alex/claude/subagent-1`

This matters because same-engineer claims are intentionally ignored during conflict checks.

## 6. Important note for Claude sub-agents

Claude Code sub-agents may not inherit the full `CLAUDE.md` context. When you spin up a sub-agent, include the coordination protocol in the sub-agent task text or ensure the repo rules are visible to that sub-agent.

The shipped snippet already calls this out explicitly.

## 7. First verification

From Claude Code, confirm the `coord` toolset can:

1. `list_claims`
2. `claim_files`
3. `check_conflicts`
4. `release_claims`

If those work, the integration is live.

For symbol-level claims (v0.14+), pass `symbols` to `claim_files` to coexist with other agents on different declarations in the same file:

```python
claim_files(
    engineer="alex/claude/main",
    patterns=["src/auth/login.ts"],
    symbols={"src/auth/login.ts": ["handleLogin", "validateCredentials"]},
    description="auth refactor",
)
```

For a specific method on a class (v0.16+), use `Parent::child` notation:

```python
await claim_files(
    engineer="alex/claude/main",
    patterns=["src/auth/router.ts"],
    symbols={"src/auth/router.ts": ["Router::handleAuth"]},
)
```

A peer claiming a disjoint symbol set on the same file is granted automatically (`AUTO_COEXIST`) instead of hitting a `409`. See [../usage-guide.md](../usage-guide.md) for when symbol claims help and when file scope is still the right call.

As of v0.19 the TypeScript parser also walks recursively into nested class declarations, so symbol claims on `"Outer::Inner::method"` extract correctly end-to-end (the API has always accepted the notation; v0.19 makes parser-side validation match).

To see the live FIFO queue for `claim_files` waiters (v0.22+), call `my_requests(queued=True)` -- the tool forwards `?queued=true` to `GET /requests` and returns the blocking holder's engineer + pattern alongside each waiter.

To jump ahead of normal-priority waiters when the work genuinely cannot wait (v0.25+), pass `urgency` alongside `wait_seconds`: `claim_files(engineer="alex/claude/hotfix", patterns=["src/auth/login.ts"], wait_seconds=30, urgency="high", description="prod regression")`. The FIFO queue orders by priority DESC then arrival, so a `high` or `blocking` waiter jumps ahead of `normal` traffic. Default `normal` preserves strict FIFO.

To abandon a queued wait early without writing a manual HTTP call (v0.26+), call `cancel_queue_request(queue_id="q-abc123", engineer="alex/claude/main")` -- the MCP wrapper forwards a `DELETE /requests/{queue_id}` and the in-process long-poll wakes immediately.

For backpressure feedback (v0.28+), `coord-mcp` sets the `X-Coord-Engineer` request header to the current engineer id on every outbound call automatically (the `coord` CLI does the same), so coord attaches an `X-Coord-Queue-Depth` response header indicating how many queued waiters this engineer already has. No extra setup is needed; scripts calling the HTTP API directly can set the header themselves to get the same signal.

## Auth resolution order

`coord-mcp` resolves `COORD_*` env vars in this order, with the first non-placeholder value winning:

1. The MCP child process environment (shell exports, `env` block in `.mcp.json`).
2. `<repo-root>/.coordination/local.env`, auto-loaded at startup by walking up from cwd.

The placeholder values `set-me`, `example-org/example-repo`, and `http://127.0.0.1:8080` are treated as "unset" for this purpose, so even if `.mcp.json` ships placeholders the wrapper still finds the real values in the gitignored `local.env`. `coord init` is safe to run in a public repo: it writes credentials to `local.env` only. From v0.32 `.mcp.json` is gitignored (not committed), so the safest pattern is a user-scoped server (see section 0) with all real values in `local.env`.
