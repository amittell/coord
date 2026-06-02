"""Tree-sitter backed Go symbol extractor.

Walks the top level of the ``source_file`` node produced by ``tree-sitter-go``
and emits one :class:`Symbol` per claimable declaration.

Recognised top-level declarations:

- ``function_declaration`` -- ``kind='function'``
- ``method_declaration`` -- ``kind='function'``. Go's method declarations are
  top-level (the receiver lives outside the type), so they coordinate at the
  same grain as plain functions. The Symbol carries just the method name --
  receivers are intentionally dropped for v1 (sub-file claims are name-keyed
  and adding the receiver would require a second lookup table in the conflict
  engine).
- ``type_declaration`` containing one or more specs:
    - ``type_spec`` whose ``type`` child is an ``interface_type`` -> ``kind='interface'``
    - any other ``type_spec`` (struct, named primitive, type definition) -> ``kind='type'``
    - ``type_alias`` (``type Foo = Bar``) -> ``kind='type'``
- ``const_declaration`` -- one Symbol per ``const_spec`` with ``kind='const'``.
  Parenthesised blocks (``const ( A = 1; B = 2 )``) emit one Symbol per name.
- ``var_declaration`` -- one Symbol per ``var_spec`` whose value is a
  ``func_literal`` or a ``call_expression`` (the latter covers the common
  pattern ``var handler = makeHandler()``). All other var bindings are dropped
  because they are not callable units of coordination.

Generic type parameters (``func Foo[T any]``) are excluded from the captured
name because tree-sitter exposes the identifier via the ``name`` field, which
sits before the ``type_parameter_list`` child.

Nested declarations (closures inside functions, types declared inside method
bodies) are excluded by design -- we walk only the direct children of
``source_file``.
"""

from __future__ import annotations

from . import Symbol

# Cached parser; populated on first ``extract`` call.
_parser_go = None


def _go_parser():
    """Return a cached tree-sitter Go parser instance."""

    global _parser_go
    if _parser_go is not None:
        return _parser_go
    import tree_sitter_go as ts_go
    from tree_sitter import Language, Parser

    language = Language(ts_go.language())
    _parser_go = Parser(language)
    return _parser_go


def _name_text(node) -> str | None:
    """Decode the ``name`` field of a node to a string, or None if missing."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8")


def _symbol_from_function(node) -> Symbol | None:
    """Build a Symbol for a ``function_declaration`` or ``method_declaration``."""

    name = _name_text(node)
    if name is None:
        return None
    return Symbol(
        name=name,
        kind="function",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _symbols_from_type_declaration(node) -> list[Symbol]:
    """Extract type specs from a ``type_declaration``.

    A single ``type_declaration`` may hold one spec (``type Foo struct {}``)
    or many inside a parenthesised block. ``type_alias`` is a sibling of
    ``type_spec`` in the grammar, so we handle both.
    """

    out: list[Symbol] = []
    for child in node.children:
        if child.type == "type_spec":
            name = _name_text(child)
            if name is None:
                continue
            type_child = child.child_by_field_name("type")
            kind = (
                "interface"
                if type_child is not None and type_child.type == "interface_type"
                else "type"
            )
            out.append(
                Symbol(
                    name=name,
                    kind=kind,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
        elif child.type == "type_alias":
            name = _name_text(child)
            if name is None:
                continue
            out.append(
                Symbol(
                    name=name,
                    kind="type",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
    return out


def _symbols_from_const_declaration(node) -> list[Symbol]:
    """One Symbol per ``const_spec`` identifier inside a const_declaration."""

    out: list[Symbol] = []
    for child in node.children:
        if child.type != "const_spec":
            continue
        # const_spec exposes its bound names as ``identifier`` children before
        # the ``=`` token. Multi-name specs (``const A, B = 1, 2``) emit one
        # Symbol per identifier.
        for sub in child.children:
            if sub.type != "identifier":
                continue
            out.append(
                Symbol(
                    name=sub.text.decode("utf-8"),
                    kind="const",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
    return out


def _var_spec_is_callable(spec) -> bool:
    """True when a ``var_spec`` binds a value that yields a callable.

    Two cases qualify:
    - ``func_literal`` -- a function value (closure).
    - ``call_expression`` -- best-effort. Without type information we cannot
      know whether the call returns a callable, but the common pattern is a
      factory like ``var handler = makeHandler()`` and including it gives
      sub-file claims a useful unit.
    """

    expr_list = spec.child_by_field_name("value")
    # Older grammar versions did not expose a named ``value`` field; fall back
    # to scanning the children for an ``expression_list``.
    if expr_list is None:
        for sub in spec.children:
            if sub.type == "expression_list":
                expr_list = sub
                break
    if expr_list is None:
        return False
    for child in expr_list.children:
        if child.type in {"func_literal", "call_expression"}:
            return True
    return False


def _symbols_from_var_declaration(node) -> list[Symbol]:
    """Emit Symbols only for var specs that bind a callable value."""

    out: list[Symbol] = []
    for child in node.children:
        if child.type != "var_spec":
            continue
        if not _var_spec_is_callable(child):
            continue
        for sub in child.children:
            if sub.type != "identifier":
                continue
            out.append(
                Symbol(
                    name=sub.text.decode("utf-8"),
                    kind="const",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Nested declarations (closures inside functions, types declared inside
    method bodies) are excluded -- we walk only the direct children of the
    ``source_file`` node.
    """

    parser = _go_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type in {"function_declaration", "method_declaration"}:
            sym = _symbol_from_function(child)
            if sym is not None:
                out.append(sym)
        elif child.type == "type_declaration":
            out.extend(_symbols_from_type_declaration(child))
        elif child.type == "const_declaration":
            out.extend(_symbols_from_const_declaration(child))
        elif child.type == "var_declaration":
            out.extend(_symbols_from_var_declaration(child))
        # Everything else (package_clause, import_declaration, comments,
        # bare expressions) is ignored: imports are not claimable units and
        # package declarations are not symbols.

    return out
