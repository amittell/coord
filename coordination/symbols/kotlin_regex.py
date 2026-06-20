"""Regex-based Kotlin symbol extractor (fallback backend).

Used when the ``tree-sitter-kotlin`` wheel is not installed or when the
operator forces it via ``COORD_SYMBOL_PARSER=regex``. It exists so coord-mcp
can still ship symbol claims on machines without the native tree-sitter wheels.

Each pattern is anchored to column zero (``re.MULTILINE`` + ``^``) so only
file-scope declarations match; conventional Kotlin formatting indents members
inside a class body, which keeps them out of the result.

Known false-negatives (documented so callers know the trade-off):

- Methods inside a class / interface / object body. They are indented, so the
  column-zero anchoring drops them, and the regex backend therefore never sets
  ``parent``. The tree-sitter backend descends one level and attaches the
  enclosing type as ``parent``; install the ``symbols`` extra for that.

- Companion objects (``companion object { ... }``) live inside a class body and
  are indented, so they are likewise missed here.

- Multi-line declarations where the keyword and the name sit on separate lines.
  Real Kotlin never formats ``class``\n``Foo`` this way, but a formatter could.

- Destructuring properties (``val (a, b) = pair``) match the opening ``val`` but
  the captured name is the literal ``(`` group head and is discarded; only
  plain ``val name`` / ``const val name`` / ``var name`` bindings are emitted.

- Annotations or visibility modifiers on their own line before the declaration
  are non-matches; the declaration keyword must lead its own line (after
  optional ``data`` / ``sealed`` / ``abstract`` / ``open`` / ``enum`` /
  ``inner`` / ``value`` modifiers, which the patterns allow inline).

``end_line`` is approximated as ``start_line`` because the regex cannot track
brace matching without becoming a full parser. Conflict detection only uses the
``(file_path, name)`` pair, so this approximation does not affect the
coordination contract.
"""

from __future__ import annotations

import re

from . import Symbol

# Optional leading class/object modifiers that may precede the keyword. ``enum``
# is captured separately by the enum pattern below; the others reduce to a plain
# class. Kept as a shared fragment so every keyword pattern allows the same set.
_CLASS_MODS = r"(?:(?:public|private|internal|protected|abstract|final|open|sealed|data|inner|value|annotation)\s+)*"

# fun [<T>] Name (
# Leading modifiers (suspend, inline, operator, infix, override, visibility) are
# tolerated. Only column-zero (file-scope) functions match; members are indented.
_FUNC_RE = re.compile(
    r"^(?:(?:public|private|internal|protected|suspend|inline|operator|infix|override|open|final|external|tailrec)\s+)*"
    r"fun\s+"
    r"(?:<[^>]*>\s*)?"
    r"(?P<name>[A-Za-z_]\w*)\s*[\(<]",
    re.MULTILINE,
)

# [mods] enum class Name -> kind 'enum'. Run before the generic class pattern so
# an enum class is not downgraded to a plain class.
_ENUM_RE = re.compile(
    r"^" + _CLASS_MODS + r"enum\s+class\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

# [mods] interface Name -> kind 'interface'.
_INTERFACE_RE = re.compile(
    r"^" + _CLASS_MODS + r"(?:fun\s+)?interface\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

# [mods] class Name -> kind 'class'. The enum pattern has already consumed
# ``enum class`` lines; we skip any line the enum scan claimed.
_CLASS_RE = re.compile(
    r"^" + _CLASS_MODS + r"class\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

# [mods] object Name -> kind 'class' (a named singleton).
_OBJECT_RE = re.compile(
    r"^(?:(?:public|private|internal|protected)\s+)*object\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)

# [mods] (val | var | const val) Name -> kind 'const'. Destructuring forms
# (``val (a, b) = ...``) do not match because the name group requires an
# identifier immediately after the keyword.
_PROPERTY_RE = re.compile(
    r"^(?:(?:public|private|internal|protected|const|lateinit|open|override|final)\s+)*"
    r"(?:val|var)\s+(?P<name>[A-Za-z_]\w*)\b",
    re.MULTILINE,
)


def _line_of(content: str, offset: int) -> int:
    """1-indexed line number containing the byte at ``offset``."""

    return content.count("\n", 0, offset) + 1


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations found by the regex scan.

    Matches are anchored to column zero (``re.MULTILINE`` + ``^``) so members
    under conventional indentation are excluded. Each match emits exactly one
    Symbol; ``end_line`` equals ``start_line``.

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

    for match in _ENUM_RE.finditer(content):
        _record(match.group("name"), "enum", match.start())

    for match in _INTERFACE_RE.finditer(content):
        _record(match.group("name"), "interface", match.start())

    for match in _CLASS_RE.finditer(content):
        name = match.group("name")
        line = _line_of(content, match.start())
        # Skip if the enum scan already claimed this declaration on the same
        # line so ``enum class Foo`` does not emit both ``enum`` and ``class``.
        if (name, "enum", line) in seen:
            continue
        _record(name, "class", match.start())

    for match in _OBJECT_RE.finditer(content):
        _record(match.group("name"), "class", match.start())

    for match in _PROPERTY_RE.finditer(content):
        _record(match.group("name"), "const", match.start())

    found.sort(key=lambda pair: pair[0])
    return [sym for _, sym in found]
