"""Regex-based C# symbol extractor (fallback backend).

Used when the ``tree-sitter-c-sharp`` wheel is not installed or when the
operator forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can
still ship symbol claims on machines without the native tree-sitter wheels.

Every pattern is anchored to column zero (``re.MULTILINE`` + ``^``). C# nests
its claimable units inside namespaces and types, so conventional formatting
indents members; column-zero anchoring is what keeps nested declarations out of
the result without a brace-matching parser.

Known false-negatives (documented so callers know the trade-off):

- Methods and properties are missed. They live inside type bodies and are
  conventionally indented, so they never sit at column zero. The tree-sitter
  backend surfaces them with ``parent`` set; install the ``symbols`` extra to
  get member-level (``Class::method``) coverage.

- Types nested inside a namespace block (``namespace Foo { class Bar { } }``)
  are indented and therefore missed. File-scoped namespaces
  (``namespace Foo;``) leave their types at column zero, so those types are
  found. The tree-sitter backend descends through both namespace styles.

- Nested types (a class declared inside another class body) are indented and
  missed for the same reason.

- Enum members, ``const`` fields and fields generally are not emitted: they are
  type members and are documented misses along with methods above.

- Attributes (``[Serializable]``) and XML-doc comment lines (``///``) are simply
  non-matches; a declaration on a following line still matches normally.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses the
``(file_path, name)`` pair, so this approximation does not affect the
coordination contract.
"""

from __future__ import annotations

import re

from . import Symbol

# Modifiers that may precede a type keyword. Consumed but not captured.
_MODIFIERS = (
    r"(?:(?:public|private|protected|internal|static|sealed|abstract|"
    r"partial|new|readonly|unsafe|ref|file)\s+)*"
)

# [modifiers] class Name [<T>]
_CLASS_RE = re.compile(
    r"^" + _MODIFIERS + r"class\s+(?P<name>\w+)",
    re.MULTILINE,
)

# [modifiers] interface Name [<T>]
_INTERFACE_RE = re.compile(
    r"^" + _MODIFIERS + r"interface\s+(?P<name>\w+)",
    re.MULTILINE,
)

# [modifiers] struct Name [<T>]
_STRUCT_RE = re.compile(
    r"^" + _MODIFIERS + r"struct\s+(?P<name>\w+)",
    re.MULTILINE,
)

# [modifiers] record [class|struct] Name [<T>]
# The optional ``class``/``struct`` qualifier (``record class Foo``) is consumed
# before the name so positional records of every shape resolve to the same name.
_RECORD_RE = re.compile(
    r"^" + _MODIFIERS + r"record\s+(?:(?:class|struct)\s+)?(?P<name>\w+)",
    re.MULTILINE,
)

# [modifiers] enum Name
_ENUM_RE = re.compile(
    r"^" + _MODIFIERS + r"enum\s+(?P<name>\w+)",
    re.MULTILINE,
)


def _line_of(content: str, offset: int) -> int:
    """1-indexed line number containing the byte at ``offset``."""

    return content.count("\n", 0, offset) + 1


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero (``re.MULTILINE`` + ``^``) so members and
    namespace-nested types under conventional indentation are excluded. Each
    match emits exactly one Symbol; ``end_line`` equals ``start_line``.

    Order in the returned list follows the byte offset of the match so callers
    that rely on file order get a deterministic sequence even though we run
    several independent regexes.
    """

    found: list[tuple[int, Symbol]] = []
    seen: set[tuple[str, str, int]] = set()

    def _record(name: str, kind: str, offset: int) -> None:
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
                ),
            )
        )

    # Records are matched before classes so ``record class Foo`` is not also
    # downgraded into a bare ``class`` Symbol on the same line.
    for match in _RECORD_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _CLASS_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _INTERFACE_RE.finditer(content):
        _record(match.group("name"), "interface", match.start())

    for match in _STRUCT_RE.finditer(content):
        _record(match.group("name"), "type", match.start())

    for match in _ENUM_RE.finditer(content):
        _record(match.group("name"), "enum", match.start())

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
