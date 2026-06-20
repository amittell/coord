"""Tests for the C symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``backend`` fixture that yields the module's ``extract`` callable directly.
The tree-sitter case skips automatically when the ``tree_sitter_c`` package is
unavailable so the suite still runs cleanly on machines without the native
wheels; the regex backend is always tested.

These tests call each backend's ``extract`` directly rather than going through
``extract_symbols``. The dispatcher registry for ``.c`` / ``.h`` is wired
separately (in ``coordination/symbols/__init__.py``) by the integrator, so the
backend modules are validated here in isolation of that wiring.

Test fixtures are inline C source strings; we deliberately avoid touching the
filesystem so the tests stay fast and self-contained.

C has no classes and no member functions, so every Symbol emitted carries
``parent=None``. The parent-edge assertions below pin that the field stays
unset rather than asserting a (nonexistent) ``Class::method`` relationship.
"""

from __future__ import annotations

from typing import Callable

import pytest

from coordination.symbols import Symbol, c_regex

Extractor = Callable[[str], list[Symbol]]


def _treesitter_extract() -> Extractor:
    """Import the tree-sitter backend, skipping the test if the wheel is absent."""

    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_c")
    from coordination.symbols import c_treesitter

    return c_treesitter.extract


@pytest.fixture(
    params=[
        pytest.param("treesitter"),
        pytest.param("regex"),
    ]
)
def extract(request: pytest.FixtureRequest) -> Extractor:
    """Yield a backend's ``extract`` callable.

    The tree-sitter parameter skips via ``importorskip`` when the grammar wheel
    is not installed; the regex parameter always runs.
    """

    if request.param == "treesitter":
        return _treesitter_extract()
    return c_regex.extract


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


def test_simple_function(extract: Extractor) -> None:
    src = "int hello_world(void) {\n    return 0;\n}\n"
    result = extract(src)
    assert "hello_world" in _names(result)
    assert _by_name(result, "hello_world").kind == "function"


def test_function_has_no_parent(extract: Extractor) -> None:
    """C functions are free-standing: ``parent`` stays ``None``."""

    src = "void run(void) {\n    return;\n}\n"
    result = extract(src)
    run = _by_name(result, "run")
    assert run.kind == "function"
    assert run.parent is None


def test_pointer_return_type_function(extract: Extractor) -> None:
    """A pointer return type nests the function_declarator one level deeper."""

    src = "char *make_buffer(int n) {\n    return 0;\n}\n"
    result = extract(src)
    assert "make_buffer" in _names(result)
    assert _by_name(result, "make_buffer").kind == "function"


def test_multi_token_return_type_function(extract: Extractor) -> None:
    src = "static unsigned long counter_value(void) {\n    return 0;\n}\n"
    result = extract(src)
    assert "counter_value" in _names(result)
    assert _by_name(result, "counter_value").kind == "function"


def test_function_line_span(extract: Extractor) -> None:
    """The first declaration sits on line 1; ``start_line`` is 1-indexed."""

    src = "int first(void) {\n    return 0;\n}\n"
    result = extract(src)
    first = _by_name(result, "first")
    assert first.start_line == 1
    assert first.end_line >= first.start_line


# ---------------------------------------------------------------------------
# Structs / unions (kind='type')
# ---------------------------------------------------------------------------


def test_struct_definition(extract: Extractor) -> None:
    src = "struct Point {\n    int x;\n    int y;\n};\n"
    result = extract(src)
    assert "Point" in _names(result)
    point = _by_name(result, "Point")
    assert point.kind == "type"
    assert point.parent is None


def test_union_definition(extract: Extractor) -> None:
    src = "union Value {\n    int i;\n    float f;\n};\n"
    result = extract(src)
    assert "Value" in _names(result)
    assert _by_name(result, "Value").kind == "type"


# ---------------------------------------------------------------------------
# Enums (kind='enum')
# ---------------------------------------------------------------------------


