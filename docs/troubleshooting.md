# Troubleshooting

## `coord start` exits immediately

Cause: `COORD_AUTH_TOKEN` is missing and insecure mode is not enabled.

Fix:

```bash
coord start
```

For an explicit local-only bypass:

```bash
export COORD_ALLOW_INSECURE_NO_AUTH=true
coord-api
```

## `/claims` returns `401`

Cause: the bearer token is missing or wrong.

Fix:

```bash
curl -H "Authorization: Bearer $COORD_AUTH_TOKEN" http://127.0.0.1:8080/claims
```

## Ownership upload fails

Cause: the YAML shape is invalid.

Fixes:

- ensure the top level is a mapping
- ensure `modules` or `areas` exists
- ensure every block has a non-empty `paths` list
- use only `soft` or `hard` for `severity`

Start from `templates/.coordination/owners.example.yaml`.

## Agents still collide even though the service is running

Most likely causes:

- the agent never called `check_conflicts` or `claim_files`
- multiple workers are reusing the same `engineer` value
- the claim was too broad or too vague to match accurately

Use unique worker IDs such as `alex/claude/main` and `alex/claude/reviewer`.

## Overlap results seem weak or surprising

Cause: `COORD_REPO_ROOT` is not configured, so overlap uses heuristic matching.

Fix: if possible, point `COORD_REPO_ROOT` at a checkout of the application repo. If that is not possible, use more exact file claims.

## `coord-mcp` starts but the tools fail

Check:

1. `COORD_API_URL` points at the running API
2. `COORD_AUTH_TOKEN` matches the API token
3. `curl $COORD_API_URL/readyz` works from the same machine

## `coord doctor` fails on repo checks

Check:

1. you are inside the git repo you initialized
2. `.coordination/config.toml` exists
3. `.coordination/local.env` contains a token
4. the tool-specific file exists, such as `.mcp.json` for Claude Code
5. `CLAUDE.md` or `AGENTS.md` still contains the managed coordination block

## Stale claims remain visible

Claims expire on TTL and are also swept by a background cleanup loop. If you want them gone immediately, release them explicitly.

You can also reduce `COORD_CLEANUP_INTERVAL_SEC`.

## Docker container is unhealthy

Check:

1. the container logs for startup errors
2. `COORD_AUTH_TOKEN` is set
3. port `8080` is reachable inside the container
4. the `/data` volume is writable
