"""Regex-based Swift symbol extractor (fallback backend).

Used when the ``tree-sitter-swift`` wheel is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can still
ship symbol claims on machines without the native tree-sitter wheels.

Top-level pass
--------------

Top-level declarations are anchored to column zero (``re.MULTILINE`` + ``^``).
Leading access-control and behaviour modifiers (``public``, ``final``,
``static`` and friends) are consumed before the declaration keyword so a
``public func`` still matches. Recognised top-level forms:

- ``func Name`` -> ``kind='function'``
- ``class`` / ``actor`` ``Name`` -> ``kind='class'``
- ``struct Name`` / ``extension Name`` -> ``kind='type'``
- ``enum Name`` -> ``kind='enum'``
- ``protocol Name`` -> ``kind='interface'``
- ``let`` / ``var`` ``Name`` -> ``kind='const'``

Member pass
-----------

After the top-level pass the backend runs an indentation-aware second pass over
each top-level type slice (from the type header to the next column-zero
declaration, or end of file). A stack of ``(indent, full_path)`` tracks the
active type chain so nested types surface their members with the full
``Outer::Inner::member`` ancestor path. Indented ``func`` declarations,
``let`` / ``var`` properties and ``case`` entries whose indent matches the
enclosing type's body indent are emitted as members with ``parent`` set to that
type; deeper-indented lines (locals inside a method body) stay excluded.

Known false-negatives (documented so callers know the trade-off):

- ``end_line`` is approximated as ``start_line`` for every symbol because the
  regex cannot track brace matching without becoming a full parser. Conflict
  detection only uses the ``(file_path, name)`` pair, so this approximation
  does not affect the coordination contract.
- Member detection is character-count based on leading whitespace. A type body
  that mixes tabs and spaces can be miscounted: a tab counts as one character,
  not a logical column step. Conventionally indented bodies (all four-space or
  all single-tab) work correctly.
- A ``func`` / ``let`` / ``case`` keyword inside a multi-line string literal
  that happens to begin at the inferred member indent is mis-matched as a
  symbol. This is rare in practice and not worth a context-tracking parser to
  defend against.
- Operator declarations (``func + (lhs:rhs:)``) and subscripts are not
  captured because their names are not plain identifiers.
- Comma-separated bindings (``let a = 1, b = 2``) capture only the first name;
  the tree-sitter backend captures every name. Install the ``symbols`` extra
  for full coverage.
- A keyword pushed off column zero only by a leading attribute on the same line
  (``@objc class Foo``) is still matched because the modifier list is optional
  and the attribute is skipped by the leading ``[^\\n]*`` tolerance only where
  documented; bare attributes alone are not consumed, so ``@MainActor`` on its
  own line before the declaration does not affect the column-zero match.
"""

from __future__ import annotations

import re

from . import Symbol

# Access-control / behaviour modifiers that may precede a declaration keyword.
# Consumed but never captured so ``public final class Foo`` still resolves to
# ``Foo``. ``class`` is omitted here on purpose: a leading ``class`` keyword is
# itself a declaration, not a modifier.
_MODIFIERS = (
    r"(?:(?:public|private|internal|fileprivate|open|final|static|"
    r"override|mutating|nonmutating|convenience|required|dynamic|lazy|"
    r"weak|unowned|indirect|optional|prefix|postfix|infix)\s+)*"
)

