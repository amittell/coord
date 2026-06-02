# Sub-file (symbol-level) claims

Status: proposal, targeting v0.14.0
Author: Alex Mittell
Date: 2026-06-02

## Motivation

Coord today scopes claims by file path or glob. With small teams (2-3 agents) this is workable; the v0.11 `narrowed` / `coexist` decisions cover the occasional hot file by letting a holder voluntarily concede some scope. With larger fleets (10+ agents on one repo) the model breaks: a handful of files (`router.ts`, `package-lock.json`, the schema index, the app shell) are touched by every active claim, and every agent eventually serialises on them. The current escape hatches are reactive — invoked after a `409`, not as the default behaviour.

The bottleneck is grain size. Two agents editing different functions in `auth.ts` have no real conflict, but coord can only see "both want auth.ts" and forces them to dance. Moving the unit of coordination one level down (function or class instead of file) eliminates the false conflicts without giving up the safety properties of coord's existing model.

Non-goals for v1:
- Arbitrary byte-range claims. Symbols are the atomic unit; sub-symbol locking is not in scope.
- Methods inside a class. A claim on a class covers all of its methods. v2 can decompose.
- Languages other than TypeScript. Parser interface is language-agnostic so Python/Go drop in later, but only TS ships in v0.14.
- Sub-file dashboard panels. Audit surface is text-only for v1; dashboard work follows in v0.14.1.

## Data model

### Schema v8

```sql
-- v8: per-claim symbol scope. NULL scope_type backfills as 'file' so legacy
-- rows continue to behave as whole-file claims and the conflict engine has
-- a single branch for them.
ALTER TABLE claims ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'file';
ALTER TABLE claims ADD COLUMN narrowable BOOLEAN NOT NULL DEFAULT 1;

CREATE TABLE claim_symbols (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol_name TEXT NOT NULL,
    symbol_kind TEXT NOT NULL,
    UNIQUE (claim_id, file_path, symbol_name),
    FOREIGN KEY (claim_id) REFERENCES claims(id)
);
CREATE INDEX idx_claim_symbols_file_symbol
    ON claim_symbols (file_path, symbol_name);
CREATE INDEX idx_claim_symbols_claim
    ON claim_symbols (claim_id);
```

### Schema v10 (v0.16)

```sql
-- v10: nullable parent_symbol on claim_symbols. NULL means the symbol is
-- top-level (function, class, const, etc.); non-NULL means the symbol is
-- a method whose enclosing class / receiver type is `parent_symbol`. The
-- service splits the wire-format `"Parent::child"` notation at insert time
-- and stores the two parts in `symbol_name` and `parent_symbol` respectively.
ALTER TABLE claim_symbols ADD COLUMN parent_symbol TEXT;
CREATE INDEX idx_claim_symbols_parent
    ON claim_symbols (file_path, parent_symbol, symbol_name);
```

Overlap uses a two-level prefix-matching rule for symbol-vs-symbol comparisons on the same file:

- A claim on a bare symbol `Foo` (`parent_symbol IS NULL`, `symbol_name='Foo'`, kind `'class'`) matches every method whose `parent_symbol='Foo'`. The bare-class claim and any `Foo::method` claim are treated as overlapping (auto-block).
- Two method claims `Foo::a` and `Foo::b` (same `parent_symbol='Foo'`, different `symbol_name`) are disjoint and `AUTO_COEXIST`.
- `Foo::a` and `Bar::a` (different `parent_symbol`) are disjoint regardless of the shared leaf name.

`claims.scope_type` is one of:
- `'file'` (default, legacy): claim covers every byte of every matched path.
- `'symbol'`: claim covers only the symbols enumerated in `claim_symbols` for this id. Module-level code (imports, top-level statements outside any declared symbol) is **not** covered; another claim is needed for those.

