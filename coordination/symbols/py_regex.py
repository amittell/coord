"""Regex-based Python symbol extractor (fallback backend).

This backend is used when tree-sitter is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so the coord MCP
wrapper can still ship symbol claims on machines without the native
tree-sitter wheels.

Top-level pass
--------------

Python is whitespace-significant, so the regex anchors every match to column
zero. That single rule does the heavy lifting of excluding nested
definitions: any ``def`` or ``class`` inside another block must be indented,
so it cannot match the column-zero anchor.

v0.16 method pass
-----------------

After the top-level pass the backend runs a second, indentation-aware pass
over each top-level ``class`` slice. The slice runs from the class header
line to the next column-zero declaration (or end of file). Inside the slice
the body indent is inferred as the smallest non-zero leading whitespace
length seen on a non-blank, non-comment line. Indented ``def`` / ``async
def`` and ``NAME = lambda ...`` lines whose indent length equals the
inferred body indent are emitted as method symbols with ``parent`` set to
the enclosing class name. Lines indented deeper than the body indent are
treated as nested (closures inside a method, defs inside a conditional
block, methods of a nested class) and are excluded.

Known false-negatives (documented so callers know the trade-off):

- Decorators are ignored when looking for top-level names. ``@property`` on
  the previous line does not push the ``def`` off column zero, so the
  underlying definition is still captured. The decorator stack is not
  included in the captured line span, but the name and kind are correct.
- For class methods, decorators on the line above shift the captured
  ``start_line`` to the ``def`` line, not the decorator line. The
  tree-sitter backend produces the more accurate span.
- Multi-line function signatures (``def f(\n    x,\n    y,\n):``) are still
  caught because the regex only inspects the opening line where ``def``
  lives. ``end_line`` is approximated as ``start_line`` -- it does not
  follow the signature into subsequent lines.
- Top-level ``def`` or ``class`` inside an ``if __name__ == '__main__':``
  block is excluded because it is indented (column-zero anchor).
- Top-level ``NAME = lambda ...`` becomes a ``const`` symbol. Other
  callable-producing assignments (``NAME = some_factory()``, comprehensions,
  partials) are not captured -- the regex cannot afford to evaluate the RHS
  type without becoming a real parser.
- A ``def`` keyword inside a triple-quoted string that happens to begin at
  column zero will be mis-matched as a symbol. This is rare in practice and
  not worth a context-tracking parser to defend against.
- Body-indent detection is character-count based, so PEP 8 violations that
  mix tabs and spaces within the same class body can be miscounted: a tab
  is treated as a single character, not as a logical 4 or 8 column step.
  Conventionally indented bodies (all four-space or all single-tab) work
  correctly.
- Nested ``class`` definitions inside a class body are skipped entirely.
  The two-level ``parent`` model has no slot for ``Outer::Inner::method``;
  the tree-sitter backend applies the same rule.

The regex backend approximates ``end_line`` as ``start_line`` because it
cannot track indentation-based scope without becoming a full parser. The
tree-sitter backend is the primary correctness path; this fallback is
intentionally imprecise.
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

# Indented ``def`` / ``async def`` for the class-body pass. The indent is
# captured so the caller can compare its length against the inferred body
# indent and reject deeper-nested defs.
_INDENTED_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?:async\s+)?def\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

# Indented ``NAME = lambda ...`` for the class-body pass. Same indent
# bookkeeping as the indented def regex above.
_INDENTED_LAMBDA_RE = re.compile(
    r"^(?P<indent>[ \t]+)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*lambda\b",
    re.MULTILINE,
)


_KEYWORD_TO_KIND = {
    "def": "function",
    "class": "class",
}


def _infer_body_indent(slice_text: str) -> int:
    """Return the smallest non-zero indent (in characters) inside ``slice_text``.

    Blank lines and comment-only lines do not contribute. Returns ``0`` when
    the slice has no indented content (e.g. a single-line ``class X: pass``
    or a class whose body is entirely on the header line).
    """

    smallest = 0
    for raw_line in slice_text.split("\n"):
        stripped = raw_line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        indent_len = len(raw_line) - len(stripped)
        if indent_len == 0:
            continue
        if smallest == 0 or indent_len < smallest:
            smallest = indent_len
    return smallest


def _method_matches_in_slice(
    content: str,
    slice_start: int,
    slice_end: int,
    class_name: str,
) -> list[Symbol]:
    """Scan ``content[slice_start:slice_end]`` for class-body methods.

    The slice should run from the class header offset to the next top-level
    declaration (or end of file). Indented defs and lambda assignments whose
    indent length matches the inferred body indent are emitted as method
    symbols tagged with ``parent=class_name``. Anything deeper-indented is
    treated as nested (closure / nested-class member / conditional body)
    and skipped.
    """

    slice_text = content[slice_start:slice_end]
    body_indent = _infer_body_indent(slice_text)
    if body_indent == 0:
        return []

    out: list[Symbol] = []

    for match in _INDENTED_DEF_RE.finditer(slice_text):
        if len(match.group("indent")) != body_indent:
            continue
        name = match.group("name")
        absolute_offset = slice_start + match.start()
        line = content.count("\n", 0, absolute_offset) + 1
        out.append(
            Symbol(
                name=name,
                kind="function",
                start_line=line,
                end_line=line,
                parent=class_name,
            )
        )

    for match in _INDENTED_LAMBDA_RE.finditer(slice_text):
        if len(match.group("indent")) != body_indent:
            continue
        name = match.group("name")
        absolute_offset = slice_start + match.start()
        line = content.count("\n", 0, absolute_offset) + 1
        out.append(
            Symbol(
                name=name,
                kind="const",
                start_line=line,
                end_line=line,
                parent=class_name,
            )
        )

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations and class methods found by the regex scan.

    Top-level matches are anchored to column zero so any indented definition
    outside a class body (nested defs, ``if``-guarded blocks) is
    automatically excluded. For each top-level ``class`` the backend runs an
    indentation-aware secondary pass to surface methods with ``parent``
    pointing at the class. ``end_line`` equals ``start_line`` throughout
    because the regex cannot track indentation scope without becoming a
    real parser.
    """

    out: list[Symbol] = []

    # Top-level (column-zero) anchors. Build a unified list so we can
    # compute class-body slices later: the end of one slice is the start of
    # the next column-zero anchor (or end of file).
    top_level_starts: list[int] = []
    class_anchors: list[tuple[int, str]] = []

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
        top_level_starts.append(match.start())
        if keyword == "class":
            class_anchors.append((match.start(), name))

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
        top_level_starts.append(match.start())

    # Method pass: for each top-level class, slice from its header to the
    # next top-level anchor (or EOF) and scan for indented defs / lambdas.
    top_level_starts.sort()
    for class_start, class_name in class_anchors:
        # Slice end: smallest top-level start strictly greater than the
        # class header offset, or end of content.
        slice_end = len(content)
        for start in top_level_starts:
            if start > class_start and start < slice_end:
                slice_end = start
        out.extend(
            _method_matches_in_slice(content, class_start, slice_end, class_name)
        )

    # Stable order: by start_line, then by appearance within a line. The
    # multiple passes above are independent, so without sorting a lambda at
    # line 2 could land after a def at line 10 and a class method could
    # land before its owning class. Sorting keeps the output deterministic
    # and matches the tree-sitter backend's "class then its methods" walk
    # order: the class header always has a lower start_line than its
    # methods, so they fall into place naturally.
    out.sort(key=lambda s: s.start_line)
    return out
