"""Tests for the PHP symbol extractor.

Both backends (tree-sitter and regex) are exercised. The regex backend is
always tested directly; the tree-sitter backend is guarded with
``pytest.importorskip("tree_sitter_php")`` so the suite still runs cleanly on
machines without the native wheel.

The dispatcher in ``coordination.symbols`` does not register ``.php`` yet (a
separate integrator wires the registry, ``pyproject.toml`` and ``lsp.py``), so
these tests import the backend modules and call their ``extract`` functions
directly. Test fixtures are inline PHP source strings; we deliberately avoid
touching the filesystem so the tests stay fast and self-contained.

PHP differs from Go in where methods live: Go method declarations are
top-level (the receiver sits outside the type), but PHP methods are indented
members of a class body. The column-zero-anchored regex backend therefore
cannot see methods -- only the tree-sitter backend emits them with a populated
``parent``. The method/parent tests below are tree-sitter only for that
reason; the regex backend's blindness to methods is asserted explicitly as a
documented false-negative.
"""

from __future__ import annotations

import importlib

import pytest

from coordination.symbols import Symbol
from coordination.symbols import php_regex

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_php")
except ImportError:  # pragma: no cover - depends on install state
    _TREESITTER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _treesitter_extract(content: str) -> list[Symbol]:
    """Import the tree-sitter backend lazily and run it."""

    module = importlib.import_module("coordination.symbols.php_treesitter")
    return module.extract(content)


@pytest.fixture(
    params=[
        pytest.param(
            "treesitter",
            marks=pytest.mark.skipif(
                not _TREESITTER_AVAILABLE,
                reason="tree-sitter-php not installed",
            ),
        ),
        "regex",
    ]
)
def extract(request: pytest.FixtureRequest):
    """Return the ``extract`` callable for the parametrised backend."""

    if request.param == "treesitter":
        return _treesitter_extract
    return php_regex.extract


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


def test_simple_function(extract) -> None:
    src = "<?php\n\nfunction helloWorld() {\n    return;\n}\n"
    result = extract(src)
    assert "helloWorld" in _names(result)
    assert _by_name(result, "helloWorld").kind == "function"
    assert _by_name(result, "helloWorld").parent is None


def test_function_with_args(extract) -> None:
    src = "<?php\n\nfunction add($a, $b) {\n    return $a + $b;\n}\n"
    result = extract(src)
    assert "add" in _names(result)
    assert _by_name(result, "add").kind == "function"


def test_multiple_top_level_functions(extract) -> None:
    src = (
        "<?php\n"
        "\n"
        "function first() {}\n"
        "\n"
        "function second() {}\n"
    )
    result = extract(src)
    names = _names(result)
    assert "first" in names
    assert "second" in names


# ---------------------------------------------------------------------------
# Classes, interfaces, traits, enums
# ---------------------------------------------------------------------------


def test_class_declaration(extract) -> None:
    src = "<?php\n\nclass Widget {\n    public $name;\n}\n"
    result = extract(src)
    assert "Widget" in _names(result)
    assert _by_name(result, "Widget").kind == "class"


def test_abstract_class_declaration(extract) -> None:
    src = "<?php\n\nabstract class Base {\n}\n"
    result = extract(src)
    assert "Base" in _names(result)
    assert _by_name(result, "Base").kind == "class"


def test_final_class_declaration(extract) -> None:
    src = "<?php\n\nfinal class Sealed {\n}\n"
    result = extract(src)
    assert "Sealed" in _names(result)
    assert _by_name(result, "Sealed").kind == "class"


def test_interface_declaration(extract) -> None:
    src = "<?php\n\ninterface Drawable {\n    public function draw();\n}\n"
    result = extract(src)
    assert "Drawable" in _names(result)
    assert _by_name(result, "Drawable").kind == "interface"


def test_trait_declaration(extract) -> None:
    """A trait is class-like and reduces to ``kind='class'``."""

    src = "<?php\n\ntrait Loggable {\n    public function log() {}\n}\n"
    result = extract(src)
    assert "Loggable" in _names(result)
    assert _by_name(result, "Loggable").kind == "class"


def test_enum_declaration(extract) -> None:
    src = (
        "<?php\n"
        "\n"
        "enum Suit {\n"
        "    case Hearts;\n"
        "    case Spades;\n"
        "}\n"
    )
    result = extract(src)
    assert "Suit" in _names(result)
    assert _by_name(result, "Suit").kind == "enum"


