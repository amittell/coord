# API Reference

All authenticated endpoints require a bearer token. In the examples below we assume `COORD_AUTH_TOKEN` is set in your shell (`coord start` prints a ready-to-paste `export` line).

```http
Authorization: Bearer <COORD_AUTH_TOKEN>
```

## `GET /health`

Unauthenticated liveness probe.

Response:

```text
ok
```

## `GET /readyz`

Unauthenticated readiness probe with runtime metadata.

Example response:

```json
{
  "status": "ready",
  "version": "0.1.0",
  "auth_mode": "bearer",
  "database_path": "data/coordination.db"
}
```

## `GET /meta`

Unauthenticated service metadata.

## `GET /dashboard`

Authenticated HTML dashboard that lists active claims, recent conflicts, and stored ownership. The dashboard is protected by the same bearer token as the rest of the API; load it in a browser by adding the `Authorization` header via an extension, or use `curl` plus a local proxy.

Response: `text/html`.

## `POST /claims`

Create one or more claims.

Request body:

```json
{
  "engineer": "alex/claude/main",
  "branch": "alex/feature",
  "description": "editing auth",
  "claims": [
    {"type": "file", "pattern": "src/auth/**"},
    {"type": "shared_file", "pattern": "package-lock.json"}
  ],
  "ttl_hours": 4
}
```

The top-level body accepts an optional `wait_seconds` field added in v0.21:

- `wait_seconds` (int, default `0`, range `0..600`): when the request would `409`, FIFO-queue the caller behind the blocking holder and long-poll for up to this many seconds for the holder to release. On release (manual `release_claims`, TTL expiry, request approval, or a `narrowed` / `coexist` decision) the service drains the queue and auto-grants the next entry in arrival order. `0` (or omitted) preserves the v0.13-v0.20 immediate-409 behaviour. The server caps the value at 600.

Each `ClaimItem` accepts two optional fields added in v0.14:

- `symbols` (`list[str]`): top-level symbol names within `pattern`. When present and non-empty, the claim becomes `scope_type='symbol'` and covers only the listed declarations; imports and module-level statements are explicitly not covered. When `pattern` is a glob, the symbol list applies to every matched file -- pass separate claim items if you want per-file granularity. Empty or absent: `scope_type='file'` (legacy behaviour). Entries containing `::` are interpreted as method-scope (v0.16+): `"Router::handleAuth"` claims the `handleAuth` method on the `Router` class. The server splits at insert time and stores `Router` as the parent and `handleAuth` as the leaf. Two-level only -- nested classes / nested namespaces are not yet supported.
- `narrowable` (`bool`): whether a later symbol-scope requester can auto-narrow this claim. Defaults: `file` -> `true`, `shared_file` / `module` / `symbol` -> `false`.

Symbol-scope example (mixing top-level and method-scope symbols):

```json
{
  "engineer": "alex/claude/main",
  "branch": "alex/auth-refactor",
  "claims": [
    {
      "type": "file",
      "pattern": "src/auth/login.ts",
      "symbols": ["handleLogin", "validateCredentials"]
    },
    {
      "type": "file",
      "pattern": "src/auth/router.ts",
      "symbols": ["Router::handleAuth", "Router::handleLogout"]
    }
  ],
  "ttl_hours": 2
}
```

Responses:

- `200`: claims created
- `400`: scope validation failed
- `409`: overlapping active claim exists

Conflict response shape:

```json
{
  "claim_ids": [],
  "conflicts": [
    {
      "your_pattern": "src/auth/**",
      "your_symbols": ["handleLogin"],
      "conflicting_claim": {
        "id": "claim-id",
        "engineer": "bob/claude/main",
        "pattern": "src/auth/login.ts",
        "scope_type": "symbol",
        "symbols": ["handleLogin", "logSignin"],
        "severity": "hard",
        "description": "working on login",
        "expires_at": "2026-04-10T15:00:00Z"
      },
      "overlap": ["src/auth/login.ts"],
      "symbol_overlap": [
        {"file": "src/auth/login.ts", "symbols": ["handleLogin"]}
      ]
    }
  ],
  "warnings": [],
  "options": ["wait", "narrow_claim", "escalate", "override"]
}
```

`your_symbols` echoes the requester's symbol list for the conflicting pattern. `conflicting_claim.scope_type` is `'file'` or `'symbol'`; `conflicting_claim.symbols` is present only when `scope_type='symbol'`. `symbol_overlap` lists the per-file symbol-name intersection; it is present only when both sides are symbol-scoped and their symbol sets actually overlap. Disjoint symbol sets do not produce a `409` -- the server resolves them as `AUTO_COEXIST` and returns `200` with both claims granted, cross-referenced via `coexists_with`. Pre-v0.14 clients that don't send `symbols` see the same `200` / `409` behaviour as before; the new fields are absent from their responses unless the conflict involves a v0.14 holder. See [./design/sub-file-claims.md](./design/sub-file-claims.md) for the full overlap algorithm.

