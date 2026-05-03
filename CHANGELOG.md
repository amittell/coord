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

## [0.7.2] - 2026-05-02

### Fixed

- Pre-push hook now refuses loudly when stdin is redirected but empty (an outer wrapper hook backgrounded the coord call or otherwise dropped git's ref-update stream). The pre-v0.7.2 hook silently fell through to a HEAD-vs-origin/HEAD diff in this case, which misses non-HEAD pushes, multi-ref pushes, new-branch pushes, and deletions -- exactly the failure mode that surfaced in astrowars where a project-level `run_child` wrapper was backgrounding coord with `"$@" &`. The hand-run fallback (TTY stdin) is preserved for testing, but now prints a noisy heads-up that it's the test path. The refusal message includes a worked example of the right outer-hook wiring (cache stdin once into a tempfile and redirect the coord child from it).

### Wrapping coord's hook from another pre-push hook

If your repo already has a tracked pre-push hook and you want to chain coord's check into it:

    # near the top, cache git's ref-update stream once
    PUSH_REFS="$(mktemp)"
    trap 'rm -f "$PUSH_REFS"' EXIT
    [ ! -t 0 ] && cat > "$PUSH_REFS"

    # at the call site, redirect stdin from the cache
    bash "$REPO_ROOT/.coordination/hooks/pre-push" "$@" < "$PUSH_REFS"

The hook reads `<local_ref> <local_sha> <remote_ref> <remote_sha>` lines off stdin to compute a per-ref diff. Without that input it can't tell what's actually being pushed.

## [0.7.1] - 2026-05-02

### Fixed

- `coord init --force` no longer silently destroys a tracked pre-push hook when `.git/hooks/pre-push` is a symlink to a repo file. `pathlib.Path.write_text` follows symlinks, so the previous code wrote the coord shim *through* the symlink and clobbered the target -- typically `scripts/git-hooks/pre-push` carrying real CI / lint / deploy logic. Init now detects symlinks before any write and refuses to follow them, printing actionable guidance for chaining coord's check into the user's existing hook. The non-symlink overwrite path (force=True over an existing non-coord hook) now writes a `.bak` of the previous content first.
- `coord doctor` adds a check that `.coordination/hooks/pre-push` exists. The shim in `.git/hooks/pre-push` exec's that target; if the target is missing every push silently exits zero, so deploy commits stay local without surfacing -- which is exactly how requesthub's deploys broke. The new check fails loud with a `coord upgrade` hint when the target is missing.

### Migration

Repos that have a tracked pre-push hook should chain coord's check into it with these two lines (no auto-magic; explicit beats clobbering):

    COORD_HOOK="$(git rev-parse --show-toplevel)/.coordination/hooks/pre-push"
    [ -x "$COORD_HOOK" ] && "$COORD_HOOK" "$@"

## [0.7.0] - 2026-05-02

### Changed (behaviour-affecting)

- Pre-push hook now fails closed instead of silently skipping when prerequisites are missing or transport fails. Three previously-silent bypass paths are now hard refusals:
  - `jq` not installed: was `exit 0` with a "skipping" message; is now `exit 1` with a hint to install jq or pass `--no-verify`.
  - `curl` error talking to the service: was wrapped in `|| true` so a transient network glitch produced an empty response and the check passed by default. The curl exit code is now checked explicitly; any error refuses the push.
  - Unparseable response from `/conflicts`: was treated as "no conflict"; is now refused with the raw body printed for diagnosis.
- Pre-push hook now consumes the ref-update stream that `git push` hands the hook on stdin (`<local_ref> <local_sha> <remote_ref> <remote_sha>`) and computes the diff per ref. Pre-v0.7 always diffed `HEAD...origin/HEAD` regardless of what was actually being pushed, which silently missed multi-ref pushes, non-HEAD pushes, and deleted-branch pushes. Falls back to the old HEAD-based path when run interactively without stdin.
- First-push scenarios (no remote tracking branch yet) now diff against `git hash-object -t tree /dev/null` (the empty tree). Triple-dot diff fails with the empty tree, which is why the pre-v0.7 hook punted with "could not determine diff base; skipping" in that case -- yet another silent bypass.
- Conflict-check response parsing is stricter: `.has_conflicts` must be `true` or `false`. An empty / null / unexpected value is treated as a server bug and refuses the push.

### Migration

The behaviour change only matters for environments where the hook was previously hitting a silent-skip path. If you've been relying on `jq`-missing as a tacit bypass, install jq or use `--no-verify` deliberately. Existing repos pick up the new hook on their next `coord upgrade`.

### Credits

The hook redesign was prompted by an agent in astrowars rewriting the hook on its own to close these holes. The diff was reviewed and ported upstream verbatim, with comments expanded.

## [0.6.2] - 2026-05-02

### Fixed

- `coord upgrade` now refreshes `.gitignore` too. v0.6.1 fixed the marker style for fresh repos, but the upgrade path didn't touch `.gitignore`, so existing repos couldn't migrate to the new `# coord:` markers without re-running `coord init`. Upgrade now calls `ensure_gitignore_entry` alongside the rest of the asset refresh; the in-place detection accepts either marker style, so the migration is idempotent and never duplicates the entry.

## [0.6.1] - 2026-05-02

### Fixed

- `.gitignore` managed-block markers now use shell-comment syntax (`# coord:begin` / `# coord:end`) instead of HTML-comment syntax. The HTML markers are not valid gitignore comments and were silently parsed as never-matching path patterns; an agent in astrowars saw them as broken syntax and "fixed" them, drifting the file off coord's detection contract. Detection now accepts either marker style on read, so existing repos migrate cleanly on their next `coord upgrade` without losing the entry.
- Managed blocks now embed an `AUTO-GENERATED by 'coord upgrade'. Do not hand-edit; next upgrade will overwrite.` warning as their first content line. Future agents inspecting CLAUDE.md / AGENTS.md / `.cursor/rules/coordination.mdc` see the contract immediately rather than treating the cryptic marker line as a hint to ignore. Doctor's drift comparison strips the warning before matching against the packaged snippet so the new line doesn't itself look like drift.
- `ensure_managed_block` now recognises a block whose markers were swapped to hash style (the astrowars vandalism scenario), so the next upgrade replaces it in place with proper HTML markers rather than appending a duplicate.

### Changed

- Tightened the CLAUDE.md / AGENTS.md / cursor coordination snippets: same protocol, fewer words, harder to want to "simplify."

## [0.6.0] - 2026-05-02

### Added

- Pending-requests inbox. New `GET /sessions/{session_id}/pending_requests` returns recent conflict-log entries logged against active claims a session currently holds, so an active holder can poll "has anyone been blocked on my scope?" between operations and release voluntarily. coord-mcp exposes this as a `pending_requests` tool whose default form takes no arguments and uses the current process's session id. The CLAUDE.md / AGENTS.md / cursor managed snippets have been updated to recommend polling between operations.
- Activity-based auto-expiration. Session-tagged claims now carry a `last_activity` timestamp that gets bumped on every coord call from the holder's session (`claim_files`, `check_conflicts`, `list_claims`). The cleanup sweep auto-releases any session-tagged claim that has been silent for longer than `COORD_IDLE_TIMEOUT_SEC` (default 1800 seconds / 30 minutes), catching agents that walked away without releasing. Legacy NULL-session claims keep `last_activity = NULL` and are unaffected -- they continue to use TTL only. Set `COORD_IDLE_TIMEOUT_SEC=0` to disable idle expiration cluster-wide.
- Conflict log records the requester's `session_id` (`conflict_log.attempted_session_id`), so the holder can distinguish foreign sessions from its own subagents in the pending-requests inbox.

### Changed

- coord-mcp's `list_claims` and `check_conflicts` tools now include `session_id` on every call. The conflict check itself was already session-aware in v0.5; the new wiring lets these calls also act as activity pings on the server side, keeping the holder's claims warm while it's actively reasoning rather than only when it's creating new claims.
- Schema bumped to v4 via a forwards-only migration adding nullable `claims.last_activity` and `conflict_log.attempted_session_id` columns. Pre-v4 data is preserved with NULLs.

## [0.5.0] - 2026-05-02

### Added

- Per-MCP-process session id. The conflict check now self-excludes any active claim whose `session_id` matches the caller's, so subagents spawned by a single Codex/Claude Code/Cursor process never block each other when they pick distinct engineer names. Different sessions remain adversarial. coord-mcp generates a 16-char hex id at module load and sends it on every `claim_files` call; operators can pin a stable value with `COORD_SESSION_ID`. Schema bumped to v3 via a forwards-only migration adding a nullable `claims.session_id` column; pre-v3 claims keep `session_id=NULL` and behave like the legacy engineer-only self-exclusion path. Closes the orphaned-claim trap where a parent agent left claims under engineer `codex` and its subagents (using names like `codex-server-review`) were locked out of overlapping scope until TTL expiry.
- `GET /conflicts` accepts a new `session_id=` query parameter mirroring the field on `POST /claims`.
- `POST /sessions/{session_id}/release` releases every active claim with the given session_id in one call. coord-mcp exposes this as a `release_session` tool whose default form takes no arguments and uses the current process's session id, so end-of-work cleanup is one MCP call regardless of how many engineer names the agent used.

## [0.4.2] - 2026-05-02

### Fixed

- `coord upgrade` now refreshes every tool config that exists on disk in the repo, not just the one named in `.coordination/config.toml`. A repo that wired both Claude (`.mcp.json`) and Codex (`.codex/config.toml`) by running `coord init` twice with different `--tool` values used to have only the most-recent tool's config refreshed by upgrade; the other silently kept its stale URL/token/repo id. Cursor configs are handled the same way. Upgrade still falls back to creating the tool named in `config.toml` when its file is missing, so deleting a config and re-running upgrade restores it.
- `coord doctor` now flags managed-block drift in CLAUDE.md, AGENTS.md, and the cursor rules file independently, so multi-tool repos see a drift warning for whichever doc has gone stale (previously only the primary tool from `config.toml` was checked).
- `coord doctor` now compares the embedded `COORD_AUTH_TOKEN` in each tool's MCP config against `.coordination/local.env` and reports drift. Pre-fix, rotating the token in `local.env` without running `coord upgrade` left the MCP child authenticating with the old key with no warning.

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