`claims.narrowable` is set at claim-creation time. When `true`, the claim can be auto-narrowed by an incoming symbol claim against an overlapping file. When `false`, an incoming symbol claim must use the existing `request_release` flow. Defaults:
- `file` claims: `narrowable=true`.
- `shared_file` claims: `narrowable=false` (the whole point of `shared_file` is "I expect overlap and want to see it").
- `module` claims: `narrowable=false` (the operator deliberately picked a coarse scope).
- `symbol` claims: `narrowable=false` (already at the leaf level).

### symbol_kind values

`'function' | 'class' | 'interface' | 'type' | 'const' | 'enum' | 'unknown'`

Producer is the parser (see "Parser strategy"). The value is informational — overlap is computed on `(file_path, symbol_name)` only. Kind exists for the dashboard and audit log; future versions may use it for namespace separation (e.g. allow a `function foo` and `type Foo` claim to coexist).

### Claim-shape examples

A whole-file claim (unchanged from today):

```json
{"type": "file", "pattern": "src/auth/login.ts"}
```

A symbol claim:

```json
{
  "type": "file",
  "pattern": "src/auth/login.ts",
  "symbols": ["handleLogin", "validateCredentials"]
}
```

Internally this becomes `scope_type='symbol'` with two rows in `claim_symbols`. The `pattern` is still stored so existing tooling that joins on `claims.pattern` keeps working; the conflict engine just learns to skip path-overlap when both sides are symbol-scoped on the same file.

A multi-file symbol claim:

```json
{
  "type": "file",
  "pattern": "src/auth/**.ts",
  "symbols": ["handleLogin"]
}
```

Expanded: the `symbols` list applies to **every** file matched by `pattern`. If the agent only meant to claim `handleLogin` in `login.ts`, they should pass two separate claims.

## Overlap algorithm

`compute_overlap` (heuristic and repo-rooted modes) keeps its current contract — given two patterns, return the path set intersection — but the conflict pipeline gains a post-filter step. Pseudocode:

```
def is_overlap(holder_claim, requester_claim) -> OverlapResult:
    path_intersection = compute_overlap(holder.pattern, requester.pattern)
    if not path_intersection:
        return NO_OVERLAP

    # Today's behaviour for backwards compat
    if holder.scope_type == 'file' and requester.scope_type == 'file':
        return FILE_OVERLAP(path_intersection)

    # Symbol-disjoint case: auto-coexist, no 409
    if holder.scope_type == 'symbol' and requester.scope_type == 'symbol':
        intersecting_files = path_intersection
        symbol_overlap = []
        for f in intersecting_files:
            holder_syms = symbols_for(holder.id, f)
            req_syms = symbols_for(requester.id_or_pending, f)
            both = holder_syms & req_syms
            if both:
                symbol_overlap.append((f, sorted(both)))
        if not symbol_overlap:
            return AUTO_COEXIST(path_intersection)
        return SYMBOL_OVERLAP(symbol_overlap)

    # Mixed: file-scope holder + symbol-scope requester
    if holder.scope_type == 'file' and requester.scope_type == 'symbol':
        if holder.claim_type == 'shared_file' or not holder.narrowable:
            return FILE_OVERLAP(path_intersection)
        return AUTO_NARROW(holder, requester)

    # Mixed: symbol-scope holder + file-scope requester
    if holder.scope_type == 'symbol' and requester.scope_type == 'file':
        if not requester_accepts_partial_scope():
            return FILE_OVERLAP(path_intersection)
        return PARTIAL_GRANT(requester_path_minus_held_symbols)
```

`AUTO_COEXIST` and `AUTO_NARROW` are server-side automatic decisions logged to `request_events` as new event types (`auto-coexist`, `auto-narrow`) but skip the `requests` flow entirely — there is no human-in-the-loop request to file because the resolution is mechanical and free of policy ambiguity. Audit trail is preserved; latency is one round-trip lower.

### Worked example

Holder claim:
```
{type=file, pattern=src/auth/login.ts, scope_type=file, narrowable=true}
```

Requester comes in with:
```
{type=file, pattern=src/auth/login.ts, scope_type=symbol, symbols=[handleLogin]}
```

