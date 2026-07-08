# Multi-namespace coordination

Status: design-gated, unversioned. The repo-scoped-visibility half was delivered by the v0.42 repo-bound tokens (see [repo-scoped-tokens.md](./repo-scoped-tokens.md)); the remaining namespace-isolation decisions below still want settling before code (see [roadmap.md](./roadmap.md)).
Author: Alex Mittell
Date: 2026-06-03

## Motivation

Coord today is single-tenant per instance: one ownership YAML applies
to every repo that talks to the service; one claim table covers all
of them; the queue, the conflict engine, and the auth middleware all
assume a single behavioural namespace. v0.4 carved out one exception
-- `repo`-scoped conflict checks -- by partitioning the active-claim
sweep on `claims.repo` so a `client/js/**` claim from one repo no
longer false-positives against an unrelated `client/js/**` in a
sibling service. Useful but partial: every layer above the conflict
engine still shares one `owners.yaml`, one bearer token, one
hotspot counter, and one auto-promote knob across every repo.

Three production pressures push back on the remaining assumptions.
**Monorepos**: one repo handle covering `apps/web/**`, `apps/api/**`,
and `infra/**` cannot plausibly share an ownership rule set; the
web team wants `hard` on `apps/web/router.tsx` while the infra team
wants `soft` on the same path. **Shared infra for unrelated teams**:
one coord instance hosts five product teams' repos but the
shared-files panel, the auto-promote threshold, and the hotspot
heatmap mix their signals, so a team whose repo has six hot files
gets behaviour tuned for someone else's traffic profile.
**Multi-tenant SaaS**: a managed coord offering needs one bearer
token per customer organisation so `org_a`'s `list_claims` cannot
see `org_b`'s scopes, queue depth, or webhook stream.

The v0.4 repo-scoped check did the right thing at the conflict
engine but stopped there. v0.28 extends the same partition upwards
through the ownership YAML and outwards through the auth
middleware. The fourth lever is **cross-repo coupling**: a monorepo
split into two coord repos may want one side's work to register
that it shouldn't proceed while a specific slice of the other side
is being edited. v0.28 introduces a typed `blocks` field on claims
so that dependency is declared at claim time, not encoded as a
manual `shared_files` workaround.

Non-goals for v0.28:

- Cross-instance federation. A claim filed on instance A cannot
  block a claim on instance B.
- Per-repo authentication policy. The bearer token model stays
  global through v0.28.1; per-tenant tokens land in v0.28.2. Repos
  are not themselves auth boundaries.
- Transparent migration of an existing single-tenant deployment.
  v0.28.2 ships `coord upgrade --tenants` as a tools-required path;
  existing deployments stay tenant-less unless the operator opts in.
- Cross-tenant claim visibility. There is no v0.28 surface for
  "see all tenants" except via the unscoped admin token.
- Per-tenant webhook routing in the v0.27 outbox. The outbox stays
  a single subscriber stream in v0.28; per-tenant URLs are deferred
  to v0.29.

## Data model

Phasing: v0.28.0 changes nothing on disk (per-repo rules ride
inside the existing `ownership_config.yaml_text`); v0.28.1 adds one
nullable column on `claims`; v0.28.2 adds one nullable column on
five tables plus a new `tenants` config table. Each migration is
forward-compatible with the v0.27 schema.

### Per-repo ownership rules (v0.28.0)

`ownership_config` keeps its single-row shape. The change is purely
in the YAML payload: a new optional top-level `repos:` key maps
`repo_id -> {modules: ..., shared_files: ..., areas: ...}`. When
present, `repos[X]` wins over the global section for a claim whose
`repo` field equals `X`. When absent, the document behaves as today.

Example shape:

```yaml
# Global rules apply to any repo NOT listed under `repos:`
modules:
  default:
    paths: ["**/*"]
    severity: soft
    owners: [dev-platform]
repos:
  amittell/api:
    modules:
      billing:
        paths: ["src/billing/**"]
        severity: hard
        owners: [billing-team]
    shared_files:
      - src/router.ts                # auto-promoted=2026-06-02
      - package-lock.json            # coord-managed=permanent
  amittell/web:
    modules:
      shell:
        paths: ["src/app/**"]
        severity: hard
        owners: [web-team]
```

