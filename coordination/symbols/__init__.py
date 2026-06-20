"""Symbol extraction for sub-file claims.

Public surface:

- :class:`Symbol` -- frozen dataclass describing a top-level declaration.
- :func:`extract_symbols` -- dispatcher that parses ``content`` based on the
  file extension and returns the top-level symbols.

The dispatcher honours the ``COORD_SYMBOL_PARSER`` environment variable. The
recognised values are:

- ``treesitter`` -- force the tree-sitter backend. ImportError propagates as a
  RuntimeError so misconfiguration is loud.
- ``regex`` -- force the regex backend. Useful for CI environments where the
  native tree-sitter wheels are unavailable.
- ``auto`` (default) -- attempt tree-sitter, fall back to regex if the import
  fails. This is the behaviour assumed by ``coord doctor``.

Results are cached in-process keyed on ``(file_path, sha256(content))`` so the
parser is invoked at most once per unique file payload during the life of the
interpreter. The cache is intentionally unbounded; tests and short-lived MCP
sessions are the only callers, and pruning would require ordering metadata we
do not otherwise need.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from dataclasses import dataclass
from typing import Callable

__all__ = ["Symbol", "extract_symbols", "probe_backend", "supported_extensions"]


@dataclass(frozen=True)
class Symbol:
    """A top-level declaration in a source file.

    ``start_line`` and ``end_line`` are 1-indexed and inclusive. ``kind`` is one
    of ``'function' | 'class' | 'interface' | 'type' | 'const' | 'enum'`` plus
    the catch-all ``'unknown'`` reserved for future backends.

    ``parent`` is ``None`` for top-level declarations. v0.16 introduced
    method-level symbols inside classes; for those, ``parent`` carries the
    enclosing class name so callers can address ``Foo.handle`` distinctly from
    a free-standing ``handle``. Older backends and free-standing declarations
    continue to emit ``parent=None``; positional construction still works
    because the new field has a default and is appended after ``end_line``.
    """

    name: str
    kind: str
    start_line: int
    end_line: int
    parent: str | None = None


Backend = Callable[[str], list[Symbol]]


# Registry: extension (with leading dot, lowercase) -> (language_label,
# treesitter_module_name, regex_module_name). Each backend module exposes
# a module-level ``extract(content: str) -> list[Symbol]`` function. The
# dispatcher imports lazily so a missing optional grammar (e.g.
# tree_sitter_python) only affects callers that target that language.
#
# v0.14: TypeScript only.
# v0.15: Python (.py) and Go (.go) added.
# v0.33: JavaScript, Rust, Java, C, C++, C#, Ruby, PHP, Kotlin, Swift, Scala.
#        ``.h`` maps to C and ``.hpp``/``.hh`` map to C++; no extension is
#        claimed by two languages.
_BACKENDS: dict[str, tuple[str, str, str]] = {
    ".ts": ("typescript", "ts_treesitter", "ts_regex"),
    ".tsx": ("typescript", "ts_treesitter", "ts_regex"),
    ".py": ("python", "py_treesitter", "py_regex"),
    ".go": ("go", "go_treesitter", "go_regex"),
    ".js": ("javascript", "javascript_treesitter", "javascript_regex"),
    ".jsx": ("javascript", "javascript_treesitter", "javascript_regex"),
    ".rs": ("rust", "rust_treesitter", "rust_regex"),
    ".java": ("java", "java_treesitter", "java_regex"),
    ".c": ("c", "c_treesitter", "c_regex"),
    ".h": ("c", "c_treesitter", "c_regex"),
    ".cc": ("cpp", "cpp_treesitter", "cpp_regex"),
    ".cpp": ("cpp", "cpp_treesitter", "cpp_regex"),
    ".cxx": ("cpp", "cpp_treesitter", "cpp_regex"),
    ".hpp": ("cpp", "cpp_treesitter", "cpp_regex"),
    ".hh": ("cpp", "cpp_treesitter", "cpp_regex"),
    ".cs": ("csharp", "csharp_treesitter", "csharp_regex"),
    ".rb": ("ruby", "ruby_treesitter", "ruby_regex"),
    ".php": ("php", "php_treesitter", "php_regex"),
    ".kt": ("kotlin", "kotlin_treesitter", "kotlin_regex"),
    ".kts": ("kotlin", "kotlin_treesitter", "kotlin_regex"),
    ".swift": ("swift", "swift_treesitter", "swift_regex"),
    ".scala": ("scala", "scala_treesitter", "scala_regex"),
    ".sc": ("scala", "scala_treesitter", "scala_regex"),
}

# Per-process memoisation. Key: (file_path, sha256 hex of content).
_CACHE: dict[tuple[str, str], list[Symbol]] = {}


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def supported_extensions() -> frozenset[str]:
    """Return the set of file extensions (with leading dot) the dispatcher
    knows how to parse. Useful for doctor checks and for callers that want
    to short-circuit before stat'ing a file."""

    return frozenset(_BACKENDS.keys())


