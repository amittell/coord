# Server-enforced repo scoping via repo-bound tokens

Status: proposal (draft v2 -- revised after oracle + code-review critique)
Author: (coord)
Related: issue #30, [roadmap.md](./roadmap.md), [multi-namespace.md](./multi-namespace.md)

## Problem

A hosted shared coord service fronts multiple repos. Today, claim/conflict/dashboard
visibility across repos is **not enforced by the server** -- it depends entirely on the
client sending a `repo` value:

- `engineer_tokens` binds a token to an **engineer only** (`db.py:464`: id, engineer,
  token_sha256, description, created_at, revoked_at, last_used_at, plus the v0.29.4
  expiry/rotation columns). There is no repo column.
- `repo` is client-supplied: `body.repo` on `create_claims`, and `repo=` query params on
  `/claims`, `/conflicts`, `/metrics/*`. Most other endpoints act by **id**
  (claim/request/queue/session) and carry no repo signal at all.

Issue #30 slice 1 (v0.41.0) made the **client** send `repo=COORD_REPO_ID` by default.
That is advisory, not a boundary: a stale client that omits `repo` still receives every
repo's claims, and any client can pass `all_repos`. The server cannot "know the repo the
call came from" because nothing binds the connection, session, or token to a repo.

This proposal binds the repo to the **auth token** so the server derives repo scope from
authentication and enforces it at the data-access boundary.

> **v2 note.** The first draft proposed a single `effective_repo()` helper over the few
> `repo=` query-param endpoints. Oracle + code-review review (2026-07-01) showed that is
> nowhere near enough: **14 authenticated endpoints** touch repo-tagged data, most by id,
> and a scoped token could still read or mutate another repo's claims/requests/queue via
> those. It also found a scope-escalation path through dashboard token self-service and
> OIDC. This draft rewrites the enforcement model around a data-access boundary and
> enumerates every endpoint.

## Non-goals

- Full multi-tenancy / URL-per-tenant isolation (see multi-namespace.md).
- Per-team / allowed-set tokens (one token, many repos). v1 is one repo per token; the
  allowed-set generalization is under Future work.
- Changing the engineer identity model. Tokens stay per-engineer; repo is additive.

## The model (the one irreversible decision)

Add a **nullable `repo`** column to `engineer_tokens`:

- `repo IS NULL` -> **unscoped** token = operator / back-compat. Sees and acts across all
  repos; may use `all_repos`. Every existing token is NULL after migration, so upgrade is
  **inert**. The legacy shared `COORD_AUTH_TOKEN` is likewise unscoped (operator).
- `repo = "owner/name"` -> **scoped** token. The server forces the effective repo to this
  value on every request the token authenticates. A client cannot widen it.

Single repo per token for v1: mirrors the per-repo `.coordination/local.env` (one repo id
per file), keeps enforcement a scalar compare, minimal correct step. It is **not**
truly irreversible -- a later `engineer_token_repos(token_id, repo)` join table can
backfill scalar values -- so scalar now, allowed-set later only if demand is real.

## Enforcement: an auth-derived scope, checked at the data boundary

Thread the token's repo from auth into request state:
- `AuthOutcome` gains `token_repo`; `require_auth` sets `request.state.token_repo` (None
  for shared/unscoped) and `request.state.token_scoped: bool`.

Then enforce in two layers, because most endpoints are id-addressed, not `repo=`:

1. **Query-param reads** -- a helper `effective_repo(request, client_repo)` returns the
   repo to filter by and whether `all_repos` is allowed.
2. **Id-addressed reads/writes** -- **data-access-boundary guards** that look up the
   target's repo and 403 on mismatch. Do NOT rely on handlers remembering to filter:
   ```
   require_claim_in_scope(claim_id, token_repo)
   require_request_in_scope(request_id, token_repo)
   require_queue_entry_in_scope(queue_id, token_repo)
   require_session_in_scope(session_id, token_repo)   # 403 if session spans other repos
   ```
   These live at the service/DB layer so every route that touches an id inherits them.

### Complete endpoint inventory (from the review)

Every authenticated endpoint that exposes or mutates repo-tagged data, and its rule under
a **scoped** token (unscoped tokens are unchanged):