Method-scope conflict (v0.16+): when one side holds a bare-class symbol and the other claims a method on the same class, `symbol_overlap.symbols` echoes the canonical `Parent::child` form for the method side. Example -- requester asks for `Router::handleAuth` against a holder of bare `Router`:

```json
{
  "claim_ids": [],
  "conflicts": [
    {
      "your_pattern": "src/auth/router.ts",
      "your_symbols": ["Router::handleAuth"],
      "conflicting_claim": {
        "id": "claim-id",
        "engineer": "bob/claude/main",
        "pattern": "src/auth/router.ts",
        "scope_type": "symbol",
        "symbols": ["Router"]
      },
      "overlap": ["src/auth/router.ts"],
      "symbol_overlap": [
        {"file": "src/auth/router.ts", "symbols": ["Router::handleAuth"]}
      ]
    }
  ]
}
```

Two agents on sibling methods (`Router::handleAuth` vs `Router::handleLogout`) do NOT hit this path -- the server grants both via `AUTO_COEXIST` and returns `200`.

## `GET /claims`

List claims.

Query params:

- `active_only=true|false`
- `engineer=<id>`
- `module=<substring>`

## `GET /conflicts`

Check planned paths against active claims.

Query params:

- one or more `pattern=...`
- `engineer=<id>`
- `repo=<id>` (v0.4.0+) — restrict the check to claims with the same `repo` value. Without this, the service-wide pool is checked, which can false-positive across unrelated repos.
- `session_id=<id>` (v0.6.0+, may be repeated as of v0.10.0) — additionally self-exclude any active claim sharing one of these session_ids. The pre-push hook reads every line of `.coordination/sessions.live` and forwards them all so an agent's own subagent claims under different engineer names don't false-positive on its own push. Each id also acts as an activity ping for that session's held claims.

Example:

```bash
curl "http://127.0.0.1:8080/conflicts?engineer=alex/claude/main&pattern=src/auth/login.ts" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN"
```

## `GET /repos` (v0.3.0+)

Per-repo activity summary. One row per distinct repo that has ever attached its identifier to a claim.

Returns:

```json
{
  "repos": [
    {
      "repo": "amittell/coord",
      "last_activity": "2026-05-05T01:04:00Z",
      "claims_24h": 3,
      "engineers_24h": 2,
      "active_claims": 1
    }
  ],
  "count": 1
}
```

The `repo` value is automatically attached to claims by `coord-mcp` from `COORD_REPO_ID` (set by `coord init` from `git remote get-url origin`).

## `GET /metrics/hotspots` (v0.20.0+)

Files that agents keep `409`'ing on, grouped per repo. The same series powers the dashboard's "Hotspot files (30d)" panel and is exposed standalone so external monitoring (Prometheus, digest emails, etc.) can consume it.

Query params:

- `days` (int, default `30`, range `1..90`) -- look-back window for `conflict_log` rows.
- `min_attempts` (int, default `5`) -- floor below which a file is not considered a hotspot. Rows with fewer attempts than this in the window are excluded.
- `limit` (int, default `20`, max `100`) -- top-N rows per repo, sorted by attempt count descending.
- `repo` (str, optional) -- restrict the result to a single repo identifier. When omitted every repo with at least one qualifying row appears.

Example:

```bash
curl "http://127.0.0.1:8080/metrics/hotspots?days=30&min_attempts=5&limit=10" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN"
```

Example response:

```json
{
  "series": [
    {
      "repo": "amittell/coord",
      "attempted_pattern": "src/router.ts",
      "attempts": 24,
      "distinct_attempters": 6,
      "first_seen": "2026-05-04T09:11:00Z",
      "last_seen": "2026-06-01T22:47:00Z",
      "suggested_action": "split into modules"
    },
    {
      "repo": "amittell/coord",
      "attempted_pattern": "package-lock.json",
      "attempts": 18,
      "distinct_attempters": 9,
      "first_seen": "2026-05-05T11:02:00Z",
      "last_seen": "2026-06-02T03:18:00Z",
      "suggested_action": "promote to shared_file"
    },
    {
      "repo": "amittell/coord",
      "attempted_pattern": "src/auth/session.ts",
      "attempts": 7,
      "distinct_attempters": 2,
      "first_seen": "2026-05-29T14:20:00Z",
      "last_seen": "2026-06-02T08:05:00Z",
      "suggested_action": "monitor"
    }
  ],
  "days": 30,
  "min_attempts": 5,
  "limit": 10,
  "count": 3
}
```

`suggested_action` mirrors the dashboard's threshold logic:

- `attempts >= 20` -> `"split into modules"` (the file is doing too much, repeated conflict is structural).
- `attempts >= 10` -> `"promote to shared_file"` (genuinely shared scope, make the overlap explicit).
- `attempts >= min_attempts` -> `"monitor"` (not actionable yet, but worth watching).

Empty result (no qualifying rows in the window) returns `{"series": [], "days": ..., "min_attempts": ..., "limit": ..., "count": 0}`. The signal is read-only in v0.20; the v0.21 `POST /metrics/hotspots/promote` endpoint (below) provides an operator-driven apply path.

## `POST /metrics/hotspots/promote` (v0.21.0+)

Apply a suggested action from the hotspot panel to the active `owners.yaml`. The dashboard's "promote to `shared_file`" and "split into modules" chips POST to this endpoint; operators can also call it directly from `curl` or a digest-email automation. Idempotent: applying the same action+pattern twice is a no-op and the second response simply echoes the existing rule.

Request body:

```json
{
  "action": "shared_file",
  "pattern": "package-lock.json",
  "repo": "amittell/coord",
  "note": "weekly digest 2026-06-02"
}
```

Fields:

- `action` (str, required): `"shared_file"` writes the pattern as a `shared_file` rule into `owners.yaml`. `"split"` annotates the pattern with a `split` marker for a follow-up modularisation pass; the rule survives in `owners.yaml` so the dashboard surfaces the pending split until an operator removes it.
- `pattern` (str, required): the file glob to promote. Usually copied from the hotspot row's `attempted_pattern`.
- `repo` (str, optional): repo identifier to scope the rule. When omitted, the rule applies repo-wide.
- `note` (str, optional): free-form audit note recorded alongside the rule. Surfaces in the dashboard and the audit trail.

Example:

```bash
curl -X POST http://127.0.0.1:8080/metrics/hotspots/promote \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "shared_file", "pattern": "package-lock.json"}'
```

Response shape:

```json
{
  "action": "shared_file",
  "pattern": "package-lock.json",
  "repo": "amittell/coord",
  "applied": true,
  "owners_yaml_path": ".coordination/owners.yaml",
  "rule": {"type": "shared_file", "pattern": "package-lock.json"}
}
```

`applied` is `true` when the rule was newly written and `false` when the same rule already existed (idempotent path). `400` when `action` is not one of the two supported values, or when `pattern` is empty.

The operator stays in the loop -- v0.21 never auto-promotes on its own; the endpoint only writes when actively poked.

## `GET /sessions/{session_id}/pending_requests` (v0.6.0+, extended in v0.9.0)

The merged inbox a holder polls. Returns two kinds of rows distinguished by `kind`:

- `kind: "request"` — first-class release requests filed against claims this session holds (v0.9+). The holder's next action is to `respond_to_request` (approve / deny). Pending requests carry `urgency`, `requested_pattern`, `requester_engineer`, `requester_session_id`.
- `kind: "auto-conflict"` — read-only conflict-log entries logged automatically every time a `claim_files` got 409'd against one of this session's claims (v0.6).

The first time per holder session that a first-class request appears in this feed, a `notified` audit event is recorded against the request so the operator can prove the holder saw it.

## `POST /sessions/{session_id}/release` (v0.5.0+)

Bulk-release every active claim with the given `session_id`. Used by `coord-mcp`'s `release_session` tool at end-of-work — releases claims made by every subagent under that session in one call, regardless of the engineer name they used.

```bash
curl -X POST "http://127.0.0.1:8080/sessions/sess-deadbeef/release" \
  -H "Authorization: Bearer $COORD_AUTH_TOKEN"
```

## `POST /claims/release`

Release multiple claims.

```json
{
  "claim_ids": ["claim-1", "claim-2"],
  "engineer": "alex/claude/main"
}
```

## `DELETE /claims/{claim_id}`

Release a single claim, optionally scoped by `engineer`.

## `POST /claims/{claim_id}/extend`

Extend an active claim owned by an engineer.

```json
{
  "engineer": "alex/claude/main",
  "ttl_hours": 2
}
```

## `POST /requests` (v0.9.0+)

File a release request against an active claim. Filing shortens the holder's claim TTL to `min(remaining, COORD_REQUEST_TTL_SHORT_SEC)` (default 300s) so the claim is forced to a near-term decision: the holder either responds (approve / deny) via `POST /requests/{id}/respond` or the shortened TTL fires and the claim auto-releases.

By default, the server long-polls for the holder's decision: `wait_seconds=60` (override 0–600).

Body:

```json
{
  "claim_id": "<active claim id>",
  "requester": "<engineer name>",
  "session_id": "<optional MCP session id>",
  "reason": "hot fix #1234",
  "urgency": "low | normal | high | blocking",
  "wait_seconds": 60
}
```