Resolution: a claim carrying `repo='amittell/api'` parses
`repos['amittell/api']` into a `RepoOwnership` bundle, cached in the
service layer keyed on `(repo, yaml_text_digest)`. `repo=None` falls
through to the global section. `severity_for_pattern` becomes
`severity_for_pattern_in_repo(pattern, repo, config)`:

```
def severity_for_pattern_in_repo(
    pattern: str, repo: str | None, config: OwnershipConfig
) -> str:
    if repo is not None and repo in config.repos:
        sev = severity_for_pattern(pattern, config.repos[repo].rules)
        if sev == "hard":
            return "hard"
    return severity_for_pattern(pattern, config.global_rules)
```

The fall-through is asymmetric on purpose: a per-repo rule can
promote a soft global rule to hard, but cannot demote a hard global
rule to soft. The global section stays a safety floor; relaxing a
global hard rule for one repo means removing it globally and
re-adding it per repo.

Backward compat: a YAML without `repos:` parses identically to today.
A new `parse_ownership_config` returns the structured shape;
`parse_ownership_yaml` keeps returning `list[PathRule]` (the
flattened global view) as a thin shim.

The auto-promote / auto-demote sweep (v0.22+) gains a per-repo write
path: when a hotspot is detected in `repo='amittell/api'`, the YAML
patch lands inside `repos['amittell/api'].shared_files`.
`patch_owners_yaml_with_shared_file` grows a `repo:` kwarg;
`repo=None` keeps the v0.22 behaviour.

### Cross-repo claim references (v0.28.1)

A claim can declare that it semantically blocks work in another
repo with no direct path overlap. Wire shape:

```json
{
  "type": "file",
  "pattern": "src/billing/migrate.py",
  "blocks": ["amittell/web#src/checkout/**"]
}
```

Each entry parses to `(blocked_repo, blocked_pattern)`; the pattern
uses the same glob grammar as `claims.pattern`. A same-repo entry
is rejected (use a normal extra claim).

Schema v14 adds one nullable column on `claims`:

```sql
-- v14: cross-repo block declarations. A JSON-encoded list of
-- "repo#pattern" strings the holder declares should not be edited
-- in those other repos while this claim is live. NULL means the
-- claim has no cross-repo blocks (the v0.27 default).
ALTER TABLE claims ADD COLUMN blocks_json TEXT;
CREATE INDEX idx_claims_blocks ON claims (blocks_json)
    WHERE blocks_json IS NOT NULL;
```

The partial index keeps the table layout cheap when most claims have
no blocks. SQLite supports partial indexes since 3.8; the same idiom
is already used in v0.21's queue indexes.

Conflict engine extension. After the existing same-repo overlap pass
in `create_claims`, the engine runs a second pass:

```
def cross_repo_block_pass(requester, requester_repo) -> list[ConflictEntry]:
    if requester_repo is None:
        return []  # Untagged claims are not eligible to be blocked
    blockers = await db.list_active_claims_with_blocks_for(
        target_repo=requester_repo
    )
    out = []
    for holder in blockers:
        for blocked_repo, blocked_pattern in parse_blocks(holder):
            if blocked_repo != requester_repo:
                continue
            if compute_overlap(blocked_pattern, requester.pattern):
                out.append(ConflictEntry(
                    your_pattern=requester.pattern,
                    conflicting_claim=holder,
                    reason="cross_repo_block",
                    blocked_via=f"{blocked_repo}#{blocked_pattern}",
                ))
    return out
```

`list_active_claims_with_blocks_for(target_repo)` is a new helper
backed by `WHERE released_at IS NULL AND blocks_json LIKE ?` (LIKE is
a pre-filter; the JSON parse + `compute_overlap` is authoritative).
The partial index keeps the scan cheap.

Worked example. Holder in `amittell/api` files
`{pattern: 'src/billing/migrate.py', blocks: ['amittell/web#src/checkout/**']}`.
Requester in `amittell/web` files `{pattern: 'src/checkout/cart.tsx'}`.
Same-repo pass: no overlap. Cross-repo pass: holder's blocks contain
`amittell/web#src/checkout/**`, requester's `repo` matches,
`compute_overlap` matches. Result: 409 with
`reason='cross_repo_block'`,
`blocked_via='amittell/web#src/checkout/**'`.

Cross-repo blocks are always `hard`. The holder declared an explicit
dependency; demoting to soft would render the declaration advisory
only. Soft cross-repo signals should use `shared_files`.