| Endpoint | Kind | Rule for scoped token |
|---|---|---|
| `GET /claims` | read (query) | force `repo=token_repo` |
| `GET /conflicts` | read (query) | force `repo=token_repo` |
| `GET /metrics/hotspots` | read (query) | force `repo=token_repo` |
| `GET /metrics/auto-resolutions` | read (query) | force `repo=token_repo` |
| `GET /repos` | read | return only `token_repo`'s row (no cross-repo enumeration) |
| `GET /requests`, `?queued=true` | read | filter to `token_repo` |
| `GET /requests/{id}` | read (id) | `require_request_in_scope` |
| `GET /requests/{id}/events` | read (id) | `require_request_in_scope` |
| `GET /sessions/{id}/pending_requests` | read (id) | `require_session_in_scope` |
| `POST /claims` (`create_claims`) | write | default `body.repo` to `token_repo` when absent; 403 on mismatch |
| `POST /claims/refactor` | write | same as `create_claims` (its own `repo` field) |
| `DELETE /claims/{id}` | write (id) | `require_claim_in_scope` |
| `POST /claims/release` | write (id list) | `require_claim_in_scope` for each |
| `POST /claims/{id}/extend` | write (id) | `require_claim_in_scope` |
| `POST /sessions/{id}/release` | write (id) | `require_session_in_scope`; release only in-scope claims |
| `POST /requests` (`file_request`) | write (id) | `require_claim_in_scope` on the target claim |
| `POST /requests/{id}/respond` | write (id) | `require_request_in_scope` |
| `DELETE /requests/{queue_id}` | write (id) | `require_queue_entry_in_scope` |
| `POST /metrics/hotspots/promote` | write (global) | **operator-only**: 403 for scoped tokens (mutates the shared ownership YAML) |

Ids (claim/request/queue/session) are **not secret** -- they appear in `GET /requests`
responses and elsewhere -- so id-addressed routes are the real bypass surface. This table
is the acceptance checklist: a test must cover a scoped-token 403 for each write and an
empty/filtered result for each read.

### Read semantics: silent-override only when absent; 403 on explicit mismatch

Revised from v1 (which proposed silent override for all reads):
- Scoped token, `repo` **absent** -> silently scope to `token_repo` (this is what fixes
  stale clients that send nothing).
- Scoped token, `repo` **present and != token_repo**, or `all_repos=true` -> **403**
  (fail loudly; the client thinks it queried elsewhere but did not).
- Always emit an `X-Coord-Repo-Scope: <repo>` response header (and a debug log line) when
  a scope override is applied, so an operator who drops a token into the wrong repo's
  `local.env` sees *why* results are empty instead of a silent void.

Write mismatch stays 403. Note the misconfig trap the review raised: a legacy client that
omits `body.repo` under a wrong-repo token would silently create claims in the token's
repo -- the `X-Coord-Repo-Scope` header is the mitigation for detecting this.

## Scope-escalation fixes (P0 from review)

These are the paths by which a scoped token could become unscoped; all must ship in v1:

1. **Dashboard token self-service** (`main.py:1534`): `dashboard_tokens_create` calls
   `create_engineer_token(...)` with no repo -> mints a `repo=NULL` (operator) token from
   a scoped session. Fix: a scoped session **must** force `repo=token_repo` on tokens it
   creates and **must not** be able to mint an unscoped token; `list`/`revoke` in the
   panel filter by `(engineer, token_repo)`. Only unscoped/operator sessions may mint
   unscoped or arbitrary-repo tokens.
2. **OIDC / dashboard login** (`main.py:1348`): mints unscoped short-lived tokens, so
   every SSO principal has operator-level cross-repo visibility. This is a **hard v1
   limitation**, not a deferred nicety -- operators must know before claiming scoping is
   enforced. Options: (a) OIDC operator-only; (b) `COORD_OIDC_REPO_CLAIM` maps a principal
   to a repo; (c) per-repo dashboard mints a repo-scoped session. v1 picks (a) + docs;
   (b) is the follow-up.
3. **`rotate_engineer_token`** (`db.py:2339-2373`): its SELECT and INSERT name columns
   explicitly and omit `repo`; after the migration a rotation would silently produce a
   NULL-repo (unscoped) successor with `status='ok'` -- scope vanishes with no 401. The
   `db.py` work item must update **both** the predecessor SELECT and the successor INSERT
   to carry `repo`. Add a regression test asserting a rotated scoped token stays scoped.

## Dashboard scoping is deeper than one parameter

`render_dashboard` (`dashboard.py:1024`) takes no repo param and makes ~10 data calls,
several of whose DB functions have no `repo` parameter today: `list_claims`,
`recent_conflicts`, `list_recent_claims`, `list_repos`, `list_requests`,
`count_auto_resolutions_since`, `daily_auto_resolutions`, `hotspot_files`,
`list_queued_with_holder`, `webhook_delivery_stats`. Scoping the dashboard means adding a
`repo` param to `render_dashboard` **and** threading repo through each of those (service
or DB layer, or a post-filter). Treat this as its own sized work item, not a line.

## Safety: legacy NULL-repo claims cause false-safe conflicts

