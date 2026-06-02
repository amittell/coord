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
import os
from dataclasses import dataclass
from typing import Callable

__all__ = ["Symbol", "extract_symbols"]


@dataclass(frozen=True)
class Symbol:
    """A top-level declaration in a source file.

    ``start_line`` and ``end_line`` are 1-indexed and inclusive. ``kind`` is one
    of ``'function' | 'class' | 'interface' | 'type' | 'const' | 'enum'`` plus
    the catch-all ``'unknown'`` reserved for future backends.
    """

    name: str
    kind: str
    start_line: int
    end_line: int


# Backend = Callable[[str], list[Symbol]]
_TS_EXTENSIONS = {".ts", ".tsx"}

# Per-process memoisation. Key: (file_path, sha256 hex of content).
_CACHE: dict[tuple[str, str], list[Symbol]] = {}


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _select_ts_backend() -> Callable[[str], list[Symbol]]:
    """Return the TypeScript backend honouring ``COORD_SYMBOL_PARSER``."""

    preference = os.environ.get("COORD_SYMBOL_PARSER", "auto").strip().lower()
    if preference not in {"treesitter", "regex", "auto"}:
        # Unknown value -- fall back to auto rather than crash. The doctor
        # surface is the right place to enforce strict validation.
        preference = "auto"

    if preference == "regex":
        from . import ts_regex

        return ts_regex.extract

    if preference == "treesitter":
        # Hard failure surfaces as RuntimeError so misconfiguration is loud.
        try:
            from . import ts_treesitter
        except ImportError as exc:  # pragma: no cover - exercised via env var
            raise RuntimeError(
                "COORD_SYMBOL_PARSER=treesitter but tree_sitter is not "
                "installed; install the 'symbols' extra or unset the var."
            ) from exc
        return ts_treesitter.extract

    # auto: prefer tree-sitter, silently fall back to regex.
    try:
        from . import ts_treesitter
    except ImportError:
        from . import ts_regex

        return ts_regex.extract
    return ts_treesitter.extract


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
    if dot:
        suffix = "." + ext.lower()
    else:
        suffix = ""

    if suffix in _TS_EXTENSIONS:
        backend = _select_ts_backend()
        result = backend(content)
    else:
        result = []

    _CACHE[cache_key] = result
    return result
