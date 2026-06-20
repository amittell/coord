"""Tree-sitter backed C++ symbol extractor.

Walks the top level of the ``translation_unit`` node produced by
``tree-sitter-cpp`` and emits one :class:`Symbol` per claimable declaration.
This backend owns the C++ source/header extensions (``.cc``, ``.cpp``,
``.cxx``, ``.hpp``, ``.hh``). The bare ``.h`` extension is owned by the C
backend, not this one, because a ``.h`` file is far more often C than C++.

Recognised top-level declarations:

- ``function_definition`` -- ``kind='function'``. Covers free functions and
  out-of-line member definitions. The name lives at the bottom of a
  declarator chain that may be wrapped in ``pointer_declarator`` /
  ``reference_declarator`` (``int* f()``, ``int& g()``) before reaching the
  ``function_declarator``. When the function declarator's name is a
  ``qualified_identifier`` (``void Foo::bar() {}``) the qualifier becomes the
  ``parent`` so ``Foo::bar`` notation resolves; the bare ``bar`` is the name.
- ``class_specifier`` -- ``kind='class'``. The class body is walked one or
  more levels deep so member functions surface with ``parent`` set to the
  enclosing class path (``Foo`` for a direct member, ``Foo::Inner`` for a
  member of a nested class). Nested ``class``/``struct`` specifiers recurse
  with the path extended by ``"::<inner name>"``.
- ``struct_specifier`` -- ``kind='type'``. C++ structs are classes with
  default-public access; coord treats them as the ``type`` grain to match the
  Go backend's struct handling, while still walking their bodies for methods.
- ``enum_specifier`` -- ``kind='enum'``. Scoped (``enum class Color``) and
  unscoped (``enum Color``) enums both use this node; the enumerators inside
  are not emitted as separate claimable units.

Not claimable / skipped by design:

- ``namespace_definition`` -- a namespace is not a claimable unit and this
  backend does not recurse into namespace bodies. The extractor mirrors the
  Go backend's contract of walking only the direct children of the root node,
  so declarations nested inside a ``namespace { ... }`` block are not
  surfaced. Out-of-line member definitions written at file scope
  (``void Foo::bar() {}``) are still captured because they live at the top
  level of the translation unit.
- Data members of a class/struct (``int count_;``) are not callable units of
  coordination, so a ``field_declaration`` only emits a Symbol when its
  declarator is a ``function_declarator`` (a member-function prototype).
- Free declarations, ``using`` directives, ``typedef`` aliases, template
  parameter lists, and preprocessor output are ignored.

Template handling: ``template <...>`` wraps a ``template_declaration`` whose
body is the underlying ``function_definition`` / ``class_specifier`` /
``struct_specifier``. The extractor unwraps one level of
``template_declaration`` at the top level (and inside class bodies) so a
templated free function or class still surfaces with the correct kind. The
captured span starts at the ``template_declaration`` so the claim covers the
template parameter clause as well.

``start_line`` / ``end_line`` are 1-indexed and inclusive
(``node.start_point[0] + 1`` / ``node.end_point[0] + 1``).
"""

from __future__ import annotations

from typing import Any

from . import Symbol

# Native grammar wheel this backend needs; probed by the dispatcher so a
# missing wheel degrades to the regex backend instead of crashing at call time.
GRAMMAR_MODULE = "tree_sitter_cpp"

# Cached parser; populated on first ``extract`` call.
_parser_cpp: Any = None


def _cpp_parser() -> Any:
    """Return a cached tree-sitter C++ parser instance."""

    global _parser_cpp
    if _parser_cpp is not None:
        return _parser_cpp
    import tree_sitter_cpp as ts_cpp
    from tree_sitter import Language, Parser

    language = Language(ts_cpp.language())
    _parser_cpp = Parser(language)
    return _parser_cpp


# Declarator wrappers that sit between a ``function_definition`` /
# ``field_declaration`` and the ``function_declarator`` carrying the name.
_DECLARATOR_WRAPPERS = {
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
}


