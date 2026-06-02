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

## `GET /requests` (v0.9.0+)

List requests, filterable by:

- `requester=<engineer>` (the requester's view, used by `my_requests`)
- `claim_id=<id>` (every request ever filed against a claim)
- `decision=pending | approved | denied | expired | resolved`

Each row carries the joined holder context (`holder_engineer`, `holder_pattern`, `holder_repo`).

## `GET /requests/{request_id}` (v0.9.0+)

Return the current state of a single request. Useful for polling `pending` requests to follow a long-poll that timed out.

## `GET /requests/{request_id}/events` (v0.9.0+)

Return the immutable audit timeline for a request, oldest first. Event types: `filed`, `notified`, `responded`, `expired`, `resolved`, `responded-late`. Each event has actor, session_id, timestamp, and a JSON `detail` blob with the per-event-type specifics.

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