### Per-team views (v0.28.1)

Team membership is a read-side view. No new column on `claims`;
teams are derived from the engineer name via an ownership-YAML
lookup.

The YAML gains an `engineers:` top-level block:

```yaml
engineers:
  alex/claude/main:
    team: dev-platform
    contact: alex@example.com
  bob/claude/main:
    team: web-team
  codex-reviewer:
    team: dev-platform
```

`OwnershipConfig.engineers: dict[str, EngineerMeta]` plus a
`team_for_engineer(name)` helper. List endpoints accept `?team=` and
`?engineer=` query filters; they compose with AND. Filtering happens
at the API boundary on the DB result set -- the mapping is small
enough that an in-memory join is cheaper than a denormalised column.

Filtered endpoints in v0.28.1: `GET /claims`, `GET /requests`
(any variant), `GET /repos`, `GET /metrics/auto-resolutions`,
`GET /metrics/hotspots`, and the dashboard's claim list. A team
filter applied to an endpoint with no engineer column (e.g.
`/metrics/hotspots` keyed on `repo + pattern`) is a no-op with a
`Vary: team` response header so callers can detect the no-op.

### Multi-tenant API tokens (v0.28.2)

A tenant is the auth boundary: one bearer token per tenant, every
row in every state-bearing table tagged with `tenant_id` at insert,
every read filtered to the caller's tenant.

`COORD_TENANT_TOKENS` accepts a JSON map
`{"org_a": "tok_a_xxx", "org_b": "tok_b_yyy"}`. When set, the
existing `COORD_AUTH_TOKEN` becomes the unrestricted admin token.
When unset, the system stays in single-tenant mode and
`COORD_AUTH_TOKEN` works exactly as today.

Schema v15:

```sql
-- v15: per-tenant scoping. NULL means "single-tenant deployment" --
-- pre-v15 rows and rows from a v15 deployment running without
-- COORD_TENANT_TOKENS both stay NULL. The conflict engine treats
-- NULL as its own tenant bucket so a mixed deployment never
-- accidentally exposes legacy rows across the auth boundary.
ALTER TABLE claims ADD COLUMN tenant_id TEXT;
ALTER TABLE claim_queue ADD COLUMN tenant_id TEXT;
ALTER TABLE requests ADD COLUMN tenant_id TEXT;
ALTER TABLE conflict_log ADD COLUMN tenant_id TEXT;
ALTER TABLE webhook_outbox ADD COLUMN tenant_id TEXT;
CREATE INDEX idx_claims_tenant ON claims (tenant_id, released_at);
CREATE INDEX idx_claim_queue_tenant ON claim_queue (tenant_id, state);
CREATE INDEX idx_requests_tenant ON requests (tenant_id, decision);

CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
```

The `tenants` table is operator-managed (no API surface to create
rows; the env var is the source of truth and the table is populated
by a startup sync). It exists so the dashboard and audit log can
attach a human-readable label to a `tenant_id` without parsing the
env var on every render.

Auth middleware change. `require_auth` becomes
`require_auth_with_tenant`, returning an `AuthContext(tenant_id,
is_admin)`:

```python
def require_auth_with_tenant(authorization) -> AuthContext:
    settings = get_settings()
    if not settings.tenant_tokens:
        require_auth(authorization)  # legacy path
        return AuthContext(tenant_id=None, is_admin=True)
    token = _parse_bearer(authorization)
    if token == settings.admin_token:
        return AuthContext(tenant_id=None, is_admin=True)
    for tid, t in settings.tenant_tokens.items():
        if hmac.compare_digest(token, t):
            return AuthContext(tenant_id=tid, is_admin=False)
    raise HTTPException(401, "Invalid bearer token")
```

Every route receives `AuthContext` via `Depends` and forwards
`tenant_id` to the service layer; the service filters every read on
`tenant_id` and stamps every write with the caller's tenant.

Cross-tenant requests are rejected at the service boundary, not at
the DB: a caller from tenant A who tries to `release_claims` on a
claim with `tenant_id != A` gets `404 Not Found` (not 403, to avoid
leaking existence). The admin token sees every tenant and gains a
`?tenant=` query filter to scope the dashboard view.

## Algorithm changes

Functions touched per cut.

v0.28.0 (per-repo ownership):

