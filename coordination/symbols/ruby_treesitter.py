"""Tree-sitter backed Ruby symbol extractor.

This backend walks the top level of the ``program`` node produced by the
``tree-sitter-ruby`` grammar.

Recognised top-level declarations:

- ``method`` (``def foo ... end``) -- ``kind='function'``, ``parent=None``.
  Method names that carry Ruby's punctuation suffixes (predicate ``foo?``,
  bang ``foo!``, setter ``foo=``) keep the suffix because the grammar exposes
  the whole token via the ``name`` field.
- ``singleton_method`` (``def self.foo``, ``def Klass.bar``) -- ``kind='function'``.
  The receiver (``self`` or a constant) lives in the ``object`` field and is
  dropped; only the ``name`` field is captured, so ``def self.create`` and a
  plain ``def create`` coordinate under the same name within their scope.
- ``class`` (``class Foo ... end``) -- ``kind='class'``. The body is walked
  recursively so methods and nested namespaces surface with the full ancestor
  path in ``parent``.
- ``module`` (``module Bar ... end``) -- ``kind='class'`` as well. Ruby modules
  are a distinct construct from classes, but the :class:`Symbol` kind set has
  no separate ``module`` kind, so both collapse to ``'class'``; the
  coordination grain only cares about the namespace and its members.

Methods inside a class or module body carry ``parent`` set to the enclosing
namespace name. v0.17-style recursion walks nested ``class`` / ``module``
definitions so an inner namespace and its methods are surfaced too, each
carrying the full ancestor path joined by ``"::"``. A direct member of a
top-level class ``Foo`` has ``parent='Foo'``; a method on ``Foo::Inner`` has
``parent='Foo::Inner'``. The overlap engine matches on the full canonical
path, so arbitrary nesting depth works without schema changes.

Methods nested inside other method bodies (defs defined dynamically inside a
def) stay excluded -- only direct members of a namespace body are emitted.
Singleton-class blocks (``class << self``) and methods generated via
metaprogramming (``define_method``, ``attr_accessor``) are likewise out of
scope; they are not lexical ``method`` nodes in the namespace body.
"""

from __future__ import annotations

from typing import Any

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_ruby"

# Lazy module-level parser cache so a missing tree_sitter dependency does not
# break the package on import.
_parser_rb: Any = None


def _ruby_parser() -> Any:
    """Return a cached tree-sitter Ruby parser instance."""

    global _parser_rb
    if _parser_rb is not None:
        return _parser_rb
    import tree_sitter_ruby as ts_ruby
    from tree_sitter import Language, Parser

    language = Language(ts_ruby.language())
    _parser_rb = Parser(language)
    return _parser_rb


# Namespace nodes whose bodies are walked recursively. Both map to the
# ``'class'`` kind because the Symbol dataclass has no separate ``module``
# kind.
_NAMESPACE_TYPES = {"class", "module"}

# Method nodes (plain and singleton) collapse to ``kind='function'``.
_METHOD_TYPES = {"method", "singleton_method"}


def _name_text(node: Any) -> str | None:
    """Decode the ``name`` field of a node to a string, or None if missing."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8")


def _method_symbol(node: Any, parent: str | None) -> Symbol | None:
    """Build a Symbol for a ``method`` or ``singleton_method`` node.

    The receiver of a ``singleton_method`` (``self`` or a constant) lives in
    the ``object`` field and is intentionally dropped; only the bare method
    name is captured so singleton and instance methods coordinate under the
    same name within their namespace.
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


def _body_children(node: Any) -> list[Any]:
    """Return the statement nodes that make up a namespace body.

    ``tree-sitter-ruby`` wraps a class / module body in a ``body_statement``
    node in some grammar revisions and exposes the statements as direct
    children in others. This helper normalises both shapes: if a
    ``body_statement`` child exists, its children are the body statements;
    otherwise the namespace node's own children are used (the structural
    tokens -- ``class`` keyword, the name constant, an optional superclass,
    the trailing ``end`` -- are simply ignored by the caller because they are
    not method or namespace nodes).
    """

    for child in node.children:
        if child.type == "body_statement":
            return list(child.children)
    return list(node.children)


def _walk_namespace(node: Any, ancestor_path: str | None) -> list[Symbol]:
    """Emit a namespace symbol and walk its body recursively.

    ``ancestor_path`` is the ``"::"``-joined path that leads UP TO (but does
    not include) this namespace. ``None`` for a top-level class / module;
    ``"Outer"`` for a namespace declared directly inside ``Outer``; and so on.

    The returned list always starts with this namespace's own symbol, followed
    by its members in source order. Inner namespaces are followed immediately
    by their own members (depth-first traversal), matching the visual order of
    the source file.
    """

    name = _name_text(node)
    if name is None:
        return []
    full_path = f"{ancestor_path}::{name}" if ancestor_path else name

    out: list[Symbol] = [
        Symbol(
            name=name,
            kind="class",
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            parent=ancestor_path,
        )
    ]

    for child in _body_children(node):
        if child.type in _METHOD_TYPES:
            sym = _method_symbol(child, full_path)
            if sym is not None:
                out.append(sym)
            continue
        if child.type in _NAMESPACE_TYPES:
            out.extend(_walk_namespace(child, full_path))
            continue
        # Anything else (constant assignments, calls such as ``attr_accessor``,
        # ``include`` / ``require`` statements, comments) is not a claimable
        # member in v1. Closures and defs nested inside a method body are
        # excluded because the walk only visits direct members of this body.

    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations and nested members as :class:`Symbol`s.

    The top-level scan covers direct children of the ``program`` node. For
    every namespace (top-level or nested) the backend additionally walks the
    namespace body and emits its direct members with ``parent`` set to the
    full ancestor path (``"Outer"``, ``"Outer::Inner"`` and so on). Output
    order follows source order: each namespace is immediately followed by its
    members (with inner namespaces recursively expanded in place) before the
    next top-level declaration.
    """

    parser = _ruby_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type in _METHOD_TYPES:
            sym = _method_symbol(child, None)
            if sym is not None:
                out.append(sym)
            continue
        if child.type in _NAMESPACE_TYPES:
            out.extend(_walk_namespace(child, None))
            continue
        # Everything else (comments, require/load calls, bare expressions,
        # top-level constant assignments) is ignored: it is not a claimable
        # unit of coordination.

    return out
