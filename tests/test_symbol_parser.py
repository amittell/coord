"""Tests for the TypeScript symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``backend`` fixture. The tree-sitter case skips automatically when the
``tree_sitter`` package or its TypeScript grammar is unavailable, so the
suite still runs cleanly on machines without the native wheels.

Test fixtures are inline TypeScript strings; we deliberately avoid touching
the filesystem so the cache key (``file_path``, content hash) is stable and
the tests stay fast.
"""

from __future__ import annotations

import importlib

import pytest

from coordination import symbols
from coordination.symbols import Symbol, extract_symbols

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_typescript")
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
                reason="tree-sitter not installed",
            ),
        ),
        "regex",
    ]
)
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Force a specific backend for the duration of the test."""

    monkeypatch.setenv("COORD_SYMBOL_PARSER", request.param)
    # Bust any cached entries built with a different backend setting.
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
# Basic declarations
# ---------------------------------------------------------------------------


def test_function_declaration(backend: str) -> None:
    src = "function helloWorld() {\n  return 1;\n}\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["helloWorld"]
    assert _by_name(result, "helloWorld").kind == "function"


def test_exported_function(backend: str) -> None:
    src = "export function publicFn() { return true; }\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["publicFn"]
    assert result[0].kind == "function"


def test_exported_async_function(backend: str) -> None:
    src = "export async function fetchData() { return await load(); }\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["fetchData"]
    assert result[0].kind == "function"


def test_class_with_generics(backend: str) -> None:
    src = "export class Container<T> {\n  value: T;\n}\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["Container"]
    assert result[0].kind == "class"


def test_interface_declaration(backend: str) -> None:
    src = "interface IThing {\n  id: string;\n}\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["IThing"]
    assert result[0].kind == "interface"


def test_type_alias(backend: str) -> None:
    src = "export type UserId = string;\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["UserId"]
    assert result[0].kind == "type"


def test_enum(backend: str) -> None:
    src = "enum Direction {\n  Up,\n  Down,\n}\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["Direction"]
    assert result[0].kind == "enum"


def test_const_arrow(backend: str) -> None:
    src = "const handler = (x: number) => x + 1;\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["handler"]
    assert result[0].kind == "const"


def test_default_export_named_function(backend: str) -> None:
    src = "export default function namedDefault() { return 0; }\n"
    result = extract_symbols("sample.ts", src)
    # Even though it is the default export, it has a name -- we keep the
    # caller-supplied name and let the API layer decide normalisation.
    assert "namedDefault" in _names(result)


def test_default_export_anonymous(backend: str) -> None:
    src = "export default function() { return 42; }\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["default"]
    assert result[0].kind == "function"


def test_default_export_anonymous_async(backend: str) -> None:
    src = "export default async function() { return await x(); }\n"
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["default"]
    assert result[0].kind == "function"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_comment_only_file(backend: str) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract_symbols("sample.ts", src)
    assert result == []


def test_imports_only_file(backend: str) -> None:
    src = (
        "import React from 'react';\n"
        "import { useState } from 'react';\n"
        "import type { Props } from './types';\n"
    )
    result = extract_symbols("sample.ts", src)
    assert result == []


def test_nested_function_not_top_level(backend: str) -> None:
    src = (
        "function outer() {\n"
        "  function inner() {\n"
        "    return 1;\n"
        "  }\n"
        "  return inner();\n"
        "}\n"
    )
    result = extract_symbols("sample.ts", src)
    # outer must be present; inner must NOT be -- it is not a top-level
    # declaration.
    assert "outer" in _names(result)
    assert "inner" not in _names(result)


def test_comment_with_function_word_does_not_match(backend: str) -> None:
    src = (
        "// this function is great\n"
        "/* function notReal() {} */\n"
        "function real() {}\n"
    )
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["real"]


def test_jsx_const_component(backend: str) -> None:
    src = (
        "import React from 'react';\n"
        "export const MyComp = (props: Props) => {\n"
        "  return <div>{props.x}</div>;\n"
        "};\n"
    )
    result = extract_symbols("sample.tsx", src)
    assert "MyComp" in _names(result)
    assert _by_name(result, "MyComp").kind == "const"


def test_const_non_function_excluded(backend: str) -> None:
    """`const X = 42` is not a callable scope and is dropped by tree-sitter.

    The regex backend cannot reliably distinguish a const value from a const
    function (it does not look at the RHS), so it includes the symbol. Both
    behaviours are acceptable contracts: the test asserts the per-backend
    expectation rather than forcing them to agree.
    """

    src = "export const NOT_A_FUNCTION = 42;\nexport const fn = () => 1;\n"
    result = extract_symbols("sample.ts", src)
    names = _names(result)
    assert "fn" in names
    if backend == "treesitter":
        assert "NOT_A_FUNCTION" not in names


def test_multiple_declarations_preserve_order(backend: str) -> None:
    src = (
        "function a() {}\n"
        "class B {}\n"
        "interface C {}\n"
        "type D = number;\n"
        "enum E { X }\n"
        "const f = () => 1;\n"
    )
    result = extract_symbols("sample.ts", src)
    assert _names(result) == ["a", "B", "C", "D", "E", "f"]


def test_start_line_is_one_indexed(backend: str) -> None:
    src = "\n\nfunction third() {}\n"
    result = extract_symbols("sample.ts", src)
    assert result[0].start_line == 3


# ---------------------------------------------------------------------------
# Dispatch / caching / env-var behaviour
# ---------------------------------------------------------------------------


def test_unknown_extension_returns_empty(backend: str) -> None:
    assert extract_symbols("foo.unknown", "function ignored() {}") == []
    assert extract_symbols("noext", "function ignored() {}") == []
    assert extract_symbols("foo.py", "def x(): pass") == []


def test_cache_returns_same_list_object() -> None:
    """Same (path, content) pair must round-trip through the cache."""

    src = "function cached() {}\n"
    first = extract_symbols("cache.ts", src)
    second = extract_symbols("cache.ts", src)
    # The cache hands back the same list object, not a copy -- callers must
    # treat the result as read-only.
    assert first is second


def test_cache_invalidates_on_content_change() -> None:
    first = extract_symbols("evolve.ts", "function v1() {}\n")
    second = extract_symbols("evolve.ts", "function v2() {}\n")
    assert _names(first) == ["v1"]
    assert _names(second) == ["v2"]


def test_env_var_unknown_value_falls_back_to_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus COORD_SYMBOL_PARSER setting must not crash; auto wins."""

    monkeypatch.setenv("COORD_SYMBOL_PARSER", "bananas")
    symbols._CACHE.clear()
    result = extract_symbols("sample.ts", "function still_works() {}\n")
    assert _names(result) == ["still_works"]


def test_regex_backend_documented_anonymous_default() -> None:
    """Regex backend handles the documented anonymous default form."""

    import os

    saved = os.environ.get("COORD_SYMBOL_PARSER")
    os.environ["COORD_SYMBOL_PARSER"] = "regex"
    symbols._CACHE.clear()
    try:
        result = extract_symbols(
            "anon.ts", "export default function() { return 1; }\n"
        )
    finally:
        if saved is None:
            del os.environ["COORD_SYMBOL_PARSER"]
        else:
            os.environ["COORD_SYMBOL_PARSER"] = saved
        symbols._CACHE.clear()

    assert _names(result) == ["default"]
    assert result[0].kind == "function"
