"""Regex-based PHP symbol extractor (fallback backend).

Used when the ``tree-sitter-php`` wheel is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can still
ship symbol claims on machines without the native tree-sitter wheels.

Known false-negatives (documented so callers know the trade-off):

- ``method_declaration`` inside a class / interface / trait / enum body is not
  emitted. Conventional PSR-12 formatting indents members, and every pattern
  here is anchored to column zero, so methods are filtered out by structure.
  The tree-sitter backend walks class bodies and emits each method with its
  ``parent`` set; install the ``symbols`` extra to get method-level claims and
  ``Class::method`` coexistence.

- Nested declarations (a class declared inside another method body, or a
  closure assigned to a variable inside a function) are likewise filtered by
  the column-zero anchoring. A pathological file that outdents nested
  declarations to column zero would not match conventional PHP style.

- The ``<?php`` open tag, ``namespace`` statements, and ``use`` import clauses
  are non-matches and emit nothing -- they are not claimable units.

- A class keyword and its name split across separate lines would be missed.
  Real PHP never does this, but it is a theoretical gap the parser does not
  cover without brace tracking.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses
the ``(file_path, name)`` pair, so this approximation does not affect the
coordination contract.
"""

from __future__ import annotations

import re

from . import Symbol

# function Name (
# Top-level free functions. Anchored to column zero so indented class methods
# do not match (that is the documented false-negative). A leading return-type
# attribute or visibility modifier never appears on a top-level ``function``.
_FUNC_RE = re.compile(
    r"^function\s+(?P<name>\w+)\s*\(",
    re.MULTILINE,
)

# [abstract|final|readonly]* class Name
# Class modifiers may appear in any order and any number before ``class``; the
# repeated non-capturing group consumes them so only the name is captured.
_CLASS_RE = re.compile(
    r"^(?:(?:abstract|final|readonly)\s+)*class\s+(?P<name>\w+)",
    re.MULTILINE,
)

# interface Name
_INTERFACE_RE = re.compile(
    r"^interface\s+(?P<name>\w+)",
    re.MULTILINE,
)

# trait Name -- mapped to kind='class' to match the tree-sitter backend.
_TRAIT_RE = re.compile(
    r"^trait\s+(?P<name>\w+)",
    re.MULTILINE,
)

# enum Name [: backing-type]
# Pure enums (``enum Suit``) and backed enums (``enum Suit: string``) both
# match; the optional backing type after the name is not part of the capture.
_ENUM_RE = re.compile(
    r"^enum\s+(?P<name>\w+)",
    re.MULTILINE,
)


def _line_of(content: str, offset: int) -> int:
    """1-indexed line number containing the byte at ``offset``."""

    return content.count("\n", 0, offset) + 1


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero (``re.MULTILINE`` + ``^``) so nested
    declarations and indented class members are excluded. Each match emits
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

    for match in _FUNC_RE.finditer(content):
        _record(match.group("name"), "function", match.start())

    for match in _CLASS_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _INTERFACE_RE.finditer(content):
        _record(match.group("name"), "interface", match.start())

    for match in _TRAIT_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _ENUM_RE.finditer(content):
        _record(match.group("name"), "enum", match.start())

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
