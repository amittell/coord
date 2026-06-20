"""Tests for the JavaScript symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``backend`` fixture. The tree-sitter case skips automatically when the
``tree_sitter_javascript`` package is unavailable so the suite still runs
cleanly on machines without the native wheels.

Test fixtures are inline JavaScript source strings; we deliberately avoid
touching the filesystem so the cache key (``file_path``, content hash) is
stable and the tests stay fast.
"""

from __future__ import annotations

import importlib

import pytest

from coordination import symbols
from coordination.symbols import Symbol, extract_symbols

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_javascript")
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
                reason="tree-sitter-javascript not installed",
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
    src = "function helloWorld() {\n    return;\n}\n"
    result = extract_symbols("sample.js", src)
    assert _names(result) == ["helloWorld"]
    assert _by_name(result, "helloWorld").kind == "function"


def test_generator_function(backend: str) -> None:
    src = "function* gen() {\n    yield 1;\n}\n"
    result = extract_symbols("sample.js", src)
    assert "gen" in _names(result)
    assert _by_name(result, "gen").kind == "function"


def test_async_function(backend: str) -> None:
    src = "async function fetchThing() {\n    return await get();\n}\n"
    result = extract_symbols("sample.js", src)
    assert "fetchThing" in _names(result)
    assert _by_name(result, "fetchThing").kind == "function"


def test_exported_function(backend: str) -> None:
    src = "export function exported() {}\n"
    result = extract_symbols("sample.js", src)
    assert "exported" in _names(result)
    assert _by_name(result, "exported").kind == "function"


def test_default_export_anonymous_function(backend: str) -> None:
    """An anonymous default-exported function normalises to ``default``."""

    src = "export default function () {\n    return 1;\n}\n"
    result = extract_symbols("sample.js", src)
    assert "default" in _names(result)
    assert _by_name(result, "default").kind == "function"


