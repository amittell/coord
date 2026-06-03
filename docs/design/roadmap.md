# Coord roadmap

Status: living document
Last updated: 2026-06-03 (after v0.26.0)

This is the post-v0.26 forward look. Items here are candidates, not commitments -- order and scope move as production telemetry and operator feedback arrive. Entries already shipped live in [CHANGELOG.md](../../CHANGELOG.md); see also [docs/design/sub-file-claims.md](./sub-file-claims.md) for the v0.14-v0.26 arc design.

## v0.27 -- Notifications and integrations

The hotspot lifecycle is operationally complete (detect -> promote -> demote -> queue -> cancel) but consumers of those events are all *pulling* via dashboard polling or `/metrics/*` queries. v0.27 turns those into *pushable* events so external systems (Slack, PagerDuty, GitHub PR bots) can react in real time.

Candidate items:

- Webhook delivery for `auto-promote`, `auto-demote`, `auto-coexist`, `auto-narrow`, `claim_granted`, `queue_grant`, and `request_release` events. Configurable via `COORD_WEBHOOK_URL` + an HMAC-signed payload so the receiver can verify provenance.
- Per-event-type subscription filter (default subscribes to all; operators dial down noise via `COORD_WEBHOOK_EVENTS=auto-promote,queue_grant`).
- A first-party Slack adapter that turns the webhook payloads into a single channel message stream. Optional bot mode that responds to `/coord pending` and `/coord queue` slash commands.
- GitHub PR comment integration: when a pre-push hook 409s, the next push that does succeed includes a comment on the PR description listing the files that bounced and which engineer held them. Closes the "why did my PR sit unmerged" feedback gap.
- Outbox table for webhook delivery retry: failed webhook POSTs land in a `webhook_outbox` row with exponential-backoff retry so a transient Slack outage does not lose events.

Risk: webhook delivery is async and external, which complicates the test surface. Will need a fake HTTP receiver fixture in `tests/`.

## v0.28 -- Multi-namespace coordination

Today coord is single-tenant per instance: one ownership YAML, one claim table, one queue. v0.28 scales to multi-repo and multi-team usage without forcing a separate coord instance per topology.

Candidate items:

- Per-repo ownership rules: ownership YAML gains a top-level `repos:` key mapping `repo_id -> {modules: ..., shared_files: ...}`. The legacy global rules still apply as a fallback when the repo is not listed.
- Cross-repo claim references: a claim can declare `blocks: ["amittell/api#src/billing/**"]` to express that work in repo A semantically depends on work in repo B not being touched. The conflict engine flags overlap across both repos.
- Per-team views in the dashboard: `?team=` filter on every list endpoint. Maps `engineer -> team` via an `engineer_team` mapping in the ownership YAML.
- Multi-tenant API tokens: `COORD_TENANT_TOKENS` env supports a JSON map of `{tenant_id: bearer_token}` so a single coord instance can fan out to two organisations without leaking visibility.

Risk: namespace design has lots of decisions (URL shape, tenant isolation level, billing surface) that will pull engineering time. May benefit from a separate design doc (`docs/design/multi-namespace.md`) before implementation.

## v0.29 -- Queue quality of service

v0.21-v0.26 covered priority, age boost, and cancellation, but production traffic at scale will surface starvation, monopoly, and backpressure issues the current model does not address.

Candidate items:

- Priority decay (counterpart to age boost): a `blocking` claim that has been queued for `COORD_QUEUE_PRIORITY_DECAY_SEC` (default 300) drops to `high`, then `normal` after another decay window. Prevents a misclassified urgent request from monopolising the head of the queue forever.
- Per-engineer rate limiting: an engineer cannot have more than N active claims or more than M queued requests at once (`COORD_MAX_CLAIMS_PER_ENGINEER`, `COORD_MAX_QUEUED_PER_ENGINEER`). Returns 429 with a `Retry-After` header.
- Per-repo claim quotas mirroring the v0.4 max-claim-ratio but at the queue layer: a repo whose queue is > N deep refuses new wait_seconds requests with a "service degraded" hint, surfacing pushback at the API instead of letting waiters pile up.
- Backpressure signals on every response: `X-Coord-Queue-Depth` header counts the requester's current queue position so the client can decide whether to wait or escalate.
- Fairness pass on `pop_next_waiting_queue_entry`: every Nth pop ignores priority and pops by raw FIFO to guarantee `low`-priority waiters eventually win.

Risk: rate-limiting interacts with the v0.5 session_id self-exclusion and the v0.10 multi-session activity ping in ways that need careful testing. Coord must not 429 an agent's own subagents.

## v0.30 -- Language-server-aware claims

The v0.14-v0.17 symbol claims rely on tree-sitter for parsing, which extracts top-level structure but does not know about types, callsites, or refactor safety. v0.30 elevates the symbol claim from "lexical match on a name" to "semantic match on a definition".

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
- **Auto-cleanup of dead engineer IDs**: when an engineer has had no `claim_files` or `list_claims` call for N days, surface a "stale engineer" warning in the dashboard and offer a one-click cleanup that releases their lingering claims. Catches abandoned worktrees.
- **Postgres-only optimisations** (post-Postgres-backend): NOTIFY-driven queue grant (zero poll latency), partitioned conflict_log for repos with >1M attempts per month, read-replica routing for the dashboard.

## Done in the v0.13 -> v0.26 arc (for context)

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
