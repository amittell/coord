"""Tree-sitter backed C# symbol extractor.

Walks the ``compilation_unit`` node produced by ``tree-sitter-c-sharp`` and
emits one :class:`Symbol` per claimable declaration.

Unlike Go, C# nests its claimable units: types live inside namespaces and
members (methods, properties) live inside types. The extractor therefore
descends through namespace containers (which are not themselves claimable) to
reach the top-level type declarations, then descends one level into each type
body to surface its members with ``parent`` set to the enclosing type name so
``Class::method`` notation works. Nested types are handled recursively: a type
declared inside another type's body is emitted in its own right and its members
parent to the nested type name.

Recognised declarations:

- ``class_declaration`` -- ``kind='class'``
- ``interface_declaration`` -- ``kind='interface'``
- ``struct_declaration`` -- ``kind='type'`` (a struct is a value type with no
  dedicated Symbol kind, so it maps to the generic ``'type'`` grain)
- ``record_declaration`` / ``record_struct_declaration`` -- ``kind='class'``
  (records are reference-like declarations that behave as classes for
  coordination purposes)
- ``enum_declaration`` -- ``kind='enum'``
- ``method_declaration`` -- ``kind='function'``, ``parent`` = enclosing type
- ``property_declaration`` -- ``kind='function'``, ``parent`` = enclosing type.
  C# has no ``property`` Symbol kind; a property is a member-level claimable
  unit addressed the same way as a method, so it reuses ``'function'``.

Containers that are not claimable but are descended into:

- ``namespace_declaration`` (``namespace Foo { ... }``)
- ``file_scoped_namespace_declaration`` (``namespace Foo;`` with the rest of
  the file as its body)

Namespaces are never emitted as symbols; only the type and member declarations
they enclose are. Type parameters on generic declarations
(``class Container<T>``) are excluded from the captured name because the grammar
exposes the bare identifier via the ``name`` field, which precedes the
``type_parameter_list`` child.
"""

from __future__ import annotations

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_c_sharp"

# Cached parser; populated on first ``extract`` call.
_parser_csharp = None

# Type declarations and the Symbol kind they map to.
_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "struct_declaration": "type",
    "record_declaration": "class",
    "record_struct_declaration": "class",
    "enum_declaration": "enum",
}

# Member declarations inside a type body and the Symbol kind they map to.
_MEMBER_KINDS = {
    "method_declaration": "function",
    "property_declaration": "function",
}

# Container nodes that are not claimable but whose body we descend into.
_NAMESPACE_NODES = {
    "namespace_declaration",
    "file_scoped_namespace_declaration",
}


def _csharp_parser():
    """Return a cached tree-sitter C# parser instance."""

    global _parser_csharp
    if _parser_csharp is not None:
        return _parser_csharp
    import tree_sitter_c_sharp as ts_csharp
    from tree_sitter import Language, Parser

    language = Language(ts_csharp.language())
    _parser_csharp = Parser(language)
    return _parser_csharp


def _name_text(node) -> str | None:
    """Decode the ``name`` field of a node to a string, or None if missing."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8")


def _body_node(node):
    """Return the body container of a declaration, or ``None``.

    Type and namespace declarations expose their member/declaration list via the
    ``body`` field. Some grammar revisions name it ``body``; we fall back to a
    scan for the first ``declaration_list`` child when the field is absent.
    """

    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.children:
        if child.type == "declaration_list":
            return child
    return None


def _walk_type(node, out: list[Symbol]) -> None:
    """Emit a type declaration and recurse into its body for members.

    ``node`` is one of the keys in :data:`_TYPE_KINDS`. The type itself is
    emitted with ``parent=None`` (types coordinate at file grain regardless of
    the namespace that encloses them). Direct members surface with ``parent``
    set to this type's name; nested types recurse so their members parent to the
    nested type name.
    """

    name = _name_text(node)
    if name is None:
        return
    out.append(
        Symbol(
            name=name,
            kind=_TYPE_KINDS[node.type],
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
    )

    body = _body_node(node)
    if body is None:
        return
    for child in body.children:
        if child.type in _MEMBER_KINDS:
            member_name = _name_text(child)
            if member_name is None:
                continue
            out.append(
                Symbol(
                    name=member_name,
                    kind=_MEMBER_KINDS[child.type],
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    parent=name,
                )
            )
        elif child.type in _TYPE_KINDS:
            _walk_type(child, out)


def _walk_container(node, out: list[Symbol]) -> None:
    """Descend a compilation unit or namespace body, dispatching declarations.

    Type declarations are handed to :func:`_walk_type`; namespace declarations
    are descended into (but never emitted); everything else (using directives,
    attributes, global statements) is ignored.
    """

    for child in node.children:
        if child.type in _TYPE_KINDS:
            _walk_type(child, out)
        elif child.type in _NAMESPACE_NODES:
            body = _body_node(child)
            if body is not None:
                _walk_container(body, out)


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Namespaces are transparent containers: the extractor descends through them
    to reach the type declarations they hold. Each type emits one Symbol and its
    direct methods and properties emit one Symbol each with ``parent`` set to the
    enclosing type name. Nested types recurse so members parent to the nearest
    enclosing type.
    """

    parser = _csharp_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    _walk_container(root, out)
    return out
