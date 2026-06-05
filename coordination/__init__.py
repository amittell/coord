"""Multi-agent coordination service (HTTP API + MCP + dashboard)."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("coord-mcp-server")
except PackageNotFoundError:
    # Fallback for environments where the package is imported from a
    # source checkout without ``pip install`` having run (no dist-info
    # written). In normal installs (wheel, sdist, editable) the metadata
    # lookup succeeds. ``0.0.0+unknown`` is a valid PEP 440 local version
    # and parses cleanly through ``packaging.version.Version`` so the
    # update-notice path does not crash on it.
    __version__ = "0.0.0+unknown"


# Banner shown by `coord --version`. Figlet "slant" style with a tagline
# underneath. Kept as a raw string so the forward and back slashes in the
# letterforms do not have to be double-escaped.
BANNER = r"""                              __
   _________  ____  _________/ /
  / ___/ __ \/ __ \/ ___/ __  /
 / /__/ /_/ / /_/ / /  / /_/ /
 \___/\____/\____/_/   \__,_/
"""
