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

v0.16 additions -- methods inside a class are now emitted as separate symbols.
For each top-level ``class_definition`` (including decorated ones) the
backend walks the direct children of the class's body ``block`` and emits:

- ``function_definition`` -> ``Symbol(kind='function', parent=<class name>)``.
  Covers regular methods, dunder methods (``__init__``, ``__repr__`` etc.),
  and async methods. Async / non-async collapse to ``'function'`` because
  the dataclass has no ``async_function`` kind.
- ``decorated_definition`` whose inner is a ``function_definition`` -> same
  treatment as above. Decorators such as ``@property``, ``@staticmethod``,
  and ``@classmethod`` do not change the emitted kind or the parent.
- ``assignment`` (inside an ``expression_statement``) whose right hand side
  is a ``lambda`` -> ``Symbol(kind='const', parent=<class name>)``. This
  mirrors the top-level lambda rule for class-body bindings such as
  ``handler = lambda x: x``.

Methods nested inside other functions (closures) stay excluded -- only
direct children of the class body block are emitted. Nested ``class``
definitions inside a class body are skipped entirely (the two-level
``parent`` model has no slot for ``Outer::Inner::method``). Functions
defined inside ``if __name__ == '__main__':`` guards or other compound
statements at module scope also stay excluded.
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
    """Build a Symbol from a function_definition or class_definition node."""

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
    """Unwrap a decorated_definition and emit the underlying symbol.

    The decorator stack is preserved in the start/end span so the claim
    covers the decorators as well as the definition body. The kind comes
    from the inner ``function_definition`` / ``class_definition`` -- decorators
    such as ``@property`` or ``@staticmethod`` do not change the kind.

    Returns a tuple of ``(symbol, inner_definition_node)`` so the caller can
    walk into the inner node's body for v0.16 method extraction when the
    inner is a ``class_definition``. Returns ``None`` if the decorated node
    does not wrap a recognised definition.
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
    class_name: str,
    span_node: Any | None = None,
) -> Symbol | None:
    """Build a method Symbol from a function_definition inside a class body.

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
        parent=class_name,
    )


def _method_symbol_from_lambda(node: Any, class_name: str) -> Symbol | None:
    """Match ``NAME = lambda ...`` at class-body scope.

    Mirrors :func:`_symbol_from_lambda_assignment` but tags the result with
    ``parent=class_name`` so it surfaces as a method-shaped const.
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
        parent=class_name,
    )


def _methods_from_class(class_node: Any, class_name: str) -> list[Symbol]:
    """Walk the direct children of a class body and emit method symbols.

    Only direct children of the ``block`` node count; closures inside a
    method body and nested class bodies are not traversed. Nested
    ``class_definition`` nodes are skipped entirely because the two-level
    ``parent`` model in v0.16 cannot represent ``Outer::Inner::method``.
    """

    body = class_node.child_by_field_name("body")
    if body is None:
        return []

    out: list[Symbol] = []
    for child in body.children:
        if child.type == "function_definition":
            sym = _method_symbol_from_function(child, class_name)
            if sym is not None:
                out.append(sym)
            continue

        if child.type == "decorated_definition":
            inner: Any = None
            for c in child.children:
                if c.type == "function_definition":
                    inner = c
                    break
            if inner is None:
                # Decorated nested class inside a class body -- skip, same
                # reasoning as bare nested classes below.
                continue
            sym = _method_symbol_from_function(inner, class_name, span_node=child)
            if sym is not None:
                out.append(sym)
            continue

        if child.type == "expression_statement":
            sym = _method_symbol_from_lambda(child, class_name)
            if sym is not None:
                out.append(sym)
            continue

        # Anything else (nested class_definition, docstrings, bare
        # assignments, pass statements, conditional blocks, etc.) does not
        # produce a method symbol in v0.16. Nested classes are intentionally
        # skipped because Outer::Inner::method would need three-level parent
        # tracking that the dataclass and schema do not yet support.

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations and class methods as :class:`Symbol`s.

    Top-level scan covers direct children of the ``module`` node. For every
    top-level class (including decorated classes) the backend additionally
    walks the class's body block and emits its direct-child methods with
    ``parent`` set to the class name. Output order follows source order:
    each class is immediately followed by its methods, before the next
    top-level declaration.
    """

    parser = _python_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type in _DEFINITION_KINDS:
            sym = _symbol_from_definition(child)
            if sym is not None:
                out.append(sym)
                if child.type == "class_definition":
                    out.extend(_methods_from_class(child, sym.name))
            continue

        if child.type == "decorated_definition":
            result = _symbol_from_decorated(child)
            if result is not None:
                sym, inner = result
                out.append(sym)
                if inner.type == "class_definition":
                    out.extend(_methods_from_class(inner, sym.name))
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