Path intersection: `{src/auth/login.ts}`. Holder is `file`/narrowable=true, requester is `symbol`. → `AUTO_NARROW`. Server-side:

1. Update holder row: `coexists_with=[requester_claim_id]`, no scope_type change (still file, but conceptually "file minus claimed symbols").
2. Create requester row: `scope_type=symbol`, `coexists_with=[holder_claim_id]`, symbols populated.
3. Append to `request_events`: `event_type='auto-narrow'`, actor=server, detail={holder_id, requester_id, file, symbols}.
4. Return `201` to requester.
5. Holder learns about the narrow on its next `pending_requests` poll: a row with `kind='auto-narrow-notice'` informs them of the new partner. No action required.

The holder's effective scope is "the file minus the partners' symbols". When the holder edits anything outside those symbols, it sees no conflict. When the holder tries to edit `handleLogin`, it's on them to coordinate with the partner directly (coord is advisory not enforcing -- same posture as `coexist` today).

### Worked example: bare class vs method (v0.16)

Holder claim:
```
{type=file, pattern=src/auth/router.ts, scope_type=symbol, symbols=[Router]}
```
Stored: one `claim_symbols` row with `symbol_name='Router'`, `parent_symbol=NULL`, `symbol_kind='class'`.

Requester:
```
{type=file, pattern=src/auth/router.ts, scope_type=symbol, symbols=[Router::handleAuth]}
```
Stored at insert time as `symbol_name='handleAuth'`, `parent_symbol='Router'`.

Path intersection: `{src/auth/router.ts}`. Both symbol-scoped. Symbol overlap check: the holder's bare-class row (`Router`, parent NULL) matches the requester's row whose `parent_symbol='Router'`. `SYMBOL_OVERLAP({src/auth/router.ts: [Router::handleAuth]})` -> `409`. The reverse direction (holder = method, requester = bare class) symmetrically blocks.

### Worked example: sibling methods (v0.16)

Holder claim:
```
{type=file, pattern=src/auth/router.ts, scope_type=symbol, symbols=[Router::handleA]}
```
Requester:
```
{type=file, pattern=src/auth/router.ts, scope_type=symbol, symbols=[Router::handleB]}
```

Path intersection: `{src/auth/router.ts}`. Both rows have `parent_symbol='Router'` but distinct `symbol_name`. Symbol overlap is empty: `AUTO_COEXIST` -> `201`, both claims live with mutual `coexists_with`. A third agent claiming the bare `Router` then blocks against both partners (bare-class vs method rule above).

## State machine deltas

v0.11 decisions stay: `approved | denied | narrowed | coexist`. v0.14 adds two **automatic** decisions that bypass the `requests` table:

- `auto-coexist`: server granted both claims because their symbol sets were disjoint on the overlapping file(s).
- `auto-narrow`: server granted the requester's symbol claim alongside an existing narrowable file claim, marking them as coexisting partners.

`request_events.event_type` gains `'auto-coexist'` and `'auto-narrow'`. These rows have `request_id=NULL` (no request was filed); the join becomes a left-join. The dashboard can surface them under "today's auto-resolutions" so operators see the volume.

A new `pending_requests` row kind, `'auto-narrow-notice'`, surfaces auto-narrows to the holder. The holder cannot reject (decision was already taken) but can `request_release` against the new partner if they need to escalate back to a full file claim.

## API surface

### `POST /claims`

Request body extended:

```json
{
  "engineer": "alex/claude/main",
  "branch": "alex/auth-refactor",
  "claims": [
    {
      "type": "file",
      "pattern": "src/auth/login.ts",
      "symbols": ["handleLogin", "validateCredentials"],
      "narrowable": false
    },
    {
      "type": "file",
      "pattern": "src/auth/logout.ts"
    }
  ],
  "ttl_hours": 2
}
```

`symbols` is optional. When present and non-empty, the claim becomes `scope_type='symbol'`; when absent or empty, `scope_type='file'`. `narrowable` is optional with the defaults listed in the Data Model section.

