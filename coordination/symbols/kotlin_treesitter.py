"""Tree-sitter backed Kotlin symbol extractor.

Walks the top level of the ``source_file`` node produced by
``tree-sitter-kotlin`` and emits one :class:`Symbol` per claimable
declaration. Methods inside a class / interface / object body are descended
one level so they can be addressed with ``Class::method`` notation.

Recognised declarations:

- ``function_declaration`` at file scope -- ``kind='function'``, ``parent=None``.
- ``class_declaration`` -- ``kind='class'`` for ``class``, ``kind='interface'``
  for ``interface``, and ``kind='enum'`` when the ``enum`` modifier is present
  (``enum class Color { ... }``). ``data class`` / ``sealed class`` /
  ``abstract class`` all reduce to ``kind='class'``.
- ``object_declaration`` -- ``kind='class'`` (a Kotlin ``object`` is a named
  singleton; it coordinates at the same grain as a class).
- Methods inside a class / interface / object body -- ``function_declaration``
  nodes one level down -- emit ``kind='function'`` with ``parent`` set to the
  enclosing type name so ``Foo::handle`` is distinct from a free-standing
  ``handle``.
- ``companion_object`` inside a class body -- ``kind='class'`` with the
  enclosing class as ``parent``. An unnamed companion takes the conventional
  name ``Companion``; ``companion object Named`` keeps ``Named``.
- Top-level ``property_declaration`` (``val`` / ``const val`` / ``var``) --
  ``kind='const'``. Properties are the file-scoped callable-adjacent unit the
  conflict engine can usefully coordinate on.

Nesting posture:

Kotlin classes can nest arbitrarily, but the coordination grain only needs a
single parent edge: a method's enclosing type. We descend exactly one level
into a class / interface / object body to attach methods and the companion
object, mirroring how the Go backend records the receiver type as ``parent``.
Deeper nesting (a class declared inside another class, a local fun inside a
method body) is intentionally not flattened into top-level symbols because
those units are not independently claimable from outside their enclosing
scope.

Generic type parameters (``fun <T> id(x: T): T``, ``class Box<T>``) are
excluded from the captured name because the name node sits before the
``type_parameters`` child in the grammar.
"""

from __future__ import annotations

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_kotlin"

# Cached parser; populated on first ``extract`` call.
_parser_kotlin = None


def _kotlin_parser():
    """Return a cached tree-sitter Kotlin parser instance."""

    global _parser_kotlin
    if _parser_kotlin is not None:
        return _parser_kotlin
    import tree_sitter_kotlin as ts_kotlin
    from tree_sitter import Language, Parser

    language = Language(ts_kotlin.language())
    _parser_kotlin = Parser(language)
    return _parser_kotlin


def _decl_name(node) -> str | None:
    """Return the declared name of a declaration node, or ``None``.

    The Kotlin grammar exposes the name through an ``identifier`` (the
    ``tree-sitter-kotlin`` 1.x revision) or, on older fwcd revisions, a
    ``simple_identifier`` (functions, properties, objects) / ``type_identifier``
    (classes). We first try the ``name`` field if the grammar revision exposes
    one, then fall back to scanning the direct children. Scanning stops before
    the body / parameter list so we never pick up a parameter or member name.
    """

    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8")

    for child in node.children:
        if child.type in {"identifier", "type_identifier", "simple_identifier"}:
            return child.text.decode("utf-8")
        if child.type in {
            "class_body",
            "enum_class_body",
            "function_body",
            "function_value_parameters",
            "primary_constructor",
        }:
            break
    return None


def _has_modifier(node, keyword: str) -> bool:
    """True when ``node`` carries ``keyword`` among its leading modifiers.

    Kotlin spells ``enum class`` / ``data class`` / ``sealed class`` with the
    qualifier as a modifier preceding the ``class`` keyword. The grammar groups
    these under a ``modifiers`` child; older revisions inline the modifier
    tokens directly. We accept either shape and stop scanning once the
    ``class`` / ``interface`` keyword is reached so a body token never matches.
    """

    for child in node.children:
        if child.type == "modifiers":
            for mod in child.children:
                if mod.text.decode("utf-8") == keyword:
                    return True
                if mod.type == keyword:
                    return True
        elif child.type == keyword:
            return True
        elif child.type in {"class", "interface", "type_identifier"}:
            break
    return False


