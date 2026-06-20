"""Tests for the Swift symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``backend`` fixture. The tree-sitter case skips automatically when the
``tree_sitter_swift`` package is unavailable so the suite still runs cleanly
on machines without the native wheels; the regex backend is always exercised.

Test fixtures are inline Swift source strings; we deliberately avoid touching
the filesystem so the cache key (``file_path``, content hash) is stable and the
tests stay fast.
"""

from __future__ import annotations

import importlib

import pytest

from coordination import symbols
from coordination.symbols import Symbol, extract_symbols

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_swift")
except ImportError:  # pragma: no cover - depends on install state
    _TREESITTER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Ensure the in-process cache does not leak state between tests."""

    symbols._CACHE.clear()


@pytest.fixture(
    params=[
        pytest.param(
            "treesitter",
            marks=pytest.mark.skipif(
                not _TREESITTER_AVAILABLE,
                reason="tree-sitter-swift not installed",
            ),
        ),
        "regex",
    ]
)
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Force a specific backend for the duration of the test."""

    monkeypatch.setenv("COORD_SYMBOL_PARSER", request.param)
    symbols._CACHE.clear()
    return request.param


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _names(syms: list[Symbol]) -> list[str]:
    return [s.name for s in syms]


def _by_name(syms: list[Symbol], name: str) -> Symbol:
    for s in syms:
        if s.name == name:
            return s
    raise AssertionError(f"missing symbol {name!r} in {_names(syms)}")


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def test_simple_function(backend: str) -> None:
    src = "func helloWorld() {\n    return\n}\n"
    result = extract_symbols("sample.swift", src)
    assert "helloWorld" in _names(result)
    assert _by_name(result, "helloWorld").kind == "function"
    assert _by_name(result, "helloWorld").parent is None


def test_function_with_modifiers(backend: str) -> None:
    """A ``public func`` still resolves to the bare function name."""

    src = "public func doWork() {}\n"
    result = extract_symbols("sample.swift", src)
    assert "doWork" in _names(result)
    assert _by_name(result, "doWork").kind == "function"


