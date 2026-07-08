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
| `.coordination/hooks/pre-push` | Git pre-push check against `/conflicts` (same script `coord init` installs at `.git/hooks/pre-push`: sources `.coordination/local.env`, refuses when `jq` is missing, forwards `sessions.live` session ids) |
| `github-coordination-semantic.yml` | Copy to `.github/workflows/coordination-semantic.yml` (PR + merge-group CI template) |
| `MERGE_QUEUE.md` | Notes for merge queue + stacked PR workflow |
| `skills/coordinating-file-claims/` | Agent Skill (SKILL.md, agentskills.io format) teaching any skill-capable agent to install, configure, and use coord. Copy the directory into `.agents/skills/` (Codex), `.claude/skills/` (Claude Code), or `.cursor/skills/` (Cursor) |

## Set environment variables

The `.example` MCP wirings (`.mcp.json.example`, `.codex/config.toml.example`, `.cursor/mcp.json.example`) carry the documented placeholder values (`COORD_AUTH_TOKEN=set-me`, `COORD_API_URL=http://127.0.0.1:8080`, `COORD_REPO_ID=example-org/example-repo`) -- the exact strings `coord-mcp` treats as "unset", so the wrapper falls through to `.coordination/local.env` for the real credentials. Do not replace the placeholders with real values in the copied file; that would shadow `local.env`. Since v0.32 `coord init` gitignores the destination machine configs (`.mcp.json`, `.codex/config.toml`, `.cursor/mcp.json`) in the application repo, but the placeholder values keep them safe even if a repo still tracks them.

`coord-mcp` auto-loads `.coordination/local.env` at startup and treats those three strings as "unset", so the real credentials live only in `.coordination/local.env` (which `coord init` writes and `/.coordination/` is added to `.gitignore`):

- `COORD_TOKEN` / `COORD_AUTH_TOKEN` (depending on tool): bearer token for the coordination API
- `COORD_SERVICE_URL` / `COORD_API_URL`: base URL of the deployed coordination service
- `COORD_REPO_ID`: repo identifier (e.g. `your-org/your-app`) attached to every claim from this repo

If you want a one-off shell override (e.g. while pointing at a staging service from a tracked checkout), `export`ing the variable in the same shell that launches the editor/CLI still wins; the wrapper only fills in unset or placeholder values.

See the root `README.md` "Configuration & secrets" section for the full tracked-vs-gitignored model and `docs/architecture.md` for the resolution order.
