"""Tree-sitter backed Scala symbol extractor.

This backend walks the top level of the ``compilation_unit`` node produced by
the ``tree-sitter-scala`` grammar.

Recognised top-level declarations:

- ``function_definition`` -- a ``def`` with a body. ``kind='function'``,
  ``parent=None``.
- ``function_declaration`` -- an abstract ``def`` (signature only, the shape a
  ``trait`` uses). Same ``kind='function'``; the dataclass has no separate
  "abstract" kind, so concrete and abstract defs collapse together.
- ``class_definition`` -- ``kind='class'``. ``case class`` parses as the same
  node (the ``case`` soft-keyword is a modifier child), so it also surfaces as
  ``'class'``.
- ``object_definition`` -- ``kind='class'``. Scala objects are singletons but
  coordinate at the same grain as classes; the dataclass has no dedicated
  ``object`` kind.
- ``trait_definition`` -- ``kind='interface'``. Traits are Scala's interface
  analogue, so they map onto the interface kind to match the Go / TypeScript
  posture.
- ``val_definition`` / ``var_definition`` whose bound value is a
  ``lambda_expression`` -- ``kind='const'``. Only single-identifier bindings
  that bind a function literal (``val f = (x: Int) => x``) qualify; plain data
  vals are intentionally skipped because they are not callable units of
  coordination. This mirrors the Go ``var handler = func() {}`` and Python
  ``handler = lambda x: x`` heuristics.

Methods nested inside a ``class`` / ``object`` / ``trait`` body surface with
``parent`` set to the enclosing container's name so ``Class::method`` notation
works. The backend walks RECURSIVELY into nested containers (a class declared
inside another class / object / trait), threading the full ancestor path so an
inner container and its methods are addressable as
``Outer::Inner::method``. The ``parent`` string is the ancestor chain joined
by ``"::"``; the schema column is a single ``parent_symbol`` string and the
overlap engine matches on the full canonical path, so arbitrary nesting depth
works without schema changes.

For every container encountered (top-level or nested) the backend walks the
direct children of its ``template_body`` and emits:

- ``function_definition`` / ``function_declaration`` ->
  ``Symbol(kind='function', parent=<full path>)``.
- ``class_definition`` / ``object_definition`` / ``trait_definition`` -> the
  container's own symbol followed by a recursive walk into its body, with the
  path extended by ``"::<inner name>"``.
- ``val_definition`` / ``var_definition`` binding a ``lambda_expression`` ->
  ``Symbol(kind='const', parent=<full path>)``, mirroring the top-level rule.

Members nested inside a method body (closures, locally declared classes) stay
excluded -- the walk only visits the direct children of each container's body.
"""

from __future__ import annotations

from typing import Any

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_scala"

# Cached parser; populated on first ``extract`` call.
_parser_scala: Any = None


def _scala_parser() -> Any:
    """Return a cached tree-sitter Scala parser instance."""

    global _parser_scala
    if _parser_scala is not None:
        return _parser_scala
    import tree_sitter_scala as ts_scala
    from tree_sitter import Language, Parser

    language = Language(ts_scala.language())
    _parser_scala = Parser(language)
    return _parser_scala


# container node type -> emitted kind.
_CONTAINER_KINDS = {
    "class_definition": "class",
    "object_definition": "class",
    "trait_definition": "interface",
}

# def node types (concrete and abstract) collapse to ``function``.
_FUNCTION_TYPES = {"function_definition", "function_declaration"}

# val / var binding node types.
_VAL_VAR_TYPES = {"val_definition", "var_definition"}


def _name_text(node: Any) -> str | None:
    """Decode the declared name of a node to a string, or ``None``.

    Tries the ``name`` field first (defs, classes, objects, traits expose it),
    then the ``pattern`` field (``val`` / ``var`` definitions bind through a
    pattern), and finally falls back to the first ``identifier`` /
    ``_type_identifier`` child so the extractor stays robust across grammar
    revisions.
    """

    name_node = node.child_by_field_name("name")
    if name_node is None:
        name_node = node.child_by_field_name("pattern")
    if name_node is not None and name_node.type in {"identifier", "_type_identifier"}:
        return name_node.text.decode("utf-8")
    if name_node is not None and name_node.type not in {"identifier", "_type_identifier"}:
        # Pattern wrappers (e.g. a tuple pattern) are not a single claimable
        # name; only a bare identifier pattern qualifies.
        for child in name_node.children:
            if child.type == "identifier":
                return child.text.decode("utf-8")
        return None
    for child in node.children:
        if child.type in {"identifier", "_type_identifier"}:
            return child.text.decode("utf-8")
    return None


