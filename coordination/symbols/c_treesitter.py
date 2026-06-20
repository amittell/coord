"""Tree-sitter backed C symbol extractor.

Walks the top level of the ``translation_unit`` node produced by
``tree-sitter-c`` and emits one :class:`Symbol` per claimable declaration.

Recognised top-level declarations:

- ``function_definition`` -- ``kind='function'``, ``parent=None``. The function
  name lives at the bottom of the declarator chain: a definition such as
  ``int *make(void) { ... }`` nests the ``function_declarator`` inside a
  ``pointer_declarator``, so we descend through ``pointer_declarator`` /
  ``parenthesized_declarator`` / ``array_declarator`` wrappers until we reach
  the ``function_declarator`` and read its identifier.
- ``struct_specifier`` / ``union_specifier`` with a ``name`` and a body
  (``field_declaration_list``) -> ``kind='type'``. The body requirement keeps
  references and forward declarations (``struct Foo x;``, ``struct Foo;``) from
  being mistaken for definitions; only the defining occurrence is a claimable
  unit.
- ``enum_specifier`` with a ``name`` and a body (``enumerator_list``) ->
  ``kind='enum'``.
- ``type_definition`` (``typedef ...``) -> ``kind='type'``. The declared name is
  the trailing declarator (a ``type_identifier``); ``typedef int a, b;`` emits
  one Symbol per name. A ``typedef struct { ... } Foo;`` emits ``Foo`` once --
  the anonymous ``struct_specifier`` it wraps has no name of its own and is not
  emitted separately.

Struct / union / enum specifiers are usually wrapped in a ``declaration`` node
at file scope (``struct Foo { ... };`` parses as a ``declaration`` whose
``type`` child is the specifier). We therefore look inside ``declaration``
nodes for a defining specifier as well as handling specifiers that appear as
direct children of ``translation_unit`` in grammar revisions that surface them
that way.

C has no classes and no methods: every Symbol this backend emits carries
``parent=None``. There is no ``Class::method`` edge to express because the
language has no member functions; the ``parent`` field exists for languages
that do (TypeScript, Python, Go receivers) and stays unset here.

Nested declarations (a ``struct`` declared inside a function body, a local
``typedef``) are excluded by design -- we walk only the direct children of
``translation_unit`` (and one level into top-level ``declaration`` nodes to
reach the specifier they wrap). Anything declared inside a function body is not
reachable as a cross-file coordination unit.
"""

from __future__ import annotations

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_c"

# Cached parser; populated on first ``extract`` call.
_parser_c = None


def _c_parser():
    """Return a cached tree-sitter C parser instance."""

    global _parser_c
    if _parser_c is not None:
        return _parser_c
    import tree_sitter_c as ts_c
    from tree_sitter import Language, Parser

    language = Language(ts_c.language())
    _parser_c = Parser(language)
    return _parser_c


