"""Tests for the Kotlin symbol extractors.

Both backends (tree-sitter and regex) are exercised. The regex backend is
always tested directly because it has no native dependency. The tree-sitter
backend is guarded with ``pytest.importorskip("tree_sitter_kotlin")`` so the
suite still runs cleanly on machines without the native wheel.

Tests call the backend modules' ``extract`` functions directly rather than
going through :func:`coordination.symbols.extract_symbols`. The ``.kt`` / ``.kts``
extensions are wired into the dispatcher registry by a separate integration
step; exercising the modules directly keeps these tests independent of that
wiring while still pinning the parsing contract.

Test fixtures are inline Kotlin source strings; we deliberately avoid touching
the filesystem so the tests stay fast and deterministic.
"""

from __future__ import annotations

import importlib

import pytest

from coordination.symbols import Symbol, kotlin_regex

_TREESITTER_AVAILABLE = True
try:
    importlib.import_module("tree_sitter")
    importlib.import_module("tree_sitter_kotlin")
except ImportError:  # pragma: no cover - depends on install state
    _TREESITTER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Backend fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param(
            "treesitter",
            marks=pytest.mark.skipif(
                not _TREESITTER_AVAILABLE,
                reason="tree-sitter-kotlin not installed",
            ),
        ),
        "regex",
    ]
)
def extract(request: pytest.FixtureRequest):
    """Return the ``extract`` callable for the parametrised backend.

    The tree-sitter parameter is skipped automatically when the native wheel is
    absent; the regex parameter always runs.
    """

    if request.param == "treesitter":
        mod = importlib.import_module("coordination.symbols.kotlin_treesitter")
        return mod.extract
    return kotlin_regex.extract


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
# Functions (run for BOTH backends)
# ---------------------------------------------------------------------------


def test_simple_function(extract) -> None:
    src = "package demo\n\nfun helloWorld() {\n    return\n}\n"
    result = extract(src)
    assert "helloWorld" in _names(result)
    assert _by_name(result, "helloWorld").kind == "function"


def test_generic_function(extract) -> None:
    src = "package demo\n\nfun <T> identity(x: T): T {\n    return x\n}\n"
    result = extract(src)
    assert "identity" in _names(result)
    assert _by_name(result, "identity").kind == "function"


def test_top_level_func_has_no_parent(extract) -> None:
    src = "package demo\n\nfun init() {}\n"
    result = extract(src)
    init = _by_name(result, "init")
    assert init.kind == "function"
    assert init.parent is None


def test_suspend_modifier_function(extract) -> None:
    src = "package demo\n\nsuspend fun fetch(): Int {\n    return 1\n}\n"
    result = extract(src)
    assert "fetch" in _names(result)
    assert _by_name(result, "fetch").kind == "function"


# ---------------------------------------------------------------------------
# Classes / interfaces / objects / enums (run for BOTH backends)
# ---------------------------------------------------------------------------


def test_class_declaration(extract) -> None:
    src = "package demo\n\nclass Widget {\n}\n"
    result = extract(src)
    assert "Widget" in _names(result)
    assert _by_name(result, "Widget").kind == "class"


def test_data_class_is_class(extract) -> None:
    src = "package demo\n\ndata class Point(val x: Int, val y: Int)\n"
    result = extract(src)
    assert "Point" in _names(result)
    assert _by_name(result, "Point").kind == "class"


def test_interface_declaration(extract) -> None:
    src = "package demo\n\ninterface Service {\n    fun call()\n}\n"
    result = extract(src)
    assert "Service" in _names(result)
    assert _by_name(result, "Service").kind == "interface"


def test_object_declaration_is_class(extract) -> None:
    src = "package demo\n\nobject Registry {\n}\n"
    result = extract(src)
    assert "Registry" in _names(result)
    assert _by_name(result, "Registry").kind == "class"


def test_enum_class_is_enum(extract) -> None:
    src = "package demo\n\nenum class Color {\n    RED, GREEN, BLUE\n}\n"
    result = extract(src)
    assert "Color" in _names(result)
    assert _by_name(result, "Color").kind == "enum"


# ---------------------------------------------------------------------------
# Top-level properties (run for BOTH backends)
# ---------------------------------------------------------------------------


def test_const_val_is_const(extract) -> None:
    src = 'package demo\n\nconst val GREETING = "hi"\n'
    result = extract(src)
    assert "GREETING" in _names(result)
    assert _by_name(result, "GREETING").kind == "const"


def test_top_level_val_is_const(extract) -> None:
    src = "package demo\n\nval answer = 42\n"
    result = extract(src)
    assert "answer" in _names(result)
    assert _by_name(result, "answer").kind == "const"


# ---------------------------------------------------------------------------
# Empty / structural files (run for BOTH backends)
# ---------------------------------------------------------------------------


def test_comment_only_file(extract) -> None:
    src = "// just a comment\n/* also a comment */\n"
    result = extract(src)
    assert result == []


def test_package_and_imports_only(extract) -> None:
    src = (
        "package demo\n"
        "\n"
        "import kotlin.math.PI\n"
        "import kotlin.collections.List\n"
    )
    result = extract(src)
    assert result == []


# ---------------------------------------------------------------------------
# Ordering (run for BOTH backends)
# ---------------------------------------------------------------------------


