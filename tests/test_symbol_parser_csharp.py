"""Tests for the C# symbol extractor.

Top-level type declarations (class / interface / struct / enum / record) are
exercised against BOTH backends via a parametrised ``backend`` fixture. The
tree-sitter case skips automatically when the ``tree_sitter_c_sharp`` package is
unavailable so the suite still runs cleanly on machines without the native
wheels.

Member-level coverage (methods and properties with ``parent`` set, namespace
descent, nesting) is tree-sitter only: C# members are conventionally indented
inside their type body, so the column-zero-anchored regex backend cannot reach
them. Those tests guard with ``pytest.importorskip('tree_sitter_c_sharp')`` and
force the tree-sitter backend explicitly.

Test fixtures are inline C# source strings; we deliberately avoid touching the
filesystem so the cache key (``file_path``, content hash) is stable and the
tests stay fast.
"""

from __future__ import annotations

import importlib

import pytest

from coordination import symbols
from coordination.symbols import Symbol, csharp_regex, extract_symbols

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_c_sharp")
except ImportError:  # pragma: no cover - depends on install state
    _TREESITTER_AVAILABLE = False

# The dispatcher registration for ``.cs`` is wired separately (in
# ``__init__.py``). These tests call each backend's ``extract`` directly via
# :func:`_extract` so they stand alone regardless of registration state; a
# dedicated test below covers the dispatcher path and skips until ``.cs`` is
# registered.
_CS_REGISTERED = ".cs" in symbols.supported_extensions()


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
                reason="tree-sitter-c-sharp not installed",
            ),
        ),
        "regex",
    ]
)
def backend(request: pytest.FixtureRequest):
    """Yield an ``extract`` callable for the parametrised backend.

    The regex backend is exercised by importing its module ``extract`` directly
    so it always runs. The tree-sitter backend is loaded lazily and skipped when
    the wheel is absent, satisfying the same guard
    ``pytest.importorskip('tree_sitter_c_sharp')`` expresses.
    """

    if request.param == "treesitter":
        pytest.importorskip("tree_sitter_c_sharp")
        from coordination.symbols import csharp_treesitter

        return csharp_treesitter.extract
    return csharp_regex.extract


@pytest.fixture
def treesitter():
    """Yield the tree-sitter ``extract`` callable, skipping when wheel absent."""

    pytest.importorskip("tree_sitter_c_sharp")
    from coordination.symbols import csharp_treesitter

    return csharp_treesitter.extract


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
# Top-level type declarations (both backends)
# ---------------------------------------------------------------------------


def test_simple_class(backend) -> None:
    src = "class Widget\n{\n}\n"
    result = backend(src)
    assert "Widget" in _names(result)
    assert _by_name(result, "Widget").kind == "class"


def test_class_with_modifiers(backend) -> None:
    """Access and inheritance modifiers must not block the match or the name."""

    src = "public sealed partial class Service\n{\n}\n"
    result = backend(src)
    assert "Service" in _names(result)
    assert _by_name(result, "Service").kind == "class"


def test_interface(backend) -> None:
    src = "public interface IHandler\n{\n}\n"
    result = backend(src)
    assert "IHandler" in _names(result)
    assert _by_name(result, "IHandler").kind == "interface"


def test_struct_maps_to_type(backend) -> None:
    """A struct has no dedicated Symbol kind and maps to ``'type'``."""

    src = "public struct Point\n{\n}\n"
    result = backend(src)
    assert "Point" in _names(result)
    assert _by_name(result, "Point").kind == "type"


def test_enum(backend) -> None:
    src = "public enum Color\n{\n    Red,\n    Green,\n}\n"
    result = backend(src)
    assert "Color" in _names(result)
    assert _by_name(result, "Color").kind == "enum"


def test_record_maps_to_class(backend) -> None:
    """A positional record coordinates at class grain."""

    src = "public record Money(decimal Amount, string Currency);\n"
    result = backend(src)
    assert "Money" in _names(result)
    assert _by_name(result, "Money").kind == "class"


def test_record_class_qualifier(backend) -> None:
    """``record class Foo`` resolves to the bare name with kind ``class``."""

    src = "public record class Account\n{\n}\n"
    result = backend(src)
    assert "Account" in _names(result)
    assert _by_name(result, "Account").kind == "class"


def test_generic_class_name_excludes_type_params(backend) -> None:
    """``class Container<T>`` yields the bare name ``Container``."""

    src = "public class Container<T>\n{\n}\n"
    result = backend(src)
    assert "Container" in _names(result)
    assert _by_name(result, "Container").kind == "class"


def test_file_scoped_namespace_types_surface(backend) -> None:
    """A file-scoped namespace leaves its types at column zero for both backends."""

    src = (
        "namespace MyApp;\n"
        "\n"
        "public class Top\n"
        "{\n"
        "}\n"
    )
    result = backend(src)
    names = _names(result)
    assert "Top" in names
    # The namespace itself is never a claimable symbol.
    assert "MyApp" not in names


# ---------------------------------------------------------------------------
# Empty / structural files (both backends)
# ---------------------------------------------------------------------------


def test_comment_only_file(backend) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = backend(src)
    assert result == []


