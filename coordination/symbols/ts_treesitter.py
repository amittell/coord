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

Nested classes inside a method body are NOT recursed into; only direct
members of the outermost top-level class are emitted. This matches the
coarseness contract from the design doc (one level of nesting only).
"""

from __future__ import annotations

from . import Symbol

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


def _symbols_from_class_body(class_node) -> list[Symbol]:
    """Emit method-level symbols for a single ``class_declaration``.

    Only direct children of the class's ``class_body`` are inspected. Nested
    classes inside method bodies are NOT recursed into; the design doc keeps
    coordination to one level of nesting.
    """

    class_name_node = class_node.child_by_field_name("name")
    if class_name_node is None:
        return []
    class_name = class_name_node.text.decode("utf-8")

    body = class_node.child_by_field_name("body")
    if body is None or body.type != "class_body":
        return []

    out: list[Symbol] = []
    for member in body.children:
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
                    parent=class_name,
                )
            )
            continue

        if member.type == "public_field_definition":
            value_node = member.child_by_field_name("value")
            if value_node is None or not _function_like(value_node):
                continue
            name = _method_name(member)
            if name is None:
                continue
            out.append(
                Symbol(
                    name=name,
                    kind="const",
                    start_line=member.start_point[0] + 1,
                    end_line=member.end_point[0] + 1,
                    parent=class_name,
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
    children of the ``program`` node. v0.16 additionally descends one level
    into every top-level ``class_declaration`` to emit its methods and
    arrow-valued fields with ``parent=<class name>``. Deeper nesting
    (functions inside functions, classes inside methods) is intentionally
    excluded.
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
            sym = _symbol_from_named_declaration(decl, is_default)
            if sym is not None:
                out.append(sym)
            # v0.16: also emit method-level symbols for top-level classes.
            if decl.type == "class_declaration":
                out.extend(_symbols_from_class_body(decl))
            continue

        if decl.type in {"lexical_declaration", "variable_declaration"}:
            out.extend(_symbols_from_variable_declaration(decl))
            continue

        # Anything else (export specifiers, bare expressions, import
        # statements) is ignored. Imports are not claimable units; export
        # specifier lists re-export existing symbols already covered.

    return out