def _unwrap_to_function_declarator(node: Any) -> Any | None:
    """Descend a declarator chain to the ``function_declarator``, or None.

    A function definition's declarator may be wrapped in pointer / reference /
    parenthesized declarators (``int* f()``, ``int& g()``, ``int (f)()``).
    We unwrap those wrappers, following the ``declarator`` field, until we
    reach the ``function_declarator`` or run out of nodes.
    """

    current = node
    seen = 0
    while current is not None and seen < 16:
        if current.type == "function_declarator":
            return current
        if current.type in _DECLARATOR_WRAPPERS:
            current = current.child_by_field_name("declarator")
            seen += 1
            continue
        return None
    return None


def _name_and_parent(func_declarator: Any) -> tuple[str, str | None] | None:
    """Return ``(name, parent)`` for a ``function_declarator``.

    The declarator's ``declarator`` field is the thing being named:

    - ``identifier`` -> free function; ``parent=None``.
    - ``field_identifier`` -> member function named inside a class body; the
      caller supplies the enclosing class path as ``parent`` (this function
      returns ``parent=None`` and lets the caller override).
    - ``qualified_identifier`` -> out-of-line member (``Foo::bar``). The
      ``scope`` child is the qualifier (``Foo``) and becomes ``parent``; the
      ``name`` child (``bar``) is the symbol name. Nested qualifiers
      (``Outer::Inner::bar``) collapse the full scope text into ``parent``.
    - ``destructor_name`` / ``operator_name`` -> the full text is the name
      (``~Foo``, ``operator==``); ``parent=None`` unless qualified.
    """

    name_node = func_declarator.child_by_field_name("declarator")
    if name_node is None:
        return None

    node_type = name_node.type
    if node_type in {"identifier", "field_identifier"}:
        return name_node.text.decode("utf-8"), None

    if node_type in {"destructor_name", "operator_name"}:
        return name_node.text.decode("utf-8"), None

    if node_type == "qualified_identifier":
        scope = name_node.child_by_field_name("scope")
        inner = name_node.child_by_field_name("name")
        # The ``name`` of a qualified_identifier may itself be a nested
        # qualified_identifier (``A::B::c``). Walk down to the final
        # unqualified name while accumulating the full scope text.
        parent_text = scope.text.decode("utf-8") if scope is not None else None
        while inner is not None and inner.type == "qualified_identifier":
            deeper = inner.child_by_field_name("name")
            inner = deeper
        if inner is None:
            return None
        if inner.type in {
            "identifier",
            "field_identifier",
            "destructor_name",
            "operator_name",
        }:
            return inner.text.decode("utf-8"), parent_text
        return None

    return None


def _symbol_from_function_definition(
    node: Any,
    parent_path: str | None,
    span_node: Any | None = None,
) -> Symbol | None:
    """Build a Symbol for a ``function_definition``.

    ``parent_path`` is the enclosing class path for in-class definitions
    (``None`` at file scope). When the declarator is a ``qualified_identifier``
    (out-of-line ``Foo::bar``) the qualifier from the declarator wins so a
    file-scope out-of-line method still records ``parent='Foo'``.

    ``span_node`` lets a templated definition include its ``template <...>``
    clause in the line span; plain definitions pass ``span_node=None`` so the
    span comes from ``node`` itself.
    """

    declarator = node.child_by_field_name("declarator")
    func_declarator = _unwrap_to_function_declarator(declarator)
    if func_declarator is None:
        return None
    resolved = _name_and_parent(func_declarator)
    if resolved is None:
        return None
    name, qualified_parent = resolved

    parent = qualified_parent if qualified_parent is not None else parent_path

    span = span_node if span_node is not None else node
    return Symbol(
        name=name,
        kind="function",
        start_line=span.start_point[0] + 1,
        end_line=span.end_point[0] + 1,
        parent=parent,
    )