def _name_text(node) -> str | None:
    """Decode the ``name`` field of a node to a string, or None if missing."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8")


def _function_name(node) -> str | None:
    """Return the identifier declared by a ``function_definition``.

    The declarator chain wraps the ``function_declarator`` in zero or more
    ``pointer_declarator`` / ``parenthesized_declarator`` / ``array_declarator``
    nodes (for pointer or array return types). We follow the ``declarator``
    field down through those wrappers, step into the ``function_declarator``,
    then continue descending until we land on the plain identifier.
    """

    current = node.child_by_field_name("declarator")
    seen = 0
    while current is not None:
        seen += 1
        if seen > 64:
            # Defensive bound against a pathological / malformed chain.
            return None
        if current.type in {
            "pointer_declarator",
            "parenthesized_declarator",
            "array_declarator",
            "function_declarator",
        }:
            current = current.child_by_field_name("declarator")
            continue
        if current.type in {"identifier", "type_identifier", "field_identifier"}:
            return current.text.decode("utf-8")
        return None
    return None


def _symbol_from_function(node) -> Symbol | None:
    """Build a Symbol for a ``function_definition`` (``parent=None``)."""

    name = _function_name(node)
    if name is None:
        return None
    return Symbol(
        name=name,
        kind="function",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _specifier_body(node):
    """Return the body node of a struct/union/enum specifier, or ``None``.

    A defining specifier carries a ``body`` field (``field_declaration_list``
    for struct/union, ``enumerator_list`` for enum). References and forward
    declarations have no body and return ``None``.
    """

    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.children:
        if child.type in {"field_declaration_list", "enumerator_list"}:
            return child
    return None


def _symbol_from_specifier(node, anchor) -> Symbol | None:
    """Build a Symbol for a struct/union/enum specifier with name and body.

    ``anchor`` supplies the line span: when the specifier is wrapped in a
    top-level ``declaration`` we span the whole declaration (so the trailing
    ``;`` is covered); when the specifier is a direct child we span it.
    """

    name = _name_text(node)
    if name is None:
        return None
    if _specifier_body(node) is None:
        return None
    kind = "enum" if node.type == "enum_specifier" else "type"
    return Symbol(
        name=name,
        kind=kind,
        start_line=anchor.start_point[0] + 1,
        end_line=anchor.end_point[0] + 1,
    )


def _declarator_type_identifier(node) -> str | None:
    """Return the ``type_identifier`` named by a typedef declarator.

    A typedef declarator may be a bare ``type_identifier`` (``typedef int Foo``)
    or wrapped in pointer / array declarators (``typedef int *Foo`` /
    ``typedef int Foo[4]``). We descend through the wrappers to the identifier.
    """

    current = node
    seen = 0
    while current is not None:
        seen += 1
        if seen > 64:
            return None
        if current.type in {
            "pointer_declarator",
            "parenthesized_declarator",
            "array_declarator",
            "function_declarator",
        }:
            current = current.child_by_field_name("declarator")
            continue
        if current.type in {"type_identifier", "identifier"}:
            return current.text.decode("utf-8")
        return None
    return None


def _symbols_from_typedef(node) -> list[Symbol]:
    """One Symbol per declared name in a ``type_definition`` (``kind='type'``).

    ``typedef int a, b;`` declares two names; the grammar exposes each via a
    ``declarator`` field, so we iterate every ``declarator`` child rather than
    only the first.
    """

    out: list[Symbol] = []
    for child in node.children_by_field_name("declarator"):
        name = _declarator_type_identifier(child)
        if name is None:
            continue
        out.append(
            Symbol(
                name=name,
                kind="type",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        )
    return out


def _symbol_from_declaration(node) -> Symbol | None:
    """Extract a defining struct/union/enum specifier wrapped in a declaration.

    At file scope ``struct Foo { ... };`` parses as a ``declaration`` whose
    ``type`` field is the specifier. We emit a Symbol only when that specifier
    both names a type and has a body, spanning the whole declaration so the
    trailing ``;`` is included in the line range.
    """

    type_node = node.child_by_field_name("type")
    if type_node is None:
        for child in node.children:
            if child.type in {
                "struct_specifier",
                "union_specifier",
                "enum_specifier",
            }:
                type_node = child
                break
    if type_node is None:
        return None
    if type_node.type not in {
        "struct_specifier",
        "union_specifier",
        "enum_specifier",
    }:
        return None
    return _symbol_from_specifier(type_node, node)


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Nested declarations (a struct declared inside a function body, a local
    typedef) are excluded -- we walk only the direct children of the
    ``translation_unit`` node, descending one level into top-level
    ``declaration`` nodes to reach the struct/union/enum specifier they wrap.
    """

    parser = _c_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type == "function_definition":
            sym = _symbol_from_function(child)
            if sym is not None:
                out.append(sym)
        elif child.type == "type_definition":
            out.extend(_symbols_from_typedef(child))
        elif child.type == "declaration":
            sym = _symbol_from_declaration(child)
            if sym is not None:
                out.append(sym)
        elif child.type in {
            "struct_specifier",
            "union_specifier",
            "enum_specifier",
        }:
            # Some grammar revisions surface a defining specifier as a direct
            # child of translation_unit rather than wrapping it in a
            # declaration; span the specifier itself in that case.
            sym = _symbol_from_specifier(child, child)
            if sym is not None:
                out.append(sym)
        # Everything else (preprocessor directives, comments, top-level
        # variable declarations that are not struct/union/enum definitions,
        # bare expressions) is ignored: they are not claimable units.

    return out
