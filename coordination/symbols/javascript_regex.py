"""Regex-based JavaScript symbol extractor (fallback backend).

This backend is used when the ``tree-sitter-javascript`` wheel is not
installed or when the operator forces it via ``COORD_SYMBOL_PARSER=regex``.
It exists so the coord MCP wrapper can still ship symbol claims on machines
without the native tree-sitter wheels. It mirrors ``ts_regex`` minus the
TypeScript-only declaration keywords (``interface``/``type``/``enum``), which
are valid identifiers in JavaScript and therefore must not be treated as
declaration markers.

Known false-negatives (documented so callers know the trade-off):

- Anonymous arrow functions assigned through destructuring, for example
  ``const { handler = () => {} } = obj``.
- Multi-binding ``const a = 1, b = function(){}`` only catches the first
  identifier; the second binding is dropped because the regex anchors to a
  single keyword per line.
- ``export default function() {}`` with no name is normalised to
  ``name='default', kind='function'``. ``export default class {}`` follows
  the same convention.
- Comments that contain the literal text ``function`` are excluded because
  the regex anchors to the start of the line; a comment that begins with
  ``//`` will not match the keyword group.
- Nested declarations are filtered by requiring the keyword (or its
  ``export``/``async`` prefix) to start at column zero. Any indentation
  before the keyword disqualifies a line. This matches conventional
  JavaScript formatting where only top-level declarations sit at the left
  margin; nested declarations are always indented.
- Generator declarations (``function* gen() {}``) match because the ``*`` is
  consumed by the optional whitespace/star clause between the keyword and the
  name.

v0.16 added a second pass that extracts method-level symbols from inside
top-level class bodies. The class body is bounded by counting ``{`` / ``}``
characters from the opening brace on the ``class Foo {`` line. v0.19 extends
this with a stack: when an inner ``static Inner = class {`` field is
encountered inside an active class body, the inner class itself emits with
``parent=<outer path>`` and gets pushed onto the stack. Subsequent
method-shaped lines are attributed to the top-of-stack class. The stack is
popped when the brace depth dips below the depth at which the class was
pushed.

The brace counter ignores strings, regex literals, template literals, and
comments -- it is a pure character count. This is brittle by design; the goal
is a useful fallback when tree-sitter is unavailable, not a parser. Known
false-positives and false-negatives for the method pass:

- Braces inside string/template/regex literals are counted, so a method body
  containing the literal text ``"}"`` will end the class early.
- ``//`` and ``/* */`` comments containing ``{`` or ``}`` shift the counter.
- Comments containing class-header-shaped text (``// static Inner = class {``)
  are matched as if they were real headers and may push a phantom entry onto
  the nested-class stack.
- Static initialisation blocks (``static { ... }``) confuse the brace counter
  only insofar as their contents are scanned for method-shaped lines; the
  design doc rules these out as not-claimable anyway.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching across multi-line bodies for top-level declarations either.
"""

from __future__ import annotations

import re

from . import Symbol

# Keywords that mark a top-level declaration we want to expose. JavaScript has
# no interface/type/enum declarations, so the keyword set is narrower than the
# TypeScript backend.
_KIND_BY_KEYWORD = {
    "function": "function",
    "class": "class",
    "const": "const",
    "let": "const",
    "var": "const",
}

# Named declaration:
#   optional 'export' (with optional 'default'), optional 'async',
#   keyword, optional generator '*', identifier.
_NAMED_RE = re.compile(
    r"^"
    r"(?:export\s+(?:default\s+)?)?"
    r"(?:async\s+)?"
    r"(?P<keyword>function|class|const|let|var)"
    r"\s*\*?\s*"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)

# Anonymous default export: ``export default function(...)`` or
# ``export default class { ... }`` with no name. We normalise these to a
# synthetic ``default`` symbol.
_ANON_DEFAULT_RE = re.compile(
    r"^export\s+default\s+(?:async\s+)?(?P<keyword>function|class)\s*\*?\s*[(<{]",
    re.MULTILINE,
)

# Reserved words / control-flow keywords that can appear at the start of an
# indented line and look method-shaped. Filtering them keeps ``if (x) {}`` and
# friends out of the method symbol list. Unlike the TypeScript backend this
# list omits ``interface``/``type``/``enum`` because those are ordinary
# identifiers in JavaScript and may legitimately name a method.
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
        "this",
        "super",
        "function",
        "class",
        "const",
        "let",
        "var",
        "import",
        "export",
        "from",
    }
)

# Top-level class headers: matches ``class Foo {`` and ``export class Foo {``
# at column zero. The opening brace must appear on the same line; tracking the
# body across "class Foo\n{" would require multi-line state we do not carry.
# The captured ``name`` is the class identifier.
_CLASS_HEADER_RE = re.compile(
    r"^(?:export\s+(?:default\s+)?)?class\s+"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"[^\n{]*"
    r"\{",
    re.MULTILINE,
)

# Static / instance class-valued field: ``static Inner = class Inner { ... }``
# or ``static Inner = class { ... }``. The binding name on the LHS is the
# addressable identifier; the RHS class expression's own name (if present) is
# ignored. The opening brace of the class expression must live on the same
# line for the brace counter to bound the body correctly.
_FIELD_CLASS_VALUE_RE = re.compile(
    r"^[ \t]*"
    r"(?:static\s+)?"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*=\s*class(?:\s+[A-Za-z_$][A-Za-z0-9_$]*)?"
    r"[^\n{]*"
    r"\{"
)

