#!/usr/bin/env python3
"""Migrate coord durable state (engineer_tokens + ownership_config) from a
source SQLite coord database into a target Postgres coord database.

This is part of the coord HA re-architecture cutover (see
``docs/designs/coord-ha-rearchitecture.md`` Section 8 and
``docs/runbooks/coord-ha-cutover.md``). An empty Postgres would drop the
per-engineer bearer tokens -- and prod runs
``COORD_REQUIRE_PER_ENGINEER_TOKEN=true`` -- which would lock out every agent
until tokens were re-provisioned. It would also drop the auto-promoted
shared-file ownership rules. Both are small, durable, and must survive the
cutover; this script carries them across, and ONLY them.

What this script deliberately does NOT touch
--------------------------------------------
Ephemeral claim state -- ``claims``, ``claim_symbols``, ``claim_queue``,
``requests``, ``request_events``, ``conflict_log``, ``webhook_outbox`` -- is
intentionally left behind. Active claims are re-announced by agents within a
TTL after cutover (idempotent retry makes that safe); open requests/queue
waiters are acknowledged-as-dropped. The webhook outbox is drained on the old
instance BEFORE cutover (a separate runbook step), not migrated here.

Zero new dependencies
---------------------
The source is read with the standard-library ``sqlite3`` module. The target
write is performed by GENERATING idempotent SQL
(``INSERT ... ON CONFLICT DO UPDATE``) and applying it through ``psql``. The
script therefore runs with a stock Python 3 and the ``psql`` client only -- it
does not import asyncpg, SQLAlchemy, or any coord module, so it works before
the dual-backend image is even built.

Idempotency
-----------
Re-running the script against the same source and target converges to the same
state:

* ``engineer_tokens`` rows upsert on the immutable credential identity
  ``token_sha256``; the existing row ``id`` is preserved on conflict so the
  rotation chain (``rotated_from`` pointers) stays intact.
* ``ownership_config`` is the singleton row ``id = 1``; it upserts on ``id``.

Source columns are discovered at runtime (``PRAGMA table_info``), so a source
at an older schema version (fewer columns) migrates cleanly -- absent columns
take their Postgres defaults / NULL.

Usage
-----
Dry run (prints the SQL it would apply and the psql command, writes nothing)::

    python3 scripts/migrate_tokens_ownership.py \\
        --sqlite /data/coordination.db \\
        --postgres-url "$COORD_DATABASE_URL" \\
        --postgres-schema "$COORD_POSTGRES_SCHEMA" \\
        --dry-run

Live import (pipes the generated SQL into psql, atomically)::

    python3 scripts/migrate_tokens_ownership.py \\
        --sqlite /data/coordination.db \\
        --postgres-url "$COORD_DATABASE_URL" \\
        --postgres-schema "$COORD_POSTGRES_SCHEMA"

Self-test (builds a temporary SQLite, generates SQL, asserts invariants;
no Postgres or psql required)::

    python3 scripts/migrate_tokens_ownership.py --self-test
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

# Tables this script migrates, with their idempotency rules.
#
# ``conflict`` is the ON CONFLICT arbiter (a column carrying a UNIQUE / PK
# constraint in the Postgres schema). ``preserve`` lists columns that must NOT
# be overwritten on conflict (kept from the existing target row); everything
# else is refreshed from the source via ``EXCLUDED``.
TOKEN_TABLE = "engineer_tokens"
OWNERSHIP_TABLE = "ownership_config"
DEFAULT_POSTGRES_SCHEMA = "coord"
_POSTGRES_SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")

# The full Postgres column set, used purely for a sanity warning if the source
# carries a column the target schema is not expected to have. The migration
# itself is driven by the source's actual columns, not this list.
EXPECTED_TOKEN_COLUMNS = (
    "id",
    "engineer",
    "token_sha256",
    "description",
    "created_at",
    "revoked_at",
    "last_used_at",
    "expires_at",
    "rotated_from",
    "rotation_grace_until",
    "request_count",
    "last_source_ip",
    "last_user_agent",
    "repo",
)
EXPECTED_OWNERSHIP_COLUMNS = ("id", "yaml_text", "updated_at")

# coord schema version this script was written against (matches
# CURRENT_SCHEMA_VERSION in coordination/db.py at authoring time). Used only to
# emit a warning if the source DB is newer than we know about. Kept in lockstep
# by tests/test_audit_coordination_hardening.py, which asserts equality against
# coordination.db.CURRENT_SCHEMA_VERSION so this constant cannot silently
# drift when a new migration lands.
KNOWN_SCHEMA_VERSION = 19

# Ephemeral tables that MUST never appear in the generated SQL. Asserted by the
# self-test; documented here as the contract.
EPHEMERAL_TABLES = (
    "claims",
    "claim_symbols",
    "claim_symbol_callsites",
    "claim_symbol_renames",
    "claim_queue",
    "requests",
    "request_events",
    "conflict_log",
    "webhook_outbox",
)


def validate_postgres_schema(value: str) -> str:
    """Return a safe application schema name or raise ``ValueError``.

    Keep this validator dependency-free and in lockstep with
    ``coordination.config.validate_postgres_schema``: this migration is meant
    to run with stock Python plus ``psql``, before the coord package is
    necessarily installed.
    """
    if not _POSTGRES_SCHEMA_RE.fullmatch(value):
        raise ValueError(
            "Postgres schema must be a lowercase identifier: 1-63 "
            "characters matching [a-z_][a-z0-9_]*"
        )
    if (
        value == "public"
        or value == "information_schema"
        or value.startswith("pg_")
    ):
        raise ValueError(
            "Postgres schema must not target a system schema "
            "(public, information_schema, or pg_*)"
        )
    return value


def _target_table(postgres_schema: str, table: str) -> str:
    """Render a fully-qualified target table from validated identifiers."""
    schema = validate_postgres_schema(postgres_schema)
    # ``table`` is always one of the two module constants above, not source
    # data. Quote both components so the import never depends on search_path.
    return f'"{schema}"."{table}"'


def _sql_literal(value: object) -> str:
    """Render a Python value read from SQLite as a Postgres SQL literal.

    Single quotes are doubled. Postgres runs with
    ``standard_conforming_strings = on`` by default, so backslashes inside a
    plain ``'...'`` literal are literal characters and need no extra escaping.
    Timestamps are stored in coord as ISO-8601 TEXT (``...Z``); emitting them as
    quoted strings lets Postgres coerce them whether the target column is
    ``text`` or ``timestamptz``.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        # Render booleans explicitly; SQLite may surface them as 0/1 ints, in
        # which case this branch never fires and the int branch handles it,
        # which Postgres accepts for a boolean column just as well.
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        # Defensive: no coord durable column is a BLOB, but render bytea via the
        # hex format so an unexpected BLOB does not corrupt the output. With
        # standard_conforming_strings on, the literal text "\x.." is handed to
        # bytea's input function verbatim.
        return "'\\x" + bytes(value).hex() + "'::bytea"
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return the column names of ``table`` in definition order, or [] if the
    table does not exist."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    return cur.fetchone() is not None


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, "schema_version"):
        return None
    cur = conn.execute("SELECT version FROM schema_version WHERE id = 1")
    row = cur.fetchone()
    return int(row[0]) if row is not None else None


