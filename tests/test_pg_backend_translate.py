"""Dialect-translation guards for :mod:`coordination.pg_backend`.

Two layers:

* Pure unit tests (no DB, always run) pinning the subtle rewrites the oracle
  review flagged: MAX/MIN NULL semantics, lexical string/comment/identifier
  safety in the ``?`` and function rewriters, the INSERT OR REPLACE
  restriction, and the fail-fast guards for unsupported time-function shapes.

* A golden test (PG-only) that enumerates EVERY static SQL string passed to a
  cursor ``.execute``/``.executemany`` in ``coordination/db.py`` and
  ``coordination/service.py``, translates it, and asserts (a) the placeholder
  count is preserved and (b) the translation PARSES + PLANS on the real PG
  container via ``EXPLAIN (GENERIC_PLAN)``. This is the durable guard against a
  rewriter silently mangling a query the targeted tests never exercise.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from coordination.pg_backend import _skip_noncode, translate

_PG_URL = os.environ.get("COORD_DATABASE_URL", "")
_PG_SELECTED = _PG_URL.startswith("postgresql://") or _PG_URL.startswith(
    "postgres://"
)

_PKG = Path(__file__).resolve().parent.parent / "coordination"


def _count_placeholders(sql: str) -> int:
    """Count ``?`` params in code position (outside strings/comments/idents)."""
    i = 0
    n = len(sql)
    count = 0
    while i < n:
        skip = _skip_noncode(sql, i)
        if skip is not None:
            i = skip
            continue
        if sql[i] == "?":
            count += 1
        i += 1
    return count


# --------------------------------------------------------------------------
# MAX / MIN NULL semantics (SQLite scalar MAX/MIN -> NULL if any arg NULL).
# --------------------------------------------------------------------------


def test_scalar_max_emits_null_guarded_greatest() -> None:
    out = translate("SELECT MAX(a, b) FROM t")
    assert "GREATEST(a, b)" in out
    assert "(a) IS NULL OR (b) IS NULL" in out
    assert "CASE WHEN" in out


def test_scalar_min_emits_null_guarded_least() -> None:
    out = translate("SELECT MIN(a, b, c) FROM t")
    assert "LEAST(a, b, c)" in out
    assert "(a) IS NULL OR (b) IS NULL OR (c) IS NULL" in out


def test_aggregate_max_single_arg_untouched() -> None:
    out = translate("SELECT MAX(last_activity) FROM claims")
    assert "MAX(last_activity)" in out
    assert "GREATEST" not in out
    assert "CASE WHEN" not in out


# --------------------------------------------------------------------------
# Lexical safety: never rewrite inside strings / comments / quoted identifiers.
# --------------------------------------------------------------------------


def test_question_mark_in_string_literal_not_a_placeholder() -> None:
    out = translate("SELECT 'huh?' AS x, c FROM t WHERE c = ?")
    assert "'huh?'" in out  # literal ? preserved
    assert out.count("$1") == 1
    assert "$2" not in out


def test_function_name_in_string_literal_not_rewritten() -> None:
    out = translate("SELECT 'datetime(x)' AS lit, datetime(col) FROM t")
    assert "'datetime(x)'" in out  # literal untouched
    # the real call was translated (cast to timestamptz)
    assert "::timestamptz" in out


def test_function_name_in_line_comment_not_rewritten() -> None:
    out = translate("SELECT 1 -- datetime(col) MAX(a,b)\nFROM t")
    assert "-- datetime(col) MAX(a,b)" in out
    assert "GREATEST" not in out
    assert "::timestamptz" not in out


def test_function_name_in_block_comment_not_rewritten() -> None:
    out = translate("SELECT /* MAX(a,b) datetime(x) */ 1 FROM t")
    assert "/* MAX(a,b) datetime(x) */" in out
    assert "GREATEST" not in out


def test_question_mark_in_quoted_identifier_preserved() -> None:
    out = translate('SELECT "weird?col" FROM t WHERE a = ?')
    assert '"weird?col"' in out
    assert out.count("$1") == 1
    assert "$2" not in out


def test_placeholder_numbering_sequential_across_code_only() -> None:
    out = translate("SELECT ? , 'lit?' , ? FROM t WHERE x = ?")
    assert "$1" in out and "$2" in out and "$3" in out
    assert "$4" not in out


# --------------------------------------------------------------------------
# INSERT OR REPLACE restriction (P2).
# --------------------------------------------------------------------------


def test_insert_or_replace_known_tables_ok() -> None:
    out = translate(
        "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)"
    )
    assert "ON CONFLICT (id) DO UPDATE SET version = excluded.version" in out

    out2 = translate(
        "INSERT OR REPLACE INTO ownership_config (id, yaml_text, updated_at) "
        "VALUES (1, ?, ?)"
    )
    assert "ON CONFLICT (id) DO UPDATE SET" in out2


def test_insert_or_replace_unknown_table_raises() -> None:
    with pytest.raises(ValueError, match="INSERT OR REPLACE"):
        translate("INSERT OR REPLACE INTO claims (id, pattern) VALUES (?, ?)")


def test_insert_or_replace_non_id_leading_col_raises() -> None:
    with pytest.raises(ValueError, match="INSERT OR REPLACE"):
        translate(
            "INSERT OR REPLACE INTO schema_version (version, id) VALUES (?, 1)"
        )


def test_insert_or_ignore_translates() -> None:
    out = translate("INSERT OR IGNORE INTO claim_symbols (claim_id) VALUES (?)")
    assert out.rstrip().endswith("ON CONFLICT DO NOTHING")


# --------------------------------------------------------------------------
# Fail-fast guards for unsupported time-function shapes (P1).
# --------------------------------------------------------------------------


def test_datetime_modifier_form_raises() -> None:
    with pytest.raises(ValueError, match="datetime"):
        translate("SELECT 1 FROM t WHERE c < datetime('now', '-1 hour')")


def test_strftime_modifier_form_raises() -> None:
    with pytest.raises(ValueError):
        translate("SELECT strftime('%s', col, '+1 day') FROM t")


def test_julianday_raises() -> None:
    with pytest.raises(ValueError, match="julianday"):
        translate("SELECT julianday('now') - julianday(col) FROM t")


def test_julianday_inside_string_does_not_raise() -> None:
    out = translate("SELECT 'julianday(x)' AS note FROM t")
    assert "'julianday(x)'" in out


def test_supported_datetime_and_strftime_still_translate() -> None:
    assert "::timestamptz" in translate("SELECT datetime(col) FROM t")
    assert "extract(epoch from" in translate("SELECT strftime('%s','now')")
    assert "to_char(" in translate("SELECT strftime('%Y-%m-%d', col) FROM t")


# --------------------------------------------------------------------------
# Golden test: every static SQL string parses + plans on PG, count preserved.
# --------------------------------------------------------------------------

_DML_PREFIXES = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH")

# Statements that reference SQLite-only catalogs run exclusively on the SQLite
# bootstrap/migration path (PostgresStore overrides init and never executes
# them), so they cannot -- and need not -- parse against PG. They are still
# placeholder-count-checked; only the EXPLAIN parse skips them.
_SQLITE_ONLY = re.compile(r"\bsqlite_(?:master|sequence|temp_master)\b", re.IGNORECASE)


def _static_sql_strings(module: str) -> list[tuple[str, int]]:
    """Return (sql, lineno) for every literal SQL string passed positionally to
    a ``.execute``/``.executemany`` call in ``module``. Adjacent string
    literals are concatenated by the parser into one ``ast.Constant``; dynamic
    SQL (f-strings, ``+`` concatenation with variables) is a non-Constant node
    and is skipped (those run through ``translate`` in the live suite)."""
    path = _PKG / module
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in ("execute", "executemany")):
            continue
        if not node.args:
            continue
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            found.append((a.value, node.lineno))
    return found


def _all_static_dml() -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for module in ("db.py", "service.py"):
        for sql, lineno in _static_sql_strings(module):
            head = sql.lstrip().upper()
            if head.startswith(_DML_PREFIXES):
                out.append((module, sql, lineno))
    return out


def test_golden_corpus_is_nontrivial() -> None:
    corpus = _all_static_dml()
    # Sanity floor so the golden test can never silently cover nothing.
    assert len(corpus) > 40, len(corpus)


def test_placeholder_count_preserved_for_all_static_dml() -> None:
    for module, sql, lineno in _all_static_dml():
        translated = translate(sql)
        want = _count_placeholders(sql)
        got = len(set(re.findall(r"\$\d+", translated)))
        assert got == want, (
            f"{module}:{lineno} placeholder count drift: "
            f"? count={want} but $N count={got}\n{sql!r}"
        )


@pytest.mark.skipif(
    not _PG_SELECTED,
    reason="EXPLAIN parse check needs the PG container",
)
async def test_all_static_dml_parses_on_pg(tmp_path: Path) -> None:
    from coordination.pg_backend import PostgresStore, _get_pool

    store = PostgresStore(tmp_path / "golden.sqlite")
    await store.init()
    pool = await _get_pool(store._dsn)

    failures: list[str] = []
    raw = await pool.acquire()
    try:
        await store._set_search_path(raw)
        for module, sql, lineno in _all_static_dml():
            if _SQLITE_ONLY.search(sql):
                continue  # SQLite bootstrap-only; never runs on PG
            try:
                translated = translate(sql).strip().rstrip(";")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{module}:{lineno} translate() raised: {exc}")
                continue
            # EXPLAIN cannot span multiple statements.
            if ";" in translated:
                continue
            try:
                # GENERIC_PLAN (PG16+) plans a parameterized statement without
                # supplying values -- exactly what we need to validate $N SQL.
                await raw.execute(f"EXPLAIN (GENERIC_PLAN) {translated}")
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    f"{module}:{lineno} EXPLAIN failed: {type(exc).__name__}: "
                    f"{exc}\n  translated: {translated!r}\n  source: {sql!r}"
                )
    finally:
        await pool.release(raw)

    assert not failures, "non-parsing translations:\n" + "\n".join(failures)


def test_postgres_store_accepts_writer_queue_kwarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (PR #66 Copilot review): build_service constructs
    ``Database(path, writer_queue=...)`` unconditionally; with a Postgres DSN
    that dispatches to PostgresStore, whose __init__ must accept the kwarg
    (and ignore it -- the in-process writer serialization is a SQLite
    concern, so the inherited flag must stay False)."""
    from coordination.db import Database
    from coordination.pg_backend import PostgresStore

    monkeypatch.setenv("COORD_DATABASE_URL", "postgresql://u:p@localhost/x")
    store = Database(tmp_path / "db.sqlite", writer_queue=True)
    assert isinstance(store, PostgresStore)
    assert store._writer_queue is False  # SQLite-only concern, forced off
