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
      "conflicting_claim": {
        "id": "claim-id",
        "engineer": "bob/claude/main",
        "pattern": "src/auth/login.ts",
        "severity": "hard",
        "description": "working on login",
        "expires_at": "2026-04-10T15:00:00Z"
      },
      "overlap": ["src/auth/login.ts"]
    }
  ],
  "warnings": [],
  "options": ["wait", "narrow_claim", "escalate", "override"]
}
```

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

Example:

```bash
curl "http://127.0.0.1:8080/conflicts?engineer=alex/claude/main&pattern=src/auth/login.ts" \
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

## `POST /config/ownership`

Upload ownership YAML.

The server validates the YAML shape before storing it. Invalid configs return `400`.

## `GET /config/ownership`

Returns the stored ownership YAML as `text/yaml`, or `204` if nothing has been uploaded yet.
