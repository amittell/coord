"""Tree-sitter backed Python symbol extractor.

This backend walks the top level of the ``module`` node produced by the
``tree-sitter-python`` grammar.

Recognised top-level declarations:

- ``function_definition`` -- ``kind='function'``. Async functions parse as the
  same node type (with an ``async`` leaf token); the dataclass has no separate
  ``async_function`` kind, so they collapse into ``'function'`` too.
- ``class_definition`` -- ``kind='class'``. Generic / type-parameterised
  classes (``class Foo[T]:``) use the same node; the name field excludes the
  parameter clause so ``name='Foo'``.
- ``decorated_definition`` -- unwraps to the underlying ``function_definition``
  or ``class_definition`` and inherits its kind. Decorators do not alter the
  emitted kind.
- ``expression_statement`` containing a top-level ``assignment`` whose right
  hand side is a ``lambda`` -- ``kind='const'``. Only single-name lambda
  bindings (``NAME = lambda ...``) qualify in v1; tuple targets, attribute
  targets, comprehensions, and other callable-producing expressions are
  intentionally skipped to keep the heuristic predictable.

v0.16 added method-level symbols inside top-level classes. v0.17 extends that
to walk RECURSIVELY into nested class definitions so an inner class and its
methods are also surfaced, each carrying the full ancestor path in ``parent``.

For every class encountered (top-level or nested) the backend walks the
direct children of the class's body ``block`` and emits:

- ``function_definition`` -> ``Symbol(kind='function', parent=<full path>)``.
  Covers regular methods, dunder methods (``__init__``, ``__repr__`` etc.),
  and async methods. Async / non-async collapse to ``'function'`` because
  the dataclass has no ``async_function`` kind.
- ``decorated_definition`` whose inner is a ``function_definition`` -> same
  treatment as above. Decorators such as ``@property``, ``@staticmethod``,
  and ``@classmethod`` do not change the emitted kind or the parent.
- ``class_definition`` (or decorated ``class_definition``) ->
  ``Symbol(kind='class', parent=<full path>)``, followed by a recursive walk
  into the inner class's body with the path extended by ``"::<inner name>"``.
- ``assignment`` (inside an ``expression_statement``) whose right hand side
  is a ``lambda`` -> ``Symbol(kind='const', parent=<full path>)``. This
  mirrors the top-level lambda rule for class-body bindings such as
  ``handler = lambda x: x``.

The ``parent`` string is the ancestor chain joined by ``"::"``. A direct
member of a top-level class ``Foo`` has ``parent='Foo'``; a method on
``Foo.Inner.Deeper`` has ``parent='Foo::Inner::Deeper'``. The schema column
is a single ``parent_symbol`` string and the overlap engine matches on the
full canonical path, so arbitrary nesting depth works without schema
changes.

Methods nested inside other functions (closures) stay excluded -- only
direct children of a class body block are emitted. Functions defined inside
``if __name__ == '__main__':`` guards or other compound statements at module
scope also stay excluded.
"""

from __future__ import annotations

from typing import Any

from . import Symbol

# Lazy module-level parser cache so a missing tree_sitter dependency does not
# break the package on import.
_parser_py: Any = None


def _python_parser() -> Any:
    """Return a cached tree-sitter Python parser instance."""

    global _parser_py
    if _parser_py is not None:
        return _parser_py
    import tree_sitter_python as ts_py
    from tree_sitter import Language, Parser

    language = Language(ts_py.language())
    _parser_py = Parser(language)
    return _parser_py


_DEFINITION_KINDS = {
    "function_definition": "function",
    "class_definition": "class",
}