def test_multi_line_function_signature(backend: str) -> None:
    src = (
        "func multiLine(\n"
        "    a: Int,\n"
        "    b: Int\n"
        ") -> Int {\n"
        "    return a + b\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    assert "multiLine" in _names(result)
    assert _by_name(result, "multiLine").kind == "function"


# ---------------------------------------------------------------------------
# Classes, structs, actors and their methods
# ---------------------------------------------------------------------------


def test_class_with_method(backend: str) -> None:
    src = (
        "class Server {\n"
        "    func start() {}\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    assert _by_name(result, "Server").kind == "class"
    start = _by_name(result, "start")
    assert start.kind == "function"
    assert start.parent == "Server"


def test_struct_is_type_with_method(backend: str) -> None:
    src = (
        "struct Point {\n"
        "    func distance() -> Double { return 0 }\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    point = _by_name(result, "Point")
    assert point.kind == "type"
    distance = _by_name(result, "distance")
    assert distance.kind == "function"
    assert distance.parent == "Point"


def test_actor_is_class(backend: str) -> None:
    src = (
        "actor Counter {\n"
        "    func bump() {}\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    assert _by_name(result, "Counter").kind == "class"
    assert _by_name(result, "bump").parent == "Counter"


def test_class_property_is_const(backend: str) -> None:
    src = (
        "class Config {\n"
        "    let name = \"x\"\n"
        "    var count = 0\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    name = _by_name(result, "name")
    count = _by_name(result, "count")
    assert name.kind == "const"
    assert name.parent == "Config"
    assert count.kind == "const"
    assert count.parent == "Config"


# ---------------------------------------------------------------------------
# Enums and cases
# ---------------------------------------------------------------------------


def test_enum_kind_and_cases(backend: str) -> None:
    src = (
        "enum Direction {\n"
        "    case north\n"
        "    case south\n"
        "    func opposite() -> Direction { return self }\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    direction = _by_name(result, "Direction")
    assert direction.kind == "enum"
    north = _by_name(result, "north")
    assert north.kind == "const"
    assert north.parent == "Direction"
    assert _by_name(result, "south").parent == "Direction"
    opposite = _by_name(result, "opposite")
    assert opposite.kind == "function"
    assert opposite.parent == "Direction"


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


def test_protocol_is_interface_with_method(backend: str) -> None:
    src = (
        "protocol Drawable {\n"
        "    func draw()\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    drawable = _by_name(result, "Drawable")
    assert drawable.kind == "interface"
    draw = _by_name(result, "draw")
    assert draw.kind == "function"
    assert draw.parent == "Drawable"


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


def test_extension_members_parent_to_extended_type(backend: str) -> None:
    src = (
        "extension String {\n"
        "    func shout() -> String { return self }\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    ext = _by_name(result, "String")
    assert ext.kind == "type"
    shout = _by_name(result, "shout")
    assert shout.kind == "function"
    assert shout.parent == "String"


# ---------------------------------------------------------------------------
# Top-level const
# ---------------------------------------------------------------------------


def test_top_level_let_is_const(backend: str) -> None:
    src = "let maxRetries = 3\n"
    result = extract_symbols("sample.swift", src)
    max_retries = _by_name(result, "maxRetries")
    assert max_retries.kind == "const"
    assert max_retries.parent is None


def test_top_level_var_is_const(backend: str) -> None:
    src = "var shared = 0\n"
    result = extract_symbols("sample.swift", src)
    shared = _by_name(result, "shared")
    assert shared.kind == "const"
    assert shared.parent is None


# ---------------------------------------------------------------------------
# Nesting
# ---------------------------------------------------------------------------


def test_nested_type_member_carries_full_path(backend: str) -> None:
    """A method on a nested type carries the ``Outer::Inner`` ancestor path."""

    src = (
        "class Outer {\n"
        "    struct Inner {\n"
        "        func work() {}\n"
        "    }\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    outer = _by_name(result, "Outer")
    assert outer.kind == "class"
    assert outer.parent is None
    inner = _by_name(result, "Inner")
    assert inner.kind == "type"
    assert inner.parent == "Outer"
    work = _by_name(result, "work")
    assert work.kind == "function"
    assert work.parent == "Outer::Inner"


def test_local_declaration_inside_method_excluded(backend: str) -> None:
    """A function declared inside a method body must not surface as a member.

    Conventional Swift indents a local function deeper than a method, so the
    regex backend filters it by indent; the tree-sitter backend walks only the
    direct children of each type body, never function bodies.
    """

    src = (
        "class Holder {\n"
        "    func outer() {\n"
        "        func inner() {}\n"
        "        inner()\n"
        "    }\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    names = _names(result)
    assert "outer" in names
    assert "inner" not in names
    assert _by_name(result, "outer").parent == "Holder"


def test_sibling_methods_distinct_parents(backend: str) -> None:
    """Methods on two sibling types each carry their own type as ``parent``."""

    src = (
        "class Alpha {\n"
        "    func one() {}\n"
        "}\n"
        "class Beta {\n"
        "    func two() {}\n"
        "}\n"
    )
    result = extract_symbols("sample.swift", src)
    assert _by_name(result, "one").parent == "Alpha"
    assert _by_name(result, "two").parent == "Beta"


# ---------------------------------------------------------------------------
# Empty / structural files
# ---------------------------------------------------------------------------


def test_comment_only_file(backend: str) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract_symbols("sample.swift", src)
    assert result == []


def test_imports_only_file(backend: str) -> None:
    src = "import Foundation\nimport UIKit\n"
    result = extract_symbols("sample.swift", src)
    assert result == []


# ---------------------------------------------------------------------------
# Dispatcher path
# ---------------------------------------------------------------------------


def test_dispatcher_routes_swift_extension(backend: str) -> None:
    """A ``.swift`` file path must dispatch to the Swift backend and return symbols."""

    src = "func dispatched() {}\n"
    result = extract_symbols("foo.swift", src)
    assert result, "expected non-empty result for a simple Swift func"
    assert "dispatched" in _names(result)