def test_using_directives_only(backend) -> None:
    src = "using System;\nusing System.Collections.Generic;\n"
    result = backend(src)
    assert result == []


# ---------------------------------------------------------------------------
# Dispatcher path
# ---------------------------------------------------------------------------


def test_dispatcher_routes_cs_extension() -> None:
    """A ``.cs`` file path must dispatch to the C# backend and return symbols.

    Skipped until the dispatcher registration for ``.cs`` is wired in
    ``__init__.py`` (done by a separate integrator). Once registered this pins
    that ``extract_symbols`` routes ``.cs`` content to a C# backend.
    """

    if not _CS_REGISTERED:
        pytest.skip(".cs not yet registered in the dispatcher")
    symbols._CACHE.clear()
    src = "public class Dispatched\n{\n}\n"
    result = extract_symbols("foo.cs", src)
    assert result, "expected non-empty result for a simple C# class"
    assert "Dispatched" in _names(result)


# ---------------------------------------------------------------------------
# Members and nesting (tree-sitter only)
# ---------------------------------------------------------------------------


def test_method_has_parent(treesitter) -> None:
    """A method carries ``kind='function'`` and ``parent`` set to its type."""

    src = (
        "public class Service\n"
        "{\n"
        "    public int Compute(int x)\n"
        "    {\n"
        "        return x + 1;\n"
        "    }\n"
        "}\n"
    )
    result = treesitter(src)
    compute = _by_name(result, "Compute")
    assert compute.kind == "function"
    assert compute.parent == "Service"


def test_property_has_parent(treesitter) -> None:
    """A property is a member-level claimable unit with ``parent`` set."""

    src = (
        "public class Person\n"
        "{\n"
        "    public string Name { get; set; }\n"
        "}\n"
    )
    result = treesitter(src)
    name = _by_name(result, "Name")
    assert name.kind == "function"
    assert name.parent == "Person"


def test_sibling_methods_distinct_same_parent(treesitter) -> None:
    """Two methods on the same type both parent to that type, independently."""

    src = (
        "public class Calc\n"
        "{\n"
        "    public int Add(int a, int b) { return a + b; }\n"
        "    public int Sub(int a, int b) { return a - b; }\n"
        "}\n"
    )
    result = treesitter(src)
    add = _by_name(result, "Add")
    sub = _by_name(result, "Sub")
    assert add.parent == "Calc"
    assert sub.parent == "Calc"
    assert add.kind == "function"
    assert sub.kind == "function"


def test_interface_method_has_parent(treesitter) -> None:
    """Interface method declarations parent to the interface name."""

    src = (
        "public interface IRepository\n"
        "{\n"
        "    void Save(string id);\n"
        "}\n"
    )
    result = treesitter(src)
    repo = _by_name(result, "IRepository")
    assert repo.kind == "interface"
    save = _by_name(result, "Save")
    assert save.kind == "function"
    assert save.parent == "IRepository"


def test_block_namespace_descent(treesitter) -> None:
    """Types inside a braced namespace are reached; the namespace is not emitted."""

    src = (
        "namespace MyApp.Domain\n"
        "{\n"
        "    public class Order\n"
        "    {\n"
        "        public void Place() { }\n"
        "    }\n"
        "}\n"
    )
    result = treesitter(src)
    names = _names(result)
    assert "Order" in names
    assert "MyApp.Domain" not in names
    assert "MyApp" not in names
    order = _by_name(result, "Order")
    assert order.kind == "class"
    place = _by_name(result, "Place")
    assert place.parent == "Order"


def test_nested_type_members_parent_to_nested(treesitter) -> None:
    """A nested type parents to its enclosing type and its members to the full path."""

    src = (
        "public class Outer\n"
        "{\n"
        "    public void OuterMethod() { }\n"
        "\n"
        "    public class Inner\n"
        "    {\n"
        "        public void InnerMethod() { }\n"
        "    }\n"
        "}\n"
    )
    result = treesitter(src)
    names = _names(result)
    assert "Outer" in names
    assert "Inner" in names
    inner = _by_name(result, "Inner")
    assert inner.kind == "class"
    assert inner.parent == "Outer"
    outer_method = _by_name(result, "OuterMethod")
    inner_method = _by_name(result, "InnerMethod")
    assert outer_method.parent == "Outer"
    assert inner_method.parent == "Outer::Inner"


def test_top_level_type_has_no_parent(treesitter) -> None:
    """A type emits with ``parent=None`` regardless of an enclosing namespace."""

    src = (
        "namespace MyApp\n"
        "{\n"
        "    public class Standalone\n"
        "    {\n"
        "    }\n"
        "}\n"
    )
    result = treesitter(src)
    standalone = _by_name(result, "Standalone")
    assert standalone.kind == "class"
    assert standalone.parent is None


def test_enum_inside_namespace(treesitter) -> None:
    """An enum nested in a braced namespace still surfaces with kind ``enum``."""

    src = (
        "namespace MyApp\n"
        "{\n"
        "    public enum Status\n"
        "    {\n"
        "        Active,\n"
        "        Inactive,\n"
        "    }\n"
        "}\n"
    )
    result = treesitter(src)
    status = _by_name(result, "Status")
    assert status.kind == "enum"
    assert status.parent is None
