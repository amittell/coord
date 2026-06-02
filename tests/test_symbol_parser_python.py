"""Tests for the Python symbol extractor.

Both backends (tree-sitter and regex) are exercised via a parametrised
``backend`` fixture. The tree-sitter case skips automatically when the
``tree_sitter`` package or its Python grammar is unavailable, so the suite
still runs cleanly on machines without the native wheels.

Test fixtures are inline Python source strings; we deliberately avoid
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
    importlib.import_module("tree_sitter_python")
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
                reason="tree-sitter-python not installed",
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


def test_def_function(backend: str) -> None:
    src = "def hello_world():\n    return 1\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["hello_world"]
    assert _by_name(result, "hello_world").kind == "function"


def test_async_def_function(backend: str) -> None:
    src = "async def fetch_data():\n    return await load()\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["fetch_data"]
    # The dataclass has no async kind -- async defs collapse to 'function'.
    assert result[0].kind == "function"


def test_class_basic(backend: str) -> None:
    src = "class Container:\n    value = 0\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Container"]
    assert result[0].kind == "class"


def test_class_with_metaclass(backend: str) -> None:
    src = "class Container(metaclass=Meta):\n    pass\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Container"]
    assert result[0].kind == "class"


def test_class_with_generic_params(backend: str) -> None:
    """PEP 695 type-parameterised classes -- name excludes the param clause."""

    src = "class Container[T]:\n    value: T\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Container"]
    assert result[0].kind == "class"


def test_decorated_property(backend: str) -> None:
    """Decorators do not change the underlying definition's kind."""

    src = "@property\ndef foo(self):\n    return self._foo\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["foo"]
    assert result[0].kind == "function"


def test_decorated_staticmethod(backend: str) -> None:
    src = "@staticmethod\ndef helper():\n    return 1\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["helper"]
    assert result[0].kind == "function"


def test_decorator_stack(backend: str) -> None:
    """Multiple stacked decorators still resolve to the inner def's name."""

    src = "@cache\n@retry(3)\ndef stacked():\n    return load()\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["stacked"]
    assert result[0].kind == "function"


def test_top_level_lambda_assignment(backend: str) -> None:
    src = "handler = lambda x: x + 1\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["handler"]
    assert result[0].kind == "const"


def test_multi_line_signature(backend: str) -> None:
    src = "def long_signature(\n    a,\n    b,\n    c,\n):\n    return a + b + c\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["long_signature"]
    assert result[0].kind == "function"


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_nested_function_excluded(backend: str) -> None:
    """Functions defined inside another function must not be top-level."""

    src = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
    )
    result = extract_symbols("sample.py", src)
    assert "outer" in _names(result)
    assert "inner" not in _names(result)


def test_methods_emitted_with_parent(backend: str) -> None:
    """v0.16: methods inside a class are emitted with ``parent`` set.

    Pre-v0.16 this test asserted methods stayed nested and only the
    enclosing class surfaced. v0.16 introduced method-level symbols so
    ``__init__``, ``public``, and ``__repr__`` now each appear as a
    function with ``parent='Service'``. Dunders are treated like any
    other method -- there is no special-case exclusion.
    """

    src = (
        "class Service:\n"
        "    def __init__(self):\n"
        "        self.x = 0\n"
        "    def public(self):\n"
        "        return self.x\n"
        "    def __repr__(self):\n"
        "        return 'S'\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Service", "__init__", "public", "__repr__"]

    service = _by_name(result, "Service")
    assert service.kind == "class"
    assert service.parent is None

    for method_name in ("__init__", "public", "__repr__"):
        method = _by_name(result, method_name)
        assert method.kind == "function"
        assert method.parent == "Service"


def test_classmethod_emitted_with_parent(backend: str) -> None:
    """A classmethod surfaces as a method symbol with ``parent`` set.

    Pre-v0.16 the decorated ``@classmethod`` def stayed nested under the
    enclosing class. v0.16 emits it as a function with ``parent='Registry'``
    -- the decorator does not change the kind or the parent attribution.
    """

    src = (
        "class Registry:\n"
        "    @classmethod\n"
        "    def register(cls, item):\n"
        "        pass\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Registry", "register"]

    register = _by_name(result, "register")
    assert register.kind == "function"
    assert register.parent == "Registry"


def test_name_main_guard_excludes_inner_def(backend: str) -> None:
    """Defs guarded by ``if __name__ == '__main__':`` are indented and so
    are excluded from the top-level symbol list. This matches the
    tree-sitter walk (only direct module children) and the regex backend's
    column-zero anchor."""

    src = "if __name__ == '__main__':\n    def helper():\n        pass\n"
    result = extract_symbols("sample.py", src)
    assert "helper" not in _names(result)


def test_annotated_class_attribute_not_symbol(backend: str) -> None:
    """``value: int = 0`` is not a callable scope and is not captured.

    A lambda RHS would qualify (captured elsewhere); a literal RHS does not.
    """

    src = "value: int = 0\nother: str = 'x'\n"
    result = extract_symbols("sample.py", src)
    assert result == []


def test_comment_only_file(backend: str) -> None:
    src = "# just a comment\n# another comment\n"
    result = extract_symbols("sample.py", src)
    assert result == []


def test_imports_only_file(backend: str) -> None:
    src = (
        "import os\n"
        "import sys\n"
        "from typing import Any, Callable\n"
        "from collections import defaultdict\n"
    )
    result = extract_symbols("sample.py", src)
    assert result == []


