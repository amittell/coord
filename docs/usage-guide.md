# Usage Guide

This guide covers the normal workflow once the service is already deployed.

## Core concepts

- A claim reserves a file path, glob, or shared file for a period of time.
- A conflict check asks whether a planned edit overlaps with someone else's active claim.
- A shared-file claim is for intentionally shared hotspots such as a routing table, schema index, or app shell.
- Ownership config marks certain areas as `soft` or `hard` so the service can flag higher-risk boundaries.

## Recommended claim lifecycle

1. Check conflicts on the files you expect to touch.
2. Create a claim before making edits.
3. Extend the claim if the work will outlive the TTL.
4. Release the claim as soon as the branch is pushed or the task is abandoned.

## Engineer naming convention

Use an engineer ID that is unique per active worker, not just per human.

Good:

- `alex/claude/main`
- `alex/claude/refactor`
- `alex/codex/test-fix`

Risky:

- `alex`

Why this matters: claims from the same `engineer` are intentionally ignored during conflict checks. If you reuse one ID for multiple simultaneous workers, those workers will not block each other.

From v0.5.0 onward, `coord-mcp` also generates a per-process `session_id` and tags every claim with it. The conflict check additionally self-excludes any active claim sharing your session_id, regardless of engineer name. This handles the common case of one Codex/Claude session spawning multiple subagents under different engineer names — they all share the same `session_id` and so don't false-conflict against each other. Different sessions stay adversarial.

## Repo identifiers (v0.3.0+)

`coord-mcp` automatically attaches a `repo` identifier (e.g. `amittell/coord`) to every claim it creates, derived by `coord init` from `git remote get-url origin` and stored as `COORD_REPO_ID` in `.coordination/local.env`. The conflict check is repo-scoped: a claim with `repo=A` only conflicts against other `repo=A` claims, so the same path pattern in two unrelated repos sharing one coord instance won't false-positive.

Pre-v0.3 claims (and any `claim_files` call without a repo) live in a legacy `repo=NULL` bucket that's self-consistent: NULL conflicts with NULL, never with tagged claims.

The `/repos` endpoint and the dashboard's "repositories" panel surface aggregate activity per repo.

## Claim sizing guidance

Prefer the narrowest claim that still reflects the real work.

Good:

- `src/auth/login.ts`
- `src/auth/**`
- `apps/web/app/api/billing/**`

Avoid:

- `src/**`
- `**/*`

Very broad claims are rejected when `COORD_REPO_ROOT` is configured and the claim exceeds the configured scope limits.

## Symbol-level claims (v0.14+)

When several agents need to coexist on different parts of one hot file (a multi-route handler, a shared component module, a feature-flag dispatch table), file-scope claims force them to serialise even though the actual edits don't overlap. Symbol-scope claims push the unit of coordination one level down: the claim covers only the named top-level declarations, and disjoint symbol sets on the same file resolve as `AUTO_COEXIST` without filing a request.

When to reach for them:

- TypeScript files where two or more agents predictably want different functions, classes, or types within the same file.
- A file claim that keeps getting `409`'d on hotspots where the work is actually disjoint.

When file-scope is still the right call:

- Small files where the whole content effectively moves together.
- Files in languages without a v0.14 parser (Python, Go land in v0.15; everything else stays file-scope).
- Edits that touch imports, top-level statements outside any declared symbol, or anything that changes module shape. Symbol claims do NOT cover module-level code -- you need a file claim for that, even if you also hold a symbol claim on the same file.

Passing `symbols` on `claim_files` (a `dict[str, list[str]]` keyed by file path, values are symbol names within that file) flips the claim to `scope_type='symbol'`. Without `symbols`, behaviour is identical to pre-v0.14.

The `narrowable` flag controls whether a file claim can be auto-narrowed by an incoming symbol claim. Defaults:

- `file` claims: `narrowable=true`. A symbol-scope requester arriving against your file claim triggers `AUTO_NARROW`: both claims live, your effective scope shrinks to "file minus the partner's symbols", and you get a `pending_requests` notice on your next poll.
- `shared_file` claims: `narrowable=false`. The whole point is to make overlap visible.
- `module` claims: `narrowable=false`. Coarse scope was deliberate.
- `symbol` claims: `narrowable=false`. Already at the leaf level.

Pass `narrowable=false` on a file claim to opt out: an incoming symbol claim then has to file an explicit `request_release`. See [./design/sub-file-claims.md](./design/sub-file-claims.md) for the overlap algorithm, parser strategy, and migration notes.

### Method-level claims (v0.16+)

For large classes, structs, or Go receiver types where multiple agents predictably touch different methods (a `Router` with 30 routes, a `Database` with a dozen query helpers), v0.16 pushes the unit of coordination one more level down: methods become individually claimable using a `Parent::child` notation in the `symbols` list.

```python
claim_files(
    engineer="alex/claude/main",
    patterns=["src/auth/router.ts"],
    symbols={"src/auth/router.ts": ["Router::handleAuth"]},
)
```

