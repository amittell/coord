"""Static guard for the SQLite-seam boundary (design Section 7 / conftest shim).

The PG test harness (``tests/conftest.py``) monkeypatches ``aiosqlite.connect``
so that any production module opening a raw aiosqlite connection is transparently
redirected to the Postgres schema. That shim is what lets the whole suite run on
PG -- but it ALSO silently masks any module that reaches past the ``Database``
abstraction to talk to SQLite directly. This test asserts the async SQLite seam
stays confined to ``coordination/db.py`` (the one method, ``Database._connect``,
that ``PostgresStore`` overrides), so the shim's coverage is complete and the
abstraction is the only async DB door.

It also DOCUMENTS -- without porting them here -- that the operator CLIs
(``cli_outbox``, ``cli_engineers``) still open synchronous ``sqlite3``
connections and ``cli_tokens`` constructs a bare ``Database`` (always the SQLite
class): these are the known-unported surfaces that the conftest shim does NOT
cover (it only patches the async ``aiosqlite.connect``). They must be ported to
the backend-dispatched store before they work against PG.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "coordination"


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _is_attr_call(call: ast.Call, root: str, attr: str) -> bool:
    f = call.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == attr
        and isinstance(f.value, ast.Name)
        and f.value.id == root
    )


def test_only_db_module_calls_aiosqlite_connect() -> None:
    offenders: dict[str, int] = {}
    for path in sorted(_PKG.glob("*.py")):
        if path.name == "db.py":
            continue  # the single sanctioned async SQLite seam
        tree = ast.parse(path.read_text(), filename=str(path))
        hits = sum(
            1 for c in _calls(tree) if _is_attr_call(c, "aiosqlite", "connect")
        )
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "aiosqlite.connect() must only be called from coordination/db.py "
        "(the seam PostgresStore overrides); the conftest PG shim silently "
        f"masks any other caller. Offending modules: {offenders}"
    )


def test_known_unported_sqlite_operator_clis_are_documented() -> None:
    """Pins the CURRENT set of operator CLIs that still bind to SQLite
    directly. If this set changes (a CLI is ported, or a new direct-SQLite CLI
    appears) this test fails so the porting boundary is revisited deliberately
    rather than drifting silently."""
    direct_sqlite3: set[str] = set()
    bare_database: set[str] = set()
    for path in sorted(_PKG.glob("cli_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for c in _calls(tree):
            if _is_attr_call(c, "sqlite3", "connect"):
                direct_sqlite3.add(path.name)
            if isinstance(c.func, ast.Name) and c.func.id == "Database":
                bare_database.add(path.name)

    # These are the documented, intentionally-unported SQLite operator paths:
    # cli_engineers/cli_outbox open sync sqlite3 for their mutator paths, and
    # all three construct a bare ``Database`` (the SQLite class, never
    # PostgresStore) for the read helpers they reuse.
    assert direct_sqlite3 == {"cli_engineers.py", "cli_outbox.py"}, direct_sqlite3
    assert bare_database == {
        "cli_engineers.py",
        "cli_outbox.py",
        "cli_tokens.py",
    }, bare_database
