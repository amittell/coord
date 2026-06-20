"""Tests for the C++ symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``extract`` fixture. The tree-sitter case skips automatically when the
``tree_sitter_cpp`` package is unavailable so the suite still runs cleanly on
machines without the native wheels. The regex backend is always exercised.

The dispatcher in ``coordination.symbols`` is wired separately (by the
integrator that registers the C++ extensions), so these tests call each
backend module's ``extract`` function directly rather than going through
``extract_symbols``. That keeps the coverage honest regardless of whether the
``.cc`` / ``.cpp`` / ``.cxx`` / ``.hpp`` / ``.hh`` extensions are registered
yet.

Test fixtures are inline C++ source strings; we deliberately avoid touching
the filesystem so the tests stay fast and deterministic.
"""

from __future__ import annotations

from typing import Callable

import pytest

from coordination.symbols import Symbol
from coordination.symbols import cpp_regex

# Resolve the tree-sitter backend only if its native wheel is present. The
# import is guarded so collection does not fail on a regex-only install.
cpp_treesitter = pytest.importorskip(
    "coordination.symbols.cpp_treesitter",
    reason="import guard; the treesitter param is skipped when the wheel is absent",
)

_TREESITTER_AVAILABLE = True
try:  # pragma: no cover - depends on install state
    import importlib

    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_cpp")
except ImportError:  # pragma: no cover - depends on install state
    _TREESITTER_AVAILABLE = False


Extractor = Callable[[str], list[Symbol]]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param(
            "treesitter",
            marks=pytest.mark.skipif(
                not _TREESITTER_AVAILABLE,
                reason="tree-sitter-cpp not installed",
            ),
        ),
        "regex",
    ]
)
def extract(request: pytest.FixtureRequest) -> Extractor:
    """Return the ``extract`` callable for the parametrised backend."""

    if request.param == "treesitter":
        return cpp_treesitter.extract
    return cpp_regex.extract


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
# Free functions (both backends)
# ---------------------------------------------------------------------------


def test_free_function(extract: Extractor) -> None:
    src = "int add(int a, int b) {\n    return a + b;\n}\n"
    result = extract(src)
    assert "add" in _names(result)
    add = _by_name(result, "add")
    assert add.kind == "function"
    assert add.parent is None


def test_void_free_function(extract: Extractor) -> None:
    src = "void run() {\n    return;\n}\n"
    result = extract(src)
    assert "run" in _names(result)
    assert _by_name(result, "run").kind == "function"


def test_out_of_line_method_has_parent(extract: Extractor) -> None:
    """``void Foo::bar() {}`` carries ``parent='Foo'`` in both backends."""

    src = "void Foo::bar() {\n    return;\n}\n"
    result = extract(src)
    assert "bar" in _names(result)
    bar = _by_name(result, "bar")
    assert bar.kind == "function"
    assert bar.parent == "Foo"


# ---------------------------------------------------------------------------
# Classes / structs / enums (both backends, header line)
# ---------------------------------------------------------------------------


def test_class_kind(extract: Extractor) -> None:
    src = "class Widget {\npublic:\n    void draw();\n};\n"
    result = extract(src)
    assert "Widget" in _names(result)
    assert _by_name(result, "Widget").kind == "class"


def test_struct_kind_is_type(extract: Extractor) -> None:
    """A C++ struct is treated as the ``type`` grain, matching Go structs."""

    src = "struct Point {\n    int x;\n    int y;\n};\n"
    result = extract(src)
    assert "Point" in _names(result)
    assert _by_name(result, "Point").kind == "type"


def test_enum_kind(extract: Extractor) -> None:
    src = "enum Color {\n    Red,\n    Green,\n    Blue,\n};\n"
    result = extract(src)
    assert "Color" in _names(result)
    assert _by_name(result, "Color").kind == "enum"


def test_scoped_enum_kind(extract: Extractor) -> None:
    """``enum class`` (scoped enum) also surfaces as ``kind='enum'``."""

    src = "enum class Mode {\n    Fast,\n    Slow,\n};\n"
    result = extract(src)
    assert "Mode" in _names(result)
    assert _by_name(result, "Mode").kind == "enum"


# ---------------------------------------------------------------------------
# Empty / structural files (both backends)
# ---------------------------------------------------------------------------


def test_comment_only_file(extract: Extractor) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract(src)
    assert result == []


def test_include_only_file(extract: Extractor) -> None:
    src = '#include <vector>\n#include "local.hpp"\n'
    result = extract(src)
    assert result == []


