"""Regex-based C symbol extractor (fallback backend).

Used when the ``tree-sitter-c`` wheel is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can still
ship symbol claims on machines without the native tree-sitter wheels.

Known false-negatives (documented so callers know the trade-off):

- ``typedef`` whose declared name is not on the ``typedef`` line::

      typedef struct {
          int x;
      } Foo;

  Only the opening ``typedef`` line is scanned and the name ``Foo`` sits on a
  later line, so nothing is emitted for it. The tree-sitter backend handles
  these correctly; install the ``symbols`` extra to get full coverage.

- ``struct`` / ``union`` / ``enum`` whose tag name is not on the keyword line
  (``struct\n  Foo {`` style). Real C never does this, but it is legal.

- Multi-line function signatures where the function name is not on the same
  line as its return type (e.g. the return type alone on the first line). The
  pattern needs the return type, name and opening ``(`` reachable from a single
  column-zero anchor.

Known false-positives (documented for the same reason):

- Function *prototypes* (declarations ending in ``;``) and ``K&R``-style
  declarations match the function pattern just like definitions do. The
  conflict engine keys on ``(file_path, name)`` and a prototype shares the
  name of its definition, so a stray prototype hit folds into the same claim
  rather than creating a spurious distinct unit.

- ``struct`` / ``union`` / ``enum`` *references* at column zero
  (``struct Foo gObject;``) match the tag pattern. The tree-sitter backend
  distinguishes a definition (it has a body) from a reference; the regex
  backend cannot without brace tracking and accepts the over-match.

- Macro lines that happen to look like ``NAME(`` at column zero (function-like
  macro *invocations*, not ``#define``) can match the function pattern. The
  ``#define`` itself starts with ``#`` and is a non-match.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses the
``(file_path, name)`` pair, so this approximation does not affect the
coordination contract.

C has no classes and no methods, so every Symbol carries ``parent=None`` -- the
``parent`` field is reserved for languages with member functions (TypeScript,
Python, Go receivers) and stays unset here.

Nested declarations are filtered by anchoring every pattern to column zero
(``re.MULTILINE`` + ``^``). Conventional C formatting indents declarations
inside function bodies, which keeps them out of the result; a file that
outdented nested declarations to column zero would be unconventional C.
"""

from __future__ import annotations

import re

from . import Symbol

# Function definition / prototype at column zero:
#   <return-type-and-modifiers> <name>(
# The return type is required (at least one token before the name) so that a
# bare ``name(`` macro invocation without a type is less likely to match. A
# leading ``typedef`` is excluded via negative lookahead so function-pointer
# typedefs fall to the typedef pattern instead of being miscounted as
# functions. Pointer return types (``int *make(``) are tolerated: the ``*`` and
# surrounding whitespace are consumed before the captured name.
_FUNC_RE = re.compile(
    r"^(?!typedef\b)"
    r"(?:[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*\s+|\s*)"
    r"\*?\s*"
    r"(?P<name>[A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)

# struct Tag  ->  kind='type'
_STRUCT_RE = re.compile(
    r"^struct\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

# union Tag  ->  kind='type'
_UNION_RE = re.compile(
    r"^union\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

# enum Tag  ->  kind='enum'
_ENUM_RE = re.compile(
    r"^enum\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

# Single-line typedef whose name is the last identifier before the ``;``:
#   typedef int MyInt;
#   typedef unsigned long ulong;
#   typedef struct Node Node;
# Multi-line typedefs (anonymous struct body then name) are a documented miss.
_TYPEDEF_RE = re.compile(
    r"^typedef\b[^\n;]*?\b(?P<name>[A-Za-z_]\w*)\s*;",
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
                    parent=None,
                ),
            )
        )

    for match in _TYPEDEF_RE.finditer(content):
        _record(match.group("name"), "type", match.start())

    for match in _STRUCT_RE.finditer(content):
        _record(match.group("name"), "type", match.start())

    for match in _UNION_RE.finditer(content):
        _record(match.group("name"), "type", match.start())

    for match in _ENUM_RE.finditer(content):
        _record(match.group("name"), "enum", match.start())

    for match in _FUNC_RE.finditer(content):
        # The struct/union/enum/typedef keywords can lead a line that also ends
        # in ``(`` (rare); they have already been recorded with the right kind,
        # so skip any function match whose captured name is a bare keyword.
        if match.group("name") in {"struct", "union", "enum", "typedef"}:
            continue
        _record(match.group("name"), "function", match.start())

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
