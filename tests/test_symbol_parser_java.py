"""Tests for the Java symbol extractor.

Both backends (tree-sitter and regex) are exercised. The ``backend`` fixture
parametrises over both for the declarations they agree on -- the column-zero
top-level types (class / interface / enum / record). The tree-sitter case
skips automatically when the ``tree_sitter_java`` package is unavailable so the
suite still runs cleanly on machines without the native wheels.

Methods, constructors and parent edges live indented inside type bodies and are
only recovered by the tree-sitter backend; those tests are guarded with
``pytest.importorskip("tree_sitter_java")``. A dedicated regex test pins the
documented false-negative that the regex backend emits no methods.

Test fixtures are inline Java source strings; we deliberately avoid touching
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
    importlib.import_module("tree_sitter_java")
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
                reason="tree-sitter-java not installed",
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
# Top-level types (both backends)
# ---------------------------------------------------------------------------


def test_class_declaration(backend: str) -> None:
    src = (
        "package demo;\n"
        "\n"
        "public class Greeter {\n"
        "    void hello() {}\n"
        "}\n"
    )
    result = extract_symbols("Greeter.java", src)
    assert "Greeter" in _names(result)
    assert _by_name(result, "Greeter").kind == "class"


def test_interface_declaration(backend: str) -> None:
    src = (
        "package demo;\n"
        "\n"
        "public interface Service {\n"
        "    void run();\n"
        "}\n"
    )
    result = extract_symbols("Service.java", src)
    assert "Service" in _names(result)
    assert _by_name(result, "Service").kind == "interface"


def test_enum_declaration(backend: str) -> None:
    src = (
        "package demo;\n"
        "\n"
        "public enum Color {\n"
        "    RED, GREEN, BLUE;\n"
        "}\n"
    )
    result = extract_symbols("Color.java", src)
    assert "Color" in _names(result)
    assert _by_name(result, "Color").kind == "enum"


def test_record_declaration(backend: str) -> None:
    """Records map to ``kind='class'`` -- the Symbol vocabulary has no record."""

    src = "package demo;\n\npublic record Point(int x, int y) {}\n"
    result = extract_symbols("Point.java", src)
    assert "Point" in _names(result)
    assert _by_name(result, "Point").kind == "class"


def test_unmodified_class(backend: str) -> None:
    """A package-private class (no leading modifier) still surfaces."""

    src = "class Bare {}\n"
    result = extract_symbols("Bare.java", src)
    assert "Bare" in _names(result)
    assert _by_name(result, "Bare").kind == "class"


def test_final_abstract_modifiers(backend: str) -> None:
    """Stacked modifiers before the keyword are consumed."""

    src = (
        "public abstract class Base {}\n"
        "\n"
        "public final class Leaf {}\n"
    )
    result = extract_symbols("Types.java", src)
    names = _names(result)
    assert "Base" in names
    assert "Leaf" in names
    assert _by_name(result, "Base").kind == "class"
    assert _by_name(result, "Leaf").kind == "class"


def test_multiple_top_level_types_in_order(backend: str) -> None:
    """Several column-zero types surface in file order."""

    src = (
        "package demo;\n"
        "\n"
        "class Alpha {}\n"
        "\n"
        "interface Beta {}\n"
        "\n"
        "enum Gamma { A, B }\n"
    )
    result = extract_symbols("Demo.java", src)
    names = _names(result)
    assert names.index("Alpha") < names.index("Beta") < names.index("Gamma")
    assert _by_name(result, "Beta").kind == "interface"
    assert _by_name(result, "Gamma").kind == "enum"


# ---------------------------------------------------------------------------
# Empty / structural files (both backends)
# ---------------------------------------------------------------------------


def test_comment_only_file(backend: str) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract_symbols("Empty.java", src)
    assert result == []


def test_package_and_imports_only(backend: str) -> None:
    src = (
        "package demo;\n"
        "\n"
        "import java.util.List;\n"
        "import java.util.Map;\n"
    )
    result = extract_symbols("Imports.java", src)
    assert result == []


# ---------------------------------------------------------------------------
# Dispatcher path (both backends)
# ---------------------------------------------------------------------------


def test_dispatcher_routes_java_extension(backend: str) -> None:
    """A ``.java`` file path must dispatch to the Java backend."""

    src = "public class Dispatched {}\n"
    result = extract_symbols("Dispatched.java", src)
    assert result, "expected non-empty result for a simple Java class"
    assert "Dispatched" in _names(result)


# ---------------------------------------------------------------------------
# Regex backend: documented false-negative for members
# ---------------------------------------------------------------------------


def test_regex_backend_emits_no_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regex backend captures only top-level types, never members.

    Methods live indented inside the body and so are skipped by the
    column-zero anchor. This pins the documented false-negative.
    """

    monkeypatch.setenv("COORD_SYMBOL_PARSER", "regex")
    symbols._CACHE.clear()
    src = (
        "public class Holder {\n"
        "    public void doWork() {}\n"
        "    public Holder() {}\n"
        "}\n"
    )
    result = extract_symbols("Holder.java", src)
    names = _names(result)
    assert "Holder" in names
    assert "doWork" not in names
    assert all(s.kind != "function" for s in result)


