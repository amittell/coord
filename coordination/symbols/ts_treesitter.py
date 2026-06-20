"""Tree-sitter backed TypeScript symbol extractor.

This backend walks the top level of the ``program`` node produced by the
``tree-sitter-typescript`` grammar. The .tsx grammar is used so JSX-embedded
declarations (a React component bound to a ``const``) parse without losing
any sibling declarations.

Recognised top-level declarations:

- ``function_declaration`` -- ``kind='function'``
- ``class_declaration`` -- ``kind='class'``
- ``interface_declaration`` -- ``kind='interface'``
- ``type_alias_declaration`` -- ``kind='type'``
- ``enum_declaration`` -- ``kind='enum'``
- ``lexical_declaration`` / ``variable_declaration`` -- ``kind='const'`` when
  the binding is a ``function_expression`` or ``arrow_function``. Other
  bindings are ignored (a ``const x = 42`` is not a callable, so it is not a
  unit of coordination for sub-file claims).

Export wrappers (``export X``, ``export default X``) are unwrapped before
classification. An anonymous default-exported function emits
``name='default', kind='function'`` which matches the wire format documented
in ``docs/design/sub-file-claims.md``.

Generics on the declaration name (``function foo<T>``) are dropped from the
captured name because tree-sitter exposes the identifier via the ``name``
field, which excludes the type-parameter clause.

v0.16 also emits method-level symbols nested inside a ``class_declaration``.
Each direct child of the class's ``class_body`` is examined and emitted with
``parent=<class name>``:

- ``method_definition`` -> ``kind='function'`` (covers regular methods, the
  ``constructor``, static methods, async methods, getters and setters; the
  ``get``/``set`` distinction is collapsed for v0.16).
- ``public_field_definition`` whose value is an ``arrow_function`` or
  ``function_expression`` -> ``kind='const'``.

v0.19 extends the class walk to RECURSE into nested ``class_declaration``
nodes encountered as direct members of a class body. The inner class itself
emits as a symbol with ``parent=<outer class name>`` (or the full ancestor
path for deeper nesting), and its members emit with the full path joined by
``"::"``. So ``class Outer { class Inner { handle() {} } }`` produces:

- ``Outer`` -- ``parent=None``
- ``Inner`` -- ``parent='Outer'``
- ``handle`` -- ``parent='Outer::Inner'``

The overlap engine's recursive prefix matching (see
``coordination/overlap_symbols.py``) handles arbitrary depths without schema
changes. Nested classes inside a method body (closures) are still excluded;
only direct members of a class body block are walked.
"""

from __future__ import annotations

from . import Symbol

# Native grammar wheel this backend needs. The dispatcher probes it (via
# find_spec) at selection time so a missing wheel degrades to the regex
# backend in auto mode instead of crashing when extract() is finally called.
GRAMMAR_MODULE = "tree_sitter_typescript"

# Lazy imports so a missing tree_sitter dependency does not break the package.
_parser_tsx = None


def _tsx_parser():
    """Return a cached tree-sitter TSX parser instance.

    The TSX grammar is a superset of the TS grammar for our purposes and lets
    us parse ``.tsx`` files without picking a different backend. Plain ``.ts``
    files parse fine because TSX nodes only appear when JSX syntax is present.
    """

    global _parser_tsx
    if _parser_tsx is not None:
        return _parser_tsx
    import tree_sitter_typescript as ts_ts
    from tree_sitter import Language, Parser

    language = Language(ts_ts.language_tsx())
    _parser_tsx = Parser(language)
    return _parser_tsx


def _declaration_for(node):
    """Unwrap an ``export_statement`` to its inner declaration.

    Returns ``(decl_node, is_default_export)``. ``decl_node`` is ``None`` when
    the export statement does not contain a declaration we care about (for
    example ``export { foo }`` or ``export default 42``).
    """

    if node.type != "export_statement":
        return node, False

    is_default = False
    decl = None
    for child in node.children:
        if child.type == "default":
            is_default = True
            continue
        # Skip pure keyword tokens.
        if child.type in {"export", ";", ","}:
            continue
        decl = child
    return decl, is_default


