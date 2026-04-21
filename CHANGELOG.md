# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project adheres to
Semantic Versioning.

## [Unreleased]

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
