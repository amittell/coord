"""Regex-based C++ symbol extractor (fallback backend).

Used when the ``tree-sitter-cpp`` wheel is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can still
ship symbol claims on machines without the native tree-sitter wheels. This
backend owns the C++ source/header extensions (``.cc``, ``.cpp``, ``.cxx``,
``.hpp``, ``.hh``); the bare ``.h`` extension is owned by the C backend.

Every pattern is anchored to column zero (``re.MULTILINE`` + ``^``). That
single rule excludes member functions and types nested inside an indented
class body or namespace block, mirroring the Go regex backend's contract of
emitting only top-level declarations. Out-of-line member definitions written
at file scope (``void Foo::bar() { ... }``) sit at column zero and ARE
captured, with the qualifier recorded as ``parent`` so ``Foo::bar`` notation
resolves even through the fallback.

Recognised top-level declarations:

- ``class Name`` -> ``kind='class'``.
- ``struct Name`` -> ``kind='type'`` (matches the tree-sitter backend's
  treatment of structs as the ``type`` grain).
- ``enum Name`` / ``enum class Name`` / ``enum struct Name`` -> ``kind='enum'``.
- ``ReturnType Foo::bar(`` at column zero -> ``kind='function'``,
  ``parent='Foo'`` (out-of-line member definition).
- ``ReturnType name(`` at column zero -> ``kind='function'``, ``parent=None``
  (free function definition or prototype).

Known false-negatives / false-positives (documented so callers know the
trade-off):

- Member functions defined or declared inside a class body are indented under
  conventional formatting, so the column-zero anchor skips them. The
  tree-sitter backend walks class bodies and surfaces these with ``parent``
  set; install the ``symbols`` extra for full coverage.
- Declarations wrapped in a ``namespace { ... }`` block are indented and are
  likewise skipped. Out-of-line definitions at file scope are still caught.
- Forward declarations (``class Foo;``) and the leading line of a class
  definition are matched identically, so a forward declaration emits a
  ``class`` symbol with ``end_line == start_line``. This is a harmless
  over-emission: the conflict engine keys on ``(file_path, name)``.
- Multi-line return types or templates that put the function name on a
  separate line from its return type are missed because the free-function
  regex inspects a single line.
- Macros that expand into definitions, function-like macros, and
  preprocessor directives are not interpreted.
- A function name preceded by control-flow keywords (``if``, ``for``,
  ``while``, ``switch``, ``return``, ``sizeof``) at column zero is excluded so
  an unbraced ``if (...)`` at file scope is not mistaken for a function. This
  list is conservative; an exotic identifier collision is possible but rare.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses
the ``(file_path, name)`` pair, so this approximation does not affect the
coordination contract. Output order follows the byte offset of each match so
callers that rely on file order get a deterministic sequence even though
several independent regexes run.
"""

from __future__ import annotations

import re

from . import Symbol

# class Name -- captures the leading identifier after the ``class`` keyword.
# A trailing ``final``, base-class list, or ``{`` does not affect the name
# capture because ``\w+`` stops at the first non-word character.
_CLASS_RE = re.compile(
    r"^class\s+(?P<name>\w+)",
    re.MULTILINE,
)

# struct Name -- same shape as the class pattern, emitted as ``kind='type'``.
_STRUCT_RE = re.compile(
    r"^struct\s+(?P<name>\w+)",
    re.MULTILINE,
)

# enum Name / enum class Name / enum struct Name. The optional ``class`` /
# ``struct`` keyword for scoped enums is consumed before the name.
_ENUM_RE = re.compile(
    r"^enum\s+(?:(?:class|struct)\s+)?(?P<name>\w+)",
    re.MULTILINE,
)

# Out-of-line member definition: ``ReturnType Foo::bar(``. The return type and
# any qualifiers occupy the leading run of type tokens; the qualifier
# (``Foo``) and the member name (``bar``) are captured. Nested qualifiers
# (``Outer::Inner::bar``) collapse to the last scope segment via the greedy
# leading run, which is the parent the tree-sitter backend records as well.
_OUT_OF_LINE_METHOD_RE = re.compile(
    r"^(?P<lead>[\w:<>,&*\[\]][\w:<>,&*\[\]\t ]*?)?"
    r"(?P<parent>\w+)::(?P<name>~?\w+)[\t ]*\(",
    re.MULTILINE,
)

# Free function: ``ReturnType name(`` at column zero. The leading run is one or
# more type tokens (allowing pointers, references, templates, and namespace
# qualifiers in the return type); the final ``\w+`` before ``(`` is the name.
# Requires at least one whitespace-separated leading token so a bare
# ``name(`` call expression at column zero is not captured.
_FREE_FUNCTION_RE = re.compile(
    r"^(?P<lead>[\w:<>,&*\[\]][\w:<>,&*\[\]\t ]*?[\t ][*&\t ]*)"
    r"(?P<name>\w+)[\t ]*\(",
    re.MULTILINE,
)

# Control-flow / operator keywords that must never be treated as a function
# name when the free-function regex matches a statement at column zero.
_NON_FUNCTION_NAMES = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "catch",
        "else",
        "do",
        "case",
    }
)


def _line_of(content: str, offset: int) -> int:
    """1-indexed line number containing the byte at ``offset``."""

    return content.count("\n", 0, offset) + 1


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero (``re.MULTILINE`` + ``^``) so members
    nested inside an indented class body or namespace block are excluded. Each
    match emits exactly one Symbol; ``end_line`` equals ``start_line``.

    Order in the returned list follows the byte offset of the match so callers
    that rely on file order get a deterministic sequence even though we run
    several independent regexes. Duplicate ``(name, kind, line)`` triples are
    collapsed so a declaration matched by two overlapping patterns surfaces
    once.
    """

    found: list[tuple[int, Symbol]] = []
    seen: set[tuple[str, str, int]] = set()

    def _record(
        name: str, kind: str, offset: int, parent: str | None = None
    ) -> None:
        line = _line_of(content, offset)
        key = (name, kind, line)
        if key in seen:
            return
        seen.add(key)
        found.append(
            (
                offset,
                Symbol(
                    name=name,
                    kind=kind,
                    start_line=line,
                    end_line=line,
                    parent=parent,
                ),
            )
        )

    for match in _CLASS_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _STRUCT_RE.finditer(content):
        _record(match.group("name"), "type", match.start())

    for match in _ENUM_RE.finditer(content):
        _record(match.group("name"), "enum", match.start())

    for match in _OUT_OF_LINE_METHOD_RE.finditer(content):
        name = match.group("name")
        if name in _NON_FUNCTION_NAMES:
            continue
        _record(
            name,
            "function",
            match.start("parent"),
            parent=match.group("parent"),
        )

    for match in _FREE_FUNCTION_RE.finditer(content):
        name = match.group("name")
        if name in _NON_FUNCTION_NAMES:
            continue
        line = _line_of(content, match.start("name"))
        # Skip if the out-of-line method scan already claimed this name on the
        # same line -- a ``Foo::bar(`` definition also satisfies the broader
        # free-function pattern, and we want the parented version to win.
        if (name, "function", line) in seen:
            continue
        # ``Foo::bar`` lines must be owned by the out-of-line pass so the
        # qualifier is recorded; if the leading run swallowed a ``::`` the
        # name here is the unqualified tail and the parented record already
        # exists, so the guard above filters it. A lone ``name(`` with a
        # qualified parent that the method pass missed (e.g. an operator)
        # falls through as a free function, which is acceptable.
        _record(name, "function", match.start("name"))

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
