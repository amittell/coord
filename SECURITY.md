# Security policy

## Authentication

Every authenticated endpoint requires `Authorization: Bearer <token>` matching `COORD_AUTH_TOKEN`. The comparison is constant-time and the token is never logged, never echoed in error responses, and never written to the dashboard or CHANGELOG.

The MCP stdio bridge (`coord-mcp`) reads the same token from its environment and forwards it on every HTTP call.

## Token rotation

Token rotation is a config change plus a restart:

1. Edit `.coordination/local.env` (gitignored). Set a fresh `COORD_AUTH_TOKEN=...`.
2. Restart the service (`coord stop && coord start --background`, or roll the container).
3. Update any MCP client configs that hardcode the old token, and any CI secrets that hold it.

For deployment templates that pull the token from a secrets manager (HashiCorp Vault, AWS Secrets Manager, Kubernetes secret), rotate it there and restart the workload. `coord` re-reads the env on startup.

## Reporting vulnerabilities

Open a GitHub security advisory: <https://github.com/amittell/coord/security/advisories/new>.

Do not file a public issue. Maintainers acknowledge confirmed reports within 7 days. We'll work with you on disclosure timing.

## Supported versions

Only the latest tagged release on `main` receives security fixes. Fixes ship as a patch release (vX.Y.Z+1). If you're running an older version, the supported path is to upgrade.

## Out of scope

- Vulnerabilities in user-supplied agent code or workflows that call the API
- Third-party MCP wrappers, IDE plugins, or CLI tools that proxy to `coord`
- Any deployment topology that disables auth via `COORD_ALLOW_INSECURE_NO_AUTH=true`. That flag exists for local development only; using it in a multi-tenant or networked environment is a deployment bug, not a `coord` bug.
- Denial-of-service via legitimate API usage (rate limiting is the operator's responsibility; deploy a reverse proxy if you need it)