def test_backed_enum_declaration(extract) -> None:
    """A backed enum (``enum Suit: string``) is still ``kind='enum'``."""

    src = (
        "<?php\n"
        "\n"
        "enum Status: string {\n"
        "    case Active = 'active';\n"
        "    case Closed = 'closed';\n"
        "}\n"
    )
    result = extract(src)
    assert "Status" in _names(result)
    assert _by_name(result, "Status").kind == "enum"


# ---------------------------------------------------------------------------
# Empty / structural files
# ---------------------------------------------------------------------------


def test_php_tag_only(extract) -> None:
    src = "<?php\n"
    result = extract(src)
    assert result == []


def test_comment_only_file(extract) -> None:
    src = "<?php\n// just a comment\n/* also a comment */\n"
    result = extract(src)
    assert result == []


def test_namespace_and_use_only(extract) -> None:
    src = (
        "<?php\n"
        "\n"
        "namespace App\\Models;\n"
        "\n"
        "use App\\Support\\Helper;\n"
    )
    result = extract(src)
    assert result == []


# ---------------------------------------------------------------------------
# Regex backend: methods are a documented false-negative
# ---------------------------------------------------------------------------


def test_regex_skips_indented_methods() -> None:
    """The regex backend captures the class but not its indented methods."""

    src = (
        "<?php\n"
        "\n"
        "class Service {\n"
        "    public function handle() {}\n"
        "    private function setup() {}\n"
        "}\n"
    )
    result = php_regex.extract(src)
    names = _names(result)
    assert "Service" in names
    assert "handle" not in names
    assert "setup" not in names


# ---------------------------------------------------------------------------
# Tree-sitter backend: methods carry their enclosing type as parent
# ---------------------------------------------------------------------------


def test_method_parent_in_class() -> None:
    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "class Service {\n"
        "    public function handle() {}\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    assert "Service" in _names(result)
    handle = _by_name(result, "handle")
    assert handle.kind == "function"
    assert handle.parent == "Service"


def test_sibling_methods_share_parent() -> None:
    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "class Service {\n"
        "    public function start() {}\n"
        "    public function stop() {}\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    start = _by_name(result, "start")
    stop = _by_name(result, "stop")
    assert start.parent == "Service"
    assert stop.parent == "Service"


def test_method_parent_in_interface() -> None:
    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "interface Drawable {\n"
        "    public function draw();\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    draw = _by_name(result, "draw")
    assert draw.kind == "function"
    assert draw.parent == "Drawable"


def test_method_parent_in_trait() -> None:
    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "trait Loggable {\n"
        "    public function log() {}\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    log = _by_name(result, "log")
    assert log.kind == "function"
    assert log.parent == "Loggable"


def test_method_parent_in_enum() -> None:
    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "enum Suit {\n"
        "    case Hearts;\n"
        "    public function label(): string {\n"
        "        return 'suit';\n"
        "    }\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    label = _by_name(result, "label")
    assert label.kind == "function"
    assert label.parent == "Suit"


def test_methods_on_different_classes_distinct() -> None:
    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "class Server {\n"
        "    public function handle() {}\n"
        "}\n"
        "\n"
        "class Client {\n"
        "    public function send() {}\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    handle = _by_name(result, "handle")
    send = _by_name(result, "send")
    assert handle.parent == "Server"
    assert send.parent == "Client"


def test_top_level_function_has_no_parent() -> None:
    pytest.importorskip("tree_sitter_php")
    src = "<?php\n\nfunction init() {}\n"
    result = _treesitter_extract(src)
    init = _by_name(result, "init")
    assert init.kind == "function"
    assert init.parent is None


def test_nested_class_in_method_excluded() -> None:
    """An anonymous/nested class inside a method body is not a top-level decl.

    The extractor walks only ``program`` children and one level of class body
    members, so a class created inside a method (via ``new class {}`` or a
    nested declaration) does not surface as an independent top-level symbol.
    """

    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "class Factory {\n"
        "    public function make() {\n"
        "        return new class {\n"
        "            public function inner() {}\n"
        "        };\n"
        "    }\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    names = _names(result)
    assert "Factory" in names
    assert "make" in names
    # The anonymous class and its method are nested below the method body and
    # must not appear as top-level symbols.
    assert "inner" not in names
    make = _by_name(result, "make")
    assert make.parent == "Factory"


def test_start_and_end_lines_span_class_body() -> None:
    """``end_line`` covers the whole class body in the tree-sitter backend."""

    pytest.importorskip("tree_sitter_php")
    src = (
        "<?php\n"
        "\n"
        "class Box {\n"
        "    public function open() {}\n"
        "}\n"
    )
    result = _treesitter_extract(src)
    box = _by_name(result, "Box")
    # ``class Box`` opens on line 3 and the closing brace is line 5.
    assert box.start_line == 3
    assert box.end_line == 5
