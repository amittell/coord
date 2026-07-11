"""Zero-offender guard for the direct-SQLite boundary (design Section 7).

The PG test harness (``tests/conftest.py``) monkeypatches ``aiosqlite.connect``
so that any production module opening a raw aiosqlite connection is transparently
redirected to the Postgres schema. That shim is what lets the whole suite run on
PG -- but it ALSO silently masks any module that reaches past the ``Database``
abstraction to talk to SQLite directly (the sync ``sqlite3`` path is not even
shimmed -- it would just hit an empty local file under PG).

This test asserts the DB seam stays confined to the TWO sanctioned functions in
``coordination/db.py`` -- ``Database._connect`` (the per-op connection the
``PostgresStore`` overrides) and ``Database._ensure_writer`` (the persistent
single-writer connection, a plain-SQLite concern ``PostgresStore`` forces off).
NO other function in ``db.py``, and no other production module under
``coordination/``, may call ``aiosqlite.connect`` or ``sqlite3.connect``. The
carve-out is function-scoped, not a blanket ``db.py`` exemption: a raw connect
added to any OTHER method of ``db.py`` (a scope-guard lookup, a stats query)
would bypass the seam and silently break on Postgres, exactly the class of bug
the audit found -- so it must be caught here rather than waved through because
it happens to live in ``db.py``. The operator CLIs (``cli_outbox``,
``cli_engineers``, ``cli_tokens``) were ported to the backend-dispatched
``Database`` / store API, so the formerly-documented offender set is now empty.

The single sanctioned exception outside those functions is an in-memory
connection (``sqlite3.connect(":memory:")``): ``coordination/pg_backend.py``
replays the SQLite ``MIGRATIONS`` chain into a throwaway ``:memory:`` database
to generate the consolidated PG schema. That connection never touches real data
and never opens the ``database_path`` file, so it is not a data-store seam.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "coordination"

# Function-scoped carve-out: within these modules, only the named functions may
# open a raw driver connection -- every other function in the same module is
# still guarded. ``db.py`` owns the two legitimate SQLite seams; nothing else
# in the package gets a per-function pass (pg_backend's schema generator is
# handled by the ``:memory:`` carve-out instead).
_FUNCTION_SCOPED_EXEMPTIONS = {"db.py": {"_connect", "_ensure_writer"}}

_CONNECT_ROOTS = {"sqlite3", "aiosqlite"}


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map ``id(node) -> name of the nearest-enclosing function def`` for every
    descendant of ``tree``. A node not inside any function maps to ``""``.

    Walks the tree tracking the innermost ``def`` / ``async def`` on the way
    down, so a call in ``Database._connect`` resolves to ``"_connect"`` while a
    call in a sibling method resolves to that method's name -- this is what
    makes the carve-out function-scoped rather than module-wide.
    """
    mapping: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mapping[id(child)] = current
                visit(child, child.name)
            else:
                mapping[id(child)] = current
                visit(child, current)

    visit(tree, "")
    return mapping


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
    """No production function under ``coordination/`` may open a raw
    ``sqlite3``/``aiosqlite`` connection, except (1) ``Database._connect`` /
    ``Database._ensure_writer`` in ``db.py`` -- the two sanctioned SQLite seams
    -- and (2) the ``:memory:`` schema generator in ``pg_backend.py``. A
    non-empty offender set means a function is reaching past the ``Database``
    abstraction and would silently break (or be masked by the conftest shim)
    under Postgres. The carve-out is per-function, so a raw connect added to any
    OTHER method of ``db.py`` is still flagged here."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        allowed_funcs = _FUNCTION_SCOPED_EXEMPTIONS.get(path.name, set())
        enclosing = _enclosing_functions(tree)
        hits: list[str] = []
        for c in _calls(tree):
            root = _connect_root(c)
            if root is None:
                continue
            if _is_memory_connect(c):
                continue  # throwaway :memory: DDL scratchpad, not a data store
            if enclosing.get(id(c)) in allowed_funcs:
                continue  # the sanctioned per-function SQLite seam
            hits.append(
                f"{root}.connect @ line {c.lineno} "
                f"in {enclosing.get(id(c)) or '<module>'}"
            )
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "sqlite3.connect()/aiosqlite.connect() must only be called from "
        "coordination/db.py's _connect / _ensure_writer seams (which "
        "PostgresStore overrides / forces off); the conftest PG shim masks "
        "async callers and the sync sqlite3 path silently hits an empty local "
        "file under Postgres. Port these to the Database/store API. Offending "
        f"modules: {offenders}"
    )