`service.py:672` filters conflicts to `r.get("repo") == repo`, which excludes NULL-repo
claims. So a scoped-token `GET /conflicts` (and the pre-push hook it backs) will **not**
see conflicts from claims made under the shared token or any unscoped/omitted-repo client.
During a transition where NULL and scoped claims coexist, scoped clients get **false-safe
pre-push results**. This is a safety risk, not a cosmetic note. Rollout must include a
step to drain NULL-repo claims (let them expire / release them) before switching a hot
repo to scoped tokens, and the docs must call it out.

## CLI / lifecycle

- `coord tokens create <engineer> --repo <id>` mints a scoped token; omit for unscoped.
- `coord tokens list` shows a `repo` column (`all` for NULL) so unscoped tokens are
  visible at a glance (they are the remaining exposure during rollout).
- `coord tokens rotate` inherits the predecessor's repo (see fix #3 above).
- Centralize repo-id validation (non-empty, sane max length, no control chars,
  `owner/name`-ish) shared by `COORD_REPO_ID`, `--repo`, token creation, and request
  bodies/params.

## Rollout / back-compat

1. Migration vN adds nullable `repo`; existing tokens become unscoped -> inert upgrade,
   client-version-independent (even the OLD `coord-mcp` that sends no `repo` is filtered
   server-side once its `local.env` carries a scoped token -- this is what actually fixes
   the leak seen after slice 1).
2. Operators drain NULL-repo claims on a hot repo, then swap its `local.env` to a
   repo-scoped token; unscoped tokens keep working elsewhere throughout.
3. `coord tokens list` surfaces remaining unscoped tokens; when hosted mode has many,
   warn.
4. `COORD_REQUIRE_SCOPED_TOKEN=true` (rejects NULL-repo tokens on non-operator routes)
   pulled forward from "someday" to a near follow-up, since it is the switch that makes
   scoping actually mandatory for a hosted deployment.

## Interactions (verified against current code)

- Rate limits key on the request-body engineer; `session_id` self-exclusion unchanged --
  repo is orthogonal to both.
- `all_repos` is honored only for unscoped tokens; ignored/403 for scoped ones -- closes
  the current hole where any client reads every repo by passing `all_repos`.

## Work breakdown

1. Schema migration vN: nullable `repo` on `engineer_tokens`.
2. `db.py`: `create_engineer_token(repo=...)`; `resolve_engineer_token` /
   `lookup_engineer_token` return `repo`; `list_engineer_tokens` surfaces it;
   **`rotate_engineer_token` SELECT+INSERT carry `repo`**; add repo params to the dashboard
   DB fns listed above; add `require_*_in_scope` lookups (claim/request/queue/session).
3. `main.py`: `AuthOutcome.token_repo`; `require_auth` sets `request.state.token_repo` +
   `token_scoped`; `effective_repo()` for query reads; boundary guards on every
   id-addressed route in the inventory; `create_claims` + `claim_refactor` default/reject;
   `/repos` filter; `/metrics/hotspots/promote` operator-only; `X-Coord-Repo-Scope` header.
4. Dashboard: `render_dashboard(repo=...)` + thread repo through its ~10 calls; token
   panel forces `repo=token_repo` and forbids unscoped mint from scoped sessions.
5. Auth entry points: OIDC/login path documented operator-only (or repo-mapped).
6. CLI: `coord tokens create --repo`, `list` column, rotate inheritance; shared repo-id
   validator.
7. Tests: the inventory table as an acceptance checklist (scoped-token 403 per write,
   filtered/empty per read), auth threading, read absent-vs-mismatch, rotation keeps
   scope, dashboard-mint no-escalation, back-compat NULL token, legacy-NULL false-safe
   documented behavior.
8. Docs: `deployment.md` hosted-multi-repo section incl. the NULL-claim drain step and the
   OIDC limitation; roadmap slot as v0.42.0.

No feature flag needed for the mechanism: NULL = unscoped keeps it inert until scoped
tokens exist. `COORD_REQUIRE_SCOPED_TOKEN` is the separate "make it mandatory" switch.

## Open questions (post-review)

1. OIDC in v1: operator-only (proposed) vs block dashboard login when scoping is required
   vs ship `COORD_OIDC_REPO_CLAIM` immediately?
2. `POST /sessions/{id}/release` under a scoped token when the session spans repos: 403
   the whole call (proposed) or partial-release only in-scope claims and report the count?
3. Ship `COORD_REQUIRE_SCOPED_TOKEN` in the same release (so hosted deployments can
   actually enforce) or the next?
4. Allowed-set tokens: is single-repo going to bite soon enough to justify the join table
   in this migration rather than a later one?
