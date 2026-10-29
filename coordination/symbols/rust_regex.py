"""Regex-based Rust symbol extractor (fallback backend).

The fallback recognizes declarations at file scope and inside inline modules,
including cfg-gated modules. Module paths qualify their declarations; impl
methods also include their implementing type. Function, trait and macro bodies
are not declaration scopes. Modules themselves are not emitted as symbols.

This is a conservative, line-oriented fallback, not a Rust parser. Declaration
keywords and names, and module/impl headers and opening braces, must share a
line. Multiple declarations on one line and macro-generated items are omitted.
Strings and comments are masked before counting braces so they cannot change a
namespace. Install the ``symbols`` extra for tree-sitter's full syntax support.
Raw function names retain the fallback's existing spelling without ``r#``;
module paths preserve that prefix. ``end_line`` is approximated as ``start_line``;
claim matching uses names/parents.
"""

from __future__ import annotations

import re

from . import Symbol

_VIS = r"(?:pub(?:\s*\([^)]*\))?\s+)?"
_FN_MODS = r"(?:(?:const|async|unsafe|extern)\s+)*"
_FUNC_RE = re.compile(r"^" + _VIS + _FN_MODS + r"fn\s+(?:r#)?(?P<name>\w+)\s*[\(<]")
_DECLARATIONS = (
    (_FUNC_RE, "function"),
    (re.compile(r"^" + _VIS + r"(?:struct|union)\s+(?P<name>\w+)"), "type"),
    (re.compile(r"^" + _VIS + r"enum\s+(?P<name>\w+)"), "enum"),
    (re.compile(r"^" + _VIS + r"(?:unsafe\s+)?trait\s+(?P<name>\w+)"), "interface"),
    (re.compile(r"^" + _VIS + r"type\s+(?P<name>\w+)"), "type"),
    (re.compile(r"^" + _VIS + r"(?:const|static)\s+(?:mut\s+)?(?P<name>\w+)\s*:"), "const"),
)
_MODULE_RE = re.compile(r"^" + _VIS + r"mod\s+(?P<name>(?:r#)?\w+)\s*\{")
_IMPL_HEADER_RE = re.compile(
    r"^impl\b[^\n{]*?(?:\bfor\s+)?(?P<type>\w+)(?:<[^\n{]*>)?\s*\{"
)
_RAW_STRING_RE = re.compile(r'(?:br|cr|r)(#{0,255})"')
_CHAR_RE = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|[^\n])|[^'\\\n])'")


def _code_only(content: str) -> str:
    """Mask comments/literals, retaining newlines and offsets (not lifetimes)."""
    chars = list(content)
    index = 0
    size = len(content)
    while index < size:
        start = index
        if content.startswith("//", index):
            end = content.find("\n", index)
            index = size if end < 0 else end
        elif content.startswith("/*", index):
            index += 2
            nesting = 1
            while index < size and nesting:
                if content.startswith("/*", index):
                    nesting += 1
                    index += 2
                elif content.startswith("*/", index):
                    nesting -= 1
                    index += 2
                else:
                    index += 1
        elif (
            (index == 0 or not (content[index - 1].isalnum() or content[index - 1] == "_"))
            and (raw := _RAW_STRING_RE.match(content, index))
        ):
            terminator = '"' + raw.group(1)
            end = content.find(terminator, raw.end())
            index = size if end < 0 else end + len(terminator)
        elif content[index] == '"':
            index += 1
            while index < size:
                if content[index] == "\\":
                    index = min(size, index + 2)
                elif content[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
        elif char := _CHAR_RE.match(content, index):
            index = char.end()
        else:
            index += 1
            continue
        for position in range(start, index):
            if chars[position] != "\n":
                chars[position] = " "
    return "".join(chars)


def extract(content: str) -> list[Symbol]:
    """Return declarations in source order with qualified module/impl parents."""
    found: list[Symbol] = []
    # Each scope is (body depth, qualified parent, is_impl).
    scopes: list[tuple[int, str, bool]] = []
    depth = 0
    for line_number, line in enumerate(_code_only(content).splitlines(), 1):
        stripped = line.lstrip()
        scope = scopes[-1] if scopes else (0, "", False)
        if depth == scope[0]:
            parent = scope[1] or None
            patterns = ((_FUNC_RE, "function"),) if scope[2] else _DECLARATIONS
            for pattern, kind in patterns:
                if match := pattern.match(stripped):
                    found.append(Symbol(name=match.group("name"), kind=kind,
                                        start_line=line_number, end_line=line_number,
                                        parent=parent))
                    break
            if not scope[2]:
                module = _MODULE_RE.match(stripped)
                impl = _IMPL_HEADER_RE.match(stripped)
                if module or impl:
                    if module is not None:
                        name = module.group("name")
                    else:
                        assert impl is not None
                        name = impl.group("type")
                    qualified = f"{parent}::{name}" if parent else name
                    scopes.append((depth + 1, qualified, impl is not None))
        depth = max(0, depth + line.count("{") - line.count("}"))
        while scopes and depth < scopes[-1][0]:
            scopes.pop()
    return found
