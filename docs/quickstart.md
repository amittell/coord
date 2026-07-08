# Quickstart

This is the shortest path from clone to a working coordination service.

## 1. Install and start the service

From a checkout of this repo:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
coord start --background
```

The service listens on `http://127.0.0.1:8080`. `coord start` prints an `export COORD_AUTH_TOKEN=...` line you can paste into your shell for the `curl` calls below.

Quick checks:

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/readyz
```

## 2. Initialize an existing repo

Inside the repo you want to coordinate:

```bash
cd /path/to/your-app
coord init --tool claude --mode local --yes
coord doctor
```

Expected result: the repo now contains `.mcp.json`, `CLAUDE.md`, `.coordination/config.toml`, `.coordination/owners.yaml`, and a pre-push hook.

## 3. Create a claim

```bash
curl -X POST http://127.0.0.1:8080/claims \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "engineer": "alex/claude/main",
    "branch": "alex/feature/claims",
    "description": "editing auth module",
    "claims": [{"type": "file", "pattern": "src/auth/**"}]
  }'
```

Expected result: a `200` response with one or more `claim_ids`.

## 4. Check for conflicts

```bash
curl "http://127.0.0.1:8080/conflicts?engineer=bob/claude/main&pattern=src/auth/login.ts" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN"
```

Expected result: `has_conflicts: true` while the first claim is active.

## 5. Run the MCP bridge

In a second shell:

```bash
coord-mcp
```

`coord-mcp` walks up from cwd to find `<repo-root>/.coordination/local.env` (written by `coord init`) and loads `COORD_API_URL` and `COORD_AUTH_TOKEN` from it. Explicit env beats the file, so the manual exports below are only needed when running `coord-mcp` outside an initialised repo or when overriding for a one-off test:

```bash
export COORD_API_URL=http://127.0.0.1:8080
export COORD_AUTH_TOKEN="$(grep '^COORD_AUTH_TOKEN=' /path/to/your-app/.coordination/local.env | cut -d= -f2-)"
coord-mcp
```

Your editor or CLI tool should launch `coord-mcp` as an MCP server command. The bridge talks to the HTTP API; it does not need a separate database.

## 6. Wire your coding tool

- Claude Code: see `integrations/claude-code.md`
- Codex CLI: see `integrations/codex-cli.md`
- Cursor: use `templates/.cursor/mcp.json.example`

## 7. Recommended next step

Once the service is reachable, move to `getting-started.md` and set up:

1. an ownership file
2. agent guardrails in `CLAUDE.md` or `AGENTS.md`
3. the pre-push check