# ---------------------------------------------------------------------------
# Tree-sitter only: in-class methods, nesting, templates
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_inline_method_parent() -> None:
    """A method defined inside a class body carries ``parent='ClassName'``."""

    src = (
        "class Server {\n"
        "public:\n"
        "    void start() {\n"
        "        return;\n"
        "    }\n"
        "};\n"
    )
    result = cpp_treesitter.extract(src)
    assert "Server" in _names(result)
    assert _by_name(result, "Server").kind == "class"
    start = _by_name(result, "start")
    assert start.kind == "function"
    assert start.parent == "Server"


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_method_prototype_in_class_parent() -> None:
    """A member-function prototype (no body) still surfaces with parent set."""

    src = (
        "class Client {\n"
        "public:\n"
        "    int send(const char* data);\n"
        "    int count_;\n"
        "};\n"
    )
    result = cpp_treesitter.extract(src)
    send = _by_name(result, "send")
    assert send.kind == "function"
    assert send.parent == "Client"
    # Plain data members are not callable units and must not surface.
    assert "count_" not in _names(result)


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_struct_method_parent() -> None:
    """Methods inside a struct parent to the struct name; struct kind='type'."""

    src = (
        "struct Vec {\n"
        "    double length() const {\n"
        "        return 0.0;\n"
        "    }\n"
        "};\n"
    )
    result = cpp_treesitter.extract(src)
    assert _by_name(result, "Vec").kind == "type"
    length = _by_name(result, "length")
    assert length.kind == "function"
    assert length.parent == "Vec"


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_nested_class_full_path_parent() -> None:
    """A method on a nested class carries the full ``Outer::Inner`` path."""

    src = (
        "class Outer {\n"
        "public:\n"
        "    class Inner {\n"
        "    public:\n"
        "        void deep() {\n"
        "            return;\n"
        "        }\n"
        "    };\n"
        "};\n"
    )
    result = cpp_treesitter.extract(src)
    names = _names(result)
    assert "Outer" in names
    assert "Inner" in names
    inner = _by_name(result, "Inner")
    assert inner.kind == "class"
    assert inner.parent == "Outer"
    deep = _by_name(result, "deep")
    assert deep.kind == "function"
    assert deep.parent == "Outer::Inner"


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_out_of_line_method_treesitter_parent() -> None:
    """Out-of-line ``Foo::bar`` definitions parent to the qualifier."""

    src = (
        "class Foo {\n"
        "public:\n"
        "    void bar();\n"
        "};\n"
        "\n"
        "void Foo::bar() {\n"
        "    return;\n"
        "}\n"
    )
    result = cpp_treesitter.extract(src)
    bars = [s for s in result if s.name == "bar"]
    assert any(s.parent == "Foo" for s in bars)
    assert all(s.kind == "function" for s in bars)


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_templated_free_function() -> None:
    """A templated free function surfaces with ``kind='function'``."""

    src = (
        "template <typename T>\n"
        "T identity(T value) {\n"
        "    return value;\n"
        "}\n"
    )
    result = cpp_treesitter.extract(src)
    ident = _by_name(result, "identity")
    assert ident.kind == "function"
    assert ident.parent is None


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_pointer_return_free_function() -> None:
    """A pointer return type (``int* f()``) still resolves the function name."""

    src = "int* allocate(int n) {\n    return nullptr;\n}\n"
    result = cpp_treesitter.extract(src)
    alloc = _by_name(result, "allocate")
    assert alloc.kind == "function"
    assert alloc.parent is None


@pytest.mark.skipif(
    not _TREESITTER_AVAILABLE, reason="tree-sitter-cpp not installed"
)
def test_namespace_body_not_walked() -> None:
    """Declarations inside a namespace block are not surfaced as top-level.

    The namespace itself is not claimable and the extractor does not recurse
    into namespace bodies, mirroring the Go backend's top-level-only contract.
    """

    src = (
        "namespace app {\n"
        "void hidden() {\n"
        "    return;\n"
        "}\n"
        "}\n"
    )
    result = cpp_treesitter.extract(src)
    assert "app" not in _names(result)
    assert "hidden" not in _names(result)


# ---------------------------------------------------------------------------
# Regex backend: line numbers and parent capture verified directly
# ---------------------------------------------------------------------------


def test_regex_out_of_line_parent_direct() -> None:
    """Exercise the regex backend's qualifier capture explicitly."""

    src = "int Calc::total() {\n    return 0;\n}\n"
    result = cpp_regex.extract(src)
    total = _by_name(result, "total")
    assert total.kind == "function"
    assert total.parent == "Calc"
    assert total.start_line == 1
    assert total.end_line == 1


def test_regex_struct_is_type_direct() -> None:
    src = "struct Box {\n    int w;\n};\n"
    result = cpp_regex.extract(src)
    box = _by_name(result, "Box")
    assert box.kind == "type"
    assert box.start_line == 1


def test_regex_indented_member_excluded() -> None:
    """An indented in-class method is below column zero and is skipped."""

    src = (
        "class Engine {\n"
        "    void run() {\n"
        "        return;\n"
        "    }\n"
        "};\n"
    )
    result = cpp_regex.extract(src)
    names = _names(result)
    assert "Engine" in names
    # The regex backend documents that indented members are missed.
    assert "run" not in names