Response shape unchanged for file claims. For symbol claims, the `claims[].symbols` array is echoed back and the response's `conflicts` payload gains a `symbol_overlap` field per entry when overlap is symbol-level:

```json
{
  "claim_ids": [],
  "conflicts": [
    {
      "your_pattern": "src/auth/login.ts",
      "your_symbols": ["handleLogin"],
      "conflicting_claim": {
        "id": "claim-id",
        "engineer": "bob/claude/main",
        "pattern": "src/auth/login.ts",
        "scope_type": "symbol",
        "symbols": ["handleLogin", "logSignin"]
      },
      "symbol_overlap": [
        {"file": "src/auth/login.ts", "symbols": ["handleLogin"]}
      ]
    }
  ]
}
```

### `GET /conflicts`

Query string gains `symbol=<file>::<name>` (repeatable). The result groups overlaps as before but distinguishes file-level vs symbol-level rows so the pre-push hook can render a useful diff.

### MCP

`claim_files` gains an optional `symbols: dict[str, list[str]] | None = None` parameter. Keys are file paths (each must also appear in `patterns` or `shared_files`), values are symbol names within that file. A `narrowable: bool | None = None` parameter mirrors the API field. Pseudocode:

```python
@mcp.tool()
async def claim_files(
    engineer: str,
    patterns: list[str],
    description: str | None = None,
    branch: str | None = None,
    shared_files: list[str] | None = None,
    ttl_hours: int | None = None,
    symbols: dict[str, list[str]] | None = None,
    narrowable: bool | None = None,
) -> dict[str, Any]:
    ...
```

No new tool. Keeps the agent-facing surface narrow.

## Parser strategy

