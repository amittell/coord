"""Regex-based Go symbol extractor (fallback backend).

Used when the ``tree-sitter-go`` wheel is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can still
ship symbol claims on machines without the native tree-sitter wheels.

Known false-negatives (documented so callers know the trade-off):

- Parenthesised ``const`` / ``var`` blocks::

      const (
          A = 1
          B = 2
      )

  Only the opening ``const``/``var`` line is matched, and that line has no
  identifier on it, so nothing is emitted. The tree-sitter backend handles
  these correctly; install the ``symbols`` extra to get full coverage.

- Multi-line type aliases or definitions where the keyword and the name sit
  on separate lines. Real Go code never does this, but linters tolerate it.

- ``//go:build`` and ``// +build`` constraint lines are simply non-matches
  (they start with ``//``) so build-tagged files behave like any other file.

- Methods on pointer / value receivers parse correctly because the receiver
  list ``(r *T)`` is optional in the leading regex.

- Nested declarations are filtered by anchoring every pattern to column zero.
  Conventional Go formatting puts nested closures and types under
  indentation, which keeps them out of the result. A pathological file that
  outdents nested declarations to column zero would not be valid Go.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses
the ``(file_path, name)`` pair, so this approximation does not affect the
coordination contract.
"""

from __future__ import annotations

import re

from . import Symbol

# func [optional (receiver)] Name [generic params] (
# The receiver group covers methods; tree-sitter exposes method_declaration as
# a separate node but for the regex backend we treat both as ``kind='function'``
# because the conflict engine does not care which one fired.
_FUNC_RE = re.compile(
    r"^func\s+(?:\([^)]*\)\s+)?(?P<name>\w+)\s*[\(\[]",
    re.MULTILINE,
)

# type Name interface { ... }
_TYPE_INTERFACE_RE = re.compile(
    r"^type\s+(?P<name>\w+)\s+interface\b",
    re.MULTILINE,
)

# type Name (struct | = Alias | OtherType)
# Run this after the interface pattern so interface declarations do not get
# downgraded to kind='type'. The trailing alternation covers:
#   - struct types:    ``type Foo struct {...}``
#   - aliases:         ``type Foo = bar``
#   - named types:     ``type Foo bar``
_TYPE_OTHER_RE = re.compile(
    r"^type\s+(?P<name>\w+)\s+(?:struct\b|=\s*\w|\w+)",
    re.MULTILINE,
)

# const | var Name <type-or-=>
# Best-effort: catches single-line declarations like ``const Foo = 1`` and
# ``var Bar int = 0``. Parenthesised blocks are documented misses.
_CONST_VAR_RE = re.compile(
    r"^(?:const|var)\s+(?P<name>\w+)\s+",
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
                Symbol(name=name, kind=kind, start_line=line, end_line=line),
            )
        )

    for match in _FUNC_RE.finditer(content):
        _record(match.group("name"), "function", match.start())

    for match in _TYPE_INTERFACE_RE.finditer(content):
        _record(match.group("name"), "interface", match.start())

    for match in _TYPE_OTHER_RE.finditer(content):
        name = match.group("name")
        line = _line_of(content, match.start())
        # Skip if the interface scan already claimed this declaration on the
        # same line -- keeps a single ``type Foo interface {}`` from emitting
        # both an ``interface`` and a ``type`` symbol.
        if (name, "interface", line) in seen:
            continue
        _record(name, "type", match.start())

    for match in _CONST_VAR_RE.finditer(content):
        _record(match.group("name"), "const", match.start())

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
