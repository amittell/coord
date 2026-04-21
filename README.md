# Multi-Agent Team Coordination

`multi-agent-coordination` is a small coordination layer for teams running multiple agent sessions against the same codebase. It gives Claude Code, Codex CLI, and Cursor a shared source of truth for active file/module claims so agents can check, claim, extend, and release work before they step on each other.

**This repo IS the coordination service.** You run one instance of it (locally during development, or as a container in whatever infra your team already uses) and point your application repos at it with `coord init`. The application repos you coordinate live elsewhere.

The stack is intentionally simple:

- FastAPI HTTP API (`coordination.main:app`) for claims, conflicts, ownership config, and dashboard access
- SQLite storage with WAL enabled
- MCP stdio bridge (`coord-mcp`) so editor/CLI tools can talk to the service as native tools
- Shipped as a container image so you can deploy it on any infra that runs containers
- Integration templates for `CLAUDE.md`, `AGENTS.md`, pre-push hooks, and CI

## Docs

- `docs/quickstart.md`: fastest path from clone to first successful claim
- `docs/getting-started.md`: fuller install + rollout guide
- `docs/usage-guide.md`: day-to-day workflow for engineers and agent sessions
- `docs/architecture.md`: component model, request flow, and scaling notes
- `docs/api-reference.md`: endpoint reference and example payloads
- `docs/deployment.md`: container contract and operator notes for self-hosting
- `docs/troubleshooting.md`: common setup and runtime issues
- `docs/integrations/claude-code.md`: Claude Code-first integration
- `docs/integrations/codex-cli.md`: Codex CLI integration
- Cursor users: see `templates/.cursor/mcp.json.example` and the Cursor rule under `templates/.cursor/rules/`
- `CHANGELOG.md`: notable changes between versions

## Quickstart

From inside a checkout of this repository:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
coord start --background
```

`coord start` prints the API URL, dashboard URL, and an `export COORD_AUTH_TOKEN=...` line you can paste into your shell.

Useful URLs:

- API base: `http://127.0.0.1:8080`
- readiness: `http://127.0.0.1:8080/readyz`
- dashboard: `http://127.0.0.1:8080/dashboard`

Then, inside the repo you want to coordinate:

```bash
cd /path/to/your-app
coord init --tool claude --mode local --yes
coord doctor
```

Advanced MCP note:

```bash
export COORD_API_URL=http://127.0.0.1:8080
coord-mcp
```

`coord` is the main product surface:

- `coord start`: boot a local coordination service with sane defaults
- `coord init`: wire the current repo for Claude Code, Codex CLI, or Cursor
- `coord doctor`: verify the repo wiring and service connectivity

`coord-api` and `coord-mcp` still exist for advanced use and compatibility.

## First API Call

`coord start` printed an `export COORD_AUTH_TOKEN=...` line. Paste it into your shell, then:

```bash
curl -X POST http://127.0.0.1:8080/claims \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "engineer": "alex/claude/main",
    "branch": "alex/feature",
    "description": "touching auth module",
    "claims": [{"type": "file", "pattern": "src/auth/**"}],
    "ttl_hours": 4
  }'
```

`POST /claims` returns:

- `200` when claims are created
- `409` when overlapping active claims exist
- `400` when scope validation fails

`GET /conflicts` returns `safe`, `safe_to_proceed`, `has_conflicts`, and an optional `suggestion`.

## Operator Defaults

- Auth is now explicit: set `COORD_AUTH_TOKEN` in normal use.
- Local unauthenticated mode is possible, but only if you opt in with `COORD_ALLOW_INSECURE_NO_AUTH=true`.
- `COORD_REPO_ROOT` is optional but strongly recommended when the service can access a checkout of the application repo, because overlap detection is more accurate with `git ls-files`.

## Local Assets

- `.env.example`: environment variable template
- `compose.yaml`: local Docker Compose launcher
- `Makefile`: common install, run, lint, test, and smoke targets
- `templates/`: files to copy into the application repo you want to coordinate
- `scripts/completions/`: bash and zsh shell completion scripts for the `coord` CLI. Bash: copy `coord.bash` to `/etc/bash_completion.d/coord` (or `~/.local/share/bash-completion/completions/coord`). Zsh: copy `_coord` to a directory on `$fpath` (for example `/usr/local/share/zsh/site-functions/`) and run `autoload -U compinit && compinit`.

## Repo Integration

The easiest path is to use `coord init` in the application repo. For most teams:

1. Start the local service with `coord start` or point at an existing shared service.
2. Run `coord init --tool claude --mode local --yes`.
3. Run `coord doctor`.
4. Refine the generated `.coordination/owners.yaml` and upload it with `POST /config/ownership` if you want stronger ownership guidance.

Start with `docs/integrations/claude-code.md` if your team is primarily on Claude Code.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `COORD_DATABASE_PATH` | SQLite path. Default: `./data/coordination.db` |
| `COORD_AUTH_TOKEN` | Bearer token required by the HTTP API |
| `COORD_ALLOW_INSECURE_NO_AUTH` | Only for explicit local/demo mode; default `false` |
| `COORD_REPO_ROOT` | Optional repo path used for accurate overlap checks via `git ls-files` |
| `COORD_API_URL` | Base URL for the MCP stdio bridge. Default: `http://127.0.0.1:8080` |
| `COORD_HOST` | Bind host for the API server. Default: `0.0.0.0` |
| `COORD_PORT` | Bind port for the API server. Default: `8080` |
| `COORD_LOG_LEVEL` | Uvicorn log level. Default: `info` |
| `COORD_MAX_CLAIM_FILES` | Max files a single claim may cover. Default: `100` |
| `COORD_MAX_CLAIM_RATIO` | Max fraction of repo a single claim may cover. Default: `0.2` |
| `COORD_CLEANUP_INTERVAL_SEC` | Background expiration sweep interval. Default: `900` |
| `COORD_DEFAULT_TTL_HOURS` | Default TTL for normal claims. Default: `4` |
| `COORD_SHARED_TTL_HOURS` | TTL for shared-file claims. Default: `2` |
| `COORD_DISABLE_BACKGROUND_CLEANUP` | Set to `true`/`1`/`yes` to skip the in-process claim expiration sweep (useful for tests or external schedulers). Default: unset |
| `COORD_HOME` | Base directory for `coord start` local state (token file and SQLite). Default: `~/.coord` |

## Development

```bash
make install
make run
make init
make doctor
make lint
make test
```

## Docker

```bash
docker build -t coordination .
docker run \
  -e COORD_AUTH_TOKEN=secret \
  -p 8080:8080 \
  coordination
```
