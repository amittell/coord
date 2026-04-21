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
