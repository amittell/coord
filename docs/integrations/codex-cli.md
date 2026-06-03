# Codex CLI Integration

Codex CLI can use the coordination bridge through MCP as a local command.

## 1. Easiest path

Inside the application repo:

```bash
coord init --tool codex --mode local --yes
coord doctor
```

That will create `.coordination/config.toml`, `.coordination/local.env`, `.coordination/owners.yaml`, `.codex/config.toml`, and `AGENTS.md`.

## 2. Install the MCP bridge locally

From a checkout of this repo (or by installing the published package):

```bash
source .venv/bin/activate
pip install -e .
```

## 3. Configure Codex manually

Copy `templates/.codex/config.toml.example` into the appropriate Codex config location and make sure the environment variables are set in the shell that launches Codex.

Recommended shell setup:

```bash
export COORD_API_URL="https://coordination.internal.example"
export COORD_AUTH_TOKEN="replace-me"
```

The example config is:

```toml
[mcp_servers.coord]
command = "coord-mcp"
args = []
enabled = true
required = false
tool_timeout_sec = 30
```

## 4. Add repo instructions

Append `templates/AGENTS.md.snippet.md` to the application repo's `AGENTS.md`.

That gives Codex a clear coordination protocol to follow.

## 5. Use a unique engineer ID per worker

Suggested IDs:

- `alex/codex/main`
- `alex/codex/fix-tests`
- `alex/codex/reviewer`

## 6. Verify tool access

Within Codex, confirm the `coord` MCP server exposes:

- `list_claims`
- `check_conflicts`
- `claim_files`
- `release_claims`

If the tools show up but fail at runtime, double-check `COORD_API_URL` and `COORD_AUTH_TOKEN` in the shell environment that starts Codex.

For symbol-level claims (v0.14+), pass `symbols` to `claim_files` so two Codex sessions can edit different declarations in the same TypeScript file without serialising:

```python
claim_files(
    engineer="alex/codex/main",
    patterns=["src/auth/login.ts"],
    symbols={"src/auth/login.ts": ["handleLogin", "validateCredentials"]},
    description="auth refactor",
)
```

For a specific method on a class (v0.16+), use `Parent::child` notation:

```python
await claim_files(
    engineer="alex/codex/main",
    patterns=["src/auth/router.ts"],
    symbols={"src/auth/router.ts": ["Router::handleAuth"]},
)
```

A peer claiming a disjoint symbol set on the same file is granted automatically (`AUTO_COEXIST`) instead of hitting a `409`. See [../usage-guide.md](../usage-guide.md) for when symbol claims help and when file scope is still the right call.

As of v0.19 the TypeScript parser also walks recursively into nested class declarations, so symbol claims on `"Outer::Inner::method"` extract correctly end-to-end (the API has always accepted the notation; v0.19 makes parser-side validation match).

To see the live FIFO queue for `claim_files` waiters (v0.22+), call `my_requests(queued=True)` -- the tool forwards `?queued=true` to `GET /requests` and returns the blocking holder's engineer + pattern alongside each waiter.

To jump ahead of normal-priority waiters when the work genuinely cannot wait (v0.25+), pass `urgency` alongside `wait_seconds`: `claim_files(engineer="alex/codex/hotfix", patterns=["src/auth/login.ts"], wait_seconds=30, urgency="high", description="prod regression")`. The FIFO queue orders by priority DESC then arrival, so a `high` or `blocking` waiter jumps ahead of `normal` traffic. Default `normal` preserves strict FIFO.

To abandon a queued wait early without writing a manual HTTP call (v0.26+), call `cancel_queue_request(queue_id="q-abc123", engineer="alex/codex/main")` -- the MCP wrapper forwards a `DELETE /requests/{queue_id}` and the in-process long-poll wakes immediately.

## Auth resolution order

`coord-mcp` resolves `COORD_*` env vars in this order, with the first non-placeholder value winning:

1. The MCP child process environment (shell exports, `[mcp_servers.coord.env]` block in Codex config, `env` block in `.mcp.json`).
2. `<repo-root>/.coordination/local.env`, auto-loaded at startup by walking up from cwd.

The placeholder values `set-me`, `example-org/example-repo`, and `http://127.0.0.1:8080` are treated as "unset" for this purpose, so a committed `.mcp.json` template can ship them harmlessly and the wrapper still finds the real values in the gitignored `local.env`. This is why you can leave your Codex config minimal (just `command = "coord-mcp"`) and have `coord init` manage credentials via `local.env`.
