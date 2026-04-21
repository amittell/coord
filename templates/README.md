# Repo integration templates

Copy the pieces you need into your application repository (same paths relative to repo root).
For step-by-step rollout guidance, see `../docs/getting-started.md` plus the tool-specific guides under `../docs/integrations/`.

| Path | Purpose |
|------|---------|
| `.coordination/owners.example.yaml` | Rename to `owners.yaml` and customize; upload to coordination service via `POST /config/ownership` |
| `.coordination/MODULE_GUIDE.md` | How to modularize a monolith incrementally |
| `.coordination/eslint.restricted-imports.example.cjs` | Example ESLint `no-restricted-imports` for module boundaries |
| `.cursor/mcp.json.example` | Cursor MCP wiring for the coordination server |
| `.cursor/rules/coordination.mdc` | Cursor agent rules |
| `.mcp.json.example` | Claude Code MCP wiring |
| `CLAUDE.md.snippet.md` | Append coordination section to `CLAUDE.md` |
| `.codex/config.toml.example` | Codex CLI MCP wiring |
| `AGENTS.md.snippet.md` | Append coordination section to `AGENTS.md` |
| `.coordination/hooks/pre-push` | Git pre-push check against `/conflicts` |
| `github-coordination-semantic.yml` | Copy to `.github/workflows/coordination-semantic.yml` (PR + merge-group CI template) |
| `MERGE_QUEUE.md` | Notes for merge queue + stacked PR workflow |

Set environment variables:

- `COORD_TOKEN` / `COORD_AUTH_TOKEN` (depending on tool): bearer token for the coordination API
- `COORD_SERVICE_URL` / `COORD_API_URL`: base URL of the deployed coordination service
