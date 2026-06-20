"""Tests for the Rust symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``backend`` fixture. The tree-sitter case skips automatically when the
``tree_sitter_rust`` package is unavailable so the suite still runs cleanly
on machines without the native wheels.

Test fixtures are inline Rust source strings; we deliberately avoid touching
the filesystem so the cache key (``file_path``, content hash) is stable
and the tests stay fast.
"""

from __future__ import annotations

import importlib

import pytest

from coordination import symbols
from coordination.symbols import Symbol, extract_symbols

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_rust")
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
                reason="tree-sitter-rust not installed",
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
    src = "fn hello_world() {\n    return;\n}\n"
    result = extract_symbols("sample.rs", src)
    assert _names(result) == ["hello_world"]
    assert _by_name(result, "hello_world").kind == "function"


def test_pub_function(backend: str) -> None:
    src = "pub fn exported() {}\n"
    result = extract_symbols("sample.rs", src)
    assert "exported" in _names(result)
    assert _by_name(result, "exported").kind == "function"


def test_generic_function(backend: str) -> None:
    src = "fn generic<T>(x: T) -> T { x }\n"
    result = extract_symbols("sample.rs", src)
    assert "generic" in _names(result)
    assert _by_name(result, "generic").kind == "function"


def test_async_unsafe_function(backend: str) -> None:
    src = "pub async unsafe fn risky() {}\n"
    result = extract_symbols("sample.rs", src)
    assert "risky" in _names(result)
    assert _by_name(result, "risky").kind == "function"