The MCP wrapper (running in the developer's repo, with cheap filesystem access) does the symbol extraction at claim time. The server validates against its own parse if `COORD_REPO_ROOT` is set; if the two disagree, the client wins for v1 (server logs the discrepancy as a `parser_drift` event).

Parser interface (`coordination/symbols/__init__.py`):

```python
@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str  # 'function' | 'class' | 'interface' | 'type' | 'const' | 'enum' | 'unknown'
    start_line: int  # 1-indexed
    end_line: int

def extract_symbols(file_path: str, content: str) -> list[Symbol]:
    """Return top-level declared symbols in this file.

    Dispatches by file extension. Unsupported extensions return [].
    """
```

### TypeScript backend

Two parsers:

1. **tree-sitter** (`coordination/symbols/ts_treesitter.py`): uses `tree-sitter-typescript` from PyPI. Walks the parse tree, picks top-level `function_declaration`, `class_declaration`, `interface_declaration`, `type_alias_declaration`, `enum_declaration`, plus `lexical_declaration` (`const`/`let`/`var`) when the binding is a function expression or arrow function. Correct under decorators, generics, and JSX. Adds a build-time dependency.

2. **regex fallback** (`coordination/symbols/ts_regex.py`): pattern matches against `^export?\s*(function|class|interface|type|enum|const|let|var)\s+(\w+)`. Misses decorated declarations, mistakes some assignments. Good enough for the 90% case when tree-sitter isn't installed.

Selection: `COORD_SYMBOL_PARSER=treesitter|regex|auto` (default `auto` = try tree-sitter, fall back to regex). `coord doctor` warns when `auto` resolves to `regex`.

### Future backends

`coordination/symbols/py_treesitter.py`, `coordination/symbols/go_treesitter.py` follow the same interface. v0.15 target.

## Migration + backward compat

v8 migration is two columns + one new table. Existing rows backfill cleanly:

- `scope_type` defaults to `'file'`; existing rows behave exactly as today.
- `narrowable` defaults to `true`; existing rows are eligible for auto-narrow. This is a behaviour change for pre-v0.14 holders, but a safe one: a narrow only happens when a symbol-scope requester comes in, and pre-v0.14 clients can't produce those.

Pre-v0.14 clients:

- Old `coord-mcp` calls `POST /claims` without `symbols`. Server creates `scope_type='file'`. No behaviour change.
- Old `coord-mcp` receives a conflict response that may include `symbol_overlap` for newer holders. The client ignores the unknown field. Behaviour: 409 as before.
- Old `GET /conflicts` returns the union of file-scope and symbol-scope conflicts. Symbol-scope conflicts with non-overlapping symbols are filtered to `safe=true` (because they would have auto-coexisted on `POST /claims`). Old clients see fewer false positives, never more.

`coord upgrade` (when a project's coord version is bumped) no longer needs to do anything for v0.14: the new schema lives server-side, and the MCP wrapper auto-discovers the new tool args from FastMCP introspection.

## Rollout

- **v0.14.0**: schema v8, parser layer (TS), service-layer overlap, API surface, MCP wrapper, doctor check, design doc + integration doc updates. Marked **experimental** in CHANGELOG -- symbol overlap is opt-in via the `symbols` field; default behaviour for any caller that doesn't pass it is identical to v0.13.
- **v0.14.1**: dashboard surfaces symbol claims; `auto-coexist` / `auto-narrow` count is exposed under "/repos". Tree-sitter is upgraded from soft to hard dependency if metrics show <5% regex fallback.
- **v0.15.0**: Python + Go parsers. Symbol claims marked stable.
- **v0.16.0** (shipped): methods inside classes are individually claimable via `Parent::child` notation. Schema v10 adds `claim_symbols.parent_symbol`; the overlap algorithm gains a two-level prefix-matching rule so a bare-class claim and a method claim auto-block, while two sibling methods auto-coexist. TS/Python/Go parsers record the enclosing class / receiver type as `parent`. Non-method nested classes and nested namespaces remain out of scope -- candidate for v0.17.

## Open questions

1. **Class methods.** v1 makes a class claim cover all its methods. Is that too coarse for large classes (e.g. a `Router` with 30 routes)? **Shipped in v0.16:** methods are individually claimable via `Parent::child` notation. A claim on the bare class still covers every method (auto-blocks any `Parent::*` method claim, and vice versa); two sibling methods auto-coexist. Non-method nested classes are intentionally out of scope for v0.16 -- candidate for v0.17.
2. **Renames.** If a holder claims `handleLogin` and the file is renamed mid-claim (or the symbol is renamed), the claim becomes orphaned. v1 behaviour: the claim survives (no enforcement), the pre-push hook will report stale symbols. v2 could re-validate on every conflict check, but that's expensive without an LSP.
3. **Anonymous default export** (`export default function(...)`). v1 normalises to symbol name `default`. Two such claims on the same file overlap.
4. **JSX inline components** (`const Foo = (props) => …`). v1 catches these via the `lexical_declaration` rule. Decorated React components may slip through the regex fallback.
5. **Server-side parser dependency**. Should the server bundle tree-sitter so it can validate independently? Decision: only when `COORD_REPO_ROOT` is set and the operator opts in via `COORD_SERVER_PARSE=true`. Default off; client-supplied symbols are trusted.

## Test plan

Each implementation chunk lands with tests in the existing pytest harness:

- `tests/test_db_migration.py`: v7→v8 round-trip, backfill values, new index presence, rollback safety.
- `tests/test_symbol_parser.py`: TS fixtures covering function, class, interface, type, const-as-arrow, default export, generics, decorators. Tree-sitter + regex both tested via parametrise.
- `tests/test_overlap_symbols.py`: full overlap matrix — file/file, file/symbol (narrowable + non-narrowable), symbol/file, symbol/symbol (disjoint + overlapping), with multi-file patterns.
- `tests/test_api.py` additions: POST /claims accepts `symbols`, returns `symbol_overlap` in conflicts, auto-coexist returns 201 with `coexists_with`, auto-narrow updates holder's `coexists_with`.
- `tests/test_mcp_server.py` additions: `claim_files(symbols=...)` propagates correctly via httpx MockTransport.

Target: +60-80 tests, ~95% line coverage on new code.