The service splits at insert time and stores `Router` as the parent and `handleAuth` as the leaf. Two-level prefix-matching overlap rules:

- **Sibling methods auto-coexist.** Two agents on `Router::handleAuth` and `Router::handleLogout` are granted simultaneously via `AUTO_COEXIST` -- same posture as two disjoint top-level symbol claims on the same file.
- **Class blocks all its methods (and vice versa).** A claim on the bare `Router` (no `::`) covers every `Router::*` method; an incoming method claim against an existing bare-class claim hits `SYMBOL_OVERLAP` and `409`s. Symmetrically: holding `Router::handleAuth` blocks an incoming claim on bare `Router`.
- **Different parents are disjoint.** `Router::handle` and `Logger::handle` share a leaf name but no parent; they coexist freely.

When to reach for method scope:

- A file with one big class / struct that several agents predictably need different methods of.
- A symbol-scope claim on the parent that keeps getting `409`'d because the actual edits are on disjoint methods.

Limitations in v0.16: two-level only. Nested classes (`Outer::Inner`) and nested namespaces are NOT yet supported and will be parsed as a single two-level pair. Top-level symbols whose name happens to contain `::` should be avoided as claim targets until v0.17.

### Hotspot file suggestions (v0.20+)

The dashboard surfaces a "Hotspot files (30d)" panel that ranks files by how often agents have `409`'d on them in the last 30 days, grouped per repo. Each row carries the attempt count, the number of distinct attempting engineers, and a suggested-action chip:

- **split into modules** -- attempts well above the threshold; the file is doing too much and the underlying conflict will keep recurring until it's broken up.
- **promote to `shared_file`** -- attempts above the threshold but the file is genuinely shared (lockfiles, routing tables, schema index); switching the claim type to `shared_file` makes the overlap explicit.
- **monitor** -- just above `min_attempts`; not actionable yet, but worth watching.

The signal is read-only in v0.20 -- nothing happens automatically. Auto-promote ships in v0.21 (see below). The same series is exposed at `GET /metrics/hotspots?days=30` for external monitoring (Prometheus scrapes, weekly digest emails, etc.); see [./api-reference.md](./api-reference.md) for query params and response shape.

#### Applying the suggestion (v0.21+)

The "promote to `shared_file`" and "split into modules" chips on the dashboard hotspot rows now have actionable counterparts. POST to `/metrics/hotspots/promote` with the chosen action and pattern to write the corresponding rule into the active `owners.yaml`:

```bash
curl -X POST http://127.0.0.1:8080/metrics/hotspots/promote \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "shared_file",
    "pattern": "package-lock.json",
    "repo": "amittell/coord",
    "note": "weekly digest 2026-06-02"
  }'
```

`action` is `"shared_file"` (promote the pattern to a `shared_file` rule) or `"split"` (annotate the pattern for a follow-up modularisation pass). Idempotent: applying the same action+pattern twice is a no-op. Operator still in the loop -- v0.21 only writes when actively poked, never on its own.

#### Hard auto-promote (v0.22+)

v0.22 adds an opt-in autopilot for the `shared_file` promotion. Two env vars on the service control it:

- `COORD_AUTO_PROMOTE_THRESHOLD` (int, default `0` -- disabled). When set to `N > 0`, the conflict pipeline auto-writes a `shared_file` rule into `owners.yaml` whenever a file's blocked-claim attempts cross `N` within the rolling window.
- `COORD_AUTO_PROMOTE_WINDOW_DAYS` (int, default `7`). The look-back window in days for the attempt count.

Each auto-promotion is idempotent (the same pattern is never written twice) and is recorded as an `auto-promote` row in `request_events` for audit: the `detail` JSON carries the `pattern`, `threshold`, and `window_days` that triggered it. Leaving the threshold at `0` keeps v0.21 behaviour (operator-only writes via `POST /metrics/hotspots/promote`).

## Queueing claims (v0.21+)

When `claim_files` would `409` against an active holder, the requester historically had to retry on a timer or file an explicit `request_release`. v0.21 adds a third option: pass `wait_seconds` and the service FIFO-queues the requester behind the blocking claim, long-polling for the holder to release.

```python
claim_files(
    engineer="alex/claude/main",
    patterns=["src/auth/login.ts"],
    description="auth refactor",
    wait_seconds=30,
)
```

When the holder releases (manual `release_claims`, TTL expiry, request approval, or a `narrowed` / `coexist` decision), the service drains the FIFO and auto-grants the next entry. Multiple queued requesters are served in arrival order. The server caps `wait_seconds` at 600s; pass `0` (or omit) to preserve the immediate-409 behaviour from v0.13-v0.20. The MCP wrapper accepts `wait_seconds` directly on `claim_files`; the same field exists on `POST /claims` (see [./api-reference.md](./api-reference.md)).

### Inspecting the queue (v0.22+)

`GET /requests?queued=true` returns the live FIFO queue rows joined with the blocking holder's engineer and pattern, so "who am I waiting on?" is a single round-trip:

