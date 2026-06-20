"""Regex-based Java symbol extractor (fallback backend).

Used when the ``tree-sitter-java`` wheel is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can still
ship symbol claims on machines without the native tree-sitter wheels.

The scan is anchored to column zero (``re.MULTILINE`` + ``^``) and recognises
the four top-level type declarations Java expresses: ``class``, ``interface``,
``enum`` and ``record``. Leading modifiers (``public``, ``final``, ``abstract``,
``sealed``, ...) on the same line are consumed before the keyword.

Known false-negatives (documented so callers know the trade-off):

- Methods and constructors. Java members live indented inside a type body, so
  the column-zero anchor never matches them. The regex backend therefore emits
  no ``kind='function'`` symbols and no ``parent`` edges; ``Class::method``
  claims are unavailable in regex mode. Install the ``symbols`` extra to get
  the tree-sitter backend, which walks type bodies and attaches parents.

- Nested type declarations. A class / interface / enum / record declared inside
  another type is indented and so is skipped by the same anchoring. Only the
  outermost (column-zero) type surfaces.

- Annotations or modifiers wrapped onto their own preceding lines are fine
  because we anchor on the keyword line, but a declaration whose keyword is
  pushed off column zero by leading whitespace (e.g. a type nested one level
  in but outdented) would be missed or, if outdented to column zero, wrongly
  surfaced. Conventional formatting keeps nested declarations indented.

- ``record`` is a contextual keyword in Java; an unrelated identifier named
  ``record`` appearing at column zero followed by another identifier is
  vanishingly rare in real code but would be mis-classified as a record type.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses
the ``(file_path, name)`` pair, so this approximation does not affect the
coordination contract.
"""

from __future__ import annotations

import re

from . import Symbol

# Optional leading modifiers / keywords that may precede a type keyword on the
# same line. ``non-sealed`` contains a hyphen, so it is spelled out explicitly.
_MODIFIERS = (
    r"(?:(?:public|private|protected|abstract|final|static|sealed|"
    r"non-sealed|strictfp)\s+)*"
)

# <modifiers> class Name
_CLASS_RE = re.compile(
    r"^" + _MODIFIERS + r"class\s+(?P<name>\w+)",
    re.MULTILINE,
)

# <modifiers> interface Name
_INTERFACE_RE = re.compile(
    r"^" + _MODIFIERS + r"interface\s+(?P<name>\w+)",
    re.MULTILINE,
)

# <modifiers> enum Name
_ENUM_RE = re.compile(
    r"^" + _MODIFIERS + r"enum\s+(?P<name>\w+)",
    re.MULTILINE,
)

# <modifiers> record Name
# Records map to kind='class'; see the module docstring for the contextual
# keyword caveat.
_RECORD_RE = re.compile(
    r"^" + _MODIFIERS + r"record\s+(?P<name>\w+)",
    re.MULTILINE,
)


def _line_of(content: str, offset: int) -> int:
    """1-indexed line number containing the byte at ``offset``."""

    return content.count("\n", 0, offset) + 1


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero (``re.MULTILINE`` + ``^``) so nested
    declarations under conventional indentation are excluded. Each match emits
    exactly one Symbol; ``end_line`` equals ``start_line``.

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

    for match in _CLASS_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _INTERFACE_RE.finditer(content):
        _record(match.group("name"), "interface", match.start())

    for match in _ENUM_RE.finditer(content):
        _record(match.group("name"), "enum", match.start())

    for match in _RECORD_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