def _symbol_from_method_declaration(
    node: Any,
    parent_path: str | None,
) -> Symbol | None:
    """Build a Symbol for a member-function prototype (``field_declaration``).

    Only ``field_declaration`` nodes whose declarator is a
    ``function_declarator`` (after unwrapping pointer / reference wrappers)
    qualify; plain data members return ``None``. ``parent_path`` is the
    enclosing class path.
    """

    declarator = node.child_by_field_name("declarator")
    func_declarator = _unwrap_to_function_declarator(declarator)
    if func_declarator is None:
        return None
    resolved = _name_and_parent(func_declarator)
    if resolved is None:
        return None
    name, qualified_parent = resolved

    parent = qualified_parent if qualified_parent is not None else parent_path

    return Symbol(
        name=name,
        kind="function",
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        parent=parent,
    )


def _record_container(
    node: Any,
    kind: str,
    ancestor_path: str | None,
    span_node: Any | None,
    out: list[Symbol],
) -> None:
    """Emit a class/struct symbol and recurse into its body for members.

    ``kind`` is ``'class'`` or ``'type'`` (struct). ``ancestor_path`` is the
    path leading up to but not including this container. ``span_node`` carries
    a wrapping ``template_declaration`` so the span covers the template clause.
    The container symbol is appended first, then its members in source order
    (nested containers recursively expanded in place).
    """

    name_node = node.child_by_field_name("name")
    if name_node is None:
        # Anonymous struct/class (``struct { ... } x;``) is not addressable as
        # a claim target; skip it and do not recurse.
        return
    container_name = name_node.text.decode("utf-8")
    full_path = (
        f"{ancestor_path}::{container_name}" if ancestor_path else container_name
    )

    span = span_node if span_node is not None else node
    out.append(
        Symbol(
            name=container_name,
            kind=kind,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            parent=ancestor_path,
        )
    )

    body = node.child_by_field_name("body")
    if body is None:
        return
    _walk_container_body(body, full_path, out)


def _walk_container_body(
    body: Any,
    full_path: str,
    out: list[Symbol],
) -> None:
    """Walk the ``field_declaration_list`` of a class/struct body.

    Direct-child member functions (defined or declared), nested classes and
    structs are emitted; data members, access specifiers, and using
    declarations are skipped. Nested containers recurse with the path
    extended by ``"::<inner name>"``.
    """

    for child in body.children:
        child_type = child.type

        if child_type == "function_definition":
            sym = _symbol_from_function_definition(child, full_path)
            if sym is not None:
                out.append(sym)
            continue

        if child_type == "field_declaration":
            # A nested class / struct / enum inside a class body is wrapped in
            # a ``field_declaration`` (``class Inner { ... };`` as a member).
            # Unwrap that case so nested containers recurse; otherwise the
            # field_declaration is a member-function prototype or a data
            # member.
            nested = _nested_specifier_of(child)
            if nested is not None:
                _dispatch_specifier(nested, full_path, None, out)
                continue
            sym = _symbol_from_method_declaration(child, full_path)
            if sym is not None:
                out.append(sym)
            continue

        if child_type == "class_specifier":
            _record_container(child, "class", full_path, None, out)
            continue

        if child_type == "struct_specifier":
            _record_container(child, "type", full_path, None, out)
            continue

        if child_type == "enum_specifier":
            sym = _symbol_from_enum(child, full_path, None)
            if sym is not None:
                out.append(sym)
            continue

        if child_type == "template_declaration":
            _handle_template(child, full_path, out)
            continue

        # access_specifier, using_declaration, comment, data members, etc.
        # are not claimable member-function units and are ignored.


def _nested_specifier_of(field_declaration: Any) -> Any | None:
    """Return a class/struct/enum specifier wrapped by a ``field_declaration``.

    A nested type member (``class Inner { ... };``) appears in the grammar as
    a ``field_declaration`` whose first significant child is the specifier.
    Returns the specifier node, or ``None`` when the field_declaration is an
    ordinary data member or member-function prototype.
    """

    for child in field_declaration.children:
        if child.type in {
            "class_specifier",
            "struct_specifier",
            "enum_specifier",
        }:
            return child
    return None