- `coordination/ownership.py`: `OwnershipConfig` data model,
  `parse_ownership_config`, `severity_for_pattern_in_repo`.
  `parse_ownership_yaml` and `severity_for_pattern` stay as thin
  shims for backward compat.
- `coordination/service.py`: `_rules` becomes `_ownership_config`;
  `create_claims` calls
  `severity_for_pattern_in_repo(item.pattern, body.repo, config)`.
- `coordination/ownership.py`:
  `patch_owners_yaml_with_shared_file` and
  `patch_owners_yaml_remove_shared_file` gain a
  `repo: str | None = None` kwarg; auto-promote / auto-demote
  callers pass the claim's `repo`.
- `coordination/main.py`: `POST /config/ownership` validator
  accepts the new top-level keys (`repos`, `engineers`) and
  rejects unknown ones.

v0.28.1 (cross-repo blocks + per-team views):

- `coordination/schemas.py`: `ClaimItem` gains
  `blocks: list[str] | None = None`.
- `coordination/db.py`: schema v14; `insert_claims_batch` stores
  `blocks_json`; new `list_active_claims_with_blocks_for`.
- `coordination/service.py`: `create_claims` runs the cross-repo
  pass after the same-repo overlap pass; `check_conflicts` exposes
  `?include_blocked_by=`.
- `coordination/ownership.py`: `OwnershipConfig.engineers`;
  `team_for_engineer` helper.
- `coordination/main.py`: list endpoints accept `team` and
  `engineer` query params; one shared `_filter_by_team_or_engineer`
  helper keeps the policy in one place.

v0.28.2 (multi-tenant tokens):

- `coordination/__init__.py`: `Settings.tenant_tokens` parsed from
  `COORD_TENANT_TOKENS`; `auth_token` becomes the admin token
  semantically.
- `coordination/main.py`: `require_auth_with_tenant` returns
  `AuthContext`; every route receives it via FastAPI `Depends`.
- `coordination/service.py`: every public method gains a
  `tenant_id: str | None` kwarg threaded from the route layer.
- `coordination/db.py`: schema v15; every query against `claims`,
  `claim_queue`, `requests`, `conflict_log`, `webhook_outbox` gains
  a NULL-safe `tenant_id IS ?` filter clause.
- `coordination/main.py`: new admin-only `GET /tenants`.

## API surface

### Per-repo ownership upload (v0.28.0)

`POST /config/ownership` keeps its shape; the body is YAML text.
The validator accepts the new top-level keys. Errors return 400
with a hint pointing at the offending key. `GET /config/ownership`
unchanged. No new endpoint.

### Cross-repo blocks (v0.28.1)

`POST /claims` accepts a new `blocks: list[str] | None` field on
each `ClaimItem`. Format: `"<repo>#<pattern>"`. Validation:

- The substring `#` appears exactly once.
- The `repo` portion matches `[A-Za-z0-9_./-]+`.
- The `pattern` portion passes `_validate_pattern_syntax`.
- The `repo` is not equal to the claim's own `repo`.

The claim's `conflicts` payload gains `reason: 'cross_repo_block'`
entries with a `blocked_via` field naming the holder's block
declaration that fired. `GET /conflicts` accepts
`?include_blocked_by=<repo>` to include cross-repo matches.

### Per-team filters (v0.28.1)

`GET /claims`, `GET /requests` (any variant), `GET /repos`,
`GET /metrics/auto-resolutions`, `GET /metrics/hotspots` accept
`?team=` and `?engineer=`. Filters compose with AND. Unknown team
or engineer returns an empty result (not 404) so dashboard filter
chips never have to distinguish "zero rows" from "does not exist".

### Tenant scoping (v0.28.2)

Admin-only `GET /tenants` returns
`{tenants: [{id, display_name, created_at}, ...]}`. Every
claim-bearing endpoint returns `tenant_id` per row; the bearer
token determines scope. The admin token sees all tenants and
accepts `?tenant=`. Multi-tenant deployments include
`X-Coord-Tenant` on every authenticated response so the caller can
audit which tenant the server attributed to its token.

## Migration plan

### Schema v14 (v0.28.1, cross-repo blocks)

```sql
ALTER TABLE claims ADD COLUMN blocks_json TEXT;
CREATE INDEX idx_claims_blocks ON claims (blocks_json)
    WHERE blocks_json IS NOT NULL;
```