# ---------------------------------------------------------------------------
# tree-sitter backend: methods, constructors, parents, nesting
# ---------------------------------------------------------------------------


def test_method_carries_parent() -> None:
    """A method emits ``kind='function'`` with ``parent`` = enclosing class."""

    pytest.importorskip("tree_sitter_java")
    src = (
        "public class Server {\n"
        "    public void start() {}\n"
        "}\n"
    )
    result = extract_symbols("Server.java", src)
    start = _by_name(result, "start")
    assert start.kind == "function"
    assert start.parent == "Server"


def test_constructor_carries_parent() -> None:
    """A constructor surfaces as a function parented to its class."""

    pytest.importorskip("tree_sitter_java")
    src = (
        "public class Widget {\n"
        "    public Widget(int n) {}\n"
        "    public void render() {}\n"
        "}\n"
    )
    result = extract_symbols("Widget.java", src)
    _by_name(result, "Widget")  # raises if the constructor symbol is absent
    # The class type and the constructor share the simple name; both appear.
    kinds = {s.kind for s in result if s.name == "Widget"}
    assert kinds == {"class", "function"}
    render = _by_name(result, "render")
    assert render.kind == "function"
    assert render.parent == "Widget"
    # The constructor function is parented to the class.
    ctor_fn = next(
        s for s in result if s.name == "Widget" and s.kind == "function"
    )
    assert ctor_fn.parent == "Widget"


def test_interface_method_carries_parent() -> None:
    """Methods declared in an interface body carry the interface as parent."""

    pytest.importorskip("tree_sitter_java")
    src = (
        "public interface Repo {\n"
        "    void save();\n"
        "    Object load(int id);\n"
        "}\n"
    )
    result = extract_symbols("Repo.java", src)
    save = _by_name(result, "save")
    load = _by_name(result, "load")
    assert save.kind == "function"
    assert save.parent == "Repo"
    assert load.parent == "Repo"


def test_enum_method_carries_parent() -> None:
    """Methods after the enum constant list are recovered with the enum parent."""

    pytest.importorskip("tree_sitter_java")
    src = (
        "public enum Suit {\n"
        "    HEARTS, SPADES;\n"
        "\n"
        "    public boolean isRed() { return this == HEARTS; }\n"
        "}\n"
    )
    result = extract_symbols("Suit.java", src)
    assert _by_name(result, "Suit").kind == "enum"
    is_red = _by_name(result, "isRed")
    assert is_red.kind == "function"
    assert is_red.parent == "Suit"


def test_fields_are_not_emitted() -> None:
    """Field declarations are not claimable units and never surface."""

    pytest.importorskip("tree_sitter_java")
    src = (
        "public class Config {\n"
        "    private int count;\n"
        "    public String name;\n"
        "    public int get() { return count; }\n"
        "}\n"
    )
    result = extract_symbols("Config.java", src)
    names = _names(result)
    assert "count" not in names
    assert "name" not in names
    assert "get" in names


def test_nested_class_method_parents_to_nested_type() -> None:
    """A nested class emits its own Symbol and its method gets the dotted path.

    The nested type surfaces as its own type Symbol parented to the enclosing
    type (``parent='Outer'``), and its methods carry the full ``"::"``-joined
    path of all enclosing types so ``Outer::Inner::method`` resolves.
    """

    pytest.importorskip("tree_sitter_java")
    src = (
        "public class Outer {\n"
        "    public void outerMethod() {}\n"
        "\n"
        "    static class Inner {\n"
        "        public void innerMethod() {}\n"
        "    }\n"
        "}\n"
    )
    result = extract_symbols("Outer.java", src)
    assert _by_name(result, "Outer").kind == "class"
    assert _by_name(result, "Outer").parent is None
    # The nested type is emitted with the enclosing type as its parent.
    inner = _by_name(result, "Inner")
    assert inner.kind == "class"
    assert inner.parent == "Outer"
    outer_method = _by_name(result, "outerMethod")
    inner_method = _by_name(result, "innerMethod")
    assert outer_method.parent == "Outer"
    assert inner_method.parent == "Outer::Inner"
    assert inner_method.kind == "function"


def test_methods_on_sibling_classes_distinct() -> None:
    """Two top-level classes keep their methods parented independently."""

    pytest.importorskip("tree_sitter_java")
    src = (
        "class Alpha {\n"
        "    void one() {}\n"
        "}\n"
        "\n"
        "class Beta {\n"
        "    void two() {}\n"
        "}\n"
    )
    result = extract_symbols("Pair.java", src)
    assert _by_name(result, "one").parent == "Alpha"
    assert _by_name(result, "two").parent == "Beta"
    assert _by_name(result, "Alpha").kind == "class"
    assert _by_name(result, "Beta").kind == "class"


def test_line_numbers_are_one_indexed_inclusive() -> None:
    """``start_line`` / ``end_line`` are 1-indexed and span the declaration."""

    pytest.importorskip("tree_sitter_java")
    src = (
        "public class Box {\n"
        "    public void open() {\n"
        "        return;\n"
        "    }\n"
        "}\n"
    )
    result = extract_symbols("Box.java", src)
    box = _by_name(result, "Box")
    assert box.start_line == 1
    assert box.end_line == 5
    open_m = _by_name(result, "open")
    assert open_m.start_line == 2
    assert open_m.end_line == 4
