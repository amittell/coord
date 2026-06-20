"""Tree-sitter backed Java symbol extractor.

Walks the top level of the ``program`` node produced by ``tree-sitter-java``
and emits one :class:`Symbol` per claimable declaration.

Recognised top-level declarations:

- ``class_declaration`` -- ``kind='class'``, ``parent=None``.
- ``interface_declaration`` -- ``kind='interface'``, ``parent=None``.
- ``enum_declaration`` -- ``kind='enum'``, ``parent=None``.
- ``record_declaration`` -- ``kind='class'``, ``parent=None``. A record is a
  class-shaped declaration in Java, so it coordinates at the same grain as a
  class and reuses the ``'class'`` kind (the Symbol vocabulary has no
  dedicated ``'record'`` value).

Methods and constructors:

- ``method_declaration`` and ``constructor_declaration`` are emitted with
  ``kind='function'`` and ``parent`` set to the simple name of the enclosing
  type so ``Class::method`` notation resolves. Constructors carry the class
  name as both their own name and their parent, which is the natural Java
  shape (``Foo`` constructor inside ``class Foo``).

Fields (``field_declaration``) are not claimable units of coordination and are
skipped: they hold no body to scope a sub-file claim against.

Nesting posture:

Only top-level types emit a type Symbol. A nested type (a class declared
inside another class body) does not get its own type Symbol; instead we
descend into its body so that its methods surface with ``parent`` set to the
nested type's simple name. This mirrors the Go backend's single-level parent
edge: ``parent`` always names the immediate lexical type, never a dotted path.
A claim on a method inside a nested type therefore addresses ``Inner::method``,
which the recursive ``Outer::Inner::method`` notation composes on top of.

``start_line`` / ``end_line`` are 1-indexed inclusive, taken from
``node.start_point[0] + 1`` and ``node.end_point[0] + 1``.
"""

from __future__ import annotations

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_java"

# Cached parser; populated on first ``extract`` call.
_parser_java = None

# Top-level type declaration node types and the Symbol kind each maps to.
_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "class",
}

# Member nodes inside a type body that are claimable callable units.
_METHOD_TYPES = {"method_declaration", "constructor_declaration"}


def _java_parser():
    """Return a cached tree-sitter Java parser instance."""

    global _parser_java
    if _parser_java is not None:
        return _parser_java
    import tree_sitter_java as ts_java
    from tree_sitter import Language, Parser

    language = Language(ts_java.language())
    _parser_java = Parser(language)
    return _parser_java


def _name_text(node) -> str | None:
    """Decode the ``name`` field of a node to a string, or None if missing."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8")


def _body_node(node):
    """Return the body node of a type declaration, or ``None``.

    Class, interface, enum and record declarations all expose their member
    block via the ``body`` field (``class_body`` / ``interface_body`` /
    ``enum_body``). Records reuse ``class_body``.
    """

    return node.child_by_field_name("body")


def _walk_type_body(type_node, type_name: str, out: list[Symbol]) -> None:
    """Collect methods and constructors inside ``type_node``'s body.

    Methods and constructors are emitted with ``parent=type_name``. Nested
    type declarations are not emitted as type Symbols; instead we recurse into
    their bodies so their members carry the nested type's name as ``parent``.
    The enum member block wraps its methods in an ``enum_body_declarations``
    node (after the constant list), so that wrapper is descended transparently.
    """

    body = _body_node(type_node)
    if body is None:
        return
    _scan_members(body, type_name, out)


def _scan_members(container, type_name: str, out: list[Symbol]) -> None:
    """Iterate ``container``'s children for claimable members.

    ``container`` is a type body (``class_body`` / ``interface_body`` /
    ``enum_body``) or the ``enum_body_declarations`` wrapper. Methods and
    constructors emit a Symbol; nested types recurse; the enum wrapper is
    flattened in place; everything else (fields, enum constants, comments) is
    ignored.
    """

    for child in container.children:
        if child.type in _METHOD_TYPES:
            name = _name_text(child)
            if name is None:
                continue
            out.append(
                Symbol(
                    name=name,
                    kind="function",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    parent=type_name,
                )
            )
        elif child.type in _TYPE_KINDS:
            nested_name = _name_text(child)
            if nested_name is None:
                continue
            _walk_type_body(child, nested_name, out)
        elif child.type == "enum_body_declarations":
            # Enum methods live inside this wrapper, after the constant list.
            _scan_members(child, type_name, out)
        # field_declaration, enum_constant, comments and other members are not
        # claimable units and are skipped.


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Only top-level types (direct children of ``program``) emit a type Symbol.
    Their methods and constructors -- and the methods of any nested types --
    are emitted with ``parent`` naming the immediate enclosing type so
    ``Class::method`` notation resolves.
    """

    parser = _java_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        kind = _TYPE_KINDS.get(child.type)
        if kind is None:
            # package_declaration, import_declaration, comments and stray
            # tokens are not claimable units and are ignored.
            continue
        name = _name_text(child)
        if name is None:
            continue
        out.append(
            Symbol(
                name=name,
                kind=kind,
                start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1,
            )
        )
        _walk_type_body(child, name, out)

    return out