def test_file_order_preserved(extract) -> None:
    src = (
        "package demo\n"
        "\n"
        "fun first() {}\n"
        "\n"
        "class Second {\n}\n"
        "\n"
        "fun third() {}\n"
    )
    result = extract(src)
    names = _names(result)
    assert names.index("first") < names.index("Second") < names.index("third")


# ---------------------------------------------------------------------------
# Regex backend specifics (always available)
# ---------------------------------------------------------------------------


def test_regex_indented_method_excluded() -> None:
    """The regex backend anchors to column zero, so members are dropped."""

    src = (
        "package demo\n"
        "\n"
        "class Holder {\n"
        "    fun member() {}\n"
        "}\n"
    )
    result = kotlin_regex.extract(src)
    names = _names(result)
    assert "Holder" in names
    assert "member" not in names


def test_regex_end_line_equals_start_line() -> None:
    src = "package demo\n\nfun solo() {\n    return\n}\n"
    result = kotlin_regex.extract(src)
    solo = _by_name(result, "solo")
    assert solo.start_line == solo.end_line


def test_regex_enum_not_downgraded_to_class() -> None:
    src = "package demo\n\nenum class Suit {\n    HEARTS\n}\n"
    result = kotlin_regex.extract(src)
    kinds = {s.kind for s in result if s.name == "Suit"}
    assert kinds == {"enum"}


# ---------------------------------------------------------------------------
# Tree-sitter backend specifics (members + parent edges)
# ---------------------------------------------------------------------------


def test_treesitter_method_has_parent() -> None:
    """A method inside a class body carries the class name as ``parent``."""

    pytest.importorskip("tree_sitter_kotlin")
    from coordination.symbols import kotlin_treesitter

    src = (
        "package demo\n"
        "\n"
        "class Server {\n"
        "    fun start() {}\n"
        "    fun stop() {}\n"
        "}\n"
    )
    result = kotlin_treesitter.extract(src)
    start = _by_name(result, "start")
    stop = _by_name(result, "stop")
    assert start.kind == "function"
    assert start.parent == "Server"
    assert stop.parent == "Server"


def test_treesitter_interface_method_has_parent() -> None:
    pytest.importorskip("tree_sitter_kotlin")
    from coordination.symbols import kotlin_treesitter

    src = (
        "package demo\n"
        "\n"
        "interface Repository {\n"
        "    fun findById(id: Int): String\n"
        "}\n"
    )
    result = kotlin_treesitter.extract(src)
    repo = _by_name(result, "Repository")
    assert repo.kind == "interface"
    found = _by_name(result, "findById")
    assert found.kind == "function"
    assert found.parent == "Repository"


def test_treesitter_companion_object() -> None:
    """An unnamed companion object surfaces as ``Companion`` parented to the class."""

    pytest.importorskip("tree_sitter_kotlin")
    from coordination.symbols import kotlin_treesitter

    src = (
        "package demo\n"
        "\n"
        "class Factory {\n"
        "    companion object {\n"
        "        fun create(): Factory = Factory()\n"
        "    }\n"
        "}\n"
    )
    result = kotlin_treesitter.extract(src)
    companion = _by_name(result, "Companion")
    assert companion.kind == "class"
    assert companion.parent == "Factory"
    create = _by_name(result, "create")
    assert create.parent == "Companion" or create.parent == "Factory"


def test_treesitter_named_companion_object() -> None:
    pytest.importorskip("tree_sitter_kotlin")
    from coordination.symbols import kotlin_treesitter

    src = (
        "package demo\n"
        "\n"
        "class Config {\n"
        "    companion object Defaults {\n"
        "        val timeout = 30\n"
        "    }\n"
        "}\n"
    )
    result = kotlin_treesitter.extract(src)
    defaults = _by_name(result, "Defaults")
    assert defaults.kind == "class"
    assert defaults.parent == "Config"


def test_treesitter_object_method_has_parent() -> None:
    pytest.importorskip("tree_sitter_kotlin")
    from coordination.symbols import kotlin_treesitter

    src = (
        "package demo\n"
        "\n"
        "object Singleton {\n"
        "    fun run() {}\n"
        "}\n"
    )
    result = kotlin_treesitter.extract(src)
    singleton = _by_name(result, "Singleton")
    assert singleton.kind == "class"
    run = _by_name(result, "run")
    assert run.kind == "function"
    assert run.parent == "Singleton"


def test_treesitter_top_level_func_no_parent() -> None:
    pytest.importorskip("tree_sitter_kotlin")
    from coordination.symbols import kotlin_treesitter

    src = "package demo\n\nfun free() {}\n"
    result = kotlin_treesitter.extract(src)
    free = _by_name(result, "free")
    assert free.parent is None


def test_treesitter_line_numbers_are_one_indexed_inclusive() -> None:
    """Line spans are 1-indexed and inclusive of the closing brace line."""

    pytest.importorskip("tree_sitter_kotlin")
    from coordination.symbols import kotlin_treesitter

    src = "package demo\n\nfun spanned() {\n    return\n}\n"
    result = kotlin_treesitter.extract(src)
    spanned = _by_name(result, "spanned")
    # ``fun spanned`` is on line 3; the closing brace is on line 5.
    assert spanned.start_line == 3
    assert spanned.end_line == 5
