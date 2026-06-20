"""Tree-sitter backed Swift symbol extractor.

Walks the top level of the ``source_file`` node produced by
``tree-sitter-swift`` and emits one :class:`Symbol` per claimable declaration,
recursing into type bodies so members carry their enclosing type as ``parent``.

Recognised top-level declarations:

- ``function_declaration`` -- ``kind='function'``, ``parent=None``. Free
  functions declared at file scope.
- ``class_declaration`` -- this single grammar node represents ``class``,
  ``struct``, ``actor``, ``enum`` and ``extension`` in ``tree-sitter-swift``;
  the leading keyword leaf disambiguates them. The keyword maps to a kind:
    - ``class`` / ``actor`` -> ``kind='class'``
    - ``struct`` -> ``kind='type'`` (a Swift struct is a value type; the
      dataclass has no dedicated ``struct`` kind, so it collapses to ``type``)
    - ``enum`` -> ``kind='enum'``
    - ``extension`` -> ``kind='type'``; the name is the extended type and its
      members inherit that name as ``parent``.
- ``protocol_declaration`` -- ``kind='interface'``. Method requirements inside
  the protocol body (``protocol_function_declaration``) surface as members.
- ``property_declaration`` -- a top-level ``let`` / ``var`` binding ->
  ``kind='const'``, ``parent=None``.

Members inside a type body
--------------------------

For every type encountered (top-level or nested) the backend walks the direct
children of its body and emits:

- ``function_declaration`` / ``protocol_function_declaration`` ->
  ``Symbol(kind='function', parent=<full path>)``. Covers methods, static /
  class methods, and protocol requirements.
- ``property_declaration`` -> ``Symbol(kind='const', parent=<full path>)``,
  covering stored and computed properties.
- ``enum_entry`` -> one ``Symbol(kind='const', parent=<full path>)`` per case
  name, so an enum's cases are individually claimable.
- nested ``class_declaration`` / ``protocol_declaration`` ->
  ``Symbol`` for the nested type followed by a recursive walk into its body
  with the ancestor path extended by ``"::<inner name>"``.

The ``parent`` string is the ancestor chain joined by ``"::"``. A direct
member of a top-level type ``Foo`` has ``parent='Foo'``; a method on
``Foo.Inner`` has ``parent='Foo::Inner'``. The overlap engine matches on the
full canonical path, so arbitrary nesting depth works without schema changes.

Declarations nested inside a function body (local functions, types declared
inside a method) stay excluded -- the walk visits only the direct children of
each type body, never function bodies.
"""

from __future__ import annotations

from typing import Any

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_swift"

# Cached parser; populated on first ``extract`` call.
_parser_swift: Any = None


def _swift_parser() -> Any:
    """Return a cached tree-sitter Swift parser instance."""

    global _parser_swift
    if _parser_swift is not None:
        return _parser_swift
    import tree_sitter_swift as ts_swift
    from tree_sitter import Language, Parser

    language = Language(ts_swift.language())
    _parser_swift = Parser(language)
    return _parser_swift


# class_declaration keyword leaf -> emitted kind. ``extension`` is handled
# alongside the others but documented separately because its members parent to
# the extended type.
_KEYWORD_TO_KIND = {
    "class": "class",
    "actor": "class",
    "struct": "type",
    "enum": "enum",
    "extension": "type",
}

# Body node types that hold member declarations across the type kinds.
_BODY_TYPES = {"class_body", "enum_class_body", "protocol_body"}


def _simple_name(node: Any) -> str | None:
    """Decode the ``name`` field of a node, falling back to the first
    ``simple_identifier`` child when the field is not exposed."""

    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8")
    for child in node.children:
        if child.type == "simple_identifier":
            return child.text.decode("utf-8")
    return None


def _type_name(node: Any) -> str | None:
    """Return the declared type name for a ``class_declaration``.

    For ``class`` / ``struct`` / ``enum`` / ``actor`` the name is a
    ``type_identifier``. For ``extension`` the name is a ``user_type`` such as
    ``Array`` or ``Foo.Bar``; we keep the trailing ``type_identifier`` so an
    extension on ``Foo.Bar`` parents its members under ``Bar``.
    """

    name_node = node.child_by_field_name("name")
    if name_node is None:
        for child in node.children:
            if child.type in {"type_identifier", "user_type"}:
                name_node = child
                break
    if name_node is None:
        return None

    if name_node.type == "type_identifier":
        return name_node.text.decode("utf-8")

    if name_node.type == "user_type":
        last: Any = None
        for child in name_node.children:
            if child.type == "type_identifier":
                last = child
        if last is not None:
            return last.text.decode("utf-8")
        return name_node.text.decode("utf-8")

    return name_node.text.decode("utf-8")


def _decl_keyword(node: Any) -> str | None:
    """Return the declaration keyword leaf of a ``class_declaration``.

    ``tree-sitter-swift`` exposes ``class`` / ``struct`` / ``enum`` /
    ``extension`` / ``actor`` as anonymous token children whose ``type``
    equals the literal keyword. We match on either the node type or its text
    so the lookup survives minor grammar revisions.
    """

    for child in node.children:
        if child.type in _KEYWORD_TO_KIND:
            return child.type
        text = child.text.decode("utf-8")
        if text in _KEYWORD_TO_KIND:
            return text
    return None


