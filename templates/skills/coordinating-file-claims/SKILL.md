---
name: coordinating-file-claims
description: Install, configure, and use the coord MCP server to claim files and symbols before editing in repos shared by multiple agents. Use when working in a repo with a .coordination/ directory, when claim_files returns conflicts or 429, when setting up coord in a new repo, or when the user mentions coord, claims, file locking, merge conflicts between agents, or multi-agent coordination.
license: MIT
compatibility: Requires an MCP-capable agent (Claude Code, Codex CLI, Cursor) and network access to a coord service
metadata:
  upstream: https://github.com/amittell/coord
---

# Coordinating edits with the coord MCP server

coord prevents two agents from editing the same files at the same time. Agents claim paths (or individual symbols) before editing, other agents see those claims, and conflicting work queues or negotiates instead of colliding in git.

If the repo already has a `.coordination/` directory, skip to "Use". If not, follow "Install" first.

## Install (one-time per repo)

1. Install the client CLI:

   ```bash
   pipx install coord-mcp-server   # or: pip install coord-mcp-server
   ```

2. Wire the repo. Run inside the application repo (not the coord source repo):

   ```bash
   coord init --tool claude --mode remote --service-url https://YOUR-COORD-HOST --yes
   ```

   Use `--tool codex` or `--tool cursor` for those agents, and `--mode local` if the service runs on `http://127.0.0.1:8080`. This writes `.coordination/` (config, owners.yaml, pre-push hook), `.mcp.json`, and a managed block in CLAUDE.md.

3. Get a per-engineer token from the operator (they run `coord tokens create <name>` on the server; the raw `coordt_...` value is shown exactly once). Put it in `.coordination/local.env`:

   ```
   COORD_AUTH_TOKEN=coordt_...
   ```

4. Verify. Run exactly this and only proceed when every line says OK:

   ```bash
   coord doctor
   ```

If the service itself is not deployed yet, that is an operator task: the server ships as a container image (`ghcr.io/amittell/coord`); see `docs/deployment.md` in the upstream repo.

## Configure (optional)

- `COORD_REPO_ROOT` (server side): lets the server validate claimed symbols against real files and enables LSP features.
- Lost token: tokens are unrecoverable; ask the operator to `coord tokens revoke` then `coord tokens create`. Expiring tokens can be rotated with overlap: `coord tokens rotate <id> --grace 24h`.

## Use (mandatory workflow)

The MCP server is named `coord`. Every editing session follows this sequence:

1. `coord:list_claims` at task start to see who is working where.
2. `coord:claim_files` before editing anything. Pass your engineer name, the file patterns, and a short description:

   ```json
   {"engineer": "alice/claude/myrepo", "patterns": ["src/auth/**"], "description": "refactor login"}
   ```

3. Edit only within your claimed scope. No opportunistic edits to unclaimed files.
4. `coord:release_claims` (specific claim ids) or `coord:release_session` (everything this session holds) when done.

### Claim only what you need

- Symbol-level claims let two agents share a file. Claim one function instead of the whole file:

  ```json
  {"patterns": ["src/auth/login.ts"], "symbols": {"src/auth/login.ts": ["handleLogin"]}}
  ```

  Method notation `ClassName::methodName` works and nests to any depth (`Outer::Inner::method`). Disjoint symbol claims on the same file coexist automatically.
- For a cross-file rename, `coord:claim_refactor` (file + symbol) reserves the definition plus every callsite in one batch. It returns an error when the server has no LSP enabled; fall back to claiming the files you know about.

## If claim_files returns conflicts

Do NOT edit the contested files. In order of preference:

1. Queue behind the holder: retry the same `claim_files` with `wait_seconds: 60` (and `urgency: "high"` if your work blocks the team). You are granted automatically when the holder releases; check `coord:my_requests` with `queued: true` for your position, or abandon with `coord:cancel_queue_request`.
2. Ask the holder directly: `coord:request_release` files a request against their claim; their decision (approved / denied / narrowed / coexist) appears in `coord:my_requests`.
3. If neither resolves it, stop and ask the user. Never edit around a held claim.

When YOU hold claims, check `coord:pending_requests` between operations and answer with `coord:respond_to_request`.

## Errors and recovery

- 401 "token expired" or "token was rotated": the response names the fix; ask the operator for a replacement token, update `.coordination/local.env`.
- 409 conflict payload: see "If claim_files returns conflicts" above.
- 429 with `retry_after`: you hit a rate limit (too many active claims or queue entries). Release finished claims, then wait `retry_after` seconds before retrying. Do not retry in a loop.
- Symbol rejected as nonexistent: the error lists the parseable symbols in that file; re-check spelling against that list.
- "advisory:" warnings on a successful claim: another engineer's claimed symbol has callsites inside your scope. Your claim is granted; coordinate with them or expect semantic (not textual) conflicts.
- Connection refused / doctor failures: re-run `coord doctor` and fix the first failing line; confirm the service URL in `.coordination/local.env` and that the server is reachable with `curl <url>/readyz`.

## Sub-agents

Sub-agents do not inherit this skill or the repo's CLAUDE.md automatically. When dispatching a sub-agent that edits files, include the claim/release protocol in its task text, or claim on its behalf before dispatch and state that its scope is already claimed.
