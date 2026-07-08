"""Robust parsing of ``.coordination/local.env`` files.

A single parser shared by every consumer of ``local.env`` so they all agree
on the value of a key. Historically three readers disagreed: the MCP server's
loader stripped quotes, ``coord doctor``'s token reader did not (and returned
the *first* match), and the pre-push hook just ``source``-d the file in bash
(last assignment wins, quotes stripped). A token that was quoted, indented,
duplicated, or separated by blank lines would then work in some places and
401 in others -- a confusing failure mode for operators.

:func:`parse_env` is deliberately close to what POSIX shell ``source`` does
for the simple ``KEY=VALUE`` lines these files contain:

- blank lines and ``#`` comment lines are ignored;
- leading/trailing whitespace on the line and around the value is stripped;
- a leading ``export `` is allowed and ignored;
- one layer of matching surrounding quotes (``"`` or ``'``) is removed;
- the LAST assignment of a key wins (so an appended fresh token overrides a
  stale one above it, matching ``source``).

It does NOT attempt full shell semantics (variable expansion, escapes,
multi-line values); local.env is a flat list of ``KEY=VALUE`` lines.
"""

from __future__ import annotations

from pathlib import Path


def parse_env(text: str) -> dict[str, str]:
    """Parse ``text`` (the contents of a local.env file) into a dict.

    Last assignment of a key wins. See the module docstring for the exact
    normalisation applied to each value.
    """

    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, val = line.partition("=")
        if sep != "=":
            continue
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("\"", "'"):
            val = val[1:-1]
        out[key] = val
    return out


def read_env_file(path: Path) -> dict[str, str]:
    """Read and parse a local.env file. Returns an empty dict if the file is
    absent or unreadable (callers treat a missing file as "no config")."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_env(text)


def _line_key(raw: str) -> str | None:
    """The key a ``KEY=VALUE`` line assigns, normalised exactly the way
    :func:`parse_env` normalises it (whitespace stripped, a leading
    ``export `` tolerated). Returns None for blank lines, comments, and
    lines with no ``=``."""

    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    key, sep, _ = line.partition("=")
    if sep != "=":
        return None
    key = key.strip()
    return key or None


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Rewrite ``path`` with ``updates`` applied in place, preserving
    everything coord does not manage.

    This is the single writer both ``coord init`` and ``coord upgrade`` use
    to refresh ``.coordination/local.env``. Rules:

    - Comment lines, blank lines, and assignments of keys NOT in ``updates``
      (e.g. ``COORD_USER``, ``COORD_BRANCH``, ``COORD_REPO_ROOT`` -- every
      key the MCP wrapper's ``_LOCAL_ENV_KEYS`` bootstraps, plus anything
      else an operator added) are kept verbatim, in their original order.
    - The FIRST assignment line of each updated key is replaced with the
      canonical ``KEY=VALUE`` form; later duplicate assignments of the same
      key are dropped. :func:`parse_env` applies last-assignment-wins, so
      callers resolve the effective value first and the rewrite collapses
      the duplicates onto that single authoritative line.
    - Updated keys absent from the file are appended at the end, in the
      order ``updates`` lists them. A missing file is created fresh.
    """

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    out: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        key = _line_key(raw)
        if key is None or key not in updates:
            out.append(raw)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{key}={updates[key]}")
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
