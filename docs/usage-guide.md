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
