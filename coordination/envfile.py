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
