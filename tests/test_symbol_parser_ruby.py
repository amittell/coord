"""Tests for the Ruby symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``extract`` fixture. The tree-sitter case skips automatically when the
``tree_sitter_ruby`` package is unavailable so the suite still runs cleanly
on machines without the native wheels; the regex backend is always tested.

The dispatcher does not yet route the ``.rb`` extension (a separate integrator
wires the registry), so these tests call each backend module's ``extract``
function directly rather than going through ``extract_symbols``. Test fixtures
are inline Ruby source strings; we deliberately avoid touching the filesystem
so the tests stay fast and self-contained.
"""

from __future__ import annotations

import importlib
from typing import Callable

import pytest

from coordination.symbols import Symbol, ruby_regex

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_ruby")
except ImportError:  # pragma: no cover - depends on install state
    _TREESITTER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param(
            "treesitter",
            marks=pytest.mark.skipif(
                not _TREESITTER_AVAILABLE,
                reason="tree-sitter-ruby not installed",
            ),
        ),
        "regex",
    ]
)
def extract(request: pytest.FixtureRequest) -> Callable[[str], list[Symbol]]:
    """Return the ``extract`` callable for the parametrised backend.

    The tree-sitter parameter is guarded so CI without the wheel still passes;
    the regex parameter always runs. ``importorskip`` doubly ensures the
    tree-sitter module is importable before its ``extract`` is handed back.
    """

    if request.param == "treesitter":
        pytest.importorskip("tree_sitter_ruby")
        module = importlib.import_module(
            "coordination.symbols.ruby_treesitter"
        )
        return module.extract
    return ruby_regex.extract


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
# Top-level methods
# ---------------------------------------------------------------------------


def test_simple_method(extract: Callable[[str], list[Symbol]]) -> None:
    src = "def hello_world\n  return\nend\n"
    result = extract(src)
    assert _names(result) == ["hello_world"]
    hello = _by_name(result, "hello_world")
    assert hello.kind == "function"
    assert hello.parent is None


def test_predicate_method_name(extract: Callable[[str], list[Symbol]]) -> None:
    """Ruby predicate methods keep the ``?`` suffix in the name."""

    src = "def valid?\n  true\nend\n"
    result = extract(src)
    assert "valid?" in _names(result)
    assert _by_name(result, "valid?").kind == "function"


def test_bang_method_name(extract: Callable[[str], list[Symbol]]) -> None:
    """Ruby bang methods keep the ``!`` suffix in the name."""

    src = "def save!\n  persist\nend\n"
    result = extract(src)
    assert "save!" in _names(result)
    assert _by_name(result, "save!").kind == "function"


def test_top_level_singleton_method(
    extract: Callable[[str], list[Symbol]]
) -> None:
    """``def self.x`` at top level surfaces as a function with the bare name."""

    src = "def self.build\n  new\nend\n"
    result = extract(src)
    assert "build" in _names(result)
    build = _by_name(result, "build")
    assert build.kind == "function"
    assert build.parent is None


# ---------------------------------------------------------------------------
# Classes and modules
# ---------------------------------------------------------------------------


def test_class_declaration(extract: Callable[[str], list[Symbol]]) -> None:
    src = "class Foo\nend\n"
    result = extract(src)
    assert "Foo" in _names(result)
    foo = _by_name(result, "Foo")
    assert foo.kind == "class"
    assert foo.parent is None


def test_module_declaration(extract: Callable[[str], list[Symbol]]) -> None:
    """A module maps to ``kind='class'`` (no separate module kind)."""

    src = "module Bar\nend\n"
    result = extract(src)
    assert "Bar" in _names(result)
    bar = _by_name(result, "Bar")
    assert bar.kind == "class"
    assert bar.parent is None


def test_class_with_superclass(extract: Callable[[str], list[Symbol]]) -> None:
    """A superclass clause does not pollute the captured class name."""

    src = "class Child < Parent\n  def go\n  end\nend\n"
    result = extract(src)
    assert "Child" in _names(result)
    assert _by_name(result, "Child").kind == "class"
    go = _by_name(result, "go")
    assert go.parent == "Child"


# ---------------------------------------------------------------------------
# Methods inside namespaces (parent must be set)
# ---------------------------------------------------------------------------