def _is_interface(node) -> bool:
    """True when a ``class_declaration`` is actually an ``interface``."""

    for child in node.children:
        if child.type == "interface" or child.text.decode("utf-8") == "interface":
            return True
        if child.type in {"class", "type_identifier", "modifiers"}:
            # A ``modifiers`` block precedes both keywords; keep scanning past
            # it, but stop once the concrete ``class`` keyword or the name is
            # reached -- an interface keyword always appears before those.
            if child.type == "class":
                return False
            continue
    return False


def _class_kind(node) -> str:
    """Map a ``class_declaration`` to ``'interface' | 'enum' | 'class'``."""

    if _is_interface(node):
        return "interface"
    if _has_modifier(node, "enum"):
        return "enum"
    return "class"


def _find_body(node):
    """Return the body node of a class / interface / object, or ``None``."""

    for child in node.children:
        if child.type in {"class_body", "enum_class_body"}:
            return child
    return None


def _companion_name(node) -> str:
    """Return the name of a ``companion_object`` (defaults to ``Companion``)."""

    for child in node.children:
        if child.type in {"identifier", "type_identifier", "simple_identifier"}:
            return child.text.decode("utf-8")
    return "Companion"


def _members(body, parent_name: str) -> list[Symbol]:
    """Extract methods and the companion object from a class / object body."""

    out: list[Symbol] = []
    for child in body.children:
        if child.type == "function_declaration":
            name = _decl_name(child)
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
        elif child.type == "companion_object":
            companion_name = _companion_name(child)
            out.append(
                Symbol(
                    name=companion_name,
                    kind="class",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    parent=parent_name,
                )
            )
            # Methods declared inside the companion body are claimable units
            # too; attach them to the companion object so ``Companion::create``
            # is addressable.
            companion_body = _find_body(child)
            if companion_body is not None:
                for member in companion_body.children:
                    if member.type != "function_declaration":
                        continue
                    member_name = _decl_name(member)
                    if member_name is None:
                        continue
                    out.append(
                        Symbol(
                            name=member_name,
                            kind="function",
                            start_line=member.start_point[0] + 1,
                            end_line=member.end_point[0] + 1,
                            parent=companion_name,
                        )
                    )
    return out


def _property_names(node) -> list[str]:
    """Return the bound names of a ``property_declaration``.

    Covers the common single-binding case (``val x = 1``) and the destructuring
    form (``val (a, b) = pair``) where the grammar nests a
    ``multi_variable_declaration`` of ``variable_declaration`` nodes.
    """

    names: list[str] = []
    for child in node.children:
        if child.type == "variable_declaration":
            for sub in child.children:
                if sub.type in {"identifier", "simple_identifier"}:
                    names.append(sub.text.decode("utf-8"))
                    break
        elif child.type == "multi_variable_declaration":
            for var in child.children:
                if var.type == "variable_declaration":
                    for sub in var.children:
                        if sub.type in {"identifier", "simple_identifier"}:
                            names.append(sub.text.decode("utf-8"))
                            break
    return names


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Only direct children of ``source_file`` become top-level symbols. Class /
    interface / object bodies are descended exactly one level so methods and
    the companion object surface with the enclosing type as ``parent``.
    """

    parser = _kotlin_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        if child.type == "function_declaration":
            name = _decl_name(child)
            if name is None:
                continue
            out.append(
                Symbol(
                    name=name,
                    kind="function",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
        elif child.type == "class_declaration":
            name = _decl_name(child)
            if name is None:
                continue
            out.append(
                Symbol(
                    name=name,
                    kind=_class_kind(child),
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
            body = _find_body(child)
            if body is not None:
                out.extend(_members(body, name))
        elif child.type == "object_declaration":
            name = _decl_name(child)
            if name is None:
                continue
            out.append(
                Symbol(
                    name=name,
                    kind="class",
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )
            body = _find_body(child)
            if body is not None:
                out.extend(_members(body, name))
        elif child.type == "property_declaration":
            for name in _property_names(child):
                out.append(
                    Symbol(
                        name=name,
                        kind="const",
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
        # Everything else (package_header, import_list, comments, type aliases
        # we do not model) is ignored: those are not claimable units.

    return out