def _import_backend(module_name: str) -> Backend | None:
    """Import a backend module from this package; return its ``extract``
    callable or ``None`` if the module / its native dependency is missing.
    The caller decides how to react to ``None`` (auto mode falls back,
    treesitter mode raises).

    A tree-sitter backend imports its native grammar lazily (inside the
    parser getter), so the backend module itself imports fine even when the
    grammar wheel is absent -- the ``ModuleNotFoundError`` would otherwise
    only surface when ``extract()`` is finally called, escaping auto-mode's
    fallback and crashing the caller. To make availability honest at
    selection time, a tree-sitter backend declares ``GRAMMAR_MODULE`` (the
    name of its native wheel); we probe it with ``find_spec`` (cheap, no
    heavy import) and treat the backend as unavailable when it's missing.
    """

    try:
        module = __import__(
            f"coordination.symbols.{module_name}",
            fromlist=["extract"],
        )
    except ImportError:
        return None
    grammar = getattr(module, "GRAMMAR_MODULE", None)
    if grammar is not None:
        try:
            if importlib.util.find_spec(grammar) is None:
                return None
        except (ImportError, ValueError):
            return None
    extract = getattr(module, "extract", None)
    if not callable(extract):
        return None
    return extract


def _select_backend(extension: str) -> Backend | None:
    """Resolve a backend for ``extension`` honouring ``COORD_SYMBOL_PARSER``.

    Returns ``None`` when the extension has no registered backend.
    Raises :class:`RuntimeError` only in ``treesitter`` mode when the
    native grammar is missing (consistent with the v0.14 contract).
    """

    if extension not in _BACKENDS:
        return None
    _, treesitter_mod, regex_mod = _BACKENDS[extension]

    preference = os.environ.get("COORD_SYMBOL_PARSER", "auto").strip().lower()
    if preference not in {"treesitter", "regex", "auto"}:
        preference = "auto"

    if preference == "regex":
        return _import_backend(regex_mod)

    if preference == "treesitter":
        backend = _import_backend(treesitter_mod)
        if backend is None:
            raise RuntimeError(
                f"COORD_SYMBOL_PARSER=treesitter but {treesitter_mod} is "
                "not importable; install the 'symbols' extra or unset the var."
            )
        return backend

    # auto: prefer tree-sitter, fall back to regex silently.
    backend = _import_backend(treesitter_mod)
    if backend is not None:
        return backend
    return _import_backend(regex_mod)


def probe_backend(extension: str) -> tuple[str, str]:
    """Resolve which backend would handle ``extension`` right now and why.

    Returns a ``(status, detail)`` tuple where ``status`` is one of:

    - ``"treesitter"`` -- the native tree-sitter backend imports cleanly and
      will be used (in ``auto`` or ``treesitter`` mode).
    - ``"regex"`` -- the regex fallback is what callers would get, either
      because ``COORD_SYMBOL_PARSER=regex`` is set or because the tree-sitter
      grammar is not importable on this machine.
    - ``"none"`` -- the extension is not registered, or no backend resolved
      (e.g. ``COORD_SYMBOL_PARSER=treesitter`` is set and the grammar is
      missing -- in that case ``detail`` carries the import error message).

    ``detail`` is a short human-readable explanation suitable for surfacing
    in ``coord doctor`` output. The function never raises; the
    ``treesitter``-mode RuntimeError that :func:`extract_symbols` would
    propagate is caught here and reported as ``status='none'`` so doctor can
    classify it as a hard failure instead of crashing.
    """

    if extension not in _BACKENDS:
        return ("none", f"no backend registered for {extension}")
    _, treesitter_mod, regex_mod = _BACKENDS[extension]

    preference = os.environ.get("COORD_SYMBOL_PARSER", "auto").strip().lower()
    if preference not in {"treesitter", "regex", "auto"}:
        preference = "auto"

    if preference == "regex":
        backend = _import_backend(regex_mod)
        if backend is not None:
            return ("regex", "forced via COORD_SYMBOL_PARSER=regex")
        return ("none", f"regex backend {regex_mod} not importable")

    if preference == "treesitter":
        backend = _import_backend(treesitter_mod)
        if backend is not None:
            return ("treesitter", "ok")
        return (
            "none",
            f"COORD_SYMBOL_PARSER=treesitter but {treesitter_mod} not installed",
        )

    # auto
    if _import_backend(treesitter_mod) is not None:
        return ("treesitter", "ok")
    if _import_backend(regex_mod) is not None:
        return ("regex", f"fallback: {treesitter_mod} not installed")
    return ("none", f"neither {treesitter_mod} nor {regex_mod} importable")


def extract_symbols(file_path: str, content: str) -> list[Symbol]:
    """Return top-level declared symbols in ``content``.

    Dispatch is by file extension. Unsupported extensions return an empty
    list. The result is cached on ``(file_path, sha256(content))``.
    """

    cache_key = (file_path, _content_digest(content))
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached

    _, dot, ext = file_path.rpartition(".")
    suffix = "." + ext.lower() if dot else ""

    backend = _select_backend(suffix)
    result = backend(content) if backend is not None else []

    _CACHE[cache_key] = result
    return result
