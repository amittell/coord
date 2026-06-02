"""Regex-based TypeScript symbol extractor (fallback backend).

This backend is used when tree-sitter is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so the coord MCP
wrapper can still ship symbol claims on machines without the native
tree-sitter wheels.

Known false-negatives (documented so callers know the trade-off):

- Decorated declarations (``@Injectable() class Foo {}``). The leading
  decorator pushes the keyword off the start of the line.
- Anonymous arrow functions assigned through destructuring, for example
  ``const { handler = () => {} } = obj``.
- Multi-binding ``const a = 1, b = function(){}`` only catches the first
  identifier; the second binding is dropped because the regex anchors to a
  single keyword per line.
- ``const`` that is not a function but the value spans multiple lines and
  happens to begin with whitespace followed by ``function`` later on. Not a
  practical concern but worth noting.
- ``export default function() {}`` with no name is normalised to
  ``name='default', kind='function'``. ``export default class {}`` follows
  the same convention.
- Comments that contain the literal text ``function`` are excluded because
  the regex anchors to the start of the line; a comment that begins with
  ``//`` will not match the keyword group.
- Nested declarations are filtered by requiring the keyword (or its
  ``export``/``async`` prefix) to start at column zero. Any indentation
  before the keyword disqualifies a line. This matches conventional
  TypeScript formatting where only top-level declarations sit at the left
  margin; nested declarations are always indented.

The regex backend approximates ``end_line`` as ``start_line`` because it
cannot track brace matching without becoming a full parser.
"""

from __future__ import annotations

import re

from . import Symbol

# Keywords that mark a top-level declaration we want to expose. Order matters
# only insofar as the regex alternation is greedy; all branches have distinct
# leading characters so there is no ambiguity.
_KIND_BY_KEYWORD = {
    "function": "function",
    "class": "class",
    "interface": "interface",
    "type": "type",
    "enum": "enum",
    "const": "const",
    "let": "const",
    "var": "const",
}

# Named declaration:
#   optional 'export' (with optional 'default'), optional 'async',
#   keyword, identifier.
_NAMED_RE = re.compile(
    r"^"
    r"(?:export\s+(?:default\s+)?)?"
    r"(?:async\s+)?"
    r"(?P<keyword>function|class|interface|type|enum|const|let|var)"
    r"\s+"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)

# Anonymous default export: ``export default function(...)`` or
# ``export default class { ... }`` with no name. We normalise these to a
# synthetic ``default`` symbol.
_ANON_DEFAULT_RE = re.compile(
    r"^export\s+default\s+(?:async\s+)?(?P<keyword>function|class)\s*[(<{]",
    re.MULTILINE,
)


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    The scan ignores indented matches deeper than three spaces to keep nested
    declarations out of the result. ``end_line`` equals ``start_line`` because
    the regex cannot track scope without becoming a real parser.
    """

    out: list[Symbol] = []
    seen_anon_default = False

    for match in _NAMED_RE.finditer(content):
        keyword = match.group("keyword")
        name = match.group("name")
        kind = _KIND_BY_KEYWORD[keyword]

        line = content.count("\n", 0, match.start()) + 1
        out.append(
            Symbol(
                name=name,
                kind=kind,
                start_line=line,
                end_line=line,
            )
        )

    for match in _ANON_DEFAULT_RE.finditer(content):
        # We emit a single synthetic default per file; multiple anonymous
        # default exports are a syntax error in real TS.
        if seen_anon_default:
            break
        line = content.count("\n", 0, match.start()) + 1
        out.append(
            Symbol(
                name="default",
                kind="function",
                start_line=line,
                end_line=line,
            )
        )
        seen_anon_default = True

    return out
