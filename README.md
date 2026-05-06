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

### Server (the API process)

| Variable | Description |
|----------|-------------|
| `COORD_DATABASE_PATH` | SQLite path. Default: `./data/coordination.db` |
| `COORD_AUTH_TOKEN` | Bearer token required by the HTTP API |
| `COORD_ALLOW_INSECURE_NO_AUTH` | Only for explicit local/demo mode; default `false` |
| `COORD_HOST` | Bind host for the API server. Default: `0.0.0.0` |
| `COORD_PORT` | Bind port for the API server. Default: `8080` |
| `COORD_LOG_LEVEL` | Uvicorn log level. Default: `info` |
| `COORD_LOG_JSON` | Set truthy to emit access logs as JSON instead of text. Default: unset |
| `COORD_REPO_ROOT` | Optional repo path used for accurate overlap checks via `git ls-files` |
| `COORD_REPO_SCOPE` | Restrict overlap checks (and claim-ratio enforcement) to this subdirectory of `COORD_REPO_ROOT`. Default: unset |
| `COORD_MAX_CLAIM_FILES` | Max files a single claim may cover. Default: `100` |
| `COORD_MAX_CLAIM_RATIO` | Max fraction of repo a single claim may cover (skipped in scope mode). Default: `0.2` |
| `COORD_CLEANUP_INTERVAL_SEC` | Background expiration sweep interval. Default: `900` |
| `COORD_DEFAULT_TTL_HOURS` | Default TTL for normal claims. Default: `4` |
| `COORD_SHARED_TTL_HOURS` | TTL for shared-file claims. Default: `2` |
| `COORD_IDLE_TIMEOUT_SEC` | Session-tagged claims auto-release if the holder has been silent for this many seconds (added in v0.6.0). Set to `0` to disable idle expiration cluster-wide. Default: `1800` |
| `COORD_REQUEST_TTL_SHORT_SEC` | When a release request is filed, the holder's claim TTL is clamped to `min(remaining, this)` (added in v0.9.0). Forces a near-term decision so a non-responsive holder can't sit on the scope. Default: `300` |
| `COORD_DISABLE_BACKGROUND_CLEANUP` | Set truthy to skip the in-process claim expiration sweep (useful for tests or external schedulers). Default: unset |
| `COORD_DISABLE_INSTANCE_LOCK` | Set truthy to bypass the advisory `<db>.lock` flock (useful on NFS-backed shared volumes where flock is unreliable). Default: unset |
| `COORD_LS_FILES_CACHE_TTL_SEC` | TTL for the in-process `git ls-files` cache used during overlap checks. Default: `10` |
| `COORD_HOME` | Base directory for `coord start` local state (token file and SQLite). Default: `~/.coord` |
| `COORD_START_READY_TIMEOUT_SEC` | How long `coord start --background` waits for `/readyz` before giving up. Default: `30` |

### MCP / client (set in `.coordination/local.env` or your shell)

| Variable | Description |
|----------|-------------|
| `COORD_API_URL` | Base URL for the MCP stdio bridge and pre-push hook. Default: `http://127.0.0.1:8080` |
| `COORD_SERVICE_URL` | Legacy alias for `COORD_API_URL`. Pre-push hook accepts both. |
| `COORD_TOKEN` | Legacy alias for `COORD_AUTH_TOKEN` accepted by the pre-push hook. |
| `COORD_REPO_ID` | Repo identifier (e.g. `amittell/bastionx`) attached to every claim from this repo (added in v0.3.0). Set automatically by `coord init` from `git remote get-url origin`. |
| `COORD_SESSION_ID` | Pin a stable session id across coord-mcp restarts (added in v0.5.0). Otherwise coord-mcp generates a fresh 16-char hex id at startup. |
| `COORD_NO_UPDATE_CHECK` | Set truthy to silence the once-per-24h "update available" stderr line emitted by every `coord` CLI command. Default: unset |

## Development

```bash
make install       # create .venv and install dev deps
make check         # ruff + mypy + pytest (~30s)     - run before pushing
make verify        # check + docker-smoke (~2min)    - full local CI equivalent
make test-fast     # pytest without integration tests - fast inner loop
make docker-smoke  # build image, probe /readyz, stop
```

To run `make check` automatically before every push, install the shipped hook:

```bash
ln -sf ../../scripts/git-hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

Bypass a specific push with `git push --no-verify` (docs-only changes, etc.).

## Docker

```bash
docker build -t coordination .
docker run \
  -e COORD_AUTH_TOKEN=secret \
  -p 8080:8080 \
  coordination
```
