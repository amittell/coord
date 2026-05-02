# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project adheres to
Semantic Versioning.

## [Unreleased]

### Added

- (none recorded yet)

### Changed

- (none recorded yet)

### Fixed

- (none recorded yet)

## [0.4.1] - 2026-05-02

### Fixed

- Codex MCP setup now writes an explicit `[mcp_servers.coord.env]` block in `.codex/config.toml` carrying `COORD_API_URL`, `COORD_AUTH_TOKEN`, and `COORD_REPO_ID`. The previous codex template embedded only a comment hint pointing at `.coordination/local.env`, which Codex never sources, so the MCP child silently fell back to `http://127.0.0.1:8080` and surfaced "All connection attempts failed" to operators on remote-mode repos. `coord init` and `coord upgrade` both populate the env block; existing codex repos pick up the fix on their next `coord upgrade`.

## [0.4.0] - 2026-05-01

### Changed

- Conflict detection is now repo-scoped. A claim with `repo=X` is only checked against other claims with `repo=X`; a claim with `repo=NULL` (legacy / un-tagged client) is only checked against other `repo=NULL` claims. Closes the cross-repo false-positive where, for example, a `client/js/**` claim from `amittell/astrowars` would block any push touching `client/js/**` in unrelated services on the same coord instance. Both `POST /claims` and `GET /conflicts` apply the partition.
- `GET /conflicts` accepts a new `repo=` query parameter so the pre-push hook can scope its check.
- Pre-push hook reads `COORD_REPO_ID` from `.coordination/local.env` and forwards it as `&repo=` on every `/conflicts` call. Existing repos pick up the behaviour on their next `coord upgrade`.

## [0.3.0] - 2026-05-01

### Added

- Per-repo claim tracking. Each claim now carries a repo identifier ("amittell/coord"-style slug). The MCP bridge reads `COORD_REPO_ID` and includes it on every `claim_files` call; `coord init` and `coord upgrade` derive the value from `git remote get-url origin` (HTTPS or SSH) and persist it in `.coordination/config.toml`, `.coordination/local.env`, and the tool-specific MCP config. Existing repos pick up the value on their next `coord upgrade`.
- New GET `/repos` endpoint returns one row per repo with active claim count, claims and engineers in the rolling 24h window, and last-activity timestamp.
- GET `/claims` now accepts `?repo=` to filter to a single repo's claims.
- Dashboard adds a "Repositories" panel listing every repo using the service so operators can see at a glance who's coordinating where.

### Changed

- Database schema bumped to v2 via a forwards-only migration that adds a nullable `claims.repo` column. Pre-v2 claims keep `repo=NULL` and are excluded from the per-repo aggregations.

## [0.2.1] - 2026-04-28

### Added

- Dashboard now opens with a "Recent activity (last 24h)" panel that summarises claims created, conflicts logged, distinct engineers active, and the top modules touched in the rolling 24h window. Computed from the existing claim and conflict tables, no schema changes. Closes the gap where the headline "Active claims" and "Module heatmap" sections both rendered empty between active sessions and made the page look dead.

## [0.2.0] - 2026-04-27

### Added

- `coord upgrade` command refreshes the pre-push hook, MCP config, and managed CLAUDE.md / AGENTS.md / cursor block from the latest packaged assets while preserving `.coordination/config.toml`, `.coordination/owners.yaml`, and the existing `COORD_AUTH_TOKEN`.
- `coord doctor` now flags managed asset drift (in-repo hook or managed block content does not match the packaged snippet) and points at `coord upgrade`.
- `coord doctor` now compares the locally installed CLI version against the running service's `/meta` and reports skew in either direction with an actionable hint (update local install, or bump the cluster image).
- Proactive once-per-24h update notice on every CLI command. When the configured service reports a newer version than the local install, `coord` prints a single stderr line pointing at `coord upgrade`. Throttled via a timestamp file, silent on failure, opt-out with `COORD_NO_UPDATE_CHECK=1`, skipped for `init` / `start` / `_serve` / `doctor` and outside coord-initialised repos.
- `deploy/k8s/prod/` overlay for kebabrack k3s: namespace, Traefik ingress, local-path PVC, two `VaultStaticSecret` resources (auth token + GHCR pull credentials rendered as `kubernetes.io/dockerconfigjson`), and a pinned image digest. Argocd-managed.