```bash
curl "http://127.0.0.1:8080/requests?queued=true" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN"
```

Filter to a single repo by adding `&repo=amittell/coord`. Each row carries `kind`, `queue_id`, `blocking_claim_id`, `blocking_engineer`, `blocking_pattern`, `requester_engineer`, `requester_pattern`, `position` (FIFO index, 0 = head), `state`, `enqueued_at`, and `expires_at`. The MCP wrapper exposes the same filter as `my_requests(queued=True)` for use from an agent session.

The dashboard surfaces the same data as a "pending queue" panel per repo with depth and the head-of-queue waiter so an operator can see at a glance which hotspots are accreting a queue.

## Monorepo wiring: `coord init --root`

In a monorepo, `coord init` defaults to the enclosing git work tree root, which is usually the whole repo. If several services share one repo and each service wants its own `.coordination/` directory (per-service ownership rules, per-service MCP wiring, per-service pre-push hook), pass `--root` to tell `init` where to place the generated files:

```bash
coord init --tool claude --mode local --yes --root apps/web
coord init --tool claude --mode local --yes --root services/foo
```

The argument can be an absolute path or a path relative to the current working directory. It must exist and live inside a git work tree (it does not have to be the work-tree root). Run `init` once per service you want to coordinate; each run produces an independent `.coordination/config.toml`, `local.env`, and `owners.yaml` under that service directory, and installs the pre-push hook shim at the repo's shared `.git/hooks/pre-push`.

## Shared file workflow

Use `shared_file` claims for files that many branches touch by necessity, for example:

- `package-lock.json`
- `pnpm-lock.yaml`
- `db/schema.sql`
- `app/router.ts`

These claims get a shorter TTL by default so they do not stay reserved for long.

## Example HTTP workflow

Check conflicts:

```bash
curl "http://127.0.0.1:8080/conflicts?engineer=alex/claude/main&pattern=src/auth/login.ts" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN"
```

Create a claim:

```bash
curl -X POST http://127.0.0.1:8080/claims \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "engineer": "alex/claude/main",
    "branch": "alex/auth-refresh",
    "description": "refreshing login flow",
    "claims": [{"type": "file", "pattern": "src/auth/**"}]
  }'
```

Extend a claim:

```bash
curl -X POST http://127.0.0.1:8080/claims/<claim-id>/extend \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"engineer": "alex/claude/main", "ttl_hours": 2}'
```

Release a claim:

```bash
curl -X POST http://127.0.0.1:8080/claims/release \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"claim_ids": ["<claim-id>"], "engineer": "alex/claude/main"}'
```

Release every claim from one MCP session in a single call (v0.5.0+):

```bash
curl -X POST "http://127.0.0.1:8080/sessions/<session-id>/release" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN"
```

Useful at end-of-work — `coord-mcp`'s `release_session` tool wraps this so the agent doesn't have to track every subagent's claim ids.

## Release requests (v0.9.0+)

When `claim_files` returns a `409` and the work is urgent, the requester can file an explicit request asking the holder to release. Filing shortens the holder's claim TTL to `min(remaining, COORD_REQUEST_TTL_SHORT_SEC)` (default 300s) so the claim is forced to a near-term decision.

```bash
# Requester files
curl -X POST http://127.0.0.1:8080/requests \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "claim_id": "<active claim id>",
    "requester": "alex/claude/r2",
    "reason": "hot fix #1234",
    "urgency": "high",
    "wait_seconds": 60
  }'
```

By default the call long-polls for up to `wait_seconds` (60s) so a quick decision returns the answer in the same request. Pass `wait_seconds=0` to fire-and-forget; use `GET /requests/<id>` later to check status.

The holder responds:

```bash
curl -X POST "http://127.0.0.1:8080/requests/<request-id>/respond" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approved", "engineer": "alex/claude/main", "note": "ok"}'
```

`approved` releases the claim immediately. `denied` restores the claim's original TTL (so the holder isn't punished for the request having shortened it).

The full lifecycle is recorded in an append-only `request_events` audit log queryable at `GET /requests/<id>/events`. Events: `filed`, `notified` (first time per holder session), `responded`, `expired` (shortened TTL fired before respond), `resolved` (claim released for unrelated reasons).

The MCP wrappers (`request_release`, `respond_to_request`, `wait_for_request`, `my_requests`) are usually what an agent uses; the curl recipes above are useful when debugging.

## Suggested team norms

- Claim before editing, not after.
- Treat `409` as a coordination event, not a reason to force through.
- Keep TTLs short and extend only when needed.
- Use ownership rules for stable boundaries, not for every folder.
- Review the dashboard when a merge queue or release branch feels blocked.

## Current limitations to know about

- Identity is claim-owner text, not a full user management system.
- SQLite is good for small team coordination, but not a global multi-region lock service.
- Overlap accuracy is best when the service can see a checkout of the application repo through `COORD_REPO_ROOT`.

Those limits are acceptable for the intended use case: a small-to-medium engineering team coordinating agent work on one main codebase.
