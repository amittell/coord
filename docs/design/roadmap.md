# Coord roadmap

Status: living document
Last updated: 2026-06-11 (after v0.29.3)

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

### v0.29.x candidate follow-ups

The per-engineer token surface opened up several near-term enhancements:

- **Token expiry**: ``engineer_tokens.expires_at`` column + ``coord tokens create --expires-in 30d`` flag. Auth path 401s on expired tokens with a hint to reissue. Same pattern as GitHub PATs.
- **Token rotation with grace period**: a way to issue ``v2`` of a token before revoking ``v1``, with both valid during the rotation window. Useful for rotating tokens in tools that cache them.
- **In-dashboard token management UI**: a logged-in engineer can view their own tokens, revoke them, and generate new ones from the dashboard. Today it is CLI-only on the server.
- **Token activity log**: per-token request count + last-source-IP/UA. Surfaces in the dashboard so operators can spot "this token has not been used in a month" or "this token is being used from an unexpected location".
- **CSRF tokens** for state-changing dashboard operations. ``SameSite=Lax`` already blocks cross-site POSTs, but a per-form CSRF token adds defense in depth against the SameSite=None opt-out future.
- **SSO/OIDC integration**: an alternative to per-engineer tokens where dashboard auth proxies through an external identity provider (Google, GitHub, Okta) and tokens are minted automatically.

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

## v0.30 -- Queue quality of service (continued)

v0.21-v0.28 covered priority, age boost, cancellation, fairness, decay, and the backpressure header. v0.30 (originally planned as v0.29 before per-engineer tokens jumped the queue) picks up the remaining queue-QoS items that need either schema or behavioural changes too disruptive to fit in the v0.28 no-migration release. The v0.29 per-engineer token work actually makes this easier: rate limiting now has a reliable per-engineer identity at auth time, not just a request-body engineer field that any holder of the shared token could lie about.

Candidate items:

- Per-engineer rate limiting: an engineer cannot have more than N active claims or more than M queued requests at once (`COORD_MAX_CLAIMS_PER_ENGINEER`, `COORD_MAX_QUEUED_PER_ENGINEER`). Returns 429 with a `Retry-After` header. With per-engineer tokens the limit key is the authenticated engineer (not the engineer field in the body), which closes the obvious bypass.
- Per-repo claim quotas mirroring the v0.4 max-claim-ratio but at the queue layer: a repo whose queue is > N deep refuses new wait_seconds requests with a "service degraded" hint, surfacing pushback at the API instead of letting waiters pile up.

Risk: rate-limiting interacts with the v0.5 session_id self-exclusion and the v0.10 multi-session activity ping in ways that need careful testing. Coord must not 429 an agent's own subagents.

## v0.31 -- Language-server-aware claims

The v0.14-v0.17 symbol claims rely on tree-sitter for parsing, which extracts top-level structure but does not know about types, callsites, or refactor safety. v0.31 elevates the symbol claim from "lexical match on a name" to "semantic match on a definition".

Candidate items:

- LSP integration via `pylsp`, `typescript-language-server`, `gopls`: when the server has access to the application repo (`COORD_REPO_ROOT` set), it spawns an LSP and asks for the canonical definition span. The claim covers the byte range the LSP reports, not the tree-sitter approximation.
- Symbol rename detection: when a tracked symbol gets renamed (`handleLogin` -> `handleAuth`), the active claim auto-follows by querying the LSP's rename refactor preview. Today the claim becomes a phantom; v0.30 makes it self-updating.
- Callsite-aware overlap: claiming `handleLogin` also reserves every callsite of `handleLogin`. Two agents both modifying callers of `handleLogin` would be flagged for a `coexist` decision.
- Cross-file refactor claims: `claim_refactor("rename src/auth/handleLogin -> handleAuth")` reserves the symbol and every callsite across the repo in one shot.
- Symbol-aware diff display in the dashboard: when an active symbol claim exists, render the symbol's source range with line numbers so the operator can see exactly what is locked.

Risk: LSP integration is heavyweight (spawning + warming up a language server on every conflict check is unworkable). Will need a persistent LSP-bridge process model, probably as a sidecar to the coord service.

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
