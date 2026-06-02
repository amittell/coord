"""Regex-based Python symbol extractor (fallback backend).

This backend is used when tree-sitter is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so the coord MCP
wrapper can still ship symbol claims on machines without the native
tree-sitter wheels.

Python is whitespace-significant, so the regex anchors every match to column
zero. That single rule does the heavy lifting of excluding nested
definitions: any ``def`` or ``class`` inside another block must be indented,
so it cannot match the column-zero anchor.

Known false-negatives (documented so callers know the trade-off):

- Decorators are ignored. ``@property`` on the previous line does not push the
  ``def`` off column zero, so the underlying definition is still captured by
  the column-zero rule. The decorator stack is not included in the captured
  line span, but the name and kind are correct.
- Multi-line function signatures (``def f(\n    x,\n    y,\n):``) are still
  caught because the regex only inspects the opening line where ``def`` lives.
  ``end_line`` is approximated as ``start_line`` -- it does not follow the
  signature into subsequent lines.
- Methods inside a class are excluded because they are always indented. Good.
- ``def`` or ``class`` inside an ``if __name__ == '__main__':`` block is
  excluded for the same indentation reason. (This matches the tree-sitter
  backend, which only walks direct module children.)
- Top-level ``NAME = lambda ...`` becomes a ``const`` symbol. Other
  callable-producing assignments (``NAME = some_factory()``, comprehensions,
  partials) are not captured -- the regex cannot afford to evaluate the RHS
  type without becoming a real parser.
- A ``def`` keyword inside a triple-quoted string that happens to begin at
  column zero will be mis-matched as a symbol. This is rare in practice and
  not worth a context-tracking parser to defend against.

The regex backend approximates ``end_line`` as ``start_line`` because it
cannot track indentation-based scope without becoming a full parser.
"""

from __future__ import annotations

import re

from . import Symbol

# ``def`` and ``class`` at column zero, optionally preceded by ``async`` for
# coroutine definitions. The ``re.MULTILINE`` flag makes ``^`` match the
# start of every line.
_DEF_RE = re.compile(
    r"^(?:async\s+)?(?P<keyword>def|class)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

# ``NAME = lambda ...`` at column zero. The trailing ``\b`` ensures we do not
# match identifiers that happen to start with the letters ``lambda``.
_LAMBDA_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*lambda\b",
    re.MULTILINE,
)


_KEYWORD_TO_KIND = {
    "def": "function",
    "class": "class",
}


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero so any indented definition (methods,
    nested defs, ``if``-guarded blocks) is automatically excluded. ``end_line``
    equals ``start_line`` because the regex cannot track indentation scope
    without becoming a real parser.
    """

    out: list[Symbol] = []

    for match in _DEF_RE.finditer(content):
        keyword = match.group("keyword")
        name = match.group("name")
        line = content.count("\n", 0, match.start()) + 1
        out.append(
            Symbol(
                name=name,
                kind=_KEYWORD_TO_KIND[keyword],
                start_line=line,
                end_line=line,
            )
        )

    for match in _LAMBDA_RE.finditer(content):
        name = match.group("name")
        line = content.count("\n", 0, match.start()) + 1
        out.append(
            Symbol(
                name=name,
                kind="const",
                start_line=line,
                end_line=line,
            )
        )

    # Stable order: by start_line, then by appearance within a line. The two
    # regex passes above are independent, so without sorting a lambda at line
    # 2 could land after a def at line 10. Sorting keeps the output
    # deterministic and matches the tree-sitter backend's walk order.
    out.sort(key=lambda s: s.start_line)
    return out