_DECLARATION_KINDS = {
    "function_declaration": "function",
    "class_declaration": "class",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}


def _function_like(node) -> bool:
    """True when ``node`` is an arrow function or function expression."""

    return node.type in {"arrow_function", "function_expression"}


def _symbol_from_named_declaration(node, is_default: bool) -> Symbol | None:
    """Build a Symbol from a top-level named declaration node.

    Returns ``None`` when the node is unsupported or unnamed. Anonymous
    function/class declarations only show up legally inside ``export default``;
    in that case the caller injects ``is_default=True`` and we emit a synthetic
    ``'default'`` name.
    """

    kind = _DECLARATION_KINDS.get(node.type)
    if kind is None:
        return None

    name_node = node.child_by_field_name("name")
    if name_node is None:
        if is_default and node.type == "function_declaration":
            # `export default function() {}` is parsed as
            # function_expression, not function_declaration, so this branch
            # only triggers for forms tree-sitter cannot otherwise name.
            return Symbol(
                name="default",
                kind="function",
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        return None

    return Symbol(
        name=name_node.text.decode("utf-8"),
        kind=kind,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _symbols_from_variable_declaration(node) -> list[Symbol]:
    """Extract function-bound declarators from ``const``/``let``/``var``.

    A single declaration may bind multiple names (``const a = ..., b = ...``);
    each declarator is examined independently. Only declarators whose value is
    a function or arrow expression are emitted; ``const NotFunc = 42`` is
    silently dropped because it is not a callable scope.
    """

    out: list[Symbol] = []
    for child in node.children:
        if child.type != "variable_declarator":
            continue
        name_node = child.child_by_field_name("name")
        value_node = child.child_by_field_name("value")
        if name_node is None or value_node is None:
            continue
        if not _function_like(value_node):
            continue
        # Skip destructuring patterns -- they have name nodes that are not
        # plain identifiers. Sub-file claims need a callable name, not a
        # binding pattern.
        if name_node.type != "identifier":
            continue
        out.append(
            Symbol(
                name=name_node.text.decode("utf-8"),
                kind="const",
                start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1,
            )
        )
    return out


def _method_name(node) -> str | None:
    """Return the textual name of a class member node, or ``None``.

    Tree-sitter exposes the name for both ``method_definition`` and
    ``public_field_definition`` via the ``name`` field. Computed property
    names (``[Symbol.iterator]() {}``) are not plain ``property_identifier``
    nodes and are skipped -- they have no static name addressable from a
    claim. The constructor is named ``constructor`` per the grammar.
    """

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    if name_node.type not in {"property_identifier", "identifier"}:
        return None
    return name_node.text.decode("utf-8")


def _walk_class(
    class_node,
    ancestor_path: str | None,
    name_override: str | None = None,
    span_node=None,
) -> list[Symbol]:
    """Emit the class itself plus every direct member of its body.

    ``ancestor_path`` is the chain leading UP TO (but not including) this
    class, joined by ``"::"``. ``None`` for a top-level class; ``"Outer"``
    for a class declared directly inside ``Outer``; ``"Outer::Inner"`` for
    a class declared inside ``Outer.Inner``; and so on.

    ``name_override`` lets callers force the emitted class name when the
    syntactic site provides a binding name distinct from the class
    expression's own (or in place of) name. The realistic TS pattern for a
    nested class is ``static Inner = class Inner { ... }``; the binding
    identifier ``Inner`` is the addressable name. When ``name_override`` is
    set, it wins over the class expression's optional name.

    ``span_node`` lets the emitted Symbol span the enclosing site (so the
    ``static Inner = class { ... };`` field definition's line range is
    used) instead of just the class body. ``None`` falls back to
    ``class_node``.

    The returned list always starts with this class's own symbol, followed
    by its members in source order. Nested ``class_declaration`` nodes and
    class expressions inside field values both recurse depth-first.

    ``method_definition`` (regular methods, ``constructor``, static, async,
    getters, setters) emit with ``kind='function'`` and ``parent=full_path``.
    ``public_field_definition`` whose value is an arrow/function expression
    emit with ``kind='const'`` and ``parent=full_path``; whose value is a
    class expression recurse into :func:`_walk_class` with the field's
    binding name as the addressable identifier. Other class members
    (index signatures, plain property declarations, abstract method
    signatures) are not callable scopes and are skipped.
    """

    if name_override is not None:
        class_name = name_override
    else:
        class_name_node = class_node.child_by_field_name("name")
        if class_name_node is None:
            return []
        class_name = class_name_node.text.decode("utf-8")
    full_path = f"{ancestor_path}::{class_name}" if ancestor_path else class_name

    span = span_node if span_node is not None else class_node
    out: list[Symbol] = [
        Symbol(
            name=class_name,
            kind="class",
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            parent=ancestor_path,
        )
    ]

    body = class_node.child_by_field_name("body")
    if body is None or body.type != "class_body":
        return out

    for member in body.children:
        # A nested class_declaration is not currently producible by the
        # tree-sitter-typescript grammar (the grammar treats `class X {}`
        # inside a class body as a syntax error), but the branch is kept
        # for forward compatibility with grammar evolutions or alternative
        # backends that might surface one.
        if member.type == "class_declaration":
            out.extend(_walk_class(member, full_path))
            continue

        if member.type == "method_definition":
            name = _method_name(member)
            if name is None:
                continue
            out.append(
                Symbol(
                    name=name,
                    kind="function",
                    start_line=member.start_point[0] + 1,
                    end_line=member.end_point[0] + 1,
                    parent=full_path,
                )
            )
            continue

        if member.type == "public_field_definition":
            value_node = member.child_by_field_name("value")
            if value_node is None:
                continue
            name = _method_name(member)
            if name is None:
                continue
            # Class expression on the right-hand side: recurse so the
            # nested class is addressable via its binding name. This is
            # the realistic TS shape for nested classes.
            if value_node.type == "class":
                out.extend(
                    _walk_class(
                        value_node,
                        full_path,
                        name_override=name,
                        span_node=member,
                    )
                )
                continue
            if not _function_like(value_node):
                continue
            out.append(
                Symbol(
                    name=name,
                    kind="const",
                    start_line=member.start_point[0] + 1,
                    end_line=member.end_point[0] + 1,
                    parent=full_path,
                )
            )
            continue

        # Other class members (index_signature, abstract_method_signature,
        # plain property declarations without an arrow value, etc.) are not
        # callable scopes and are skipped.

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Top-level declarations (functions, classes, interfaces, types, enums,
    function-valued ``const``/``let``/``var``) are walked from the direct
    children of the ``program`` node. Top-level ``class_declaration`` nodes
    are handed to :func:`_walk_class` which recursively emits the class
    itself, its direct members, and any nested classes (with their members
    and so on, depth-first). Functions inside functions and classes nested
    inside method bodies remain excluded -- only direct members of a class
    body block are walked.
    """

    parser = _tsx_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        decl, is_default = _declaration_for(child)
        if decl is None:
            continue

        # Handle `export default function() {}` -- anonymous function
        # expression underneath an export statement.
        if is_default and _function_like(decl):
            out.append(
                Symbol(
                    name="default",
                    kind="function",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
            continue

        if decl.type in _DECLARATION_KINDS:
            # v0.19: top-level classes go through _walk_class so the class
            # itself and its full nested-member tree are emitted in source
            # order. Other named declarations (function/interface/type/enum)
            # emit a single symbol.
            if decl.type == "class_declaration":
                out.extend(_walk_class(decl, None))
            else:
                sym = _symbol_from_named_declaration(decl, is_default)
                if sym is not None:
                    out.append(sym)
            continue

        if decl.type in {"lexical_declaration", "variable_declaration"}:
            out.extend(_symbols_from_variable_declaration(decl))
            continue

        # Anything else (export specifiers, bare expressions, import
        # statements) is ignored. Imports are not claimable units; export
        # specifier lists re-export existing symbols already covered.

    return out
