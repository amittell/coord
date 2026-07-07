"""PostgreSQL backend for coord (HA re-architecture, design Sections 5-7).

This module ships the :class:`PostgresStore` -- a drop-in subclass of
:class:`coordination.db.Database` that runs the *identical* method bodies
against PostgreSQL instead of SQLite. It does so without rewriting any of
the ~75 query methods: every non-grant method funnels through
``self._connect()`` and every grant-critical method through
``self._acquire()`` / ``self.transaction()`` (the seams introduced in P1).
This module overrides those seams to hand back an asyncpg connection
wrapped in an aiosqlite-shaped adapter (:class:`_PGConnAdapter`) that
translates the SQLite dialect on the fly (design Section 6).

Correctness (design Section 5): the per-repo advisory lock
(:meth:`PostgresStore.repo_lock`) and the per-engineer cap lock
(:meth:`PostgresStore.engineer_lock`) are real ``pg_advisory_xact_lock``
calls on the very connection the grant transaction commits on, keyed on
``coalesce(repo, '')`` (design 5.4 -- the NULL bucket is locked, never
unserialized) and ``'eng:'||engineer`` (design 5.3) respectively. The
single-leader lease (:meth:`acquire_leader_lease`, design Section 6) keeps
the background loops on exactly one replica.

Isolation: each :class:`PostgresStore` instance derives a private
PostgreSQL *schema* from its ``path`` so that tests pointed at distinct
temp paths never see each other's rows (the SQLite tests rely on a fresh
file per ``tmp_path``); two stores on the same path share a schema, which
is exactly the SQLite same-file semantics. The advisory-lock keys fold in
the schema name so locks never collide across isolated schemas.

Backend selection is by ``COORD_DATABASE_URL`` (``postgresql://``); the
default SQLite path is byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

try:
    import asyncpg
except ModuleNotFoundError:  # the driver is only needed for the Postgres
    # backend; the SQLite path and the pure SQL-translation layer
    # (translate/_skip_noncode) import fine without it, so a dev/test env
    # with only [dev] installed can still exercise the translation tests.
    asyncpg = None

from coordination.db import _BOUND_CONN, Database, _LOCK_SKIPPED

# Fixed seed for hashtextextended(text, int8). The advisory-lock namespace
# is carried in the text argument (schema + bucket + key), so a constant
# seed is sufficient; it only needs to be stable across replicas.
_NS_SEED = 0

# ---------------------------------------------------------------------------
# Connection pool (one asyncpg pool, re-created per event loop).
#
# pytest-asyncio gives most tests a fresh event loop, and an asyncpg pool is
# bound to the loop it was created on. We keep a single global pool and, when
# the running loop (or DSN) changes, synchronously ``terminate()`` the stale
# pool (its loop is already dead, so we cannot await ``close()``) and build a
# fresh one. ``min_size=0`` means a stale pool holds no idle connections, and
# ``terminate()`` drops any open ones, so the live connection count stays
# bounded by ``max_size`` regardless of how many test loops have come and gone.
# ---------------------------------------------------------------------------
_POOL: asyncpg.Pool | None = None
_POOL_LOOP: asyncio.AbstractEventLoop | None = None
_POOL_DSN: str | None = None
# Guards pool creation so two concurrent first-requests on the same loop cannot
# both observe ``_POOL is None`` across the ``await create_pool`` suspension and
# build (then leak) two pools. Keyed per running loop because an asyncio.Lock is
# bound to the loop it is used on; the dict insert itself is a synchronous,
# atomic step on a single-threaded loop so the get-or-create needs no lock.
_POOL_LOCKS: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def terminate_pool() -> None:
    """Forcibly drop the global pool (synchronous, no await). Closes every
    connection's transport immediately, so any task still blocked on a PG
    operation unblocks with a connection error instead of hanging. The test
    harness calls this after each test to drain leaked background tasks (e.g.
    wave-2 callsite enrichment) before the event loop closes."""
    global _POOL, _POOL_LOOP, _POOL_DSN
    if _POOL is not None:
        try:
            _POOL.terminate()
        except Exception:
            pass
    _POOL = None
    _POOL_LOOP = None
    _POOL_DSN = None
    # Drop the per-loop creation locks too; their loops are being torn down and
    # the next _get_pool rebuilds the lock for whatever loop is then running.
    _POOL_LOCKS.clear()


def _stale(loop: asyncio.AbstractEventLoop, dsn: str) -> bool:
    return _POOL is not None and (
        _POOL_LOOP is not loop
        or _POOL_DSN != dsn
        or getattr(_POOL, "_closed", False)
    )


async def _get_pool(dsn: str) -> asyncpg.Pool:
    global _POOL, _POOL_LOOP, _POOL_DSN
    loop = asyncio.get_running_loop()
    # Fast path: a live pool on this loop+DSN already exists.
    if _POOL is not None and not _stale(loop, dsn):
        return _POOL
    # Slow path: create (or replace a stale) pool under a per-loop lock so two
    # concurrent first-requests serialize and exactly one pool is built. The
    # get-or-create of the lock is synchronous (no await), hence race-free on a
    # single-threaded loop.
    lock = _POOL_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _POOL_LOCKS[loop] = lock
    async with lock:
        # Re-check under the lock: a peer may have built the pool while we
        # waited, in which case we must not create (and leak) a second one.
        if _POOL is not None and not _stale(loop, dsn):
            return _POOL
        if _POOL is not None and _stale(loop, dsn):
            try:
                _POOL.terminate()
            except Exception:
                pass
            _POOL = None
        if _POOL is None:
            _POOL = await asyncpg.create_pool(
                dsn,
                min_size=0,
                max_size=10,
                # See _PGConnAdapter: we SET search_path per acquire, so a
                # cached plan prepared under one schema must never be reused
                # under another. Disabling the statement cache forces
                # re-resolution.
                statement_cache_size=0,
            )
            _POOL_LOOP = loop
            _POOL_DSN = dsn
    return _POOL


# ---------------------------------------------------------------------------
# Consolidated PG v1 schema (design Section 7.4): the SQLite schema *after*
# migration 18, generated by introspecting a freshly-migrated in-memory
# SQLite database and translating its DDL. Generating it from the real
# migration chain guarantees parity instead of a hand-maintained copy that
# could drift.
# ---------------------------------------------------------------------------
_SCHEMA_BODY: str | None = None


def _translate_ddl(sql: str) -> str:
    """Translate one SQLite ``CREATE TABLE``/``CREATE INDEX`` to PG.

    The only incompatibility in the post-migration-18 schema is the
    ``BOOLEAN`` affinity on ``claims.ttl_shortened`` / ``claims.narrowable``:
    SQLite stores 0/1 integers there and the code reads/writes plain ints,
    so we map it to ``INTEGER`` to keep those integer params valid. Partial
    indexes (``WHERE revoked_at IS NULL``), ``CHECK`` constraints, foreign
    keys and ``IF NOT EXISTS`` are all valid PG already.
    """
    return re.sub(r"\bBOOLEAN\b", "INTEGER", sql, flags=re.IGNORECASE)


def _build_schema_body() -> str:
    """Generate the consolidated post-migration-18 DDL by replaying the real
    ``MIGRATIONS`` chain into an in-memory SQLite DB and introspecting
    ``sqlite_master``. Run synchronously through ``sqlite3`` (not aiosqlite)
    so it is immune to the test harness's aiosqlite redirection and never
    re-enters the migration via the dispatched ``Database`` constructor."""
    global _SCHEMA_BODY
    if _SCHEMA_BODY is not None:
        return _SCHEMA_BODY
    import sqlite3

    from coordination.db import MIGRATIONS, _split_sql_statements

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
            "version INTEGER NOT NULL)"
        )
        for _version, sql in sorted(MIGRATIONS):
            for stmt in _split_sql_statements(sql):
                conn.execute(stmt)
        rows = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "AND name <> 'schema_version' ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    _SCHEMA_BODY = ";\n".join(_translate_ddl(s) for (s,) in rows)
    return _SCHEMA_BODY


def _full_schema_sql(body: str) -> str:
    """The complete bootstrap script for one schema: version table, all
    consolidated tables/indexes, the leader-lease table, and the v18 stamp."""
    return (
        "CREATE TABLE IF NOT EXISTS schema_version (\n"
        "    id INTEGER PRIMARY KEY CHECK (id = 1),\n"
        "    version INTEGER NOT NULL\n"
        ");\n"
        + body
        + ";\n"
        "CREATE TABLE IF NOT EXISTS coord_leader_lease (\n"
        "    lease_name TEXT PRIMARY KEY,\n"
        "    holder_id TEXT NOT NULL,\n"
        "    expires_at TIMESTAMPTZ NOT NULL\n"
        ");\n"
        "INSERT INTO schema_version (id, version) VALUES (1, 18) "
        "ON CONFLICT (id) DO NOTHING;"
    )


# ---------------------------------------------------------------------------
# Dialect translation (design Section 6). Translates the SQLite SQL embedded
# in Database methods to PostgreSQL. Timestamps stay stored as ISO-8601 TEXT
# (byte-identical to SQLite, so string params need no coercion); the time
# *functions* that SQLite evaluates over that TEXT are rewritten to cast to
# timestamptz at the call site, which is the equivalent computation.
# ---------------------------------------------------------------------------


def _skip_noncode(sql: str, i: int) -> int | None:
    """If ``sql[i]`` begins a lexical span that must never be rewritten -- a
    single-quoted string literal (``''`` escape), a double-quoted identifier
    (``""`` escape), a ``--`` line comment, or a ``/* */`` block comment --
    return the index just past that span; otherwise return ``None``.

    This is the single shared skip-strings/comments primitive every rewriter
    (:func:`_replace_calls`, :func:`_convert_placeholders`,
    :func:`_scan_matching_paren`, :func:`_code_search`) uses, so a ``?``, a
    function name, or a paren inside a literal/comment/identifier is never
    mangled. Newlines are left as code (they are plain whitespace), so a line
    comment ends *at* the newline, not past it."""
    n = len(sql)
    c = sql[i]
    if c == "'" or c == '"':
        quote = c
        j = i + 1
        while j < n:
            if sql[j] == quote:
                if j + 1 < n and sql[j + 1] == quote:
                    j += 2
                    continue
                return j + 1
            j += 1
        return n
    if c == "-" and i + 1 < n and sql[i + 1] == "-":
        nl = sql.find("\n", i)
        return n if nl < 0 else nl
    if c == "/" and i + 1 < n and sql[i + 1] == "*":
        end = sql.find("*/", i + 2)
        return n if end < 0 else end + 2
    return None


def _scan_matching_paren(sql: str, open_idx: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_idx``, skipping over
    string literals, quoted identifiers and comments (via
    :func:`_skip_noncode`) so a paren inside any of those does not throw off
    the depth count."""
    depth = 0
    i = open_idx
    n = len(sql)
    while i < n:
        skip = _skip_noncode(sql, i)
        if skip is not None:
            i = skip
            continue
        c = sql[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced parentheses in SQL")


def _split_top_level(s: str) -> list[str]:
    """Split ``s`` on top-level commas (outside parens and strings)."""
    parts: list[str] = []
    depth = 0
    in_str = False
    buf: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if in_str:
            buf.append(c)
            if c == "'":
                if i + 1 < n and s[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            buf.append(c)
        elif c == "(":
            depth += 1
            buf.append(c)
        elif c == ")":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _code_search(sql: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    """First match of ``pattern`` that begins in *code* (outside any string
    literal, quoted identifier or comment). Used by the fail-fast guards."""
    i = 0
    n = len(sql)
    while i < n:
        skip = _skip_noncode(sql, i)
        if skip is not None:
            i = skip
            continue
        m = pattern.match(sql, i)
        if m:
            return m
        i += 1
    return None


def _replace_calls(sql: str, func: str, transform) -> str:
    """Replace every ``func(...)`` call (case-insensitive, whole word) with
    ``transform(inner_args_string)``, handling nested parens.

    The scan is lexical: it skips string literals, quoted identifiers and
    comments (via :func:`_skip_noncode`) so a ``func(`` that appears inside a
    ``'...'`` literal, a ``"..."`` identifier or a ``-- / /* */`` comment is
    left untouched. Only a real call in code position is rewritten."""
    pattern = re.compile(r"\b" + re.escape(func) + r"\s*\(", re.IGNORECASE)
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        skip = _skip_noncode(sql, i)
        if skip is not None:
            out.append(sql[i:skip])
            i = skip
            continue
        m = pattern.match(sql, i)
        if m:
            open_idx = m.end() - 1
            close_idx = _scan_matching_paren(sql, open_idx)
            inner = sql[open_idx + 1 : close_idx]
            out.append(transform(inner))
            i = close_idx + 1
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def _ts_expr(expr: str) -> str:
    """Render a SQLite datetime operand as a PG timestamptz expression.
    A literal ``'now'`` becomes ``now()``; everything else (a TEXT column or
    a ``?`` parameter) is cast through text so an ISO-8601 string param is
    accepted by asyncpg (the param type resolves to text, not timestamptz)."""
    s = expr.strip()
    if s.lower() == "'now'":
        return "now()"
    return f"((({expr})::text)::timestamptz)"


def _t_datetime(inner: str) -> str:
    # SQLite ``datetime()`` accepts trailing modifier args (e.g.
    # ``datetime('now','-1 hour')``); none are used in this codebase and the
    # single-operand ``_ts_expr`` cast cannot express them. Fail fast on the
    # multi-arg shape so a future caller gets a loud error instead of a
    # silently mis-evaluated timestamp.
    parts = _split_top_level(inner)
    if len(parts) != 1:
        raise ValueError(
            f"unsupported datetime() modifier form (arity {len(parts)}): {inner!r}"
        )
    return _ts_expr(inner)


def _t_strftime(inner: str) -> str:
    parts = _split_top_level(inner)
    if len(parts) != 2:
        raise ValueError(f"unsupported strftime() arity: {inner!r}")
    fmt = parts[0].strip()
    ts = _ts_expr(parts[1])
    if fmt == "'%s'":
        return f"extract(epoch from {ts})"
    if fmt == "'%Y-%m-%d'":
        return f"to_char({ts}, 'YYYY-MM-DD')"
    raise ValueError(f"unsupported strftime() format: {fmt!r}")


def _t_json_extract(inner: str) -> str:
    parts = _split_top_level(inner)
    if len(parts) != 2:
        raise ValueError(f"unsupported json_extract() arity: {inner!r}")
    expr = parts[0].strip()
    path = parts[1].strip().strip("'")
    key = path[2:] if path.startswith("$.") else path
    return f"(({expr})::jsonb ->> '{key}')"


def _t_minmax(name: str, pg_scalar: str):
    """SQLite overloads ``MAX``/``MIN`` as both aggregates (1 arg) and scalar
    many-arg functions; PostgreSQL splits them (aggregate vs GREATEST/LEAST).
    Rewrite the 2+-arg scalar form, leave the 1-arg aggregate untouched."""

    def transform(inner: str) -> str:
        args = _split_top_level(inner)
        if len(args) >= 2:
            # NULL semantics differ: SQLite's scalar MAX/MIN returns NULL if
            # ANY argument is NULL, but PG GREATEST/LEAST ignore NULLs. Wrap in
            # a CASE that reproduces SQLite's all-or-nothing behaviour exactly,
            # regardless of whether the operands are nullable by schema.
            null_guard = " OR ".join(f"({a.strip()}) IS NULL" for a in args)
            return f"(CASE WHEN {null_guard} THEN NULL ELSE {pg_scalar}({inner}) END)"
        return f"{name}({inner})"

    return transform


def _t_group_concat(inner: str) -> str:
    s = inner.strip()
    distinct = ""
    if s[:8].upper() == "DISTINCT":
        distinct = "DISTINCT "
        s = s[8:].strip()
    parts = _split_top_level(s)
    if len(parts) == 2:
        col = parts[0].strip()
        sep = parts[1].strip()
    else:
        col = s
        sep = "','"
    return f"string_agg({distinct}({col})::text, {sep})"


def _translate_insert(sql: str) -> str:
    """``INSERT OR IGNORE`` -> ``... ON CONFLICT DO NOTHING``;
    ``INSERT OR REPLACE INTO t (c0, c1, ...)`` -> upsert on the first column
    (the primary key in both call sites: schema_version.id / ownership_config.id)."""
    stripped = sql.strip()
    low = stripped.lower()
    if low.startswith("insert or ignore"):
        body = "INSERT" + stripped[len("insert or ignore") :]
        body = body.rstrip().rstrip(";").rstrip()
        return body + " ON CONFLICT DO NOTHING"
    if low.startswith("insert or replace"):
        m = re.match(
            r"(?is)INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*(.*)",
            stripped,
        )
        if not m:
            raise ValueError(f"cannot translate INSERT OR REPLACE: {sql!r}")
        table = m.group(1)
        cols_raw = m.group(2)
        rest = m.group(3).rstrip().rstrip(";").rstrip()
        cols = [c.strip() for c in cols_raw.split(",")]
        pk = cols[0]
        # The generic "first column is the conflict key" rule is only valid for
        # the two singleton-config tables that actually use INSERT OR REPLACE,
        # whose PK is the leading ``id`` column. Anything else (e.g. a multi-
        # column or non-leading PK) would upsert on the wrong key. Restrict to
        # the known-safe shapes and fail fast on anything new.
        _KNOWN_REPLACE_PK = {"schema_version": "id", "ownership_config": "id"}
        expected_pk = _KNOWN_REPLACE_PK.get(table)
        if expected_pk is None or pk != expected_pk:
            raise ValueError(
                "INSERT OR REPLACE is only translatable for "
                f"{sorted(_KNOWN_REPLACE_PK)} on their 'id' PK; got "
                f"table={table!r} leading_col={pk!r}"
            )
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
        return (
            f"INSERT INTO {table} ({cols_raw}) {rest} "
            f"ON CONFLICT ({pk}) DO UPDATE SET {updates}"
        )
    return sql


def _convert_placeholders(sql: str) -> str:
    """Rewrite SQLite ``?`` positional params to PG ``$1``, ``$2``, ...,
    skipping any ``?`` inside a string literal, quoted identifier or comment
    (via :func:`_skip_noncode`, the same lexical primitive
    :func:`_replace_calls` uses, so the two rewriters agree on what is code)."""
    out: list[str] = []
    n = 0
    i = 0
    length = len(sql)
    while i < length:
        skip = _skip_noncode(sql, i)
        if skip is not None:
            out.append(sql[i:skip])
            i = skip
            continue
        c = sql[i]
        if c == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(c)
        i += 1
    return "".join(out)


_UNSUPPORTED_TIME_FUNC = re.compile(r"\b(julianday|unixepoch)\s*\(", re.IGNORECASE)


def translate(sql: str) -> str:
    """Full SQLite -> PostgreSQL translation for a single statement."""
    # Fail fast on SQLite time functions we deliberately do not translate
    # (julianday, the unixepoch() function form). None are used today; if one
    # is ever introduced this raises at translate time rather than emitting SQL
    # that PG silently mis-runs or rejects far from the source. ``datetime`` and
    # ``strftime`` modifier shapes are rejected in their own transforms.
    bad = _code_search(sql, _UNSUPPORTED_TIME_FUNC)
    if bad is not None:
        raise ValueError(
            f"unsupported SQLite time function {bad.group(0)!r} in: {sql!r}"
        )
    sql = _translate_insert(sql)
    sql = _replace_calls(sql, "json_extract", _t_json_extract)
    sql = _replace_calls(sql, "strftime", _t_strftime)
    sql = _replace_calls(sql, "datetime", _t_datetime)
    sql = _replace_calls(sql, "GROUP_CONCAT", _t_group_concat)
    sql = _replace_calls(sql, "MAX", _t_minmax("MAX", "GREATEST"))
    sql = _replace_calls(sql, "MIN", _t_minmax("MIN", "LEAST"))
    # SQLite's null-safe ``col IS ?`` / ``col IS NOT ?`` (used for the nullable
    # ``repo`` bucket) is a syntax error in PG, where ``IS`` only pairs with
    # NULL/TRUE/FALSE/DISTINCT FROM. Map to the null-safe PG equivalents.
    sql = re.sub(r"\bIS\s+NOT\s+\?", "IS DISTINCT FROM ?", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bIS\s+\?", "IS NOT DISTINCT FROM ?", sql, flags=re.IGNORECASE)
    sql = _convert_placeholders(sql)
    return sql


def _coerce_params(params) -> list[Any]:
    """asyncpg is strictly typed: a Python ``bool`` will not encode into an
    ``INTEGER`` column (our translated BOOLEANs). The codebase only ever
    passes 0/1 ints there, but we coerce defensively so a stray bool param
    never raises mid-suite."""
    out: list[Any] = []
    for p in params:
        if isinstance(p, bool):
            out.append(1 if p else 0)
        else:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# aiosqlite-shaped adapter around an asyncpg connection.
# ---------------------------------------------------------------------------


class _PGCursor:
    """Minimal aiosqlite-cursor lookalike over a materialized asyncpg result."""

    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self._i = 0
        self.rowcount = rowcount

    async def fetchone(self):
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    async def fetchall(self):
        rows = self._rows[self._i :]
        self._i = len(self._rows)
        return rows

    async def fetchmany(self, size: int = 1):
        rows = self._rows[self._i : self._i + size]
        self._i += len(rows)
        return rows


def _as_integrity_error(exc: Exception) -> aiosqlite.IntegrityError:
    """Map asyncpg constraint violations (unique / FK / not-null / check) to
    ``aiosqlite.IntegrityError`` (== ``sqlite3.IntegrityError``) so callers and
    tests that branch on the SQLite exception type behave identically on PG."""
    return aiosqlite.IntegrityError(str(exc))


def _parse_rowcount(status: str | None) -> int:
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


class _PGConnAdapter:
    """Presents an asyncpg connection through the slice of the
    aiosqlite.Connection API the Database methods use: ``execute`` (returning
    a cursor), ``executemany``, ``executescript``, ``commit``, ``rollback``,
    and a settable ``row_factory`` (ignored -- asyncpg Records already index
    by name *and* position and convert via ``dict()``).

    Transaction model mirrors SQLite's deferred isolation: outside a bound
    grant transaction (``managed=False``) the first statement lazily opens an
    asyncpg transaction and ``commit()`` ends it (methods call ``commit()``
    themselves when they own the connection), so a multi-statement method is
    atomic exactly as it is under SQLite's implicit transaction. Inside
    :meth:`PostgresStore.transaction` (``managed=True``) the outer
    unit-of-work owns commit/rollback and these are no-ops.
    """

    def __init__(self, raw: asyncpg.Connection, *, managed: bool) -> None:
        self._raw = raw
        self._managed = managed
        self._tx: Any = None
        self.row_factory = None  # accepted and ignored

    async def _ensure_tx(self) -> None:
        if self._managed:
            return
        if self._tx is None:
            self._tx = self._raw.transaction()
            await self._tx.start()

    async def commit(self) -> None:
        if self._managed:
            return
        if self._tx is not None:
            await self._tx.commit()
            self._tx = None

    async def rollback(self) -> None:
        if self._managed:
            return
        if self._tx is not None:
            await self._tx.rollback()
            self._tx = None

    # Aliases used by the connection lifecycle in PostgresStore.
    _commit_open = commit
    _rollback_open = rollback

    def _pragma_cursor(self, sql: str) -> _PGCursor:
        # SQLite PRAGMAs are no-ops on PG. _set_wal_mode inspects the result
        # of "PRAGMA journal_mode=WAL" and expects row[0] == 'wal'.
        low = sql.lower()
        if "journal_mode" in low:
            return _PGCursor([("wal",)], 0)
        return _PGCursor([], 0)

    async def execute(self, sql: str, params=()) -> _PGCursor:
        head = sql.lstrip()[:16].upper()
        if head.startswith("PRAGMA"):
            return self._pragma_cursor(sql)
        if head.startswith("BEGIN"):
            await self._ensure_tx()
            return _PGCursor([], 0)
        if head.startswith("COMMIT"):
            await self.commit()
            return _PGCursor([], 0)
        if head.startswith("ROLLBACK"):
            await self.rollback()
            return _PGCursor([], 0)
        if not self._managed:
            await self._ensure_tx()
        query = translate(sql)
        args = _coerce_params(params)
        try:
            stmt = await self._raw.prepare(query)
            rows = await stmt.fetch(*args)
        except asyncpg.exceptions.IntegrityConstraintViolationError as exc:
            raise _as_integrity_error(exc) from exc
        return _PGCursor(rows, _parse_rowcount(stmt.get_statusmsg()))

    async def executemany(self, sql: str, seq_of_params) -> None:
        if not self._managed:
            await self._ensure_tx()
        query = translate(sql)
        try:
            await self._raw.executemany(
                query, [_coerce_params(p) for p in seq_of_params]
            )
        except asyncpg.exceptions.IntegrityConstraintViolationError as exc:
            raise _as_integrity_error(exc) from exc

    async def executescript(self, sql: str) -> _PGCursor:
        # Only the SQLite init path uses this; provided for completeness.
        await self._raw.execute(sql)
        return _PGCursor([], 0)


# ---------------------------------------------------------------------------
# The store.
# ---------------------------------------------------------------------------


class PostgresStore(Database):
    """asyncpg-backed :class:`Database` (design Sections 5-7)."""

    def __init__(self, path: Path, *, writer_queue: bool = False) -> None:
        # ``writer_queue`` is accepted for signature compatibility with
        # ``Database(path, writer_queue=...)`` construction and deliberately
        # IGNORED: the in-process writer serialization is a SQLite
        # single-writer concern; Postgres is MVCC and its writes must not be
        # serialized through one connection (nor touch the SQLite-specific
        # shared-writer plumbing). super() is called without it so the
        # inherited flag stays False.
        super().__init__(path)
        url = os.environ.get("COORD_DATABASE_URL", "")
        # asyncpg understands postgresql:// and postgres:// DSNs directly.
        self._dsn = url
        self._schema = _schema_for_path(path)
        self._inited = False
        self._init_lock = asyncio.Lock()

    # -- bootstrap ---------------------------------------------------------

    async def init(self) -> None:
        if self._inited:
            return
        async with self._init_lock:
            if self._inited:
                return
            body = _build_schema_body()
            pool = await _get_pool(self._dsn)
            async with pool.acquire() as raw:
                await raw.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                await raw.execute(
                    f'SET search_path TO "{self._schema}", public'
                )
                async with raw.transaction():
                    # Serialize concurrent first-connect across replicas so
                    # two processes never race to create the same schema.
                    await raw.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))",
                        f"{self._schema}:__init__",
                        _NS_SEED,
                    )
                    exists = await raw.fetchval(
                        "SELECT to_regclass($1)",
                        f'"{self._schema}".schema_version',
                    )
                    if exists is None:
                        await raw.execute(_full_schema_sql(body))
            self._inited = True

    async def _set_search_path(self, raw: asyncpg.Connection) -> None:
        await raw.execute(f'SET search_path TO "{self._schema}", public')

    # -- connection seams --------------------------------------------------

    def _connect(self):
        store = self

        @asynccontextmanager
        async def _cm():
            await store.init()
            pool = await _get_pool(store._dsn)
            raw = await pool.acquire()
            try:
                await store._set_search_path(raw)
                adapter = _PGConnAdapter(raw, managed=False)
                try:
                    yield adapter
                except BaseException:
                    await adapter._rollback_open()
                    raise
                else:
                    await adapter._commit_open()
            finally:
                await pool.release(raw)

        return _cm()

    @asynccontextmanager
    async def transaction(self):
        existing = _BOUND_CONN.get()
        if existing is not None:
            yield existing
            return
        await self.init()
        pool = await _get_pool(self._dsn)
        raw = await pool.acquire()
        try:
            await self._set_search_path(raw)
            tx = raw.transaction()
            await tx.start()
            adapter = _PGConnAdapter(raw, managed=True)
            token = _BOUND_CONN.set(adapter)  # type: ignore[arg-type]
            try:
                yield adapter
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
            finally:
                _BOUND_CONN.reset(token)
        finally:
            await pool.release(raw)

    @asynccontextmanager
    async def _acquire(self):
        bound = _BOUND_CONN.get()
        if bound is not None:
            yield bound, False
            return
        await self.init()
        pool = await _get_pool(self._dsn)
        raw = await pool.acquire()
        try:
            await self._set_search_path(raw)
            adapter = _PGConnAdapter(raw, managed=False)
            try:
                yield adapter, True
            except BaseException:
                await adapter._rollback_open()
                raise
            else:
                await adapter._commit_open()
        finally:
            await pool.release(raw)

    # -- locking (design 5.3, 5.4) ----------------------------------------

    @staticmethod
    async def _require_open_tx(conn, who: str) -> None:
        """Guarantee an open transaction on ``conn`` before a
        ``pg_advisory_xact_lock`` is issued on it.

        A transaction-scoped advisory lock only serializes if a transaction is
        actually open: issued outside one, asyncpg autocommits the SELECT in
        its own single-statement transaction and the lock is released the
        instant that statement returns -- leaving the overlap re-check + insert
        UNLOCKED (silent double-grant). Grants always run inside
        :meth:`transaction` (``managed=True``, txn already started); we
        ``_ensure_tx`` defensively (a no-op for the managed adapter, opens the
        lazy txn for an unmanaged one) and then hard-assert the connection is
        in a transaction so a future caller fails loudly instead of running
        unserialized."""
        await conn._ensure_tx()
        if not conn._raw.is_in_transaction():
            raise RuntimeError(
                f"{who} requires an open transaction on the grant connection; "
                "a pg_advisory_xact_lock outside a transaction releases "
                "immediately and would silently double-grant"
            )

    async def repo_lock(self, conn, repo: str | None) -> None:
        # coalesce(repo, '') so the NULL-repo bucket is locked rather than
        # running unserialized (design 5.4). The schema prefix keeps locks
        # private to this store's isolated schema.
        await self._require_open_tx(conn, "repo_lock")
        key = f"{self._schema}:repo:{repo or ''}"
        await conn._raw.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))",
            key,
            _NS_SEED,
        )

    @asynccontextmanager
    async def engineer_lock(self, conn, engineer: str):
        # Per-engineer, transaction-scoped advisory lock (design 5.3): held
        # until the grant commits, so the count+insert for one engineer is
        # serialized across replicas and the per-engineer cap cannot
        # overshoot. Disjoint engineers hash to different keys and never
        # block each other.
        await self._require_open_tx(conn, "engineer_lock")
        key = f"{self._schema}:eng:{engineer}"
        await conn._raw.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, $2))",
            key,
            _NS_SEED,
        )
        yield

    # -- single-leader election (design Section 6) ------------------------

    async def acquire_leader_lease(
        self, *, lease_name: str, holder_id: str, ttl_sec: float
    ) -> bool:
        await self.init()
        pool = await _get_pool(self._dsn)
        async with pool.acquire() as raw:
            await self._set_search_path(raw)
            async with raw.transaction():
                row = await raw.fetchrow(
                    "SELECT holder_id, expires_at FROM coord_leader_lease "
                    "WHERE lease_name = $1 FOR UPDATE",
                    lease_name,
                )
                now = datetime.now(UTC)
                expires_at = now + timedelta(seconds=ttl_sec)
                if row is None:
                    await raw.execute(
                        "INSERT INTO coord_leader_lease "
                        "(lease_name, holder_id, expires_at) VALUES ($1, $2, $3)",
                        lease_name,
                        holder_id,
                        expires_at,
                    )
                    return True
                if row["holder_id"] == holder_id or row["expires_at"] <= now:
                    await raw.execute(
                        "UPDATE coord_leader_lease SET holder_id = $1, "
                        "expires_at = $2 WHERE lease_name = $3",
                        holder_id,
                        expires_at,
                        lease_name,
                    )
                    return True
                return False


def _schema_for_path(path: Path) -> str:
    """Deterministic, valid PG identifier private to one store path. Tests
    use a unique ``tmp_path`` -> a unique schema -> the fresh-database
    isolation the SQLite suite assumes; two stores on one path share it."""
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:24]
    return f"coord_{digest}"


def acquire_instance_lock_pg() -> int:
    """The flock instance lock (db.acquire_instance_lock) is meaningless
    across pods on a shared PG (design Section 7). Return the bypass
    sentinel."""
    return _LOCK_SKIPPED
