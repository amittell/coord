"""Tests for the Scala symbol extractor.

Both backends expose a module-level ``extract(content) -> list[Symbol]``. The
tests call those backends DIRECTLY rather than routing through
``extract_symbols`` so they stand on their own regardless of when the
dispatcher registry gains the ``.scala`` / ``.sc`` extensions (that wiring is
owned by a separate integrator). A single dispatcher routing test is included
and skips cleanly while the extension is not yet registered.

Top-level declarations are exercised against BOTH backends (tree-sitter and
regex) via a parametrised ``extract`` fixture. The tree-sitter case skips
automatically when the ``tree_sitter_scala`` package is unavailable so the
suite still runs cleanly on machines without the native wheels. The regex
backend is always exercised.

Method-level and nested-container behaviour (the ``Class::method`` parent
edges) is only expressible by the tree-sitter backend -- conventional Scala
formatting indents container members, which the column-zero regex backend
deliberately skips. Those cases live in dedicated tests guarded by
``pytest.importorskip("tree_sitter_scala")`` so the regex-only environment
still passes.

Test fixtures are inline Scala source strings.
"""

from __future__ import annotations

import importlib

import pytest

from coordination.symbols import Symbol, extract_symbols, supported_extensions
from coordination.symbols import scala_regex, scala_treesitter

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_scala")
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
                reason="tree-sitter-scala not installed",
            ),
        ),
        "regex",
    ]
)
def extract(request: pytest.FixtureRequest):
    """Return the ``extract`` callable for the parametrised backend."""

    if request.param == "treesitter":
        return scala_treesitter.extract
    return scala_regex.extract


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
# Functions (top-level defs) -- both backends
# ---------------------------------------------------------------------------


def test_simple_def(extract) -> None:
    src = "def helloWorld(): Unit = {\n  ()\n}\n"
    result = extract(src)
    assert _names(result) == ["helloWorld"]
    assert _by_name(result, "helloWorld").kind == "function"


def test_generic_def(extract) -> None:
    src = "def identity[T](x: T): T = x\n"
    result = extract(src)
    assert "identity" in _names(result)
    assert _by_name(result, "identity").kind == "function"


def test_def_with_modifiers(extract) -> None:
    """Leading modifiers must not hide the def name."""

    src = "private final def secret(): Int = 1\n"
    result = extract(src)
    assert "secret" in _names(result)
    assert _by_name(result, "secret").kind == "function"


# ---------------------------------------------------------------------------
# Classes / objects / traits -- both backends
# ---------------------------------------------------------------------------


def test_class_definition(extract) -> None:
    src = "class Foo {\n  val x = 1\n}\n"
    result = extract(src)
    assert "Foo" in _names(result)
    assert _by_name(result, "Foo").kind == "class"


def test_case_class_is_class(extract) -> None:
    src = "case class Point(x: Int, y: Int)\n"
    result = extract(src)
    assert "Point" in _names(result)
    assert _by_name(result, "Point").kind == "class"


def test_object_is_class(extract) -> None:
    src = "object Singleton {\n  val n = 0\n}\n"
    result = extract(src)
    assert "Singleton" in _names(result)
    assert _by_name(result, "Singleton").kind == "class"


def test_trait_is_interface(extract) -> None:
    src = "trait Greeter {\n  def greet(): String\n}\n"
    result = extract(src)
    assert "Greeter" in _names(result)
    assert _by_name(result, "Greeter").kind == "interface"


def test_generic_class_name_excludes_type_params(extract) -> None:
    src = "class Container[T] {\n  def empty(): Boolean = true\n}\n"
    result = extract(src)
    assert "Container" in _names(result)
    assert _by_name(result, "Container").kind == "class"


# ---------------------------------------------------------------------------
# Function-binding val / var -- both backends
# ---------------------------------------------------------------------------


def test_val_bound_to_lambda(extract) -> None:
    """A val bound to a function literal is a claimable callable const."""

    src = "val handler = (x: Int) => x + 1\n"
    result = extract(src)
    assert "handler" in _names(result)
    assert _by_name(result, "handler").kind == "const"


def test_plain_data_val_excluded(extract) -> None:
    """A plain-data val is not callable, so it is not emitted."""

    src = "val answer = 42\n"
    result = extract(src)
    assert "answer" not in _names(result)


# ---------------------------------------------------------------------------
# Empty / structural files -- both backends
# ---------------------------------------------------------------------------


def test_comment_only_file(extract) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract(src)
    assert result == []