def _symbol_from_definition(node: Any) -> Symbol | None:
    """Build a top-level Symbol from a function_definition or class_definition.

    Top-level definitions always have ``parent=None``. Nested classes and
    class-body functions go through :func:`_walk_class` instead, which
    threads the ancestor path explicitly.
    """

    kind = _DEFINITION_KINDS.get(node.type)
    if kind is None:
        return None

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None

    return Symbol(
        name=name_node.text.decode("utf-8"),
        kind=kind,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _symbol_from_decorated(node: Any) -> tuple[Symbol, Any] | None:
    """Unwrap a top-level decorated_definition and emit the underlying symbol.

    The decorator stack is preserved in the start/end span so the claim
    covers the decorators as well as the definition body. The kind comes
    from the inner ``function_definition`` / ``class_definition`` -- decorators
    such as ``@property`` or ``@staticmethod`` do not change the kind.

    Returns a tuple of ``(symbol, inner_definition_node)`` so the caller can
    recurse into the inner node's body when the inner is a
    ``class_definition``. Returns ``None`` if the decorated node does not
    wrap a recognised definition.
    """

    inner: Any = None
    for child in node.children:
        if child.type in _DEFINITION_KINDS:
            inner = child
            break
    if inner is None:
        return None

    name_node = inner.child_by_field_name("name")
    if name_node is None:
        return None

    sym = Symbol(
        name=name_node.text.decode("utf-8"),
        kind=_DEFINITION_KINDS[inner.type],
        # Span covers the decorators too -- start at the outer node.
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )
    return sym, inner


def _symbol_from_lambda_assignment(node: Any) -> Symbol | None:
    """Match ``NAME = lambda ...`` at module scope.

    ``node`` is an ``expression_statement``. The first child is expected to
    be an ``assignment`` with an identifier ``left`` and a ``lambda`` ``right``.
    Multi-target / augmented / attribute assignments are ignored to keep v1
    behaviour predictable.
    """

    assignment: Any = None
    for child in node.children:
        if child.type == "assignment":
            assignment = child
            break
    if assignment is None:
        return None

    left = assignment.child_by_field_name("left")
    right = assignment.child_by_field_name("right")
    if left is None or right is None:
        return None
    if left.type != "identifier":
        return None
    if right.type != "lambda":
        return None

    return Symbol(
        name=left.text.decode("utf-8"),
        kind="const",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _method_symbol_from_function(
    func_node: Any,
    full_parent_path: str,
    span_node: Any | None = None,
) -> Symbol | None:
    """Build a method Symbol from a function_definition inside a class body.

    ``full_parent_path`` is the ancestor chain (joined by ``"::"``) for the
    enclosing class -- e.g. ``"Outer"`` for a direct member of ``Outer`` and
    ``"Outer::Inner"`` for a method on the nested ``Inner`` class.

    ``span_node`` lets a decorated method include its decorator stack in the
    line span (caller passes the outer ``decorated_definition``); plain
    methods pass ``span_node=None`` so the span comes from ``func_node``.
    """

    name_node = func_node.child_by_field_name("name")
    if name_node is None:
        return None

    span = span_node if span_node is not None else func_node
    return Symbol(
        name=name_node.text.decode("utf-8"),
        kind="function",
        start_line=span.start_point[0] + 1,
        end_line=span.end_point[0] + 1,
        parent=full_parent_path,
    )


def _method_symbol_from_lambda(node: Any, full_parent_path: str) -> Symbol | None:
    """Match ``NAME = lambda ...`` at class-body scope.

    Mirrors :func:`_symbol_from_lambda_assignment` but tags the result with
    ``parent=full_parent_path`` so it surfaces as a method-shaped const.
    """

    assignment: Any = None
    for child in node.children:
        if child.type == "assignment":
            assignment = child
            break
    if assignment is None:
        return None

    left = assignment.child_by_field_name("left")
    right = assignment.child_by_field_name("right")
    if left is None or right is None:
        return None
    if left.type != "identifier":
        return None
    if right.type != "lambda":
        return None

    return Symbol(
        name=left.text.decode("utf-8"),
        kind="const",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        parent=full_parent_path,
    )


def _walk_class(
    class_node: Any,
    ancestor_path: str | None,
    span_node: Any | None = None,
) -> list[Symbol]:
    """Emit a class symbol and walk its body recursively.

    ``ancestor_path`` is the dotted path that leads UP TO (but does not
    include) this class. ``None`` for a top-level class; ``"Outer"`` for a
    class declared directly inside ``Outer``; ``"Outer::Inner"`` for a class
    declared inside ``Outer.Inner``; and so on.

    ``span_node`` lets a decorated class include its decorator stack in the
    line span (caller passes the outer ``decorated_definition``). Plain
    classes pass ``span_node=None`` so the span comes from ``class_node``.

    The returned list always starts with this class's own symbol, followed
    by its members in source order. Inner classes are followed immediately
    by their own members (depth-first traversal), matching the visual order
    of the source file.
    """

    name_node = class_node.child_by_field_name("name")
    if name_node is None:
        return []
    class_name = name_node.text.decode("utf-8")
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
    if body is None:
        return out

    for child in body.children:
        if child.type == "function_definition":
            sym = _method_symbol_from_function(child, full_path)
            if sym is not None:
                out.append(sym)
            continue

        if child.type == "class_definition":
            out.extend(_walk_class(child, full_path))
            continue

        if child.type == "decorated_definition":
            inner: Any = None
            for c in child.children:
                if c.type in _DEFINITION_KINDS:
                    inner = c
                    break
            if inner is None:
                continue
            if inner.type == "function_definition":
                sym = _method_symbol_from_function(inner, full_path, span_node=child)
                if sym is not None:
                    out.append(sym)
            elif inner.type == "class_definition":
                out.extend(_walk_class(inner, full_path, span_node=child))
            continue

        if child.type == "expression_statement":
            sym = _method_symbol_from_lambda(child, full_path)
            if sym is not None:
                out.append(sym)
            continue

        # Anything else (docstrings, bare assignments, pass statements,
        # conditional blocks, etc.) does not produce a symbol. Closures
        # nested inside a method body are excluded because the walk only
        # visits direct children of this class's body block.

    return out


def extract(content: str) -> list[Symbol]:
    """Return module-level declarations and nested class members as :class:`Symbol`s.

    Top-level scan covers direct children of the ``module`` node. For every
    class (top-level or nested inside another class) the backend additionally
    walks the class's body block and emits its direct-child members with
    ``parent`` set to the full ancestor path (``"Outer"``,
    ``"Outer::Inner"``, ``"Outer::Inner::Deeper"`` and so on). Output order
    follows source order: each class is immediately followed by its members
    (with inner classes recursively expanded in place) before the next
    top-level declaration.
    """

    parser = _python_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type == "function_definition":
            sym = _symbol_from_definition(child)
            if sym is not None:
                out.append(sym)
            continue

        if child.type == "class_definition":
            out.extend(_walk_class(child, None))
            continue

        if child.type == "decorated_definition":
            result = _symbol_from_decorated(child)
            if result is not None:
                sym, inner = result
                if inner.type == "class_definition":
                    out.extend(_walk_class(inner, None, span_node=child))
                else:
                    out.append(sym)
            continue

        if child.type == "expression_statement":
            sym = _symbol_from_lambda_assignment(child)
            if sym is not None:
                out.append(sym)
            continue

        # Anything else (imports, bare expressions, augmented assignments,
        # module docstrings, ``if __name__ == '__main__':`` guards, ``try``
        # blocks, etc.) is not a claimable top-level symbol in v1.

    return out
