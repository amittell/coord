"""Zero-offender guard for the direct-SQLite boundary (design Section 7).

The PG test harness (``tests/conftest.py``) monkeypatches ``aiosqlite.connect``
so that any production module opening a raw aiosqlite connection is transparently
redirected to the Postgres schema. That shim is what lets the whole suite run on
PG -- but it ALSO silently masks any module that reaches past the ``Database``
abstraction to talk to SQLite directly (the sync ``sqlite3`` path is not even
shimmed -- it would just hit an empty local file under PG).

This test asserts the DB seam stays confined to ``coordination/db.py`` (the one
method, ``Database._connect``, that ``PostgresStore`` overrides): NO other
production module under ``coordination/`` may call ``aiosqlite.connect`` or
``sqlite3.connect``. The operator CLIs (``cli_outbox``, ``cli_engineers``,
``cli_tokens``) were ported to the backend-dispatched ``Database`` / store API,
so the formerly-documented offender set is now empty.

The single sanctioned exception is an in-memory connection
(``sqlite3.connect(":memory:")``): ``coordination/pg_backend.py`` replays the
SQLite ``MIGRATIONS`` chain into a throwaway ``:memory:`` database to generate
the consolidated PG schema. That connection never touches real data and never
opens the ``database_path`` file, so it is not a data-store seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "coordination"

# The async seam ``PostgresStore`` overrides lives here; the sync ``sqlite3``
# generator that builds the PG schema lives in pg_backend but is exempted below
# via the ``:memory:`` carve-out, so only db.py is excluded wholesale.
_EXEMPT_MODULES = {"db.py"}

_CONNECT_ROOTS = {"sqlite3", "aiosqlite"}


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _connect_root(call: ast.Call) -> str | None:
    """Return ``"sqlite3"`` / ``"aiosqlite"`` when ``call`` is a
    ``<root>.connect(...)`` call, else None."""
    f = call.func
    if (
        isinstance(f, ast.Attribute)
        and f.attr == "connect"
        and isinstance(f.value, ast.Name)
        and f.value.id in _CONNECT_ROOTS
    ):
        return f.value.id
    return None


def _is_memory_connect(call: ast.Call) -> bool:
    """True when the connect target is the literal ``":memory:"`` -- a
    throwaway in-memory DB, never the ``database_path`` data store."""
    if not call.args:
        return False
    first = call.args[0]
    return isinstance(first, ast.Constant) and first.value == ":memory:"


def test_no_direct_sqlite_connect_outside_db_module() -> None:
    """No production module under ``coordination/`` (except ``db.py``) may open
    a raw ``sqlite3``/``aiosqlite`` connection, except the sanctioned
    ``:memory:`` schema generator in ``pg_backend.py``. A non-empty offender
    set means a module is reaching past the ``Database`` abstraction and would
    silently break (or be masked by the conftest shim) under Postgres."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(_PKG.glob("*.py")):
        if path.name in _EXEMPT_MODULES:
            continue  # the single sanctioned async SQLite seam
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits: list[str] = []
        for c in _calls(tree):
            root = _connect_root(c)
            if root is None:
                continue
            if _is_memory_connect(c):
                continue  # throwaway :memory: DDL scratchpad, not a data store
            hits.append(f"{root}.connect @ line {c.lineno}")
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "sqlite3.connect()/aiosqlite.connect() must only be called from "
        "coordination/db.py (the seam PostgresStore overrides); the conftest "
        "PG shim masks async callers and the sync sqlite3 path silently hits "
        "an empty local file under Postgres. Port these to the Database/store "
        f"API. Offending modules: {offenders}"
    )