Backfill: every existing row keeps `blocks_json = NULL`. The
conflict engine treats NULL as "no cross-repo blocks" (v0.27
behaviour). v0.27 binaries against a v14 DB tolerate the extra
column; the partial index is harmless. Column-drop rollback is not
supported (SQLite limitation); the operator-facing rollback is
"don't write `blocks` from clients", restoring v0.27 semantics
with no DB change.

### Schema v15 (v0.28.2, tenant scoping)

```sql
ALTER TABLE claims ADD COLUMN tenant_id TEXT;
ALTER TABLE claim_queue ADD COLUMN tenant_id TEXT;
ALTER TABLE requests ADD COLUMN tenant_id TEXT;
ALTER TABLE conflict_log ADD COLUMN tenant_id TEXT;
ALTER TABLE webhook_outbox ADD COLUMN tenant_id TEXT;

CREATE INDEX idx_claims_tenant ON claims (tenant_id, released_at);
CREATE INDEX idx_claim_queue_tenant ON claim_queue (tenant_id, state);
CREATE INDEX idx_requests_tenant ON requests (tenant_id, decision);

CREATE TABLE tenants (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
```

Backfill: every existing row gets `tenant_id = NULL`. A v15
deployment without `COORD_TENANT_TOKENS` continues to write NULL
on every insert and treats every read as `tenant_id IS NULL` --
byte-identical to v0.27.

Tenant introduction: when the operator sets `COORD_TENANT_TOKENS`,
a startup sync populates `tenants` from the env-var keys. Existing
NULL-tenant rows are NOT auto-tagged -- they remain accessible
only via the admin token. `coord upgrade --tenants
--assign-orphans-to=<id>` is the documented path for retroactively
tagging pre-tenant rows.

NULL semantics:

- No tenants configured: every claim is NULL-tagged; `tenant_id IS ?`
  with `NULL` matches all rows.
- Tenants configured + legacy NULL-tenant rows present: the legacy
  rows are invisible to any tenant-scoped read. Only the admin
  token sees them. Silent cross-tenant leakage would be worse than
  hiding.

Rollback: unset `COORD_TENANT_TOKENS` and restart; the binary
reverts to single-tenant mode and only admin-tagged rows are
visible.

## Rollout phases

**v0.28.0** -- per-repo ownership rules + backward-compat YAML
shape. Smallest first slice; tests that the ownership YAML can grow
without breaking existing setups. No schema change. Existing
deployments are bit-identical until the operator uploads a YAML
with a `repos:` key. Ships: `OwnershipConfig`,
`parse_ownership_config`, `severity_for_pattern_in_repo`, per-repo
`shared_files` patching in the auto-promote / auto-demote sweep,
extended `POST /config/ownership` validator, this design doc.

**v0.28.1** -- cross-repo blocks + per-team filters. Builds on
v0.28.0. Schema v14 (one nullable column on `claims`). Ships:
`blocks` field on `ClaimItem`, cross-repo conflict-engine pass,
`engineers:` YAML block, `?team=` / `?engineer=` filters on list
endpoints, dashboard team-chip row.

**v0.28.2** -- multi-tenant API tokens. Highest-blast-radius cut;
ships last. Schema v15 (five nullable columns + `tenants` table).
Ships: `COORD_TENANT_TOKENS` env var, `AuthContext` and
tenant-aware `require_auth`, tenant filtering on every service-layer
read and write, admin-only `GET /tenants`, `coord upgrade --tenants`
CLI helper, README env-var row.

## Documentation deltas

The README env-var table picks up one row in v0.28.2:

| Env var | Description |
|---|---|
| `COORD_TENANT_TOKENS` | Multi-tenant API (v0.28.2): JSON map `{tenant_id: bearer_token}`. When set, `COORD_AUTH_TOKEN` becomes the unscoped admin token; every other request is scoped to the tenant identified by its bearer. Default: unset. |

v0.28.0 and v0.28.1 add no env vars; per-repo ownership and per-team
views are configured via `/config/ownership`. The architecture doc
gains a "Multi-namespace model" section describing the
`(tenant_id, repo)` two-level partition.

## Open questions

1. **Cross-repo blocks against an unknown repo.** A `blocks` entry
   naming a repo coord has never seen: silent no-op or 400 at claim
   creation? Proposal: silent no-op, because coord has no a-priori
   repo registry; a hard-fail surface waits for v0.29.
