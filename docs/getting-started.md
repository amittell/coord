# Getting Started

This guide assumes:

- one team is hosting the coordination service
- engineers connect to it from Claude Code, Codex CLI, or Cursor
- the application repo being coordinated is separate from this repo

## Step 1: Run the coordination service

Choose one of these paths.

### Option A: local Python process

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
coord start --background
```

### Option B: Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

If you use Compose, make sure `.env` contains a real `COORD_AUTH_TOKEN`.

## Step 2: Decide how accurate overlap checks need to be

The service can run with or without access to the application repo checkout.

- Best accuracy: set `COORD_REPO_ROOT` to a checkout of the application repo on the machine hosting the service.
- Good enough for early rollout: leave `COORD_REPO_ROOT` unset and use more specific file claims instead of huge globs.

Without `COORD_REPO_ROOT`, overlap detection falls back to heuristic matching. That is usable, but less precise.

## Step 3: Create an ownership file

Start from `templates/.coordination/owners.example.yaml`.

Example:

```yaml
modules:
  auth:
    paths:
      - src/auth/**
    severity: hard
    owners:
      - identity-team
```

Upload it:

```bash
curl -X POST http://127.0.0.1:8080/config/ownership \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: text/plain" \
  --data-binary @templates/.coordination/owners.example.yaml
```

## Step 4: Integrate the application repo

In the application repo you want to coordinate:

```bash
coord init --tool claude --mode local --yes
coord doctor
```

That command sequence will:

1. create `.coordination/config.toml` (gitignored)
2. create `.coordination/local.env` (gitignored, holds the bearer token)
3. create `.coordination/owners.yaml` (gitignored)
4. patch `.mcp.json` or the selected tool config (placeholder env values only; gitignored since v0.32, and previously committed copies are untracked)
5. patch `CLAUDE.md` or `AGENTS.md` (tracked, protocol snippet inside a managed block)
6. install a pre-push hook at `.git/hooks/pre-push`
7. patch `.gitignore` with `/.coordination/` plus the machine configs (`.mcp.json`, `.cursor/mcp.json`, `.codex/config.toml`) (tracked)

If you prefer manual rollout, the template inventory still lives in `../templates/README.md`.

### Configuration & secrets

The MCP registration files (`.mcp.json`, `.codex/config.toml`, `.cursor/mcp.json`) carry only the placeholder env values (`COORD_AUTH_TOKEN=set-me`, `COORD_REPO_ID=example-org/example-repo`, `COORD_API_URL=http://127.0.0.1:8080`) and are gitignored since v0.32 -- machine config is per-checkout state, and committing it polluted PRs. `coord init` regenerates them locally and untracks previously committed copies. The real values live only in `.coordination/local.env`, which is also gitignored.

`coord-mcp` reconciles the two at startup: it walks up from its working directory looking for `.coordination/local.env`, and for each `COORD_*` allowlisted variable overrides any unset or placeholder value with what the file carries. Real values supplied via shell exports or via a non-placeholder env block in `.mcp.json` are preserved (explicit > file > built-in defaults). The `Authorization` header is also dropped when the token is a documented placeholder, so a misconfigured setup yields a clean `401` rather than `Bearer set-me`.

Operational implications:

- rotating the bearer token = edit `.coordination/local.env` in every coordinated repo; the MCP registration files do not need to change.
- a stale `.mcp.json` (e.g. one regenerated against a sanitised template, or a pre-v0.32 committed copy) does not require a `coord init --force` to recover; `coord-mcp` will fall through to `local.env`.
- the pre-push hook loads only the keys it needs from `.coordination/local.env` as inert data, independent of the MCP wrapper, so it is unaffected by editor/CLI restarts without executing shell syntax from the file.

See `docs/integrations/claude-code.md` and `docs/integrations/codex-cli.md` for the resolution order in tool-specific terms.

## Step 5: Use session-scoped engineer names

This is important for teams running multiple agents per person.

Recommended pattern:

- `alex/claude/main`
- `alex/claude/reviewer`
- `alex/codex/fixer`

Do not reuse the exact same `engineer` string for multiple active workers if you want them to conflict with each other. Claims exclude the same `engineer` from conflict checks by design.

## Step 6: Teach agents the workflow

Your agent rules should follow this order:

1. check conflicts before editing
2. claim files or modules before editing
3. release claims when done

There is deliberately no agent-facing "extend" tool: every MCP call carrying the session id acts as an activity ping, so an active session's claims are not idle-expired out from under it, and a claim that hits its hard TTL is simply re-claimed by the next `claim_files`. If a claim genuinely needs more wall-clock time up front, an operator can extend it directly over HTTP with `POST /claims/{claim_id}/extend` (see `usage-guide.md`).

The shipped snippets in `templates/` already encode this workflow.

## Step 7: Verify the rollout

Use two different engineer IDs and confirm:

1. engineer A can create a claim
2. engineer B sees a conflict on overlapping files
3. the dashboard shows the active claim
4. releasing the claim clears the conflict

Then move to `usage-guide.md` for the day-to-day operating model.