def test_imports_only_file(extract) -> None:
    src = (
        "package example\n"
        "\n"
        "import scala.collection.mutable\n"
        "import java.util.List\n"
    )
    result = extract(src)
    assert result == []


def test_package_declaration_only(extract) -> None:
    src = "package example\n"
    result = extract(src)
    assert result == []


# ---------------------------------------------------------------------------
# Methods + nesting (parent edges) -- tree-sitter only
# ---------------------------------------------------------------------------


def test_method_parent_on_class() -> None:
    """A method inside a class carries ``parent='ClassName'``."""

    pytest.importorskip("tree_sitter_scala")
    src = (
        "class Server {\n"
        "  def start(): Unit = ()\n"
        "  def stop(): Unit = ()\n"
        "}\n"
    )
    result = scala_treesitter.extract(src)
    names = _names(result)
    assert "Server" in names
    assert "start" in names
    assert "stop" in names
    start = _by_name(result, "start")
    assert start.kind == "function"
    assert start.parent == "Server"
    assert _by_name(result, "stop").parent == "Server"


def test_method_parent_on_object() -> None:
    """A method inside an object carries the object name as ``parent``."""

    pytest.importorskip("tree_sitter_scala")
    src = "object Registry {\n  def register(): Unit = ()\n}\n"
    result = scala_treesitter.extract(src)
    register = _by_name(result, "register")
    assert register.kind == "function"
    assert register.parent == "Registry"


def test_abstract_method_in_trait() -> None:
    """An abstract def in a trait surfaces as a function with the trait parent."""

    pytest.importorskip("tree_sitter_scala")
    src = "trait Greeter {\n  def greet(name: String): String\n}\n"
    result = scala_treesitter.extract(src)
    greeter = _by_name(result, "Greeter")
    assert greeter.kind == "interface"
    greet = _by_name(result, "greet")
    assert greet.kind == "function"
    assert greet.parent == "Greeter"


def test_nested_container_parent_path() -> None:
    """A method on a nested container carries the full ``Outer::Inner`` path."""

    pytest.importorskip("tree_sitter_scala")
    src = (
        "object Outer {\n"
        "  class Inner {\n"
        "    def deep(): Int = 1\n"
        "  }\n"
        "}\n"
    )
    result = scala_treesitter.extract(src)
    inner = _by_name(result, "Inner")
    assert inner.kind == "class"
    assert inner.parent == "Outer"
    deep = _by_name(result, "deep")
    assert deep.kind == "function"
    assert deep.parent == "Outer::Inner"


def test_lambda_val_method_parent() -> None:
    """A lambda-bound val inside a class is a const method with a parent."""

    pytest.importorskip("tree_sitter_scala")
    src = (
        "class Handlers {\n"
        "  val onClick = (e: Int) => e * 2\n"
        "}\n"
    )
    result = scala_treesitter.extract(src)
    on_click = _by_name(result, "onClick")
    assert on_click.kind == "const"
    assert on_click.parent == "Handlers"


def test_methods_on_distinct_containers_independent() -> None:
    """Sibling containers must not bleed parent state into one another."""

    pytest.importorskip("tree_sitter_scala")
    src = (
        "class Alpha {\n"
        "  def one(): Unit = ()\n"
        "}\n"
        "class Beta {\n"
        "  def two(): Unit = ()\n"
        "}\n"
    )
    result = scala_treesitter.extract(src)
    assert _by_name(result, "one").parent == "Alpha"
    assert _by_name(result, "two").parent == "Beta"


def test_top_level_def_has_no_parent() -> None:
    """A plain top-level def keeps ``parent=None``."""

    pytest.importorskip("tree_sitter_scala")
    src = "def freeStanding(): Unit = ()\n"
    result = scala_treesitter.extract(src)
    free = _by_name(result, "freeStanding")
    assert free.kind == "function"
    assert free.parent is None


# ---------------------------------------------------------------------------
# Dispatcher path
# ---------------------------------------------------------------------------


def test_dispatcher_routes_scala_extension() -> None:
    """``.scala`` is registered in the dispatcher (v0.33); the extension must
    stay in ``supported_extensions()`` and route to this backend. Asserted
    unconditionally so a dropped registration turns the suite red rather
    than silently skipping."""

    assert ".scala" in supported_extensions(), (
        ".scala must stay registered in the dispatcher extension map"
    )
    src = "object Main {\n  def run(): Unit = ()\n}\n"
    result = extract_symbols("foo.scala", src)
    assert result, "expected non-empty result for a simple Scala object"
    assert "Main" in _names(result)
