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

1. create `.coordination/config.toml`
2. create `.coordination/local.env`
3. create `.coordination/owners.yaml`
4. patch `.mcp.json` or the selected tool config
5. patch `CLAUDE.md` or `AGENTS.md`
6. install a pre-push hook

If you prefer manual rollout, the template inventory still lives in `../templates/README.md`.

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
3. extend claims if work runs long
4. release claims when done

The shipped snippets in `templates/` already encode this workflow.

## Step 7: Verify the rollout

Use two different engineer IDs and confirm:

1. engineer A can create a claim
2. engineer B sees a conflict on overlapping files
3. the dashboard shows the active claim
4. releasing the claim clears the conflict

Then move to `usage-guide.md` for the day-to-day operating model.