2. **Per-team vs per-engineer as the primary filter.** Both filter
   chip rows on every list view is busy. Proposal: team is the
   primary axis (visible chip row), engineer is a secondary axis
   (row drill-down). The query API supports both independently.
3. **Tenant-scoped admin writes.** Should the admin token be allowed
   to write on behalf of a tenant? Proposal: no -- the admin token
   is read-mostly. Writes with `?tenant=` stamp that value;
   writes without get `tenant_id = NULL` (admin-only visibility).
4. **Webhook outbox tenancy.** Should events carry `tenant_id` and
   should the receiver be per-tenant? Proposal: payload gains
   `tenant_id`; receiver stays single (one `COORD_WEBHOOK_URL` per
   instance). Per-tenant URLs deferred to v0.29.
5. **Per-repo auto-promote thresholds.** Should each `repos[X]`
   block carry its own `auto_promote: {threshold, window_days}`?
   Proposal: yes, but deferred to v0.28.0.1 so the v0.28.0 cut stays
   scope-bounded; env var stays the global default.
6. **Cross-tenant `blocks` entries.** A `blocks` entry naming a repo
   owned by a different tenant: silent no-op or 400? Proposal: 400
   -- the declaration is meaningless without visibility and silent
   acceptance would mislead the caller.

## Test plan

Each cut lands tests in the existing pytest harness. Counts are
rough against the v0.27 baseline of ~2100 tests.

v0.28.0 (per-repo ownership), ~55 tests:

- `test_ownership.py` (+30): parametrised cases for the new YAML
  shape (global-only, repos-only, both, modules vs areas, per-repo
  shared_files patching).
- `test_service.py` (+10): per-repo severity resolution via two
  claims on the same pattern in different repos.
- `test_owners_yaml_patch.py` (+15): `repo=` kwarg on the patch
  helpers.

v0.28.1 (cross-repo blocks + per-team views), ~60 tests:

- `test_db_migration.py` (+8): v13 -> v14 round-trip, NULL
  backfill, partial index, v0.27 binary tolerates v14.
- `test_service.py` (+15): cross-repo block pass end to end;
  mixed-pass; same-repo block rejected.
- `test_api.py` (+12): `blocks` accepted, malformed format
  rejected, `?include_blocked_by=` honoured.
- `test_ownership.py` (+8): `engineers:` parsing,
  `team_for_engineer` lookup.
- `test_api.py` (+18): `?team=` / `?engineer=` filters including
  the `Vary: team` no-op.

v0.28.2 (multi-tenant tokens), ~70 tests:

- `test_db_migration.py` (+10): v14 -> v15 round-trip, NULL
  backfill, `tenants` table populated by startup sync.
- `test_main_auth.py` (+20, new file): admin token sees all;
  tenant token sees own only; cross-tenant 404; admin writes
  NULL-tenant without `?tenant=`, tagged with; unknown bearer 401s.
- `test_service.py` (+25): tenant filter on every read, tenant
  stamp on every write, conflict engine never crosses the boundary.
- `test_api.py` (+15): `GET /tenants` admin-only, tenant scoping
  on list endpoints, `X-Coord-Tenant` header present.

Cumulative budget: ~185 new tests. Existing tests need monkeypatch
updates only where they exercise `severity_for_pattern` (v0.28.0
thin shim) or `require_auth` (v0.28.2 tenant-aware); service-layer
signatures keep their shape via `tenant_id=None` defaults, so the
bulk of the existing test surface is unaffected.

## See also

- [./roadmap.md](./roadmap.md) -- post-v0.27 forward look; v0.28
  was one bullet there, this doc is the expansion.
- [./sub-file-claims.md](./sub-file-claims.md) -- v0.14-v0.26
  precedent for phasing a multi-version arc; the structure here is
  borrowed wholesale.
- [../architecture.md](../architecture.md) -- current single-tenant
  model; v0.28.2 adds "Multi-namespace model" superseding parts of
  "Auth model" and "Scaling notes".
- [../../README.md](../../README.md) -- env-var table that picks up
  `COORD_TENANT_TOKENS` in v0.28.2.
- [../../CHANGELOG.md](../../CHANGELOG.md) -- v0.4 (repo-scoped
  conflict check) and v0.6 (session_id arc) are the most directly
  relevant prior architectural moves.
