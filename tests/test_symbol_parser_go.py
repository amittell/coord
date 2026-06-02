"""Tests for the Go symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``backend`` fixture. The tree-sitter case skips automatically when the
``tree_sitter_go`` package is unavailable so the suite still runs cleanly
on machines without the native wheels.

Test fixtures are inline Go source strings; we deliberately avoid touching
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
    importlib.import_module("tree_sitter_go")
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
                reason="tree-sitter-go not installed",
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
# Functions and methods
# ---------------------------------------------------------------------------


def test_simple_function(backend: str) -> None:
    src = "package main\n\nfunc helloWorld() {\n    return\n}\n"
    result = extract_symbols("sample.go", src)
    assert _names(result) == ["helloWorld"]
    assert _by_name(result, "helloWorld").kind == "function"


def test_method_on_receiver(backend: str) -> None:
    """v0.16: method receivers populate ``parent``."""

    src = (
        "package main\n"
        "\n"
        "type Foo struct{}\n"
        "\n"
        "func (f *Foo) DoIt() error { return nil }\n"
    )
    result = extract_symbols("sample.go", src)
    assert "DoIt" in _names(result)
    do_it = _by_name(result, "DoIt")
    assert do_it.kind == "function"
    assert do_it.parent == "Foo"


def test_generic_function(backend: str) -> None:
    src = "package main\n\nfunc Generic[T any](x T) T { return x }\n"
    result = extract_symbols("sample.go", src)
    assert "Generic" in _names(result)
    assert _by_name(result, "Generic").kind == "function"


def test_multi_line_function_signature(backend: str) -> None:
    src = (
        "package main\n"
        "\n"
        "func multiLine(\n"
        "    a int,\n"
        "    b int,\n"
        ") int { return a + b }\n"
    )
    result = extract_symbols("sample.go", src)
    assert "multiLine" in _names(result)
    assert _by_name(result, "multiLine").kind == "function"


def test_exported_and_unexported_both_appear(backend: str) -> None:
    """Go visibility is encoded in case; the parser must not filter on it."""

    src = (
        "package main\n"
        "\n"
        "func Exported() {}\n"
        "\n"
        "func unexported() {}\n"
    )
    result = extract_symbols("sample.go", src)
    names = _names(result)
    assert "Exported" in names
    assert "unexported" in names


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


def test_type_struct(backend: str) -> None:
    src = (
        "package main\n"
        "\n"
        "type Foo struct {\n"
        "    Name string\n"
        "}\n"
    )
    result = extract_symbols("sample.go", src)
    assert "Foo" in _names(result)
    assert _by_name(result, "Foo").kind == "type"


def test_type_interface(backend: str) -> None:
    src = (
        "package main\n"
        "\n"
        "type Bar interface {\n"
        "    DoIt() error\n"
        "}\n"
    )
    result = extract_symbols("sample.go", src)
    assert "Bar" in _names(result)
    assert _by_name(result, "Bar").kind == "interface"


def test_type_alias(backend: str) -> None:
    src = "package main\n\ntype Foo = Bar\n"
    result = extract_symbols("sample.go", src)
    assert "Foo" in _names(result)
    assert _by_name(result, "Foo").kind == "type"


# ---------------------------------------------------------------------------
# Constants and variables
# ---------------------------------------------------------------------------


def test_const_declaration(backend: str) -> None:
    src = 'package main\n\nconst Greeting = "hi"\n'
    result = extract_symbols("sample.go", src)
    assert "Greeting" in _names(result)
    assert _by_name(result, "Greeting").kind == "const"


def test_var_with_function_value(backend: str) -> None:
    """A var bound to a function literal is a claimable callable."""

    src = "package main\n\nvar handler = func() int { return 42 }\n"
    result = extract_symbols("sample.go", src)
    assert "handler" in _names(result)
    assert _by_name(result, "handler").kind == "const"


# ---------------------------------------------------------------------------
# Empty / structural files
# ---------------------------------------------------------------------------


def test_comment_only_file(backend: str) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract_symbols("sample.go", src)
    assert result == []


def test_imports_only_file(backend: str) -> None:
    src = (
        "package main\n"
        "\n"
        'import "fmt"\n'
        'import (\n'
        '    "os"\n'
        '    "strings"\n'
        ')\n'
    )
    result = extract_symbols("sample.go", src)
    assert result == []


def test_package_declaration_only(backend: str) -> None:
    src = "package main\n"
    result = extract_symbols("sample.go", src)
    assert result == []


# ---------------------------------------------------------------------------
# Build-tag prefixed file
# ---------------------------------------------------------------------------


def test_build_tag_prefixed_file(backend: str) -> None:
    """Build tags are comment lines; the first real decl after them surfaces."""

    src = (
        "//go:build linux\n"
        "// +build linux\n"
        "\n"
        "package mypkg\n"
        "\n"
        "func BuildTagged() {}\n"
    )
    result = extract_symbols("sample.go", src)
    assert "BuildTagged" in _names(result)
    assert _by_name(result, "BuildTagged").kind == "function"


# ---------------------------------------------------------------------------
# Top-level only
# ---------------------------------------------------------------------------


def test_nested_type_spec_inside_func_excluded(backend: str) -> None:
    """A type declared inside a function body must not surface as top-level.

    Conventional Go formatting indents nested declarations, which keeps them
    out of the regex backend; the tree-sitter backend walks only direct
    children of ``source_file`` so it filters them by structure.
    """

    src = (
        "package main\n"
        "\n"
        "func outer() {\n"
        "    type Inner struct{}\n"
        "    _ = Inner{}\n"
        "}\n"
    )
    result = extract_symbols("sample.go", src)
    names = _names(result)
    assert "outer" in names
    assert "Inner" not in names


def test_method_on_embedded_type_still_top_level(backend: str) -> None:
    """A method on a type that embeds another type is still a top-level decl.

    Method declarations are siblings of type declarations in the Go AST; the
    receiver type can be a struct that embeds other types and the method must
    still surface with ``kind='function'``.
    """

    src = (
        "package main\n"
        "\n"
        "type Inner struct{}\n"
        "\n"
        "type Outer struct {\n"
        "    Inner\n"
        "}\n"
        "\n"
        "func (o *Outer) Surfaces() {}\n"
    )
    result = extract_symbols("sample.go", src)
    assert "Surfaces" in _names(result)
    surfaces = _by_name(result, "Surfaces")
    assert surfaces.kind == "function"
    assert surfaces.parent == "Outer"


# ---------------------------------------------------------------------------
# Dispatcher path
# ---------------------------------------------------------------------------


def test_dispatcher_routes_go_extension(backend: str) -> None:
    """A ``.go`` file path must dispatch to the Go backend and return symbols."""

    src = "package main\n\nfunc dispatched() {}\n"
    result = extract_symbols("foo.go", src)
    assert result, "expected non-empty result for a simple Go func"
    assert "dispatched" in _names(result)


# ---------------------------------------------------------------------------
# v0.16: methods on receiver types
# ---------------------------------------------------------------------------


def test_pointer_receiver_extracts_parent(backend: str) -> None:
    """``func (s *Server) Start()`` carries ``parent='Server'``."""

    src = (
        "package main\n"
        "\n"
        "type Server struct{}\n"
        "\n"
        "func (s *Server) Start() error { return nil }\n"
    )
    result = extract_symbols("sample.go", src)
    start = _by_name(result, "Start")
    assert start.kind == "function"
    assert start.parent == "Server"


def test_value_receiver_extracts_parent(backend: str) -> None:
    """A value receiver (no ``*``) reduces to the same parent name."""

    src = (
        "package main\n"
        "\n"
        "type Server struct{}\n"
        "\n"
        "func (s Server) Stop() error { return nil }\n"
    )
    result = extract_symbols("sample.go", src)
    stop = _by_name(result, "Stop")
    assert stop.kind == "function"
    assert stop.parent == "Server"


def test_unnamed_receiver_still_extracts_type(backend: str) -> None:
    """Receiver binding name is optional: ``func (*Server) Reset()``."""

    src = (
        "package main\n"
        "\n"
        "type Server struct{}\n"
        "\n"
        "func (*Server) Reset() {}\n"
    )
    result = extract_symbols("sample.go", src)
    reset = _by_name(result, "Reset")
    assert reset.kind == "function"
    assert reset.parent == "Server"


def test_top_level_func_has_no_parent(backend: str) -> None:
    """Plain top-level functions keep ``parent=None``."""

    src = "package main\n\nfunc Init() {}\n"
    result = extract_symbols("sample.go", src)
    init = _by_name(result, "Init")
    assert init.kind == "function"
    assert init.parent is None


def test_methods_with_generic_receiver(backend: str) -> None:
    """Generic receivers drop the type-parameter list: ``Container[T]`` -> ``Container``."""

    src = (
        "package main\n"
        "\n"
        "type Container[T any] struct{}\n"
        "\n"
        "func (c *Container[T]) Add(item T) {}\n"
    )
    result = extract_symbols("sample.go", src)
    add = _by_name(result, "Add")
    assert add.kind == "function"
    assert add.parent == "Container"


def test_methods_on_different_receivers_distinct(backend: str) -> None:
    """Two methods on different receivers carry different ``parent`` values."""

    src = (
        "package main\n"
        "\n"
        "type Server struct{}\n"
        "type Client struct{}\n"
        "\n"
        "func (s *Server) Handle() {}\n"
        "func (c *Client) Send() {}\n"
    )
    result = extract_symbols("sample.go", src)
    handle = _by_name(result, "Handle")
    send = _by_name(result, "Send")
    assert handle.parent == "Server"
    assert send.parent == "Client"
