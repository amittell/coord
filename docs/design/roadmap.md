# Coord roadmap

Status: living document
Last updated: 2026-06-19 (after v0.32.4)

This is the post-v0.29 forward look. Items here are candidates, not commitments -- order and scope move as production telemetry and operator feedback arrive. Entries already shipped live in [CHANGELOG.md](../../CHANGELOG.md); see also [docs/design/sub-file-claims.md](./sub-file-claims.md) for the v0.14-v0.26 arc design.

## v0.29.0-0.29.3 (shipped) -- Per-engineer bearer tokens + dashboard login UI

v0.29 was originally sized for queue rate limiting (the QoS continuation in the older v0.29 section, now renamed v0.30). That was deferred when operator demand surfaced for per-engineer bearer tokens to retire the single shared ``COORD_AUTH_TOKEN``, and for a real HTML login form on ``/dashboard`` instead of the JSON 401 browsers used to see. Rate limiting now becomes a natural follow-on (the engineer identity it needs is what per-engineer tokens give us at auth time) and moves to v0.30.

Shipped in v0.29.0:

- Schema migration v14: ``engineer_tokens`` table. Tokens stored as ``sha256(raw_token)`` only; raw value returned exactly once at creation. Columns: id (UUID), engineer, token_sha256 (unique index), description, created_at, revoked_at, last_used_at.
- Five new Database methods cover the lifecycle: ``create_engineer_token``, ``lookup_engineer_token``, ``touch_engineer_token``, ``list_engineer_tokens``, ``revoke_engineer_token``.
- ``coord tokens create / list / revoke`` CLI. Tokens prefixed ``coordt_`` for grep-ability. ``list`` is metadata-only; raw token is never recoverable after creation. ``revoke`` idempotent.
- ``COORD_REQUIRE_PER_ENGINEER_TOKEN`` env flag. False by default (shared token still works for back-compat); switching True is the migration kill switch that rejects the shared token cluster-wide.
- Dashboard login UI: ``GET /dashboard`` renders HTML login form when unauth (instead of JSON 401). ``POST /dashboard/login`` validates + sets ``coord_session`` cookie. ``POST /dashboard/logout`` clears it. Cookie is HTTPOnly, SameSite=Lax, Secure on HTTPS, lifetime ``COORD_DASHBOARD_SESSION_LIFETIME_SEC`` (default 8h).
- 32 new tests across 4 files: ``test_engineer_tokens.py`` (db layer), ``test_cli_tokens.py`` (CLI), ``test_auth_per_engineer.py`` (HTTP auth), ``test_dashboard_login.py`` (browser login flow).

Operational hardening shipped in v0.29.1-v0.29.3:

