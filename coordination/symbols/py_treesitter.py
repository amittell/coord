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

Methods nested inside a class do NOT appear in the top-level symbol list --
this backend walks only the direct children of the ``module`` node. The same
applies to functions defined inside ``if __name__ == '__main__':`` guards or
any other compound statement.
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


def _symbol_from_decorated(node: Any) -> Symbol | None:
    """Unwrap a decorated_definition and emit the underlying symbol.

    The decorator stack is preserved in the start/end span so the claim
    covers the decorators as well as the definition body. The kind comes
    from the inner ``function_definition`` / ``class_definition`` -- decorators
    such as ``@property`` or ``@staticmethod`` do not change the kind.
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

    return Symbol(
        name=name_node.text.decode("utf-8"),
        kind=_DEFINITION_KINDS[inner.type],
        # Span covers the decorators too -- start at the outer node.
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


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


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Only direct children of the ``module`` node are inspected -- nested
    definitions (methods, functions inside ``if`` blocks, classes inside
    functions) are intentionally skipped because they are not addressable as
    top-level claims.
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
            continue

        if child.type == "decorated_definition":
            sym = _symbol_from_decorated(child)
            if sym is not None:
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
