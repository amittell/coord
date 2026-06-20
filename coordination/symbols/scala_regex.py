"""Regex-based Scala symbol extractor (fallback backend).

Used when the ``tree-sitter-scala`` wheel is not installed or when the operator
forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp can still
ship symbol claims on machines without the native tree-sitter wheels.

Each pattern is anchored to column zero (``re.MULTILINE`` + ``^``) with an
optional run of leading modifiers (``private``, ``final``, ``sealed`` and the
like). Conventional Scala formatting indents the members of a class / object /
trait body, so column-zero anchoring naturally restricts matches to top-level
declarations.

Known false-negatives (documented so callers know the trade-off):

- Methods inside a ``class`` / ``object`` / ``trait`` body are indented and are
  therefore not matched. The regex backend emits no method symbols and no
  ``parent`` edges; install the ``symbols`` extra to get the tree-sitter
  backend, which walks container bodies and records ``Class::method`` parents.

- Multi-line lambda bindings. A ``val`` / ``var`` only registers as a callable
  ``const`` when the ``=>`` of the function literal sits on the same line as
  the binding. ``val f =\n  (x: Int) => x`` is missed. Real code rarely splits
  a binding this way, and the tree-sitter backend handles it correctly.

- Plain-data ``val`` / ``var`` bindings (``val n = 42``) are deliberately not
  emitted. Only bindings whose right-hand side contains a function arrow
  (``=>``) are treated as claimable callables, matching the tree-sitter
  backend's lambda heuristic. This keeps the regex backend from flooding the
  result with every constant in the file.

- Type parameters in a name (``class Container[T]``) are excluded from the
  captured name because the ``\\w+`` only consumes the bare identifier; the
  ``[T]`` clause is left untouched.

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses the
``(file_path, name)`` pair, so this approximation does not affect the
coordination contract.
"""

from __future__ import annotations

import re

from . import Symbol

# Optional leading modifier run shared by every top-level pattern. Covers the
# Scala soft / hard modifiers that may precede a declaration keyword.
_MODS = (
    r"(?:(?:private|protected|final|sealed|abstract|implicit|lazy|override|"
    r"case)\s+)*"
)

# [mods] def name
_DEF_RE = re.compile(
    r"^" + _MODS + r"def\s+(?P<name>\w+)",
    re.MULTILINE,
)

# [mods] (case )?class Name
_CLASS_RE = re.compile(
    r"^" + _MODS + r"class\s+(?P<name>\w+)",
    re.MULTILINE,
)

# [mods] object Name
_OBJECT_RE = re.compile(
    r"^" + _MODS + r"object\s+(?P<name>\w+)",
    re.MULTILINE,
)

# [mods] trait Name
_TRAIT_RE = re.compile(
    r"^" + _MODS + r"trait\s+(?P<name>\w+)",
    re.MULTILINE,
)

# [mods] (val|var) name [: Type] = <... => ...>
# Only matches when a function arrow appears on the binding line, so plain-data
# bindings are skipped. The ``[^\n]*=>`` tail enforces the function literal.
_VAL_VAR_FUNC_RE = re.compile(
    r"^" + _MODS + r"(?:val|var)\s+(?P<name>\w+)\s*(?::[^=\n]+)?=[^\n]*=>",
    re.MULTILINE,
)


def _line_of(content: str, offset: int) -> int:
    """1-indexed line number containing the byte at ``offset``."""

    return content.count("\n", 0, offset) + 1


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero (``re.MULTILINE`` + ``^``) so indented
    container members are excluded. Each match emits exactly one Symbol;
    ``end_line`` equals ``start_line``.

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

    for match in _DEF_RE.finditer(content):
        _record(match.group("name"), "function", match.start())

    for match in _CLASS_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _OBJECT_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _TRAIT_RE.finditer(content):
        _record(match.group("name"), "interface", match.start())

    for match in _VAL_VAR_FUNC_RE.finditer(content):
        _record(match.group("name"), "const", match.start())

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