def _render_upsert(
    table: str,
    columns: list[str],
    rows: list[tuple],
    *,
    postgres_schema: str,
    conflict: tuple[str, ...],
    preserve: tuple[str, ...],
) -> str:
    """Render an idempotent multi-row INSERT ... ON CONFLICT DO UPDATE for one
    table. Returns a comment-only block when there are no rows to migrate."""
    if not rows:
        return f"-- {table}: 0 rows in source; nothing to migrate.\n"

    col_list = ", ".join(columns)
    value_rows = []
    for row in rows:
        rendered = ", ".join(_sql_literal(v) for v in row)
        value_rows.append(f"    ({rendered})")
    values_sql = ",\n".join(value_rows)

    conflict_cols = ", ".join(conflict)
    # Columns refreshed from the incoming row: everything except the conflict
    # arbiter and any explicitly preserved (immutable-on-conflict) columns.
    update_cols = [
        c for c in columns if c not in conflict and c not in preserve
    ]
    if update_cols:
        set_clause = ",\n".join(
            f"    {c} = EXCLUDED.{c}" for c in update_cols
        )
        on_conflict = (
            f"ON CONFLICT ({conflict_cols}) DO UPDATE SET\n{set_clause}"
        )
    else:
        # No mutable columns to refresh -- the row is fully defined by its
        # conflict key, so a re-run is a no-op.
        on_conflict = f"ON CONFLICT ({conflict_cols}) DO NOTHING"

    target = _target_table(postgres_schema, table)
    return (
        f"-- {table}: {len(rows)} row(s)\n"
        f"INSERT INTO {target} ({col_list}) VALUES\n"
        f"{values_sql}\n"
        f"{on_conflict};\n"
    )


