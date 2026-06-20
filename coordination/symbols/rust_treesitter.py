"""Tree-sitter backed Rust symbol extractor.

Walks the top level of the ``source_file`` node produced by
``tree-sitter-rust`` and emits one :class:`Symbol` per claimable declaration.

Recognised top-level declarations:

- ``function_item`` -- ``kind='function'``, ``parent=None``. Generic parameters
  (``fn foo<T>(...)``) are excluded from the captured name because tree-sitter
  exposes the identifier via the ``name`` field, which sits before the
  ``type_parameters`` child.
- ``struct_item`` / ``union_item`` -- ``kind='type'``.
- ``enum_item`` -- ``kind='enum'``.
- ``type_item`` (``type Foo = Bar;`` alias) -- ``kind='type'``.
- ``trait_item`` -- ``kind='interface'``. Rust traits are the closest analogue
  to an interface: a named set of method signatures and associated items. The
  trait surfaces as a single interface symbol; its associated function
  signatures are not emitted individually (mirrors the Go interface posture --
  only ``impl`` blocks contribute method symbols).
- ``const_item`` / ``static_item`` -- ``kind='const'``. Both are compile-time /
  global bindings, so they collapse to the same coordination grain.
- ``impl_item`` -- not itself a claimable unit. Each ``function_item`` directly
  inside the impl body is emitted with ``parent`` set to the implementing type
  name so ``Type::method`` notation works. ``impl Trait for Type`` parents the
  method to ``Type`` (the ``type`` field), not the trait. Generic implementing
  types (``impl Container<T>``) reduce to the bare type identifier
  (``Container``); a reference type (``impl &Foo``) and a scoped path
  (``impl foo::Bar``) reduce to ``Foo`` / ``Bar``.

``mod`` is a namespace, not a claimable unit: ``mod_item`` is skipped entirely,
and the extractor does not recurse into inline module bodies. Like the Go
backend this is a deliberate top-level-only walk -- declarations inside a
function body, a nested module, or a macro expansion are not reachable units of
cross-file coordination and are excluded by structure.
"""

from __future__ import annotations

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_rust"

# Cached parser; populated on first ``extract`` call.
_parser_rust = None


def _rust_parser():
    """Return a cached tree-sitter Rust parser instance."""

    global _parser_rust
    if _parser_rust is not None:
        return _parser_rust
    import tree_sitter_rust as ts_rust
    from tree_sitter import Language, Parser

    language = Language(ts_rust.language())
    _parser_rust = Parser(language)
    return _parser_rust


def _name_text(node) -> str | None:
    """Decode the ``name`` field of a node to a string, or None if missing."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8")


def _impl_type_name(node) -> str | None:
    """Return the implementing type name for an ``impl_item``, or ``None``.

    The implementing type is the ``type`` field of the impl. For
    ``impl Trait for Type`` that field is ``Type`` (the trait lives in the
    separate ``trait`` field), so the method parents to the concrete type and
    not the trait. One level of generic instantiation (``generic_type`` ->
    ``Container<T>``), reference (``reference_type`` -> ``&Foo``) and scoped
    path (``scoped_type_identifier`` -> ``foo::Bar``) are unwrapped so the
    parent reduces to the bare type identifier.
    """

    type_node = node.child_by_field_name("type")
    if type_node is None:
        return None

    # Reference receiver: strip the leading ``&`` (and any lifetime/mut).
    if type_node.type == "reference_type":
        inner = type_node.child_by_field_name("type")
        if inner is None:
            for child in type_node.children:
                if child.type in {
                    "type_identifier",
                    "generic_type",
                    "scoped_type_identifier",
                }:
                    inner = child
                    break
        if inner is None:
            return None
        type_node = inner

    # Generic instantiation: drop the type-argument list and keep the base.
    if type_node.type == "generic_type":
        base = type_node.child_by_field_name("type")
        if base is None:
            for child in type_node.children:
                if child.type in {"type_identifier", "scoped_type_identifier"}:
                    base = child
                    break
        if base is None:
            return None
        type_node = base

    # Scoped path (``foo::Bar``): keep the trailing segment.
    if type_node.type == "scoped_type_identifier":
        name_node = type_node.child_by_field_name("name")
        if name_node is not None:
            return name_node.text.decode("utf-8")
        last = None
        for child in type_node.children:
            if child.type == "type_identifier":
                last = child
        if last is None:
            return None
        return last.text.decode("utf-8")

    if type_node.type != "type_identifier":
        return None
    return type_node.text.decode("utf-8")


def _symbol_from_function(node, parent: str | None = None) -> Symbol | None:
    """Build a Symbol for a ``function_item``.

    ``parent`` is ``None`` for free functions and the implementing type name
    for functions discovered inside an ``impl_item`` body.
    """

    name = _name_text(node)
    if name is None:
        return None
    return Symbol(
        name=name,
        kind="function",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        parent=parent,
    )


def _simple_symbol(node, kind: str) -> Symbol | None:
    """Build a Symbol for a named, non-function declaration."""

    name = _name_text(node)
    if name is None:
        return None
    return Symbol(
        name=name,
        kind=kind,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _symbols_from_impl(node) -> list[Symbol]:
    """Emit one Symbol per ``function_item`` directly inside an impl body.

    The impl itself is not a claimable unit; only its associated functions are.
    Each gets ``parent`` set to the implementing type name so a claim on a
    method can be addressed as ``Type::method`` and disambiguated from a free
    function of the same name.
    """

    out: list[Symbol] = []
    parent = _impl_type_name(node)
    body = node.child_by_field_name("body")
    if body is None:
        for child in node.children:
            if child.type == "declaration_list":
                body = child
                break
    if body is None:
        return out
    for child in body.children:
        if child.type == "function_item":
            sym = _symbol_from_function(child, parent=parent)
            if sym is not None:
                out.append(sym)
    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Only the direct children of the ``source_file`` node are walked (plus the
    associated functions of ``impl_item`` bodies). Declarations nested inside a
    function body, a module, or a macro expansion are excluded by design.
    """

    parser = _rust_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type == "function_item":
            sym = _symbol_from_function(child)
            if sym is not None:
                out.append(sym)
        elif child.type in {"struct_item", "union_item", "type_item"}:
            sym = _simple_symbol(child, "type")
            if sym is not None:
                out.append(sym)
        elif child.type == "enum_item":
            sym = _simple_symbol(child, "enum")
            if sym is not None:
                out.append(sym)
        elif child.type == "trait_item":
            sym = _simple_symbol(child, "interface")
            if sym is not None:
                out.append(sym)
        elif child.type in {"const_item", "static_item"}:
            sym = _simple_symbol(child, "const")
            if sym is not None:
                out.append(sym)
        elif child.type == "impl_item":
            out.extend(_symbols_from_impl(child))
        # Everything else (mod_item, use_declaration, attribute_item, macro
        # invocations, comments) is ignored: modules are namespaces not
        # claimable units, imports are not symbols, and macro expansions are
        # not reachable at this layer.

    return out