def test_function_signature_spans_lines(backend: str) -> None:
    src = (
        "function multiLine(\n"
        "    a,\n"
        "    b,\n"
        ") {\n"
        "    return a + b;\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    assert "multiLine" in _names(result)
    assert _by_name(result, "multiLine").kind == "function"


# ---------------------------------------------------------------------------
# Arrow-function consts
# ---------------------------------------------------------------------------


def test_arrow_function_const(backend: str) -> None:
    """A const bound to an arrow function is a claimable callable."""

    src = "const handler = (req, res) => {\n    return res;\n};\n"
    result = extract_symbols("sample.js", src)
    assert "handler" in _names(result)
    assert _by_name(result, "handler").kind == "const"


def test_const_function_expression(backend: str) -> None:
    src = "const make = function () { return 1; };\n"
    result = extract_symbols("sample.js", src)
    assert "make" in _names(result)
    assert _by_name(result, "make").kind == "const"


def test_non_function_const(backend: str) -> None:
    """A plain value const is not a callable scope.

    The tree-sitter backend inspects the bound value and drops
    ``const answer = 42`` because it is not a function. The regex fallback
    cannot see the value type without becoming a parser, so it over-claims
    any ``const``/``let``/``var`` declaration -- a documented false-positive
    mirrored from ``ts_regex``. The assertion is therefore backend-aware.
    """

    src = "const answer = 42;\n"
    result = extract_symbols("sample.js", src)
    if backend == "treesitter":
        assert "answer" not in _names(result)
    else:
        # Regex fallback over-claims the plain const as kind='const'.
        assert _by_name(result, "answer").kind == "const"


def test_let_arrow_function(backend: str) -> None:
    src = "let run = () => {};\n"
    result = extract_symbols("sample.js", src)
    assert "run" in _names(result)
    assert _by_name(result, "run").kind == "const"


# ---------------------------------------------------------------------------
# Classes and methods
# ---------------------------------------------------------------------------


def test_class_declaration(backend: str) -> None:
    src = "class Widget {\n}\n"
    result = extract_symbols("sample.js", src)
    assert "Widget" in _names(result)
    assert _by_name(result, "Widget").kind == "class"


def test_class_method_has_parent(backend: str) -> None:
    """v0.16: class methods populate ``parent`` with the class name."""

    src = (
        "class Service {\n"
        "    handle() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    assert "handle" in _names(result)
    handle = _by_name(result, "handle")
    assert handle.kind == "function"
    assert handle.parent == "Service"


def test_class_constructor_is_method(backend: str) -> None:
    src = (
        "class Service {\n"
        "    constructor() {\n"
        "        this.ready = true;\n"
        "    }\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    ctor = _by_name(result, "constructor")
    assert ctor.kind == "function"
    assert ctor.parent == "Service"


def test_static_and_async_methods(backend: str) -> None:
    src = (
        "class Service {\n"
        "    static create() {}\n"
        "    async load() {}\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    create = _by_name(result, "create")
    load = _by_name(result, "load")
    assert create.kind == "function"
    assert create.parent == "Service"
    assert load.kind == "function"
    assert load.parent == "Service"


def test_arrow_field_method(backend: str) -> None:
    """A class field bound to an arrow function emits ``kind='const'``."""

    src = (
        "class Service {\n"
        "    onClick = (e) => {\n"
        "        return e;\n"
        "    };\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    on_click = _by_name(result, "onClick")
    assert on_click.kind == "const"
    assert on_click.parent == "Service"


def test_top_level_function_has_no_parent(backend: str) -> None:
    """Plain top-level functions keep ``parent=None``."""

    src = "function init() {}\n"
    result = extract_symbols("sample.js", src)
    init = _by_name(result, "init")
    assert init.kind == "function"
    assert init.parent is None


def test_two_classes_methods_distinct(backend: str) -> None:
    """Methods on different classes carry different ``parent`` values."""

    src = (
        "class Server {\n"
        "    start() {}\n"
        "}\n"
        "class Client {\n"
        "    send() {}\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    start = _by_name(result, "start")
    send = _by_name(result, "send")
    assert start.parent == "Server"
    assert send.parent == "Client"


# ---------------------------------------------------------------------------
# v0.19: nested classes
# ---------------------------------------------------------------------------


def test_nested_class_via_static_field(backend: str) -> None:
    """``static Inner = class { ... }`` nests with a full ``Outer::Inner`` path."""

    src = (
        "class Outer {\n"
        "    static Inner = class {\n"
        "        handle() {}\n"
        "    };\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    names = _names(result)
    assert "Outer" in names
    assert "Inner" in names
    assert "handle" in names
    outer = _by_name(result, "Outer")
    inner = _by_name(result, "Inner")
    handle = _by_name(result, "handle")
    assert outer.kind == "class"
    assert outer.parent is None
    assert inner.kind == "class"
    assert inner.parent == "Outer"
    assert handle.kind == "function"
    assert handle.parent == "Outer::Inner"


# ---------------------------------------------------------------------------
# Empty / structural files
# ---------------------------------------------------------------------------


def test_comment_only_file(backend: str) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract_symbols("sample.js", src)
    assert result == []


def test_imports_only_file(backend: str) -> None:
    src = (
        "import fs from 'fs';\n"
        "import { join } from 'path';\n"
    )
    result = extract_symbols("sample.js", src)
    assert result == []


def test_empty_file(backend: str) -> None:
    result = extract_symbols("sample.js", "")
    assert result == []


# ---------------------------------------------------------------------------
# Top-level only
# ---------------------------------------------------------------------------


def test_nested_function_excluded(backend: str) -> None:
    """A function declared inside another function must not surface."""

    src = (
        "function outer() {\n"
        "    function inner() {}\n"
        "    return inner;\n"
        "}\n"
    )
    result = extract_symbols("sample.js", src)
    names = _names(result)
    assert "outer" in names
    assert "inner" not in names


# ---------------------------------------------------------------------------
# Dispatcher path
# ---------------------------------------------------------------------------


def test_dispatcher_routes_js_extension(backend: str) -> None:
    """A ``.js`` file path must dispatch to the JavaScript backend."""

    src = "function dispatched() {}\n"
    result = extract_symbols("foo.js", src)
    assert result, "expected non-empty result for a simple JS function"
    assert "dispatched" in _names(result)


def test_dispatcher_routes_jsx_extension(backend: str) -> None:
    """A ``.jsx`` file path must dispatch to the JavaScript backend too."""

    src = "export const App = () => null;\n"
    result = extract_symbols("App.jsx", src)
    assert "App" in _names(result)
    assert _by_name(result, "App").kind == "const"
