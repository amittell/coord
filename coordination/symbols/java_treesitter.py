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

Nested types are walked recursively. A type declared inside another type body
(``class`` / ``interface`` / ``enum`` / ``record``) emits its own type Symbol
whose ``parent`` is the ``"::"``-joined path of its enclosing types
(``parent='Outer'`` for ``Outer.Inner``, ``parent='Outer::Mid'`` deeper
still). Methods inside a nested type carry ``parent`` set to the full
``"::"``-joined path of all enclosing types, so a method of ``Outer.Inner``
has ``parent='Outer::Inner'`` and resolves through the ``Outer::Inner::method``
notation. A method of a top-level type keeps ``parent='Outer'``. The overlap
engine matches on the full canonical path, so arbitrary nesting depth works
without schema changes.

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


def _walk_type_body(type_node, type_path: str, out: list[Symbol]) -> None:
    """Collect members and nested types inside ``type_node``'s body.

    ``type_path`` is the full ``"::"``-joined path of this type, INCLUDING its
    own name (``"Outer"`` for a top-level type, ``"Outer::Inner"`` for a nested
    one). Methods and constructors are emitted with ``parent=type_path``.
    Nested type declarations emit their own type Symbol with ``parent`` set to
    ``type_path`` and are then walked recursively with the path extended by the
    nested type's name. The enum member block wraps its methods in an
    ``enum_body_declarations`` node (after the constant list), so that wrapper
    is descended transparently.
    """

    body = _body_node(type_node)
    if body is None:
        return
    _scan_members(body, type_path, out)


def _scan_members(container, type_path: str, out: list[Symbol]) -> None:
    """Iterate ``container``'s children for claimable members and nested types.

    ``container`` is a type body (``class_body`` / ``interface_body`` /
    ``enum_body``) or the ``enum_body_declarations`` wrapper, and ``type_path``
    is the full ``"::"``-joined path of the enclosing type. Methods and
    constructors emit a Symbol with ``parent=type_path``; nested types emit
    their own type Symbol with ``parent=type_path`` and are then walked
    recursively under ``type_path::<nested name>``; the enum wrapper is
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
                    parent=type_path,
                )
            )
        elif child.type in _TYPE_KINDS:
            nested_name = _name_text(child)
            if nested_name is None:
                continue
            out.append(
                Symbol(
                    name=nested_name,
                    kind=_TYPE_KINDS[child.type],
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    parent=type_path,
                )
            )
            _walk_type_body(child, f"{type_path}::{nested_name}", out)
        elif child.type == "enum_body_declarations":
            # Enum methods live inside this wrapper, after the constant list.
            _scan_members(child, type_path, out)
        # field_declaration, enum_constant, comments and other members are not
        # claimable units and are skipped.


def extract(content: str) -> list[Symbol]:
    """Return top-level and nested declarations as :class:`Symbol` instances.

    Top-level types (direct children of ``program``) emit a type Symbol with
    ``parent=None``. Nested types emit their own type Symbol with ``parent``
    set to the ``"::"``-joined path of their enclosing types. Methods and
    constructors are emitted with ``parent`` set to the full ``"::"``-joined
    path of their enclosing type (``"Outer"`` for a top-level type,
    ``"Outer::Inner"`` for a nested one) so ``Outer::Inner::method`` notation
    resolves. Output order follows source order, with each nested type
    immediately followed by its own members.
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