Response: the request row, with `decision` either still `pending` (long-poll timed out, requester polls `/requests/{id}` to follow up) or `approved` / `denied` if the holder decided within the wait window.

`404` if the `claim_id` is unknown. `409` if the claim is already released or expired (no need to file; retry the original `claim_files`).

## `POST /requests/{request_id}/respond` (v0.9.0+, extended in v0.11.0)

The holder responds to an open request. Four decisions:

- `approved` (v0.9): release the claim immediately.
- `denied` (v0.9): keep the claim, restore the original TTL (so the holder isn't punished for the request having shortened it).
- `narrowed` (v0.11): close the original claim and atomically open a tighter one. Pass `narrowed_pattern`. The new claim inherits the original's engineer / branch / repo / session / TTL. The server validates `narrowed_pattern` is a subset of the holder's current pattern via the `compute_overlap` synthesizer; disjoint or broader patterns return `400`.
- `coexist` (v0.11): grant the requester a sibling claim on the same scope. Pass `coexist_pattern`. Both claims live, mutually self-excluded via `claims.coexists_with`, but still adversarial to anyone outside the pair. Cooperative not enforced -- imports and shared module-level state remain on the agents to handle.

```json
{
  "decision": "approved | denied | narrowed | coexist",
  "engineer": "<holder engineer>",
  "session_id": "<holder MCP session id>",
  "note": "ok, releasing",
  "narrowed_pattern": "<required when decision='narrowed'>",
  "coexist_pattern": "<required when decision='coexist'>"
}
```

A late respond (after the request has already terminalised) is recorded as a `responded-late` audit event but does not change state.

## `GET /requests` (v0.9.0+, extended in v0.22.0)

List requests, filterable by:

- `requester=<engineer>` (the requester's view, used by `my_requests`)
- `claim_id=<id>` (every request ever filed against a claim)
- `decision=pending | approved | denied | expired | resolved`
- `queued=true|false` (v0.22+) -- when `true`, return live FIFO queue rows instead of the `requests` table. Joins the blocking holder's context so callers get the full "who am I waiting on?" picture in one call. `repo=<id>` may be combined with this flag to restrict the queue listing.

When `queued=true`, each row is a `QueuedRequestEntry` rather than a normal request:

```json
{
  "queued": [
    {
      "kind": "queued",
      "queue_id": "q-...",
      "blocking_claim_id": "claim-...",
      "blocking_engineer": "alice/claude/main",
      "blocking_pattern": "src/auth/login.ts",
      "requester_engineer": "bob/codex/main",
      "requester_pattern": "src/auth/login.ts",
      "position": 0,
      "state": "waiting",
      "enqueued_at": "2026-06-02T17:11:00Z",
      "expires_at": "2026-06-02T17:21:00Z"
    }
  ],
  "count": 1
}
```

`position` is the FIFO index (0 = head of queue); `state` is `waiting` until the blocking claim releases. The default (`queued=false` or omitted) preserves the v0.9 shape: each row carries the joined holder context (`holder_engineer`, `holder_pattern`, `holder_repo`).

## `GET /requests/{request_id}` (v0.9.0+)

Return the current state of a single request. Useful for polling `pending` requests to follow a long-poll that timed out.

## `GET /requests/{request_id}/events` (v0.9.0+)

Return the immutable audit timeline for a request, oldest first. Event types: `filed`, `notified`, `responded`, `expired`, `resolved`, `responded-late`. Each event has actor, session_id, timestamp, and a JSON `detail` blob with the per-event-type specifics.

Auto-resolution / auto-promote audit (v0.14+, extended v0.22): server-side automatic decisions are recorded in `request_events` with `request_id=NULL`. `auto-coexist` and `auto-narrow` (v0.14) cover the symbol-claim resolutions described in [./design/sub-file-claims.md](./design/sub-file-claims.md). As of v0.22, `auto-promote` events appear in the same table whenever `COORD_AUTO_PROMOTE_THRESHOLD` triggers a `shared_file` rule write into `owners.yaml`; the `detail` JSON carries `pattern`, `threshold`, and `window_days`. These rows are visible via a global audit query (the per-request endpoint above only surfaces them when filtering by `request_id IS NULL`).

```json
{
  "events": [
    {"event_type": "filed", "actor_engineer": "bob", "detail": "{\"urgency\":\"high\",...}"},
    {"event_type": "notified", "actor_engineer": "alice", "detail": null},
    {"event_type": "responded", "actor_engineer": "alice", "detail": "{\"decision\":\"approved\",\"note\":\"ok\"}"}
  ],
  "count": 3
}
```

## `POST /config/ownership`

Upload ownership YAML.

The server validates the YAML shape before storing it. Invalid configs return `400`.

## `GET /config/ownership`

Returns the stored ownership YAML as `text/yaml`, or `204` if nothing has been uploaded yet.