# A method-shaped line inside a class body. Must be indented (anything other
# than column zero) so we never confuse it with a top-level declaration. The
# leading modifier soup is optional and order-tolerant; an optional generator
# ``*`` may sit between the modifiers and the name. We only care that the name
# comes immediately before ``(`` (a method) or, for generators, after ``*``.
_METHOD_LINE_RE = re.compile(
    r"^[ \t]+"
    r"(?:(?:static|async|get|set)\s+)*"
    r"\*?\s*"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*\(",
)

# A method-shaped arrow property: ``prop = (x) => ...`` inside a class body.
# The value side may be ``async`` and may use generics; we only need to anchor
# on ``= (`` or ``= async (`` or ``= <`` to keep false positives down.
_ARROW_PROP_RE = re.compile(
    r"^[ \t]+"
    r"(?:(?:static)\s+)*"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
    r"\s*=\s*(?:async\s+)?[(<]",
)


def _class_body_end(content: str, open_brace_index: int) -> int:
    """Return the index of the matching ``}`` for an opening ``{``.

    Pure character counting -- strings, template literals, comments, and regex
    literals are NOT skipped. The module docstring documents this limitation.
    When the input is malformed (no matching brace), the end of the string is
    returned so the body scan still terminates.
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
    """Find class-body members inside top-level class bodies.

    For each ``class Foo {`` header at column zero, locate the matching
    closing brace by simple depth counting and scan the lines between them for
    method/arrow-property/class-valued-field patterns. A stack tracks the
    enclosing-class chain so nested class declarations attribute their members
    to the full ``Outer::Inner`` path.

    The stack carries ``(class_path, push_depth)`` entries; an entry is popped
    when the running brace depth dips back to or below the depth at which the
    class body was entered.
    """

    out: list[Symbol] = []
    for header in _CLASS_HEADER_RE.finditer(content):
        class_name = header.group("name")
        open_brace = content.find("{", header.start())
        if open_brace == -1 or open_brace >= header.end():
            # The opening brace lives on a continuation line; skip this class
            # because we cannot bound the body confidently.
            continue
        end_index = _class_body_end(content, open_brace)
        body = content[open_brace + 1 : end_index]
        base_line = _line_of(content, open_brace + 1)

        # Stack of (class_path, depth_at_which_body_opened). The outer class's
        # body opens at depth 1 (we are inside the first ``{``). An inner
        # class body opens at the current depth + 1 when its header is
        # consumed.
        stack: list[tuple[str, int]] = [(class_name, 1)]
        depth = 1

        for body_line_offset, line in enumerate(body.split("\n")):
            line_number = base_line + body_line_offset

            # Pop any classes whose body has already closed at the current
            # depth before evaluating this line.
            while len(stack) > 1 and depth < stack[-1][1]:
                stack.pop()

            if not stack:
                # Defensive: never lose the outer entry; if depth went
                # negative we bail out of this header's scan.
                break

            current_path = stack[-1][0]

            # Field with class-expression value:
            #   ``static Inner = class Inner { ... }``.
            # The binding name on the LHS is the addressable identifier; the
            # class expression on the RHS holds the body that needs walking
            # with the binding name as the path component.
            field_class_match = _FIELD_CLASS_VALUE_RE.match(line)
            if field_class_match is not None:
                inner_name = field_class_match.group("name")
                inner_path = f"{current_path}::{inner_name}"
                out.append(
                    Symbol(
                        name=inner_name,
                        kind="class",
                        start_line=line_number,
                        end_line=line_number,
                        parent=current_path,
                    )
                )
                line_open = line.count("{")
                line_close = line.count("}")
                depth += 1  # entering the class expression body
                stack.append((inner_path, depth))
                depth += line_open - 1
                depth -= line_close
                while len(stack) > 1 and depth < stack[-1][1]:
                    stack.pop()
                continue

            # Method-shaped line: attribute to the current top-of-stack.
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
                            parent=current_path,
                        )
                    )
                    depth += line.count("{") - line.count("}")
                    while len(stack) > 1 and depth < stack[-1][1]:
                        stack.pop()
                    continue

            arrow_match = _ARROW_PROP_RE.match(line)
            if arrow_match is not None:
                name = arrow_match.group("name")
                if name not in _METHOD_NAME_BLACKLIST:
                    out.append(
                        Symbol(
                            name=name,
                            kind="const",
                            start_line=line_number,
                            end_line=line_number,
                            parent=current_path,
                        )
                    )

            # Always update depth based on braces seen on this line.
            depth += line.count("{") - line.count("}")
            while len(stack) > 1 and depth < stack[-1][1]:
                stack.pop()

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero (``re.MULTILINE`` + ``^``) so nested
    declarations under conventional indentation are excluded. ``end_line``
    equals ``start_line`` because the regex cannot track scope without
    becoming a real parser.
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
        # default exports are a syntax error in real JavaScript.
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
    # v0.19: walk extends into nested classes with full ancestor paths.
    out.extend(_extract_methods(content))

    return out