def _body_node(node: Any) -> Any | None:
    """Return the ``template_body`` for a container, or ``None``.

    Prefers the named ``body`` field; falls back to scanning children for a
    ``template_body`` node when the field is not exposed by the grammar.
    """

    body = node.child_by_field_name("body")
    if body is not None and body.type == "template_body":
        return body
    for child in node.children:
        if child.type == "template_body":
            return child
    return None


def _binds_lambda(node: Any) -> bool:
    """True when a ``val`` / ``var`` definition binds a function literal.

    The bound value lives in the ``value`` field; we fall back to scanning the
    trailing children for a ``lambda_expression`` when the field is absent.
    """

    value = node.child_by_field_name("value")
    if value is not None:
        return value.type == "lambda_expression"
    for child in node.children:
        if child.type == "lambda_expression":
            return True
    return False


def _function_symbol(node: Any, parent: str | None) -> Symbol | None:
    """Build a Symbol for a ``def`` (concrete or abstract)."""

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


def _val_var_symbol(node: Any, parent: str | None) -> Symbol | None:
    """Build a ``const`` Symbol for a lambda-bound ``val`` / ``var``."""

    if not _binds_lambda(node):
        return None
    name = _name_text(node)
    if name is None:
        return None
    return Symbol(
        name=name,
        kind="const",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        parent=parent,
    )


def _walk_container(node: Any, ancestor_path: str | None) -> list[Symbol]:
    """Emit a container symbol and walk its body recursively.

    ``ancestor_path`` is the path leading UP TO (but not including) this
    container: ``None`` for a top-level container, ``"Outer"`` for a container
    declared directly inside ``Outer``, ``"Outer::Inner"`` deeper still.

    The returned list starts with this container's own symbol, followed by its
    members in source order. Nested containers are followed immediately by
    their members (depth-first), matching the visual order of the source.
    """

    name = _name_text(node)
    if name is None:
        return []
    kind = _CONTAINER_KINDS[node.type]
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

    body = _body_node(node)
    if body is None:
        return out

    for child in body.children:
        if child.type in _FUNCTION_TYPES:
            sym = _function_symbol(child, full_path)
            if sym is not None:
                out.append(sym)
        elif child.type in _CONTAINER_KINDS:
            out.extend(_walk_container(child, full_path))
        elif child.type in _VAL_VAR_TYPES:
            sym = _val_var_symbol(child, full_path)
            if sym is not None:
                out.append(sym)
        # Anything else (imports, type aliases, bare expressions, plain-data
        # vals) does not produce a symbol. Closures nested inside a method
        # body are excluded because we only visit direct children of the body.

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations and container members as :class:`Symbol`s.

    The top-level scan covers direct children of the ``compilation_unit``
    node. For every container (top-level or nested) the backend additionally
    walks its body and emits direct-child members with ``parent`` set to the
    full ancestor path (``"Outer"``, ``"Outer::Inner"`` and so on). Output
    order follows source order: each container is immediately followed by its
    members, with inner containers recursively expanded in place.
    """

    parser = _scala_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type in _FUNCTION_TYPES:
            sym = _function_symbol(child, None)
            if sym is not None:
                out.append(sym)
        elif child.type in _CONTAINER_KINDS:
            out.extend(_walk_container(child, None))
        elif child.type in _VAL_VAR_TYPES:
            sym = _val_var_symbol(child, None)
            if sym is not None:
                out.append(sym)
        # Everything else (package_clause, import_declaration, comments, type
        # aliases, bare expressions) is ignored: imports and package clauses
        # are not claimable units.

    return out