- v0.29.1: ``packaging`` added to ``requirements.txt`` (was declared as a runtime dep in pyproject but never carried by ``requirements.txt`` -- transitive chain broke in PR #17's bump).
- v0.29.2: cookie ``Secure`` flag now honours ``X-Forwarded-Proto`` so origins behind a TLS-terminating proxy (Cloudflare, Traefik, nginx, ALB) still mark the cookie Secure.
- v0.29.3: cookie ``Secure`` also honours Cloudflare's ``CF-Visitor: {"scheme":"https"}`` because Traefik strips ``X-Forwarded-Proto`` from untrusted source IPs by default. Plus a ``COORD_DASHBOARD_COOKIE_FORCE_SECURE`` escape hatch for proxy chains that strip both headers.

Operational follow-ups shipped alongside (not feature work):

- Cloudflare Tunnel dual-access pattern: ``coord.mittell.ai`` (HTTPS via tunnel) for off-LAN, ``coord.kebabrack.lan`` (HTTP LAN-direct) for local work. Documented as Option 5 in ``docs/deployment.md``.
- Full ``docs/deployment.md`` TLS section covering 5 patterns (plaintext, Cloudflare Tunnel, Let's Encrypt + cert-manager DNS-01, self-signed CA, dual-access).
- ``.github/workflows/release.yml`` ``bump-manifest`` job now moves the version tag forward to the deployment-ready commit so ``git describe HEAD`` resolves to the current shipped version (was always one tag behind).
- ``.github/dependabot.yml`` ignore rule for ``pydantic-core`` until pydantic itself ships a release that adopts it (pydantic 2.13.4 pins ``pydantic-core==2.46.4`` exactly).
- Windows CI flake fix: three FIFO queue ordering tests in ``test_api.py`` were timing-flaky on Windows; replaced ``asyncio.sleep(0.05)`` with the existing ``_wait_for_queue_id`` poll helper.

### v0.29.4 (shipped) -- Token expiry, rotation, activity tracking

Shipped in v0.29.4 (schema migration v15 on ``engineer_tokens``):

- **Token expiry**: ``expires_at`` column + ``coord tokens create --expires-in 30d`` flag (``m``/``h``/``d``/``w`` units). Auth path 401s on expired tokens with the expiry timestamp and a reissue hint. NULL keeps legacy never-expires semantics.
- **Token rotation with grace period**: ``coord tokens rotate <id> --grace 24h`` mints a successor (``rotated_from`` chain) and keeps the predecessor valid until ``rotation_grace_until``. Refuses revoked/expired/already-rotated predecessors; atomic insert+grace transaction.
- **Token activity tracking**: per-token ``request_count`` + last source IP/UA, bumped best-effort on auth. Surfaced in ``coord tokens list`` with a derived status word per row; dashboard panel lands with the v0.29.x dashboard token UI.
- **Auth consolidation**: the triplicated per-engineer/shared/require-flag pipeline now lives in one ``_authenticate_bearer`` helper; per-engineer-only deployments (no shared token at all) are legal and report ``auth_mode: per_engineer``.

### v0.29.5 (shipped) -- Dashboard token management + CSRF

- **In-dashboard token management**: per-engineer sessions manage their own tokens (list, revoke, create-for-self with an expiry cap at their own token's expiry); shared-token sessions act as operator over all tokens. One-time raw-token page on create; PRG revoke.
- **CSRF tokens**: ``coord_csrf`` double-submit cookie + hidden form field on tokens/create, tokens/revoke, and logout. Login stays CSRF-exempt for curl scripting and gets a soft Origin guard instead.

### v0.29.6 (shipped) -- OIDC SSO

- **SSO/OIDC integration**: dashboard auth through any OIDC IdP (authorization code + PKCE, discovery, JWKS rotation). Successful logins mint short-lived per-engineer tokens so the whole v0.29.4/5 token surface applies; fail-closed allowlist policy for public issuers. Configure via ``COORD_OIDC_*`` env vars; see docs/deployment.md.

## v0.28.0 (shipped) -- Queue QoS + housekeeping

v0.28 was originally sized for "multi-namespace coordination" (the v0.28 section below). That body of work was deferred to a later release pending actual multi-tenant demand. Instead v0.28.0 pulls in four low-hanging items from the v0.29 queue QoS bucket and the future bucket's "auto-cleanup of dead engineer IDs" entry, all of which fit in a no-schema-migration release.

Shipped in v0.28.0:

- Backpressure header on every authenticated response. ``X-Coord-Queue-Depth: N`` is attached when the request carries an engineer signal (``X-Coord-Engineer`` header or ``engineer`` query param). N is that engineer's currently-queued waiting-claim count. Lets clients self-regulate without an extra round trip. Toggle via ``COORD_BACKPRESSURE_HEADER`` (default on).
- Queue fairness pass on ``db.pop_next_waiting_queue_entry``. Every ``COORD_QUEUE_FAIRNESS_INTERVAL``-th call (default 10) bypasses the priority CASE entirely and pops by raw FIFO position. Anti-starvation guarantee for low/normal-priority waiters under a steady stream of high/blocking traffic. Set to 0 to disable.
- Priority decay -- counterpart to the v0.26 age boost. A waiting entry's effective priority drops one level per ``COORD_QUEUE_PRIORITY_DECAY_SEC`` seconds (default 300; floor at ``low``). Prevents a misclassified urgent request from monopolising the queue head indefinitely. Set to 0 to disable.
- Stale engineer housekeeping. New ``coord engineers stale [--release]`` CLI subcommand + dashboard panel surface engineers whose most-recent activity is older than ``COORD_STALE_ENGINEER_DAYS`` (default 7). ``--release`` drops their lingering active claims. Solves the "abandoned worktree leaves dangling claims" issue.

No schema migration; all four are config + behavioural additions. Multi-namespace coordination (the original v0.28 plan) is deferred to v0.29 or v0.30 depending on which ships first when demand surfaces -- the design notes below stand.

## v0.27 -- Notifications and integrations

The hotspot lifecycle is operationally complete (detect -> promote -> demote -> queue -> cancel) but consumers of those events were all *pulling* via dashboard polling or `/metrics/*` queries. v0.27 turns those into *pushable* events so external systems (Slack, PagerDuty, GitHub PR bots) can react in real time.

Shipped in v0.27.0:

- Webhook delivery for `auto-promote`, `auto-promote-subtree`, `auto-demote`, `auto-coexist`, `auto-narrow`, `claim_granted`, `queue_grant`, and `queue_cancel` events. Configured via `COORD_WEBHOOK_URL` + an HMAC-SHA256-signed payload (`X-Coord-Signature` header) so the receiver can verify provenance against `COORD_WEBHOOK_SECRET`.
- Per-event-type subscription filter via `COORD_WEBHOOK_EVENTS` (comma-separated allowlist; empty or unset means "all").
- Outbox table for retry-on-failure (`webhook_outbox`, schema v13) with exponential backoff capped at `COORD_WEBHOOK_MAX_RETRIES` (default 5), so a transient receiver outage does not lose events; rows that exhaust their retry budget land in the `exhausted` state and are visible in the dashboard's "webhook delivery (24h)" panel.
- Five new db helpers (`enqueue_webhook`, `list_pending_webhooks`, `mark_webhook_delivered`, `mark_webhook_failed`, `webhook_delivery_stats`) and a background delivery loop wired into `main.py`'s lifespan, gated on `COORD_WEBHOOK_URL`.

Follow-ups deferred to v0.27.x (or rolled into v0.28's external-integrations theme):

- A first-party Slack adapter that turns the webhook payloads into a single channel message stream. Optional bot mode that responds to `/coord pending` and `/coord queue` slash commands.
- GitHub PR comment integration: when a pre-push hook 409s, the next push that does succeed includes a comment on the PR description listing the files that bounced and which engineer held them. Closes the "why did my PR sit unmerged" feedback gap.
- Outbox retry CLI: `coord outbox retry --exhausted` and `coord outbox stats` so an operator can drive the retry rotation by hand after fixing a receiver, without poking at SQLite directly.

## Multi-namespace coordination (deferred from v0.28)

Today coord is single-tenant per instance: one ownership YAML, one claim table, one queue. This body of work scales to multi-repo and multi-team usage without forcing a separate coord instance per topology. Originally sized for v0.28, deferred to v0.29 or v0.30 (whichever ships first) pending actual multi-tenant demand surfacing in production telemetry.

Candidate items:

- Per-repo ownership rules: ownership YAML gains a top-level `repos:` key mapping `repo_id -> {modules: ..., shared_files: ...}`. The legacy global rules still apply as a fallback when the repo is not listed.
- Cross-repo claim references: a claim can declare `blocks: ["amittell/api#src/billing/**"]` to express that work in repo A semantically depends on work in repo B not being touched. The conflict engine flags overlap across both repos.
- Per-team views in the dashboard: `?team=` filter on every list endpoint. Maps `engineer -> team` via an `engineer_team` mapping in the ownership YAML.
- Multi-tenant API tokens: `COORD_TENANT_TOKENS` env supports a JSON map of `{tenant_id: bearer_token}` so a single coord instance can fan out to two organisations without leaking visibility.

Risk: namespace design has lots of decisions (URL shape, tenant isolation level, billing surface) that will pull engineering time. May benefit from a separate design doc (`docs/design/multi-namespace.md`) before implementation.

## v0.30.0 (shipped) -- Queue quality of service (continued)

Shipped: ``COORD_MAX_CLAIMS_PER_ENGINEER`` (active-claim cap, 429 + computed ``Retry-After``), ``COORD_MAX_QUEUED_PER_ENGINEER`` (queue-entry cap at enqueue), ``COORD_MAX_QUEUE_DEPTH_PER_REPO`` (per-repo admission control with a service-degraded hint). All default 0 = disabled. At-cap engineers can still queue; a queue grant that would breach the cap expires that entry and the drain continues to the next waiter. The MCP ``claim_files`` tool surfaces 429s as structured data.

Design deviation from the original sketch, on purpose: limits key on the request-body engineer (the identity claims are stored under -- in production one per-engineer token fronts 20+ worktree agents with distinct body engineers, so token-keyed caps would lump them into one bucket). The authenticated-token ceiling that closes the spoof bypass needs a migration adding token attribution to claim rows; deferred until that migration carries its weight.

## v0.31.0 (shipped) -- Language-server-aware claims

The v0.14-v0.17 symbol claims rely on tree-sitter for parsing, which extracts structure but does not know about types, callsites, or refactor safety. v0.31.0 elevates the symbol claim from "lexical match on a name" toward "semantic match on a definition", flag-gated behind ``COORD_LSP_ENABLED`` (default off; tree-sitter behavior is byte-identical when off).

Shipped (schema migration v16):

- **LSP integration**: in-process async client pool (one ``pylsp``/``typescript-language-server``/``gopls`` child per language+repo root, JSON-RPC/stdio, idle reaping, per-server circuit breaker). Claim-time definition spans upgrade to LSP ranges (``resolved_by: lsp``); validation accepts LSP-confirmed symbols the parser missed, never the reverse. The "persistent sidecar" of the original risk note became this pool -- no separate binary.
- **Definition spans persisted always**: parser spans land in ``claim_symbols`` whenever ``COORD_REPO_ROOT`` is set, LSP or not; the dashboard renders ``file.py::sym (lines 10-42)``.
- **Callsite-aware overlap (advisory)**: granted symbol claims record callsites via a post-grant background enrichment pass; later claims by other engineers covering those callsites still grant but carry an advisory warning. Advisory rather than the originally-sketched hard ``coexist`` flag because callsite data goes stale and references depend on indexing state.
- **Symbol rename auto-follow**: a bounded background sweep follows unambiguous renames (same kind, same parent, span overlap, exactly one candidate) atomically across claim_symbols, the claim pattern, an audit row, a ``symbol_renamed`` webhook, and a dashboard note. The roadmap's "query the LSP rename refactor preview" was reinterpreted: that API works pre-apply, not post-hoc, so detection is span-anchored heuristics with a hard ambiguity stop.
- **Cross-file refactor claims**: ``POST /claims/refactor`` / MCP ``claim_refactor`` reserve the definition plus every callsite's enclosing symbol as one normal claims batch (conflicts/queue/rate limits unchanged; 503 without a live language server).

## v0.31.1-v0.31.2 + v0.32.x (shipped) -- Wiring hygiene, the user-scoped MCP model, and maintenance

No roadmap feature landed between v0.31.0 and v0.32.4. This line is operational hardening, a deliberate change to how coord wires itself into a repo, a Windows correctness fix, and routine dependency maintenance. It is recorded here so the roadmap reflects what actually shipped, not because any of it was a planned feature.

The throughline of v0.31.1 -> v0.32.2 is a single root cause: coord's generated wiring was being swept into unrelated contributor PRs, and agents were then building worktree workarounds instead of using coord. The fix moved coord to a **user-scoped MCP model** and made its machine config untracked, then taught ``coord doctor`` to read the new model without false failures.

- **v0.31.1 -- Prettier exemption.** ``coord init`` / ``coord upgrade`` detect a repo's Prettier usage and add coord's generated ``.mcp.json`` / ``.cursor/mcp.json`` (2-space JSON with placeholders) to a managed ``.prettierignore`` block, so a repo running ``prettier --check`` in CI no longer fails on coord's wiring. No-op for non-Prettier repos.
- **v0.31.2 -- Commit-risk warning.** Onboarding warns when it writes tracked wiring on a non-default branch or with an already-staged index -- the exact setup where a later ``git add -A`` sweeps coord into an unrelated PR. Advisory, never blocking; names only committable files and prints the safe ``git add``.
- **v0.32.0 -- Untracked machine config + user-scoped server.** coord's generated machine config (``.mcp.json``, ``.cursor/mcp.json``, ``.codex/config.toml``) is no longer tracked; ``coord upgrade`` untracks any an older version committed. Recommended setup is now one user-scoped server (``claude mcp add --scope user coord coord-mcp``) that resolves each repo's URL/token/repo-id from that repo's gitignored ``.coordination/local.env`` at startup -- so no tracked MCP config exists to leak into a PR. Only protocol docs and the ``.gitignore`` block stay tracked.
- **v0.32.1 -- Windows LSP path fix.** ``Path.relative_to`` compares path components case-sensitively, so on Windows it wrongly rejected under-root paths differing only in drive-letter / component case, breaking callsite enrichment, the overlap advisory, and ``claim_refactor`` de-duplication (``windows-latest`` CI had been red since v0.31.0). Replaced the three under-root checks with a shared ``relpath_under_root`` helper (``os.path.relpath`` over realpath'd operands; case-insensitive, cross-drive safe, stable across the macOS ``/var`` symlink). No behavior change on Linux/macOS; prod is unaffected (Linux, ``COORD_LSP_ENABLED`` off).
- **v0.32.2 -- doctor hardening.** ``coord doctor`` exits non-zero only when coordination is genuinely broken. Per-repo MCP config is now optional when a user-scoped coord MCP server is registered; the protocol block is accepted in ``CLAUDE.md`` *or* ``AGENTS.md`` and a missing one is a WARN (it arrives with the next default-branch merge); stale ``sessions.live`` dead-PID rows are a WARN, pruned on read.
- **v0.32.3 / v0.32.4 -- dependency maintenance.** Pinned ``requirements.txt`` bumps from grouped Dependabot PRs so the container image ships current runtime deps: cryptography 49, fastapi 0.137.2, starlette 1.3.1, anyio 4.14.0, certifi 2026.6.17, mcp 1.28.0. Full suite green across the matrix on each.

Maintenance policy (adopted 2026-06-19): routine dependency patches land on ``main`` to keep the image source current but do **not** trigger a standalone tagged release; they reach prod with the next substantive release. Security-relevant bumps (e.g. cryptography) and actual code changes still cut a release. This stops one prod redeploy per micro-bump while keeping deps current.

Also out-of-band in this window: the ``make test`` target gained a ``pgrep`` guard that refuses to start when another project pytest is already running, after a stale background run deadlocked the pre-push ``make check``.

## Future bucket (no version assigned)

Items worth tracking but not yet sized:

- **Postgres backend** (deferred from v0.26 candidate list). Removes the SQLite single-writer ceiling, unlocks LISTEN/NOTIFY to eliminate the v0.24 0.5s poll interval on the cross-process queue, enables proper hot-standby HA. Estimated 2-3 weeks of dual-backend engineering plus a 2-3x test-suite slowdown for the per-test Postgres fixture. Worth it when deployment topology shifts to multi-tenant or multi-replica + high-throughput.
- **Symbol-level coexist**: today `coexist` lets two claims share a file scope at the holder's discretion. Extend to let two symbol claims share a single symbol-scope file with explicit boundaries (`coexist_pattern` becomes `coexist_symbols`).
- **Conflict-prediction ML**: feed the conflict_log + claim history into a small model that predicts which pending claims are likely to 409. Surface in the dashboard so an operator can proactively split a module before the storm hits.
- **Activity replay / debug mode**: a `coord replay` CLI that re-runs every claim and decision from a captured timeline. Useful for reproducing tricky race conditions and validating proposed conflict-engine changes against historical traffic.
- **Operator approval workflow** (alternative to v0.22 hard auto-promote): instead of writing immediately, the conflict pipeline files a `pending_promotion` row that the operator approves via the dashboard. Bridges the gap between v0.21 fully-soft and v0.22 fully-automatic.
- **Postgres-only optimisations** (post-Postgres-backend): NOTIFY-driven queue grant (zero poll latency), partitioned conflict_log for repos with >1M attempts per month, read-replica routing for the dashboard.

## Done in the v0.13 -> v0.29 arc (for context)

This roadmap supersedes the older "candidate" markers in the v0.14 sub-file claims design doc. Everything below is shipped and lives in [CHANGELOG.md](../../CHANGELOG.md) for the full detail:

| Tag | Theme |
|---|---|
| v0.14.x | sub-file claims experimental (TS) + dashboard auto-resolution count |
| v0.15.0 | Python + Go parsers, stable |
| v0.16.0 | method-level claims (`Foo::handleA`) |
| v0.17.0 | recursive nesting + server + client validation |
| v0.18.0 | auto-resolution 30d heatmap |
| v0.19.0 | TS recursive parser |
| v0.20.0 | hotspot file detection |
| v0.21.0 | soft auto-promote + FIFO queue |
| v0.22.0 | hard auto-promote + queue visibility |
| v0.23.0 | auto-demote (closes the v0.22 ratchet) |
| v0.24.0 | cross-process FIFO queue backend |
| v0.25.0 | permanent shared-file pin + queue priority hints |
| v0.26.0 | subtree auto-promote + priority age boost + queue cancellation |
| v0.27.0 | webhook outbox + HMAC delivery + dashboard panel |
| v0.28.0 | backpressure header + queue fairness + priority decay + stale engineer cleanup |
| v0.29.0 | per-engineer bearer tokens + dashboard login UI + cookie session |
| v0.29.1 | hotfix: packaging dep in requirements.txt |
| v0.29.2 | cookie Secure honours X-Forwarded-Proto |
| v0.29.3 | cookie Secure also honours CF-Visitor (Traefik strips XFP) + force-secure escape hatch |
| v0.29.4 | token expiry + rotation with grace + activity tracking + auth consolidation (schema v15) |
| v0.29.5 | dashboard token management panel + CSRF double-submit cookie + login Origin guard |
| v0.29.6 | OIDC SSO: code+PKCE flow, SSO logins mint short-lived per-engineer tokens |
| v0.30.0 | per-engineer claim/queue caps + per-repo queue admission control (429 + Retry-After) |
| v0.31.0 | LSP-aware symbol claims: spans, callsite advisories, rename auto-follow, claim_refactor (schema v16) |
| v0.31.1 | onboarding exempts coord's generated config from a repo's Prettier check (managed .prettierignore block) |
| v0.31.2 | onboarding warns when writing tracked wiring on a non-default branch / staged index (PR-pollution guard) |
| v0.32.0 | machine config (.mcp.json/.cursor/.codex) untracked + gitignored; user-scoped coord-mcp the recommended setup |
| v0.32.1 | Windows LSP path fix: relpath_under_root replaces Path.relative_to (windows-latest CI green again) |
| v0.32.2 | coord doctor hardening: user-scoped MCP optional, CLAUDE.md-or-AGENTS.md block, dead-PID rows are WARN |
| v0.32.3 | dependency maintenance: cryptography 49, fastapi 0.137.0, starlette 1.3.1 |
| v0.32.4 | dependency maintenance: anyio 4.14.0, certifi 2026.6.17, fastapi 0.137.2, mcp 1.28.0 |