### Fixed

- Pre-push hook silently skipped the conflict check when `COORD_AUTH_TOKEN` was empty, disabling protection for any service running in `COORD_ALLOW_INSECURE_NO_AUTH` mode. Now omits the Authorization header instead of skipping, so the check still runs and a 401 is the only failure path.
- Pre-push hook ignored the repo's `.coordination/local.env` and silently fell back to `http://127.0.0.1:8080` whenever the pushing shell had no `COORD_SERVICE_URL` exported. The hook now sources `local.env` first and `coord init` writes `COORD_API_URL` and `COORD_SERVICE_URL` into it. URL fallback chain becomes: `COORD_API_URL` -> `COORD_SERVICE_URL` -> `COORD_URL` -> `http://127.0.0.1:8080`.
- Pre-push hook crashed under bash 3.2 (the macOS system bash) with `CURL_AUTH[@]: unbound variable` when the auth token was empty. Switched the auth-header expansion site to the portable `${var[@]+"${var[@]}"}` form so empty arrays no longer trip `set -u`.
- `coord doctor`'s auth probe sent `Authorization: Bearer ` (trailing space) when the token was empty, which httpx rejects as an illegal header value. Doctor now sends no Authorization header in that case and renames the check to `unauthenticated access works` with a hint pointing at `COORD_ALLOW_INSECURE_NO_AUTH`.

## [0.1.0] - 2026-04-21

### Added

- Core HTTP API for claims, conflicts, ownership configuration, and a bundled dashboard.
- MCP stdio bridge (`coord-mcp`) so Claude Code, Codex CLI, and Cursor can talk to the service as a native tool.
- `coord` CLI with `start`, `init`, `doctor`, `stop`, `status`, `claims`, and `release` subcommands.
- `coord --version` flag that prints the installed package version.
- Shell completion scripts for bash and zsh under `scripts/completions/`.
- Container image with a non-root runtime user, multi-stage build, and pinned Python dependencies.
- GitHub Actions CI matrix covering Ubuntu, macOS, and Windows.
- Release workflow with `workflow_dispatch`, SHA-pinned third-party actions, and build provenance attestations.
- Dependabot configuration for GitHub Actions and Python dependency updates.
- Windows-friendly path handling, GitHub Enterprise support, monorepo layouts, and Scalar clone support in the repo scanning helpers.
- `COORD_REPO_SCOPE` environment variable and a 10-second `git ls-files` cache to bound repo scanning cost.
- Cross-process migration safety via `BEGIN IMMEDIATE` and `busy_timeout` on the SQLite writer.
- PID marker verification for `coord stop` so we never SIGTERM an unrelated process.
- `tests/test_mcp_server.py` regression guards around the MCP stdio bridge surface.

### Changed

- Pattern negation (leading `!`) is now rejected at the API boundary with a clear 400 response instead of being silently partially supported.
- Zero-match claim scopes now emit a warning with a case-insensitive hint when a near-match exists.
- Dependabot grouping tightened: dev tools (pytest, pytest-asyncio, ruff, mypy) bundle into one PR across all version types; production deps keep minor+patch grouped with majors separate; docker base image groups all updates. Schedule moved from weekly to monthly (security advisories still fire immediately). Cuts GitHub Actions consumption on routine dependency sweeps.
- `make check` now runs ruff + mypy + pytest; new `make verify` adds a container smoke. Opt-in pre-push hook at `scripts/git-hooks/pre-push` runs local checks before `git push`.

### Fixed

- (none recorded yet)

## [0.1.0] - TBD

### Added

- Initial release.
