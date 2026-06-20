"""Regression: a missing native tree-sitter grammar must degrade to the regex
backend in ``auto`` mode, not crash the caller.

The tree-sitter backends import their grammar lazily (inside the parser
getter), so the backend *module* imports fine even when the grammar wheel is
absent -- the ``ModuleNotFoundError`` historically only surfaced when
``extract()`` was finally called, escaping auto-mode's fallback and crashing
``claim_files`` with e.g. ``No module named 'tree_sitter_typescript'``.

We simulate the missing wheel by pointing a backend's ``GRAMMAR_MODULE`` at a
name that ``find_spec`` can't resolve, which is exactly the signal the
dispatcher now probes.
"""

from __future__ import annotations

import pytest

from coordination import symbols
from coordination.symbols import extract_symbols, probe_backend, ts_treesitter


@pytest.fixture(autouse=True)
def _clear_symbol_cache():
    symbols._CACHE.clear()
    yield
    symbols._CACHE.clear()


def _simulate_missing_grammar(monkeypatch) -> None:
    # find_spec returns None for a non-existent module -> backend unavailable.
    monkeypatch.setattr(ts_treesitter, "GRAMMAR_MODULE", "tree_sitter_not_installed_xyz")
    monkeypatch.delenv("COORD_SYMBOL_PARSER", raising=False)  # default = auto


def test_auto_falls_back_to_regex_when_grammar_missing(monkeypatch):
    _simulate_missing_grammar(monkeypatch)
    # Must NOT raise; the regex backend extracts the symbol instead.
    syms = extract_symbols("widget.ts", "export function render() { return 1 }\n")
    assert any(s.name == "render" for s in syms)


def test_probe_reports_regex_when_grammar_missing(monkeypatch):
    _simulate_missing_grammar(monkeypatch)
    status, detail = probe_backend(".ts")
    assert status == "regex", detail


def test_treesitter_mode_still_raises_when_grammar_missing(monkeypatch):
    """Forcing the tree-sitter backend stays loud (the v0.14 contract)."""
    _simulate_missing_grammar(monkeypatch)
    monkeypatch.setenv("COORD_SYMBOL_PARSER", "treesitter")
    with pytest.raises(RuntimeError):
        extract_symbols("widget.ts", "export function render() {}\n")