# ---------------------------------------------------------------------------
# Ordering, line numbers, mixed declarations
# ---------------------------------------------------------------------------


def test_multiple_declarations_preserve_order(backend: str) -> None:
    src = (
        "def a():\n    pass\n"
        "class B:\n    pass\n"
        "async def c():\n    pass\n"
        "d = lambda x: x\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["a", "B", "c", "d"]


def test_start_line_is_one_indexed(backend: str) -> None:
    src = "\n\ndef third():\n    pass\n"
    result = extract_symbols("sample.py", src)
    assert result[0].start_line == 3


def test_mixed_decorated_and_plain(backend: str) -> None:
    src = (
        "def plain():\n    pass\n"
        "@cache\n"
        "def decorated():\n    pass\n"
        "class Bare:\n    pass\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["plain", "decorated", "Bare"]


# ---------------------------------------------------------------------------
# Dispatcher path
# ---------------------------------------------------------------------------


def test_py_extension_dispatches_to_python_backend(backend: str) -> None:
    """The ``.py`` extension routes through the new backends and returns a
    non-empty list for a simple def. This guards the registry wiring in
    ``coordination/symbols/__init__.py``."""

    result = extract_symbols("foo.py", "def simple():\n    return 1\n")
    assert _names(result) == ["simple"]
    assert result[0].kind == "function"


# ---------------------------------------------------------------------------
# v0.16: methods inside classes
# ---------------------------------------------------------------------------


def test_class_method_extraction_basic(backend: str) -> None:
    """A class with two methods emits three symbols: the class plus each method.

    Methods follow the class in source order; both methods carry
    ``parent='Foo'`` and ``kind='function'``.
    """

    src = (
        "class Foo:\n"
        "    def handleA(self):\n"
        "        return 1\n"
        "    def handleB(self):\n"
        "        return 2\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Foo", "handleA", "handleB"]

    foo = _by_name(result, "Foo")
    assert foo.kind == "class"
    assert foo.parent is None

    for method_name in ("handleA", "handleB"):
        method = _by_name(result, method_name)
        assert method.kind == "function"
        assert method.parent == "Foo"


def test_async_method_emitted(backend: str) -> None:
    """An ``async def`` inside a class surfaces as kind='function', parent=class.

    The dataclass has no ``async_function`` kind, so async methods collapse
    into the same ``function`` kind as their synchronous siblings.
    """

    src = (
        "class Client:\n"
        "    async def fetch(self):\n"
        "        return await load()\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Client", "fetch"]

    fetch = _by_name(result, "fetch")
    assert fetch.kind == "function"
    assert fetch.parent == "Client"


def test_decorated_method_emitted(backend: str) -> None:
    """Decorated methods (``@property``, ``@staticmethod``, ``@classmethod``)
    surface as methods with ``parent`` set; the decorator does not change
    the kind or parent attribution."""

    src = (
        "class Widget:\n"
        "    @property\n"
        "    def label(self):\n"
        "        return self._label\n"
        "    @staticmethod\n"
        "    def helper():\n"
        "        return 1\n"
        "    @classmethod\n"
        "    def from_dict(cls, data):\n"
        "        return cls()\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Widget", "label", "helper", "from_dict"]

    for method_name in ("label", "helper", "from_dict"):
        method = _by_name(result, method_name)
        assert method.kind == "function"
        assert method.parent == "Widget"


def test_method_inside_nested_class(backend: str) -> None:
    """v0.17: a nested class and its methods are emitted with the full
    ``Outer::Inner`` ancestor path in ``parent``.

    Pre-v0.17 the two-level model dropped nested classes entirely. v0.17
    extends ``parent_symbol`` to store the ancestor chain joined by
    ``"::"`` so arbitrary nesting works without a schema change. This
    test pins the new behaviour for both backends.
    """

    src = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def m(self):\n"
        "            return 1\n"
    )
    result = extract_symbols("sample.py", src)
    names = _names(result)
    assert "Outer" in names
    assert "Inner" in names
    assert "m" in names
    by_name = {s.name: s for s in result}
    assert by_name["Outer"].parent is None
    assert by_name["Inner"].parent == "Outer"
    assert by_name["m"].parent == "Outer::Inner"


def test_function_in_class_method_excluded(backend: str) -> None:
    """A closure inside a method (three levels of nesting) is not emitted.

    The class's method is emitted with ``parent='Holder'``, but the inner
    ``helper`` function lives inside the method body and is not a direct
    child of the class body, so it stays excluded -- same posture as the
    pre-v0.16 ``test_nested_function_excluded`` rule at module scope.
    """

    src = (
        "class Holder:\n"
        "    def method(self):\n"
        "        def helper():\n"
        "            return 1\n"
        "        return helper()\n"
    )
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["Holder", "method"]
    assert "helper" not in _names(result)

    method = _by_name(result, "method")
    assert method.kind == "function"
    assert method.parent == "Holder"


def test_top_level_function_unaffected(backend: str) -> None:
    """A free-standing ``def`` continues to emit with ``parent=None``.

    Sanity check that the method pass does not contaminate top-level
    symbols. ``foo`` is at module scope, not a class member.
    """

    src = "def foo():\n    return 1\n"
    result = extract_symbols("sample.py", src)
    assert _names(result) == ["foo"]
    assert result[0].kind == "function"
    assert result[0].parent is None