def test_enum_definition(extract: Extractor) -> None:
    src = "enum Color {\n    RED,\n    GREEN,\n    BLUE\n};\n"
    result = extract(src)
    assert "Color" in _names(result)
    color = _by_name(result, "Color")
    assert color.kind == "enum"
    assert color.parent is None


# ---------------------------------------------------------------------------
# Typedefs (kind='type')
# ---------------------------------------------------------------------------


def test_typedef_primitive(extract: Extractor) -> None:
    src = "typedef int MyInt;\n"
    result = extract(src)
    assert "MyInt" in _names(result)
    my_int = _by_name(result, "MyInt")
    assert my_int.kind == "type"
    assert my_int.parent is None


def test_typedef_named_struct(extract: Extractor) -> None:
    """``typedef struct Node Node;`` records the typedef name on its own line."""

    src = "typedef struct Node Node;\n"
    result = extract(src)
    assert "Node" in _names(result)
    assert _by_name(result, "Node").kind == "type"


# ---------------------------------------------------------------------------
# Empty / structural files
# ---------------------------------------------------------------------------


def test_comment_only_file(extract: Extractor) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract(src)
    assert result == []


def test_include_only_file(extract: Extractor) -> None:
    src = "#include <stdio.h>\n#include <stdlib.h>\n"
    result = extract(src)
    assert result == []


# ---------------------------------------------------------------------------
# Top-level only
# ---------------------------------------------------------------------------


def test_nested_struct_inside_function_excluded(extract: Extractor) -> None:
    """A struct declared inside a function body must not surface as top-level.

    Conventional C formatting indents nested declarations, which keeps them out
    of the regex backend; the tree-sitter backend walks only direct children of
    ``translation_unit`` so it filters them by structure.
    """

    src = (
        "void outer(void) {\n"
        "    struct Inner {\n"
        "        int v;\n"
        "    };\n"
        "}\n"
    )
    result = extract(src)
    names = _names(result)
    assert "outer" in names
    assert "Inner" not in names


# ---------------------------------------------------------------------------
# Mixed file: every C kind together, in file order
# ---------------------------------------------------------------------------


def test_mixed_declarations(extract: Extractor) -> None:
    src = (
        "typedef int Handle;\n"
        "\n"
        "struct Server {\n"
        "    int fd;\n"
        "};\n"
        "\n"
        "enum State {\n"
        "    IDLE,\n"
        "    BUSY\n"
        "};\n"
        "\n"
        "int serve(struct Server *s) {\n"
        "    return s->fd;\n"
        "}\n"
    )
    result = extract(src)
    names = _names(result)
    assert "Handle" in names
    assert "Server" in names
    assert "State" in names
    assert "serve" in names
    assert _by_name(result, "Handle").kind == "type"
    assert _by_name(result, "Server").kind == "type"
    assert _by_name(result, "State").kind == "enum"
    assert _by_name(result, "serve").kind == "function"
    # Every C symbol is free-standing: no parent edges in the language.
    assert all(s.parent is None for s in result)


# ---------------------------------------------------------------------------
# Regex backend direct: pin documented behaviour independent of parametrisation
# ---------------------------------------------------------------------------


def test_regex_backend_directly_covers_all_kinds() -> None:
    """The regex fallback runs unconditionally (no native wheel required)."""

    src = (
        "typedef unsigned long ulong;\n"
        "struct Box { int w; };\n"
        "union Tag { int a; };\n"
        "enum Mode { ON, OFF };\n"
        "void tick(void) {}\n"
    )
    result = c_regex.extract(src)
    names = _names(result)
    assert "ulong" in names
    assert "Box" in names
    assert "Tag" in names
    assert "Mode" in names
    assert "tick" in names
    assert _by_name(result, "ulong").kind == "type"
    assert _by_name(result, "Box").kind == "type"
    assert _by_name(result, "Tag").kind == "type"
    assert _by_name(result, "Mode").kind == "enum"
    assert _by_name(result, "tick").kind == "function"
    assert all(s.parent is None for s in result)
