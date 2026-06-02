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

v0.16: a second pass extracts method-level symbols from inside top-level
class bodies. The class body is bounded by counting ``{`` / ``}`` characters
from the opening brace on the ``class Foo {`` line. The brace counter
ignores strings, regex literals, template literals, and comments -- it is a
pure character count. This is brittle by design; the goal is a useful
fallback when tree-sitter is unavailable, not a parser. Known false-positives
and false-negatives for the method pass:

- Braces inside string/template/regex literals are counted, so a method body
  containing the literal text ``"}"`` will end the class early.
- ``//`` and ``/* */`` comments containing ``{`` or ``}`` shift the counter.
- Nested classes inside a method body have their methods attributed to the
  outer class (the brace counter cannot distinguish member scopes). This is
  acceptable given the tree-sitter backend correctly excludes them and is
  preferred whenever available.
- Static initialisation blocks (``static { ... }``) confuse the brace
  counter only insofar as their contents are scanned for method-shaped
  lines; the design doc rules these out as not-claimable anyway.

The regex backend approximates ``end_line`` as ``start_line`` because it
cannot track brace matching across multi-line bodies for top-level
declarations either.
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

# Reserved words that can appear at the start of an indented line and look
# method-shaped but are control flow rather than declarations. Filtering them
# keeps ``if (x) {}`` and friends out of the method symbol list.
_METHOD_NAME_BLACKLIST = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "default",
        "try",
        "catch",
        "finally",
        "return",
        "throw",
        "break",
        "continue",
        "new",
        "await",
        "yield",
        "typeof",
        "void",
        "delete",
        "in",
        "of",
        "as",
        "is",
        "this",
        "super",
        "function",
        "class",
        "interface",
        "type",
        "enum",
        "const",
        "let",
        "var",
        "import",
        "export",
        "from",
    }
)

# Top-level class headers: matches ``class Foo {`` and ``export class Foo<T> {``
# at column zero. The opening brace must appear on the same line; tracking
# the body across "class Foo\n{" would require multi-line state we do not
# carry. The captured ``name`` is the class identifier; the captured
# ``brace_offset`` is the position of the opening brace within the matched
# region so the body scan knows where to begin counting.
_CLASS_HEADER_RE = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"[^\n{]*"
    r"\{",
    re.MULTILINE,
)

# A method-shaped line inside a class body. Must be indented (anything other
# than column zero) so we never confuse it with a top-level declaration. The
# leading modifier soup is optional and order-tolerant; we only care that the
# name comes immediately before ``(`` or ``<`` (generic method).
_METHOD_LINE_RE = re.compile(
    r"^[ \t]+"
    r"(?:(?:public|private|protected|static|async|readonly|abstract|override)\s+)*"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*[(<]"
)

# A method-shaped arrow property: ``prop = (x) => ...`` inside a class body.
# The value side may be ``async`` and may use generics; we only need to
# anchor on ``= (`` or ``= async (`` or ``= <`` to keep false positives down.
_ARROW_PROP_RE = re.compile(
    r"^[ \t]+"
    r"(?:(?:public|private|protected|static|readonly|override)\s+)*"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*(?::\s*[^=]+)?"
    r"\s*=\s*(?:async\s+)?[(<]"
)


def _class_body_end(content: str, open_brace_index: int) -> int:
    """Return the index of the matching ``}`` for an opening ``{``.

    Pure character counting -- strings, template literals, comments, and
    regex literals are NOT skipped. The module docstring documents this
    limitation. When the input is malformed (no matching brace), the end of
    the string is returned so the body scan still terminates.
    """

    depth = 0
    i = open_brace_index
    n = len(content)
    while i < n:
        c = content[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _line_of(content: str, index: int) -> int:
    """Return the 1-indexed line number containing ``index``."""

    return content.count("\n", 0, index) + 1


def _extract_methods(content: str) -> list[Symbol]:
    """Find method-shaped declarations inside top-level class bodies.

    For each ``class Foo {`` header at column zero, locate the matching
    closing brace by simple depth counting and scan the lines between them
    for method/arrow-property patterns. Each hit is attributed to the
    enclosing class via ``parent``.
    """

    out: list[Symbol] = []
    for header in _CLASS_HEADER_RE.finditer(content):
        class_name = header.group("name")
        open_brace = content.find("{", header.start())
        if open_brace == -1 or open_brace >= header.end():
            # The opening brace lives on a continuation line; skip this
            # class because we cannot bound the body confidently.
            continue
        end_index = _class_body_end(content, open_brace)
        body = content[open_brace + 1 : end_index]
        base_line = _line_of(content, open_brace + 1)

        seen_on_line: set[int] = set()
        for body_line_offset, line in enumerate(body.split("\n")):
            line_number = base_line + body_line_offset

            method_match = _METHOD_LINE_RE.match(line)
            if method_match is not None:
                name = method_match.group("name")
                if name not in _METHOD_NAME_BLACKLIST:
                    out.append(
                        Symbol(
                            name=name,
                            kind="function",
                            start_line=line_number,
                            end_line=line_number,
                            parent=class_name,
                        )
                    )
                    seen_on_line.add(line_number)
                    continue

            arrow_match = _ARROW_PROP_RE.match(line)
            if arrow_match is not None and line_number not in seen_on_line:
                name = arrow_match.group("name")
                if name not in _METHOD_NAME_BLACKLIST:
                    out.append(
                        Symbol(
                            name=name,
                            kind="const",
                            start_line=line_number,
                            end_line=line_number,
                            parent=class_name,
                        )
                    )

    return out


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

    # v0.16: append method-level symbols (parent=class) for top-level classes.
    out.extend(_extract_methods(content))

    return out
