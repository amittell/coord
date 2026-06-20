"""Tree-sitter backed PHP symbol extractor.

Walks the top level of the ``program`` node produced by ``tree-sitter-php``
and emits one :class:`Symbol` per claimable declaration.

Recognised top-level declarations:

- ``function_definition`` -- ``kind='function'``, ``parent=None``. A free
  function declared at file scope (``function helper() {}``).
- ``class_declaration`` -- ``kind='class'``. Modifiers (``abstract``,
  ``final``, ``readonly``) live before the ``class`` keyword and do not affect
  the captured name. The class body is a ``declaration_list`` whose
  ``method_declaration`` children are emitted as separate Symbols with
  ``parent`` set to the class name so ``Class::method`` notation works.
- ``interface_declaration`` -- ``kind='interface'``. Its method signatures are
  ``method_declaration`` nodes (bodyless, terminated by ``;``) and are emitted
  with ``parent`` set to the interface name.
- ``trait_declaration`` -- ``kind='class'``. A trait is a class-like container
  of methods; PHP has no dedicated Symbol kind for it, so it reduces to the
  closest claimable grain (``class``). Its methods parent to the trait name.
- ``enum_declaration`` -- ``kind='enum'``. Backed enums (``enum Suit: string``)
  and pure enums are both recognised. The body is an ``enum_declaration_list``;
  its ``method_declaration`` children parent to the enum name. Enum cases
  (``case Hearts;``) are not claimable units and are skipped.

Methods (``method_declaration``) are only collected from inside a class-like
body. v0.16 attaches the enclosing type name as ``parent`` so a claim on a
method can be disambiguated from a free function with the same name and so
sibling methods of one class auto-coexist.

Nested declarations (closures assigned inside function bodies, classes declared
inside other method bodies) are excluded by design -- we walk only the direct
children of ``program`` and, one level down, the direct members of each
class-like body.
"""

from __future__ import annotations

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_php"

# Cached parser; populated on first ``extract`` call.
_parser_php = None

# Class-like declaration node types mapped to the Symbol kind they emit.
# Their bodies hold the ``method_declaration`` members we re-parent.
_CLASSLIKE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "class",
    "enum_declaration": "enum",
}


def _php_parser():
    """Return a cached tree-sitter PHP parser instance.

    ``tree-sitter-php`` exposes the grammar via ``language_php()`` (the full
    grammar that accepts ``<?php`` tags) rather than the generic ``language()``
    entry point used by most grammars. Older wheels only ship
    ``language_php_only()``; we prefer the tag-aware grammar and fall back to
    the tag-free one so both wheel shapes load.
    """

    global _parser_php
    if _parser_php is not None:
        return _parser_php
    import tree_sitter_php as ts_php
    from tree_sitter import Language, Parser

    grammar = getattr(ts_php, "language_php", None)
    if grammar is None:
        grammar = ts_php.language_php_only
    language = Language(grammar())
    _parser_php = Parser(language)
    return _parser_php


def _name_text(node) -> str | None:
    """Decode the ``name`` field of a node to a string, or None if missing."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8")


def _classlike_body(node):
    """Return the body node of a class-like declaration, or ``None``.

    Class, interface and trait bodies are ``declaration_list`` nodes; enum
    bodies are ``enum_declaration_list`` nodes. The grammar exposes both via
    the ``body`` field; we fall back to scanning children for either list type
    when the field is not populated by this grammar revision.
    """

    body = node.child_by_field_name("body")
    if body is not None:
        return body
    for child in node.children:
        if child.type in {"declaration_list", "enum_declaration_list"}:
            return child
    return None


def _methods_from_classlike(node, parent_name: str) -> list[Symbol]:
    """Emit one Symbol per ``method_declaration`` directly in a class body.

    Each method carries ``kind='function'`` and ``parent=parent_name`` so the
    coordination layer can address ``Parent::method`` distinctly from a free
    function of the same name.
    """

    out: list[Symbol] = []
    body = _classlike_body(node)
    if body is None:
        return out
    for child in body.children:
        if child.type != "method_declaration":
            continue
        name = _name_text(child)
        if name is None:
            continue
        out.append(
            Symbol(
                name=name,
                kind="function",
                start_line=child.start_point[0] + 1,
                end_line=child.end_point[0] + 1,
                parent=parent_name,
            )
        )
    return out


def _symbol_from_function(node) -> Symbol | None:
    """Build a Symbol for a top-level ``function_definition`` (``parent=None``)."""

    name = _name_text(node)
    if name is None:
        return None
    return Symbol(
        name=name,
        kind="function",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _symbols_from_classlike(node, kind: str) -> list[Symbol]:
    """Emit the class-like declaration itself plus each of its methods.

    The declaration Symbol uses ``kind`` (``class`` / ``interface`` / ``enum``);
    trait declarations are mapped to ``class`` by the caller. The methods follow
    with ``parent`` set to the declaration name.
    """

    out: list[Symbol] = []
    name = _name_text(node)
    if name is None:
        return out
    out.append(
        Symbol(
            name=name,
            kind=kind,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )
    )
    out.extend(_methods_from_classlike(node, name))
    return out


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Nested declarations (closures inside function bodies, classes declared
    inside other method bodies) are excluded -- we walk only the direct
    children of the ``program`` node and, one level deeper, the direct members
    of each class-like body.
    """

    parser = _php_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type == "function_definition":
            sym = _symbol_from_function(child)
            if sym is not None:
                out.append(sym)
        elif child.type in _CLASSLIKE_KINDS:
            out.extend(_symbols_from_classlike(child, _CLASSLIKE_KINDS[child.type]))
        # Everything else (php_tag, namespace use clauses, top-level
        # statements, comments) is ignored: those are not claimable units.

    return out
