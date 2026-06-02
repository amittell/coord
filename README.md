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

## Configuration & secrets

`coord init` lays down two kinds of files in the application repo:

1. **Tracked templates** committed to VCS with placeholder values. These tell every MCP client (Claude Code, Codex CLI, Cursor) how to spawn `coord-mcp` and tell agents the coordination protocol. They are safe to share publicly.
2. **Gitignored runtime config** under `.coordination/` carrying the real bearer token and repo identifier. Never committed.

| Path | Tracked? | What it carries |
|------|----------|-----------------|
| `.mcp.json` (Claude Code) | yes (template) | `command = "coord-mcp"` + an `env` block with placeholder `COORD_*` values |
| `.codex/config.toml` (Codex CLI) | yes (template) | Codex equivalent of the above |
| `.cursor/mcp.json` (Cursor) | yes (template) | Cursor equivalent of the above |
| `CLAUDE.md` / `AGENTS.md` | yes | Protocol snippet inside a `coord:begin … coord:end` managed block |
| `.gitignore` | yes | Managed block adds `/.coordination/` so step 2 stays untracked |
| `.coordination/config.toml` | **no** (gitignored) | Per-repo coord settings: mode, service URL, ownership file path |
| `.coordination/local.env` | **no** (gitignored) | Real `COORD_AUTH_TOKEN`, `COORD_API_URL`, `COORD_REPO_ID` |
| `.coordination/owners.yaml` | **no** (gitignored) | Per-repo ownership rules; upload to the service via `POST /config/ownership` |
| `.git/hooks/pre-push` | not in repo | Installed by `coord init`; sources `.coordination/local.env` before calling the API |

`coord init` patches `.gitignore` with `/.coordination/` automatically, so the whole `.coordination/` directory is excluded from the moment it is created. No additional setup is required to keep secrets out of git history.

### How the template + secret split works at runtime

`coord-mcp` is spawned by the editor/CLI with whatever env the tracked MCP registration provides — usually the placeholder values `set-me`, `example-org/example-repo`, and `http://127.0.0.1:8080`. At startup the wrapper walks up from its working directory (like git looking for `.git/`) until it finds `.coordination/local.env`, then for each `COORD_*` allowlisted key:

- if the variable is currently unset, **or** holds one of the documented placeholders, the wrapper overrides it from `local.env`;
- if the variable already holds a real value (a shell export, or an inline env block in `.mcp.json` with a real token), the explicit value wins.

`_headers()` also drops the `Authorization` header when the token is a documented placeholder, so a misconfigured client fails loud with a clean `401` instead of silently leaking a `Bearer set-me` request. The net effect: a tracked `.mcp.json` template can ship placeholder values to a public repo without breaking any working setup, and rotating credentials means editing one file (`.coordination/local.env`) rather than every per-tool MCP registration. See `docs/integrations/claude-code.md` and `docs/integrations/codex-cli.md` for the resolution order in tool-specific terms.

## Sub-file (symbol-level) claims

File-level locking scales poorly once 10+ agents work the same repo: hot files (`router.ts`, the schema index, the app shell) are touched by every active branch, so the conflict engine forces agents to serialise even when their actual edits don't overlap. The v0.11 `narrowed` / `coexist` decisions help, but they're reactive -- the requester still hits a `409` first. Coord v0.14 adds symbol-scope claims so two agents editing different functions in the same file coexist by default, with no human-in-the-loop request.

Claim `handleLogin` in `auth.ts` from MCP:

```python
claim_files(
    engineer="alex/claude/main",
    patterns=["src/auth/login.ts"],
    symbols={"src/auth/login.ts": ["handleLogin"]},
)
```

Or over HTTP:

```json
{
  "engineer": "alex/claude/main",
  "claims": [
    {"type": "file", "pattern": "src/auth/login.ts", "symbols": ["handleLogin"]}
  ]
}
```

Two automatic decisions kick in when symbols are involved:

- **AUTO_COEXIST**: a second symbol claim on the same file with a disjoint symbol set is granted immediately. Both claims live as cooperative partners (`coexists_with` cross-referenced). No `409`, no request filed, audit row `event_type='auto-coexist'`.
- **AUTO_NARROW**: a symbol claim arriving against an existing narrowable file claim is granted alongside the file claim. The holder's effective scope becomes "the file minus the new partner's symbols"; they get a `pending_requests` notice on their next poll but don't have to act. File claims are `narrowable=true` by default; `shared_file` and `module` claims are not.

Symbols only cover the named declarations. Imports and module-level statements still need a file claim. TypeScript is supported in v0.14; Python and Go follow in v0.15. See [./docs/design/sub-file-claims.md](./docs/design/sub-file-claims.md) for the full spec.

### Method-level scope (v0.16)

Claim a specific method on a class with the `Parent::child` notation:

```http
POST /claims
{
  "engineer": "alex/claude/main",
  "claims": [{
    "type": "file",
    "pattern": "src/auth/router.ts",
    "symbols": ["Router::handleAuth"]
  }]
}
```

Two agents on `Router::handleAuth` and `Router::handleLogout` auto-coexist; a claim on the bare `Router` blocks both (and vice versa).

### Recursive nesting (v0.17)

The notation is recursive: `"Outer::Inner::method"` works to any depth. A claim on `"Outer"` covers every descendant; a claim on `"Outer::Inner"` covers `"Outer::Inner::*"` but not `"Outer::Other::*"`. Storage is unchanged -- `parent_symbol` carries the ancestor chain joined by `::`, and the conflict engine prefix-matches on the full canonical path.

### Validation (v0.17)

When `COORD_REPO_ROOT` is set the service parses each claimed file and rejects unknown symbols with a hint listing the file's actual symbol set. The MCP wrapper also pre-validates locally before POSTing so typos fail fast without a round-trip; disable with `COORD_DISABLE_CLIENT_VALIDATION=1`.

### Observability (v0.18)

The dashboard surfaces a 30-day auto-resolution heatmap per repo so you can see whether sub-file claims are actually saving conflicts. The same series is exposed at `GET /metrics/auto-resolutions?days=30` for external monitoring.

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
| `COORD_REPO_ID` | Repo identifier (e.g. `example-org/example-app`) attached to every claim from this repo (added in v0.3.0). Set automatically by `coord init` from `git remote get-url origin`. |
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

## License

Apache License 2.0. See [`LICENSE`](./LICENSE).
