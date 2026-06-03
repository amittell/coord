# Claude Code Integration

This is the primary integration path for teams using Claude Code.

## 1. Easiest path

Inside the application repo:

```bash
coord init --tool claude --mode local --yes
coord doctor
```

That will create `.mcp.json`, patch `CLAUDE.md`, create `.coordination/config.toml`, create `.coordination/local.env`, and install the pre-push hook.

## 2. Install the MCP bridge locally

Each engineer needs `coord-mcp` available in their shell. From a checkout of this repo (or by installing the published package):

```bash
source .venv/bin/activate
pip install -e .
```

## 3. Add project MCP config manually

Copy `templates/.mcp.json.example` into the application repo as `.mcp.json`, then fill in the service URL and token.

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

## Auth resolution order

`coord-mcp` resolves `COORD_*` env vars in this order, with the first non-placeholder value winning:

1. The MCP child process environment (shell exports, `env` block in `.mcp.json`).
2. `<repo-root>/.coordination/local.env`, auto-loaded at startup by walking up from cwd.

The placeholder values `set-me`, `example-org/example-repo`, and `http://127.0.0.1:8080` are treated as "unset" for this purpose, so a committed `.mcp.json` template can ship them harmlessly and the wrapper still finds the real values in the gitignored `local.env`. This is why `coord init` is safe to run in a public repo: it writes credentials to `local.env` only and leaves `.mcp.json` as a template.