def _find_body(node: Any) -> Any | None:
    """Return the body node holding a type's members, or ``None``."""

    body = node.child_by_field_name("body")
    if body is not None and body.type in _BODY_TYPES:
        return body
    for child in node.children:
        if child.type in _BODY_TYPES:
            return child
    return None


def _property_names(node: Any) -> list[str]:
    """Return the bound identifier(s) of a ``property_declaration``.

    A ``property_declaration`` binds its name through a ``pattern`` /
    ``value_binding_pattern`` whose leaf is a ``simple_identifier``. Tuple and
    destructuring patterns (rare for top-level declarations) collapse to every
    ``simple_identifier`` found directly beneath the binding pattern.
    """

    names: list[str] = []

    def _collect(n: Any, depth: int) -> None:
        if depth > 3:
            return
        for child in n.children:
            if child.type == "simple_identifier":
                names.append(child.text.decode("utf-8"))
            elif child.type in {
                "pattern",
                "value_binding_pattern",
                "tuple_pattern",
            }:
                _collect(child, depth + 1)

    _collect(node, 0)
    return names


def _enum_case_names(node: Any) -> list[str]:
    """Return the case name(s) declared by an ``enum_entry`` node."""

    names: list[str] = []
    for child in node.children:
        if child.type == "simple_identifier":
            names.append(child.text.decode("utf-8"))
    return names


def _walk_type(node: Any, keyword: str, ancestor_path: str | None) -> list[Symbol]:
    """Emit a type symbol and walk its body recursively.

    ``keyword`` is the ``class_declaration`` keyword (``class`` / ``struct`` /
    ``enum`` / ``actor`` / ``extension``) or the literal ``"protocol"`` for a
    ``protocol_declaration``. ``ancestor_path`` is the ``"::"``-joined chain
    leading UP TO (but not including) this type: ``None`` at top level,
    ``"Outer"`` for a type declared directly inside ``Outer``, and so on.

    The returned list starts with this type's own symbol followed by its
    members in source order; nested types are expanded depth-first in place.
    """

    name = _type_name(node) if keyword != "protocol" else _simple_name(node)
    if name is None:
        return []

    if keyword == "protocol":
        kind = "interface"
    else:
        kind = _KEYWORD_TO_KIND.get(keyword, "type")

    full_path = f"{ancestor_path}::{name}" if ancestor_path else name

    out: list[Symbol] = [
        Symbol(
            name=name,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            parent=ancestor_path,
        )
    ]

    body = _find_body(node)
    if body is None:
        return out

    for child in body.children:
        if child.type in {
            "function_declaration",
            "protocol_function_declaration",
        }:
            member = _simple_name(child)
            if member is not None:
                out.append(
                    Symbol(
                        name=member,
                        kind="function",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        parent=full_path,
                    )
                )
            continue

        if child.type == "property_declaration":
            for prop in _property_names(child):
                out.append(
                    Symbol(
                        name=prop,
                        kind="const",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        parent=full_path,
                    )
                )
            continue

        if child.type == "enum_entry":
            for case_name in _enum_case_names(child):
                out.append(
                    Symbol(
                        name=case_name,
                        kind="const",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        parent=full_path,
                    )
                )
            continue

        if child.type == "class_declaration":
            inner_keyword = _decl_keyword(child)
            if inner_keyword is not None:
                out.extend(_walk_type(child, inner_keyword, full_path))
            continue

        if child.type == "protocol_declaration":
            out.extend(_walk_type(child, "protocol", full_path))
            continue

        # Anything else (initializers without names, subscripts, typealiases,
        # associated types, comments) is not emitted as a claimable member in
        # v1. Declarations nested inside a method body are excluded because the
        # walk only visits direct children of this type body.

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations and nested type members as :class:`Symbol`s.

    The top-level scan covers direct children of the ``source_file`` node. For
    every type (top-level or nested inside another type) the backend walks the
    type body and emits its direct members with ``parent`` set to the full
    ancestor path. Output order follows source order: each type is immediately
    followed by its members, with nested types recursively expanded in place.
    """

    parser = _swift_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type == "function_declaration":
            name = _simple_name(child)
            if name is not None:
                out.append(
                    Symbol(
                        name=name,
                        kind="function",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
            continue

        if child.type == "class_declaration":
            keyword = _decl_keyword(child)
            if keyword is not None:
                out.extend(_walk_type(child, keyword, None))
            continue

        if child.type == "protocol_declaration":
            out.extend(_walk_type(child, "protocol", None))
            continue

        if child.type == "property_declaration":
            for prop in _property_names(child):
                out.append(
                    Symbol(
                        name=prop,
                        kind="const",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
            continue

        # Everything else (import declarations, comments, top-level
        # statements) is ignored: imports are not claimable units and bare
        # statements are not symbols.

    return out