def build_sql(
    sqlite_path: Path,
    postgres_schema: str = DEFAULT_POSTGRES_SCHEMA,
) -> str:
    """Open the source SQLite database read-only and build the full idempotent
    SQL script that imports engineer_tokens + ownership_config. Never writes to
    or creates the source file."""
    postgres_schema = validate_postgres_schema(postgres_schema)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"source SQLite database not found: {sqlite_path}")

    uri = f"file:{sqlite_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        warnings: list[str] = []

        version = _read_schema_version(conn)
        if version is None:
            warnings.append(
                "source has no schema_version row; assuming a legacy/core schema"
            )
        elif version > KNOWN_SCHEMA_VERSION:
            warnings.append(
                f"source schema_version={version} is newer than this script "
                f"knows about (expected <= {KNOWN_SCHEMA_VERSION}); migrating "
                "the columns that are present -- review the generated SQL"
            )

        blocks: list[str] = []

        # --- engineer_tokens --------------------------------------------
        if _table_exists(conn, TOKEN_TABLE):
            token_cols = _table_columns(conn, TOKEN_TABLE)
            unexpected = [c for c in token_cols if c not in EXPECTED_TOKEN_COLUMNS]
            if unexpected:
                warnings.append(
                    f"{TOKEN_TABLE} has unexpected column(s) {unexpected}; "
                    "they will be included in the INSERT -- ensure the target "
                    "schema has them"
                )
            if "token_sha256" not in token_cols:
                raise RuntimeError(
                    f"{TOKEN_TABLE} is missing the 'token_sha256' column; "
                    "cannot build an idempotent upsert"
                )
            col_clause = ", ".join(token_cols)
            cur = conn.execute(
                f"SELECT {col_clause} FROM {TOKEN_TABLE} ORDER BY created_at, id"
            )
            token_rows = cur.fetchall()
            blocks.append(
                _render_upsert(
                    TOKEN_TABLE,
                    token_cols,
                    token_rows,
                    postgres_schema=postgres_schema,
                    conflict=("token_sha256",),
                    # Preserve the existing target id on conflict so rotation
                    # chain pointers (rotated_from -> id) are never re-pointed.
                    preserve=("id",),
                )
            )
        else:
            warnings.append(
                f"{TOKEN_TABLE} table absent in source (pre-v14 schema?); "
                "no tokens to migrate"
            )
            blocks.append(f"-- {TOKEN_TABLE}: table absent in source.\n")

        # --- ownership_config -------------------------------------------
        if _table_exists(conn, OWNERSHIP_TABLE):
            own_cols = _table_columns(conn, OWNERSHIP_TABLE)
            cur = conn.execute(
                f"SELECT {', '.join(own_cols)} FROM {OWNERSHIP_TABLE} "
                "WHERE id = 1"
            )
            own_rows = cur.fetchall()
            blocks.append(
                _render_upsert(
                    OWNERSHIP_TABLE,
                    own_cols,
                    own_rows,
                    postgres_schema=postgres_schema,
                    conflict=("id",),
                    preserve=(),
                )
            )
        else:
            warnings.append(
                f"{OWNERSHIP_TABLE} table absent in source; nothing to migrate"
            )
            blocks.append(f"-- {OWNERSHIP_TABLE}: table absent in source.\n")
    finally:
        conn.close()

    header = [
        "-- coord HA durable-state migration",
        f"-- source SQLite: {sqlite_path}",
        f"-- target Postgres schema: {postgres_schema}",
        "-- migrates: engineer_tokens, ownership_config (idempotent upserts)",
        "-- does NOT migrate ephemeral claim/queue/request/outbox state",
        "--",
        "-- Generated by scripts/migrate_tokens_ownership.py",
    ]
    for w in warnings:
        header.append(f"-- WARNING: {w}")

    body = "\n".join(blocks)
    return (
        "\n".join(header)
        + "\n\nBEGIN;\nSET LOCAL standard_conforming_strings = on;\n\n"
        + body
        + "\nCOMMIT;\n"
    )


