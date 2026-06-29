# Oracle Review

## Summary

This feature adds `coord mcp install`, wiring a repo’s `.coordination/local.env` values into Claude Code, Claude Desktop, Codex, and Cursor MCP config files with auto-detection, `--tool`, `--all`, and `--dry-run` support. The JSON writers are mostly straightforward and safe for normal configs, but the Codex TOML “block surgery” has correctness gaps where valid TOML can be parsed/detected as an existing coord server but not removed before appending a new table, producing duplicate/invalid config. Auto-detect also currently succeeds as a no-op when no tools are detected.

## Findings

### P1

#### `coordination/cli_mcp.py:263-295`, `coordination/cli_mcp.py:298-339`, `coordination/cli_mcp.py:347-353` — Codex TOML surgery can leave detected coord tables in place and append duplicates

`_codex_coord_keys()` uses `tomllib` to detect existing coord servers, but `_strip_codex_coord_tables()` only removes headers that textually match exact unquoted forms like:

```toml
[mcp_servers.coord]
[mcp_servers.coord.env]
```

Because TOML supports equivalent valid forms that the regex does not remove, a valid existing coord config can be detected but left in the file, then a fresh `[mcp_servers.coord]` block is appended. Examples:

```toml
[mcp_servers."coord"]
command = "coord-mcp"
```

```toml
[mcp_servers.coord] # existing coord server
command = "coord-mcp"
```

```toml
mcp_servers.coord.command = "coord-mcp"
```

These can produce duplicate/conflicting TOML after rewrite and corrupt Codex’s config.

**Suggestion:** Either use a TOML writer / structured update approach, or make the surgery syntax-aware enough to handle quoted keys, trailing comments, dotted keys, and all coord descendants. At minimum, validate `tomllib.loads(new_text)` immediately before writing and fail without modifying the file if the result is invalid.

---

#### `coordination/cli_mcp.py:381-383`, `coordination/cli_mcp.py:418-435`, `coordination/cli_mcp.py:450-453` — Auto-detect with no tools exits successfully after doing nothing

In auto-detect mode, `_select_tools()` returns:

```py
targets = []
skipped = list(TOOLS)
```

when nothing is detected. The later guard checks:

```py
if not targets and not skipped:
```

so it never fires in this case. The command then prints `Detected: (none)`, skips the install loop, and exits `0`, including the success message `Registered the coord MCP server`.

**Suggestion:** In auto-detect mode, return a non-zero actionable error when `not targets`, e.g. “No supported AI coding tools detected; pass --tool or --all to force a target.”

---

#### `coordination/cli_mcp.py:212-220` — JSON writer silently replaces non-object `mcpServers`

For Claude Code, Claude Desktop, and Cursor, a top-level non-object JSON config is rejected, but an existing non-dict `mcpServers` value is silently discarded:

```py
servers = data.get("mcpServers")
if not isinstance(servers, dict):
    servers = {}
```

That can clobber user data in a malformed-but-existing config instead of surfacing that the config is unexpected.

**Suggestion:** If `"mcpServers"` exists and is not a dict, raise `McpConfigError` rather than replacing it. Only create `{}` when the key is absent.

### P2

#### `tests/test_cli_mcp.py:150-160`, `tests/test_cli_mcp.py:203-220` — Codex idempotency tests only cover canonical table syntax

The Codex tests cover a fresh install, canonical re-run, and unrelated table preservation, but they do not cover valid TOML forms that `tomllib` accepts while `_strip_codex_coord_tables()` misses.

**Suggestion:** Add tests for:

- `[mcp_servers."coord"]`
- `[mcp_servers.coord] # trailing comment`
- existing coord server under a quoted non-canonical key
- dotted-key / inline-table forms, if supported or intentionally rejected
- final rewritten TOML parses successfully after every install

---

#### `tests/test_cli_mcp.py:230-251` — Missing test for auto-detect detecting no tools

There is a test where only Cursor is detected, but no test for the “nothing detected” path. That would have caught the current successful no-op behavior.

**Suggestion:** Add a test that monkeypatches `shutil.which` to `None`, creates no config dirs/files, runs without `--tool/--all`, and asserts non-zero exit plus an actionable stderr message.

---

#### `tests/test_cli_mcp.py:320-331` — Malformed config coverage only checks JSON, not TOML

Invalid JSON is tested and not clobbered, but invalid Codex TOML is not.

**Suggestion:** Add an invalid `~/.codex/config.toml` test asserting `rc == 1`, original content unchanged, and an error mentioning invalid TOML.

## Path-safety / secrets note

No obvious token leak to stdout: normal and dry-run output prints paths/actions, not env values. Writing `COORD_AUTH_TOKEN` into selected user-level MCP configs is intentional for this feature. The main safety concern is therefore avoiding accidental config corruption, especially for Codex TOML.

## VERDICT: block

Prioritized defects:

1. Fix Codex TOML duplicate/corruption cases and validate generated TOML before write.
2. Make auto-detect with zero targets exit non-zero with an actionable message.
3. Refuse JSON configs whose existing `mcpServers` is not an object.
4. Add missing tests for Codex TOML variants, invalid TOML preservation, and no-tool auto-detect.