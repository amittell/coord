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

## Auth resolution order

`coord-mcp` resolves `COORD_*` env vars in this order, with the first non-placeholder value winning:

1. The MCP child process environment (shell exports, `env` block in `.mcp.json`).
2. `<repo-root>/.coordination/local.env`, auto-loaded at startup by walking up from cwd.

The placeholder values `set-me`, `example-org/example-repo`, and `http://127.0.0.1:8080` are treated as "unset" for this purpose, so a committed `.mcp.json` template can ship them harmlessly and the wrapper still finds the real values in the gitignored `local.env`. This is why `coord init` is safe to run in a public repo: it writes credentials to `local.env` only and leaves `.mcp.json` as a template.