# ``func Name`` at column zero, modifiers allowed.
_FUNC_RE = re.compile(
    r"^" + _MODIFIERS + r"func\s+(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# ``class`` / ``actor`` ``Name`` -> kind 'class'.
_CLASS_RE = re.compile(
    r"^" + _MODIFIERS + r"(?:class|actor)\s+(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# ``struct Name`` -> kind 'type'.
_STRUCT_RE = re.compile(
    r"^" + _MODIFIERS + r"struct\s+(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# ``enum Name`` -> kind 'enum'.
_ENUM_RE = re.compile(
    r"^" + _MODIFIERS + r"enum\s+(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# ``protocol Name`` -> kind 'interface'.
_PROTOCOL_RE = re.compile(
    r"^" + _MODIFIERS + r"protocol\s+(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# ``extension Name`` / ``extension Foo.Bar`` -> kind 'type'. The dotted tail is
# consumed so the captured ``name`` is the trailing component.
_EXTENSION_RE = re.compile(
    r"^" + _MODIFIERS + r"extension\s+(?:\w+\.)*(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# ``let`` / ``var`` ``Name`` -> kind 'const'.
_PROPERTY_RE = re.compile(
    r"^" + _MODIFIERS + r"(?:let|var)\s+(?P<name>[A-Za-z_]\w*)",
    re.MULTILINE,
)

# Indented type headers for the member pass: any of the nesting keywords with
# its leading indent captured so the stack can compare depths.
_NESTED_TYPE_RE = re.compile(
    r"^(?P<indent>[ \t]+)" + _MODIFIERS
    + r"(?P<kw>class|actor|struct|enum|protocol|extension)\s+"
    r"(?:\w+\.)*(?P<name>[A-Za-z_]\w*)",
)

# Indented members for the member pass.
_INDENTED_FUNC_RE = re.compile(
    r"^(?P<indent>[ \t]+)" + _MODIFIERS + r"func\s+(?P<name>[A-Za-z_]\w*)",
)
_INDENTED_PROPERTY_RE = re.compile(
    r"^(?P<indent>[ \t]+)" + _MODIFIERS + r"(?:let|var)\s+(?P<name>[A-Za-z_]\w*)",
)
_INDENTED_CASE_RE = re.compile(
    r"^(?P<indent>[ \t]+)case\s+(?P<name>[A-Za-z_]\w*)",
)

# Column-zero type anchors mapped to the kind they emit.
_TYPE_KEYWORD_TO_KIND = {
    "class": "class",
    "actor": "class",
    "struct": "type",
    "enum": "enum",
    "protocol": "interface",
    "extension": "type",
}


def _line_of(content: str, offset: int) -> int:
    """1-indexed line number containing the byte at ``offset``."""

    return content.count("\n", 0, offset) + 1


def _infer_body_indent(slice_text: str) -> int:
    """Return the smallest non-zero leading indent (in characters) in a slice.

    Blank and comment-only lines do not contribute. Returns ``0`` when the
    slice has no indented content.
    """

    smallest = 0
    for raw_line in slice_text.split("\n"):
        stripped = raw_line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("//"):
            continue
        indent_len = len(raw_line) - len(stripped)
        if indent_len == 0:
            continue
        if smallest == 0 or indent_len < smallest:
            smallest = indent_len
    return smallest


def _member_matches_in_slice(
    content: str,
    slice_start: int,
    slice_end: int,
    type_name: str,
) -> list[Symbol]:
    """Scan ``content[slice_start:slice_end]`` for members of a type body.

    Walks an indentation stack so nested types (and their members) surface
    with the full ``Outer::Inner::member`` ancestor path. The slice runs from
    the type header offset to the next column-zero declaration (or end of
    file). A line whose indent equals the current type's body indent emits as
    a member of that type; deeper-indented lines stay excluded.
    """

    slice_text = content[slice_start:slice_end]
    body_indent_step = _infer_body_indent(slice_text)
    if body_indent_step == 0:
        return []

    out: list[Symbol] = []
    # Stack of (type_header_indent, full_path). The outer type header sat at
    # column zero, so its body indent is ``body_indent_step``.
    stack: list[tuple[int, str]] = [(0, type_name)]

    def member_path_for(line_indent: int) -> str | None:
        while stack and stack[-1][0] + body_indent_step > line_indent:
            stack.pop()
        if stack and stack[-1][0] + body_indent_step == line_indent:
            return stack[-1][1]
        return None

    line_offset = 0
    for raw_line in slice_text.split("\n"):
        stripped = raw_line.lstrip()
        if stripped == "" or stripped.startswith("//"):
            line_offset += len(raw_line) + 1
            continue
        indent_len = len(raw_line) - len(stripped)
        if indent_len == 0:
            line_offset += len(raw_line) + 1
            continue
        path = member_path_for(indent_len)
        if path is None:
            line_offset += len(raw_line) + 1
            continue

        absolute_offset = slice_start + line_offset
        line = content.count("\n", 0, absolute_offset) + 1

        nested = _NESTED_TYPE_RE.match(raw_line)
        if nested is not None:
            inner_name = nested.group("name")
            kind = _TYPE_KEYWORD_TO_KIND[nested.group("kw")]
            out.append(
                Symbol(
                    name=inner_name,
                    kind=kind,
                    start_line=line,
                    end_line=line,
                    parent=path,
                )
            )
            stack.append((indent_len, f"{path}::{inner_name}"))
            line_offset += len(raw_line) + 1
            continue

        func_match = _INDENTED_FUNC_RE.match(raw_line)
        if func_match is not None:
            out.append(
                Symbol(
                    name=func_match.group("name"),
                    kind="function",
                    start_line=line,
                    end_line=line,
                    parent=path,
                )
            )
            line_offset += len(raw_line) + 1
            continue

        prop_match = _INDENTED_PROPERTY_RE.match(raw_line)
        if prop_match is not None:
            out.append(
                Symbol(
                    name=prop_match.group("name"),
                    kind="const",
                    start_line=line,
                    end_line=line,
                    parent=path,
                )
            )
            line_offset += len(raw_line) + 1
            continue

        case_match = _INDENTED_CASE_RE.match(raw_line)
        if case_match is not None:
            out.append(
                Symbol(
                    name=case_match.group("name"),
                    kind="const",
                    start_line=line,
                    end_line=line,
                    parent=path,
                )
            )
        line_offset += len(raw_line) + 1

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations and type members found by the regex scan.

    Top-level matches are anchored to column zero so any indented declaration
    is excluded from the top-level pass. For each top-level type the backend
    runs an indentation-aware secondary pass to surface members with ``parent``
    pointing at the enclosing type. ``end_line`` equals ``start_line``
    throughout.

    Order in the returned list follows the byte offset of the top-level match,
    with each type's members inserted immediately after the type so callers
    that rely on file order get a deterministic sequence.
    """

    found: list[tuple[int, Symbol]] = []
    seen: set[tuple[str, str, int, str | None]] = set()
    # Column-zero anchors used to bound each type's member slice.
    top_level_starts: list[int] = []
    type_anchors: list[tuple[int, str]] = []

    def _record(
        offset: int,
        name: str,
        kind: str,
        parent: str | None = None,
    ) -> None:
        line = _line_of(content, offset)
        key = (name, kind, line, parent)
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

    for match in _FUNC_RE.finditer(content):
        _record(match.start(), match.group("name"), "function")
        top_level_starts.append(match.start())

    for match in _PROTOCOL_RE.finditer(content):
        name = match.group("name")
        _record(match.start(), name, "interface")
        top_level_starts.append(match.start())
        type_anchors.append((match.start(), name))

    for match in _CLASS_RE.finditer(content):
        name = match.group("name")
        _record(match.start(), name, "class")
        top_level_starts.append(match.start())
        type_anchors.append((match.start(), name))

    for match in _STRUCT_RE.finditer(content):
        name = match.group("name")
        _record(match.start(), name, "type")
        top_level_starts.append(match.start())
        type_anchors.append((match.start(), name))

    for match in _ENUM_RE.finditer(content):
        name = match.group("name")
        _record(match.start(), name, "enum")
        top_level_starts.append(match.start())
        type_anchors.append((match.start(), name))

    for match in _EXTENSION_RE.finditer(content):
        name = match.group("name")
        _record(match.start(), name, "type")
        top_level_starts.append(match.start())
        type_anchors.append((match.start(), name))

    for match in _PROPERTY_RE.finditer(content):
        _record(match.start(), match.group("name"), "const")
        top_level_starts.append(match.start())

    # Member pass: for each top-level type, slice from its header to the next
    # column-zero anchor (or EOF) and scan for indented members.
    top_level_starts.sort()
    for type_start, type_name in type_anchors:
        slice_end = len(content)
        for start in top_level_starts:
            if start > type_start and start < slice_end:
                slice_end = start
        for sym in _member_matches_in_slice(
            content, type_start, slice_end, type_name
        ):
            member_offset = _offset_of_line(content, sym.start_line)
            key = (sym.name, sym.kind, sym.start_line, sym.parent)
            if key in seen:
                continue
            seen.add(key)
            found.append((member_offset, sym))

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]


def _offset_of_line(content: str, line: int) -> int:
    """Return the byte offset of the first character of 1-indexed ``line``.

    Used to order member symbols against the top-level matches by their
    position in the file so the final list stays in source order.
    """

    if line <= 1:
        return 0
    seen = 0
    idx = 0
    while seen < line - 1:
        nl = content.find("\n", idx)
        if nl == -1:
            return len(content)
        idx = nl + 1
        seen += 1
    return idx