def _dispatch_specifier(
    node: Any,
    parent_path: str | None,
    span_node: Any | None,
    out: list[Symbol],
) -> None:
    """Route a class/struct/enum specifier to its handler.

    Shared by the class-body walk so a nested specifier unwrapped from a
    ``field_declaration`` is handled identically to a direct child.
    """

    if node.type == "class_specifier":
        _record_container(node, "class", parent_path, span_node, out)
    elif node.type == "struct_specifier":
        _record_container(node, "type", parent_path, span_node, out)
    elif node.type == "enum_specifier":
        sym = _symbol_from_enum(node, parent_path, span_node)
        if sym is not None:
            out.append(sym)


def _symbol_from_enum(
    node: Any,
    parent_path: str | None,
    span_node: Any | None,
) -> Symbol | None:
    """Build a Symbol for an ``enum_specifier`` (scoped or unscoped)."""

    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    span = span_node if span_node is not None else node
    return Symbol(
        name=name_node.text.decode("utf-8"),
        kind="enum",
        start_line=span.start_point[0] + 1,
        end_line=span.end_point[0] + 1,
        parent=parent_path,
    )


def _handle_template(
    node: Any,
    parent_path: str | None,
    out: list[Symbol],
) -> None:
    """Unwrap a ``template_declaration`` and dispatch on its inner node.

    The span passed to the inner handler is the ``template_declaration`` so
    the emitted symbol covers the ``template <...>`` clause. Only one level of
    template wrapping is unwrapped; the inner node is the actual definition.
    """

    inner: Any = None
    for child in node.children:
        if child.type in {
            "function_definition",
            "class_specifier",
            "struct_specifier",
            "enum_specifier",
        }:
            inner = child
            break
    if inner is None:
        return

    if inner.type == "function_definition":
        sym = _symbol_from_function_definition(inner, parent_path, span_node=node)
        if sym is not None:
            out.append(sym)
        return
    if inner.type == "class_specifier":
        _record_container(inner, "class", parent_path, node, out)
        return
    if inner.type == "struct_specifier":
        _record_container(inner, "type", parent_path, node, out)
        return
    if inner.type == "enum_specifier":
        sym = _symbol_from_enum(inner, parent_path, node)
        if sym is not None:
            out.append(sym)


def extract(content: str) -> list[Symbol]:
    """Return top-level declarations as :class:`Symbol` instances.

    Walks the direct children of the ``translation_unit`` node. Free functions
    and out-of-line member definitions emit ``kind='function'`` (with
    ``parent`` set to the qualifier for ``Foo::bar`` forms). Classes
    (``kind='class'``), structs (``kind='type'``), and enums (``kind='enum'``)
    emit their own symbol; class and struct bodies are walked for member
    functions and nested containers, which carry the full ancestor path in
    ``parent``. Namespaces are not claimable and their bodies are not walked.
    """

    parser = _cpp_parser()
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    out: list[Symbol] = []
    for child in root.children:
        child_type = child.type

        if child_type == "function_definition":
            sym = _symbol_from_function_definition(child, None)
            if sym is not None:
                out.append(sym)
            continue

        if child_type == "class_specifier":
            _record_container(child, "class", None, None, out)
            continue

        if child_type == "struct_specifier":
            _record_container(child, "type", None, None, out)
            continue

        if child_type == "enum_specifier":
            sym = _symbol_from_enum(child, None, None)
            if sym is not None:
                out.append(sym)
            continue

        if child_type == "template_declaration":
            _handle_template(child, None, out)
            continue

        # namespace_definition, preproc_*, using_declaration, declaration
        # (data globals), comments, and linkage_specification bodies are not
        # claimable top-level units and are skipped. We deliberately do not
        # recurse into namespaces -- only direct children of translation_unit
        # are top-level for coordination purposes.

    return out