def test_multi_line_function_signature(backend: str) -> None:
    src = (
        "fn multi_line(\n"
        "    a: i32,\n"
        "    b: i32,\n"
        ") -> i32 {\n"
        "    a + b\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    assert "multi_line" in _names(result)
    assert _by_name(result, "multi_line").kind == "function"


def test_top_level_func_has_no_parent(backend: str) -> None:
    src = "fn standalone() {}\n"
    result = extract_symbols("sample.rs", src)
    standalone = _by_name(result, "standalone")
    assert standalone.kind == "function"
    assert standalone.parent is None


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


def test_struct(backend: str) -> None:
    src = "struct Foo {\n    name: String,\n}\n"
    result = extract_symbols("sample.rs", src)
    assert "Foo" in _names(result)
    assert _by_name(result, "Foo").kind == "type"


def test_tuple_struct(backend: str) -> None:
    src = "pub struct Pair(i32, i32);\n"
    result = extract_symbols("sample.rs", src)
    assert "Pair" in _names(result)
    assert _by_name(result, "Pair").kind == "type"


def test_enum(backend: str) -> None:
    src = (
        "enum Color {\n"
        "    Red,\n"
        "    Green,\n"
        "    Blue,\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    assert "Color" in _names(result)
    assert _by_name(result, "Color").kind == "enum"


def test_trait_is_interface(backend: str) -> None:
    src = (
        "trait Greet {\n"
        "    fn hello(&self);\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    assert "Greet" in _names(result)
    assert _by_name(result, "Greet").kind == "interface"


def test_type_alias(backend: str) -> None:
    src = "type Pair = (i32, i32);\n"
    result = extract_symbols("sample.rs", src)
    assert "Pair" in _names(result)
    assert _by_name(result, "Pair").kind == "type"


# ---------------------------------------------------------------------------
# Constants and statics
# ---------------------------------------------------------------------------


def test_const_declaration(backend: str) -> None:
    src = 'const GREETING: &str = "hi";\n'
    result = extract_symbols("sample.rs", src)
    assert "GREETING" in _names(result)
    assert _by_name(result, "GREETING").kind == "const"


def test_static_declaration(backend: str) -> None:
    src = "static COUNTER: i32 = 0;\n"
    result = extract_symbols("sample.rs", src)
    assert "COUNTER" in _names(result)
    assert _by_name(result, "COUNTER").kind == "const"


# ---------------------------------------------------------------------------
# impl blocks: methods carry parent
# ---------------------------------------------------------------------------


def test_impl_method_extracts_parent(backend: str) -> None:
    """A method inside ``impl Foo`` carries ``parent='Foo'``."""

    src = (
        "struct Foo;\n"
        "\n"
        "impl Foo {\n"
        "    fn do_it(&self) -> i32 {\n"
        "        42\n"
        "    }\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    assert "do_it" in _names(result)
    do_it = _by_name(result, "do_it")
    assert do_it.kind == "function"
    assert do_it.parent == "Foo"


def test_impl_trait_for_type_parents_to_type(backend: str) -> None:
    """``impl Trait for Type`` parents the method to ``Type``, not the trait."""

    src = (
        "struct Server;\n"
        "trait Run {\n"
        "    fn run(&self);\n"
        "}\n"
        "\n"
        "impl Run for Server {\n"
        "    fn run(&self) {}\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    run = _by_name(result, "run")
    assert run.kind == "function"
    assert run.parent == "Server"


def test_impl_generic_type_reduces_to_base(backend: str) -> None:
    """``impl Container<T>`` reduces the parent to the bare type name."""

    src = (
        "struct Container<T> {\n"
        "    items: Vec<T>,\n"
        "}\n"
        "\n"
        "impl<T> Container<T> {\n"
        "    fn add(&mut self, item: T) {}\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    add = _by_name(result, "add")
    assert add.kind == "function"
    assert add.parent == "Container"


def test_multiple_methods_in_impl(backend: str) -> None:
    """Several methods in one impl block all parent to the same type."""

    src = (
        "struct Calc;\n"
        "\n"
        "impl Calc {\n"
        "    fn add(&self, a: i32, b: i32) -> i32 { a + b }\n"
        "    fn sub(&self, a: i32, b: i32) -> i32 { a - b }\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    add = _by_name(result, "add")
    sub = _by_name(result, "sub")
    assert add.parent == "Calc"
    assert sub.parent == "Calc"
    assert {add.kind, sub.kind} == {"function"}


def test_methods_on_different_types_distinct(backend: str) -> None:
    """Two impl blocks on different types keep their methods' parents distinct."""

    src = (
        "struct Server;\n"
        "struct Client;\n"
        "\n"
        "impl Server {\n"
        "    fn handle(&self) {}\n"
        "}\n"
        "\n"
        "impl Client {\n"
        "    fn send(&self) {}\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    handle = _by_name(result, "handle")
    send = _by_name(result, "send")
    assert handle.parent == "Server"
    assert send.parent == "Client"


# ---------------------------------------------------------------------------
# mod is a namespace, not a claimable unit
# ---------------------------------------------------------------------------


def test_mod_itself_not_emitted(backend: str) -> None:
    """``mod`` is a namespace and must never surface as a claimable symbol."""

    src = (
        "mod inner {\n"
        "    pub fn buried() {}\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    assert "inner" not in _names(result)


def test_items_inside_mod_excluded(backend: str) -> None:
    """A function declared inside an inline module is not a top-level symbol.

    The tree-sitter backend walks only direct children of ``source_file``; the
    regex backend filters the indented body by column-zero anchoring. A free
    function next to the module still surfaces.
    """

    src = (
        "fn top() {}\n"
        "\n"
        "mod inner {\n"
        "    fn buried() {}\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    names = _names(result)
    assert "top" in names
    assert "buried" not in names


# ---------------------------------------------------------------------------
# Empty / structural files
# ---------------------------------------------------------------------------


def test_comment_only_file(backend: str) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract_symbols("sample.rs", src)
    assert result == []


def test_use_only_file(backend: str) -> None:
    src = (
        "use std::collections::HashMap;\n"
        "use std::fmt;\n"
    )
    result = extract_symbols("sample.rs", src)
    assert result == []


# ---------------------------------------------------------------------------
# Dispatcher path
# ---------------------------------------------------------------------------


def test_dispatcher_routes_rust_extension(backend: str) -> None:
    """A ``.rs`` file path must dispatch to the Rust backend and return symbols."""

    src = "fn dispatched() {}\n"
    result = extract_symbols("foo.rs", src)
    assert result, "expected non-empty result for a simple Rust fn"
    assert "dispatched" in _names(result)


# ---------------------------------------------------------------------------
# Mixed top-level file: every kind together
# ---------------------------------------------------------------------------


def test_mixed_top_level_kinds(backend: str) -> None:
    """A file with one of each kind surfaces all of them with correct kinds."""

    src = (
        "const MAX: i32 = 10;\n"
        "static NAME: &str = \"x\";\n"
        "struct Point { x: i32, y: i32 }\n"
        "enum Dir { N, S }\n"
        "trait Move { fn go(&self); }\n"
        "type Alias = Point;\n"
        "fn run() {}\n"
        "\n"
        "impl Point {\n"
        "    fn origin() -> Point { Point { x: 0, y: 0 } }\n"
        "}\n"
    )
    result = extract_symbols("sample.rs", src)
    assert _by_name(result, "MAX").kind == "const"
    assert _by_name(result, "NAME").kind == "const"
    assert _by_name(result, "Point").kind == "type"
    assert _by_name(result, "Dir").kind == "enum"
    assert _by_name(result, "Move").kind == "interface"
    assert _by_name(result, "Alias").kind == "type"
    assert _by_name(result, "run").kind == "function"
    origin = _by_name(result, "origin")
    assert origin.kind == "function"
    assert origin.parent == "Point"