def test_method_inside_class_has_parent(
    extract: Callable[[str], list[Symbol]]
) -> None:
    src = (
        "class Account\n"
        "  def deposit(amount)\n"
        "    @balance += amount\n"
        "  end\n"
        "end\n"
    )
    result = extract(src)
    assert "Account" in _names(result)
    deposit = _by_name(result, "deposit")
    assert deposit.kind == "function"
    assert deposit.parent == "Account"


def test_method_inside_module_has_parent(
    extract: Callable[[str], list[Symbol]]
) -> None:
    src = (
        "module Helpers\n"
        "  def format(x)\n"
        "    x.to_s\n"
        "  end\n"
        "end\n"
    )
    result = extract(src)
    assert "Helpers" in _names(result)
    fmt = _by_name(result, "format")
    assert fmt.kind == "function"
    assert fmt.parent == "Helpers"


def test_singleton_method_inside_class_has_parent(
    extract: Callable[[str], list[Symbol]]
) -> None:
    """``def self.x`` inside a class is a function parented to the class."""

    src = (
        "class Widget\n"
        "  def self.create\n"
        "    new\n"
        "  end\n"
        "end\n"
    )
    result = extract(src)
    create = _by_name(result, "create")
    assert create.kind == "function"
    assert create.parent == "Widget"


def test_multiple_methods_in_class(
    extract: Callable[[str], list[Symbol]]
) -> None:
    src = (
        "class Calc\n"
        "  def add(a, b)\n"
        "    a + b\n"
        "  end\n"
        "\n"
        "  def sub(a, b)\n"
        "    a - b\n"
        "  end\n"
        "end\n"
    )
    result = extract(src)
    add = _by_name(result, "add")
    sub = _by_name(result, "sub")
    assert add.parent == "Calc"
    assert sub.parent == "Calc"
    assert add.kind == "function"
    assert sub.kind == "function"


def test_methods_on_different_classes_distinct_parents(
    extract: Callable[[str], list[Symbol]]
) -> None:
    """Receiver scope must not bleed from one class into the next."""

    src = (
        "class Server\n"
        "  def start\n"
        "  end\n"
        "end\n"
        "\n"
        "class Client\n"
        "  def send\n"
        "  end\n"
        "end\n"
    )
    result = extract(src)
    start = _by_name(result, "start")
    send = _by_name(result, "send")
    assert start.parent == "Server"
    assert send.parent == "Client"


# ---------------------------------------------------------------------------
# Nested namespaces (full ancestor path)
# ---------------------------------------------------------------------------


def test_nested_module_class_method_path(
    extract: Callable[[str], list[Symbol]]
) -> None:
    """A method on ``Outer::Inner`` carries the full ancestor path in parent."""

    src = (
        "module Outer\n"
        "  class Inner\n"
        "    def work\n"
        "    end\n"
        "  end\n"
        "end\n"
    )
    result = extract(src)
    names = _names(result)
    assert "Outer" in names
    assert "Inner" in names
    inner = _by_name(result, "Inner")
    assert inner.kind == "class"
    assert inner.parent == "Outer"
    work = _by_name(result, "work")
    assert work.kind == "function"
    assert work.parent == "Outer::Inner"


def test_nested_def_inside_method_excluded(
    extract: Callable[[str], list[Symbol]]
) -> None:
    """A def defined inside a method body must not surface as a member.

    Conventional Ruby indents the inner def deeper than the body indent, which
    keeps it out of the regex backend; the tree-sitter backend walks only the
    direct members of the namespace body so it filters by structure.
    """

    src = (
        "class Outer\n"
        "  def outer_method\n"
        "    def inner_method\n"
        "    end\n"
        "  end\n"
        "end\n"
    )
    result = extract(src)
    names = _names(result)
    assert "outer_method" in names
    assert "inner_method" not in names
    assert _by_name(result, "outer_method").parent == "Outer"


# ---------------------------------------------------------------------------
# Empty / structural files
# ---------------------------------------------------------------------------


def test_comment_only_file(extract: Callable[[str], list[Symbol]]) -> None:
    src = "# just a comment\n# another comment\n"
    result = extract(src)
    assert result == []


def test_empty_file(extract: Callable[[str], list[Symbol]]) -> None:
    result = extract("")
    assert result == []


def test_require_only_file(extract: Callable[[str], list[Symbol]]) -> None:
    src = "require 'json'\nrequire_relative 'foo'\n"
    result = extract(src)
    assert result == []