def _psql_command(postgres_url: str, psql_bin: str) -> list[str]:
    return [
        psql_bin,
        postgres_url,
        "-v",
        "ON_ERROR_STOP=1",
        "--no-psqlrc",
        "-f",
        "-",
    ]


def run_psql(postgres_url: str, sql: str, psql_bin: str) -> int:
    """Apply ``sql`` to the target Postgres via psql, reading the script from
    stdin. ``ON_ERROR_STOP=1`` plus the BEGIN/COMMIT wrapper make the import
    all-or-nothing. Returns the psql exit code."""
    resolved = shutil.which(psql_bin)
    if resolved is None:
        print(
            f"error: '{psql_bin}' not found on PATH. Install the Postgres "
            "client, or re-run with --dry-run and apply the emitted SQL "
            "manually (psql <url> -v ON_ERROR_STOP=1 -f migration.sql).",
            file=sys.stderr,
        )
        return 127
    cmd = _psql_command(postgres_url, resolved)
    proc = subprocess.run(
        cmd,
        input=sql,
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def self_test() -> int:
    """Build a temporary SQLite database resembling a real coord DB, generate
    the migration SQL, and assert the invariants that make the script safe:
    idempotent upserts, correct conflict targets, proper quote escaping, and --
    critically -- that no ephemeral table is ever referenced."""
    tmpdir = Path(tempfile.mkdtemp(prefix="coord-migrate-selftest-"))
    db_path = tmpdir / "coordination.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL);
            INSERT INTO schema_version (id, version) VALUES (1, 19);

            CREATE TABLE engineer_tokens (
                id TEXT PRIMARY KEY,
                engineer TEXT NOT NULL,
                token_sha256 TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                last_used_at TEXT,
                expires_at TEXT,
                rotated_from TEXT,
                rotation_grace_until TEXT,
                request_count INTEGER NOT NULL DEFAULT 0,
                last_source_ip TEXT,
                last_user_agent TEXT,
                repo TEXT
            );

            CREATE TABLE ownership_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                yaml_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- An ephemeral table that MUST be ignored by the migration.
            CREATE TABLE claims (
                id TEXT PRIMARY KEY,
                engineer TEXT NOT NULL,
                pattern TEXT NOT NULL
            );
            """
        )
        # Token with a single-quote in the description (escaping check), a
        # v19 repo scope, plus NULLs in the nullable columns.
        conn.execute(
            "INSERT INTO engineer_tokens (id, engineer, token_sha256, "
            "description, created_at, request_count, repo) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            (
                "tok-1",
                "alex",
                "a" * 64,
                "alex's laptop",
                "2026-06-29T12:00:00Z",
                7,
                "amittell/coord",
            ),
        )
        conn.execute(
            "INSERT INTO engineer_tokens (id, engineer, token_sha256, "
            "created_at, rotated_from) VALUES (?, ?, ?, ?, ?)",
            ("tok-2", "sam", "b" * 64, "2026-06-29T13:00:00Z", "tok-1"),
        )
        # Ownership YAML with a quote and a newline (multi-line literal check).
        conn.execute(
            "INSERT INTO ownership_config (id, yaml_text, updated_at) VALUES "
            "(1, ?, ?)",
            (
                "rules:\n  - path: 'src/**'\n    owners: [alex]\n",
                "2026-06-29T14:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO claims (id, engineer, pattern) VALUES ('c1', 'x', 'f')"
        )
        conn.commit()
        conn.close()

        sql = build_sql(db_path, DEFAULT_POSTGRES_SCHEMA)
        token_target = _target_table(DEFAULT_POSTGRES_SCHEMA, TOKEN_TABLE)
        ownership_target = _target_table(DEFAULT_POSTGRES_SCHEMA, OWNERSHIP_TABLE)

        failures: list[str] = []

        def check(cond: bool, msg: str) -> None:
            if not cond:
                failures.append(msg)

        check("BEGIN;" in sql and "COMMIT;" in sql, "missing transaction wrapper")
        check(
            f"INSERT INTO {token_target}" in sql,
            "missing engineer_tokens insert",
        )
        check(
            "ON CONFLICT (token_sha256) DO UPDATE SET" in sql,
            "missing/incorrect token conflict clause",
        )
        check(
            f"INSERT INTO {ownership_target}" in sql,
            "missing ownership_config insert",
        )
        check(
            "ON CONFLICT (id) DO UPDATE SET" in sql,
            "missing/incorrect ownership conflict clause",
        )
        # id must be preserved on token conflict (not in the SET list).
        token_block = sql.split(f"INSERT INTO {token_target}", 1)[1].split(
            ";", 1
        )[0]
        check(
            "id = EXCLUDED.id" not in token_block,
            "token id should be preserved on conflict, not overwritten",
        )
        check(
            "engineer = EXCLUDED.engineer" in token_block,
            "token mutable columns should be refreshed on conflict",
        )
        # The v19 repo scope column migrates like any other source column.
        check(
            "repo = EXCLUDED.repo" in token_block,
            "repo column should be refreshed on conflict",
        )
        check("'amittell/coord'" in sql, "repo scope value missing from insert")
        # A source at the current schema must generate zero warnings: any
        # WARNING here means EXPECTED_TOKEN_COLUMNS or KNOWN_SCHEMA_VERSION
        # has drifted behind coordination/db.py.
        check(
            "-- WARNING:" not in sql,
            "current-schema source should generate no header warnings",
        )
        # Single quotes doubled.
        check("'alex''s laptop'" in sql, "single-quote escaping failed")
        # NULLs rendered for absent optional columns.
        check("NULL" in sql, "expected NULL literals for absent columns")
        # Ephemeral tables never referenced as INSERT/SELECT targets.
        insert_targets = [
            line.split("INSERT INTO", 1)[1].split("(", 1)[0].strip()
            for line in sql.splitlines()
            if line.startswith("INSERT INTO")
        ]
        check(
            set(insert_targets) == {token_target, ownership_target},
            f"unexpected INSERT targets: {insert_targets}",
        )
        for table in EPHEMERAL_TABLES:
            check(
                f'INTO "{table}"' not in sql and f'FROM "{table}"' not in sql,
                f"ephemeral table {table} leaked into migration SQL",
            )

        if failures:
            print("SELF-TEST FAILED:", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            print("\n--- generated SQL ---\n" + sql, file=sys.stderr)
            return 1
        print("SELF-TEST PASSED: generated idempotent SQL with no ephemeral leakage.")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate coord durable state (engineer_tokens + ownership_config) "
            "from a source SQLite coord DB into a target Postgres coord DB. "
            "Ephemeral claim state is intentionally not migrated."
        ),
    )
    parser.add_argument(
        "--sqlite",
        type=Path,
        help="path to the source SQLite coord database (read-only)",
    )
    parser.add_argument(
        "--postgres-url",
        help=(
            "target Postgres connection URL (e.g. "
            "postgresql://user:pass@host:5432/coord). Passed to psql."
        ),
    )
    parser.add_argument(
        "--postgres-schema",
        type=validate_postgres_schema,
        default=os.environ.get("COORD_POSTGRES_SCHEMA", DEFAULT_POSTGRES_SCHEMA),
        help=(
            "target application schema (default: COORD_POSTGRES_SCHEMA or "
            f"{DEFAULT_POSTGRES_SCHEMA!r}); must match the coord service"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the SQL and the psql command that would run; change nothing",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="also write the generated SQL to this file (review/manual apply)",
    )
    parser.add_argument(
        "--psql-bin",
        default="psql",
        help="psql executable to use (default: psql on PATH)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the built-in self-test (no Postgres/psql needed) and exit",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.sqlite is None:
        parser.error("--sqlite is required (or use --self-test)")

    try:
        sql = build_sql(args.sqlite, args.postgres_schema)
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(sql, encoding="utf-8")
        print(f"wrote generated SQL to {args.out}", file=sys.stderr)

    if args.dry_run:
        shown_url = args.postgres_url or "$COORD_DATABASE_URL"
        sys.stdout.write(sql)
        print(
            "\n-- DRY RUN: nothing was applied.\n"
            "-- To apply, re-run without --dry-run, or pipe the SQL above to "
            "psql:\n"
            f"--   psql {shown_url} -v ON_ERROR_STOP=1 -f migration.sql",
            file=sys.stderr,
        )
        return 0

    if not args.postgres_url:
        parser.error("--postgres-url is required for a live import (or --dry-run)")

    rc = run_psql(args.postgres_url, sql, args.psql_bin)
    if rc == 0:
        print(
            "durable-state migration applied successfully to schema "
            f"{args.postgres_schema!r}.",
            file=sys.stderr,
        )
    else:
        print(
            f"psql exited with code {rc}; migration NOT applied "
            "(transaction rolled back).",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
