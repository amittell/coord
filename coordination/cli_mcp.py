"""``coord mcp install`` -- register the coord MCP server into the user's
AI coding tools so an operator never hand-edits a tool config file.

The thing this command exists to kill is the painful onboarding step where
someone copies a JSON/TOML stanza into ``~/.claude.json``, Codex's
``config.toml``, or a Cursor settings file by hand, gets the env block
wrong, and spends an afternoon debugging a 401. ``coord mcp install``:

- AUTO-DETECTS which supported tools are installed (CLI on PATH or a config
  directory/file present) and wires coord into each of them;
- AUTO-FILLS the connection settings (``COORD_API_URL``,
  ``COORD_AUTH_TOKEN``, ``COORD_REPO_ID``) from the repo's gitignored
  ``.coordination/local.env`` so the registered server works regardless of
  the tool's working directory;
- is IDEMPOTENT: it finds any existing coord entry (the ``coord`` key, or
  any server whose command runs ``coord-mcp``), UPDATES IT IN PLACE, never
  duplicates, and converges to the same config on every re-run.

Every other server/table/comment in a tool's config is preserved untouched.
A config holding invalid JSON is reported as an error rather than clobbered.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

from coordination.cli_shared import find_repo_root
from coordination.envfile import read_env_file

# The four tools we know how to wire, in a stable display/iteration order.
TOOLS = ("claude-code", "claude-desktop", "codex", "cursor")

# What every registered coord MCP server looks like. The env block carries
# the real connection settings so the server resolves them even when the
# tool spawns ``coord-mcp`` from an unrelated working directory.
_MCP_COMMAND = "coord-mcp"


class McpConfigError(Exception):
    """A tool's config could not be read or written safely.

    Raised (and caught per-tool by :func:`run_mcp`) for conditions where
    proceeding would risk corrupting an operator's file -- most importantly
    a config that exists but holds invalid JSON. We refuse rather than
    overwrite it.
    """


# ---------------------------------------------------------------------------
# connection settings from local.env
# ---------------------------------------------------------------------------


def _resolve_root(explicit: str | None) -> Path | None:
    """Return the repo root whose ``.coordination/local.env`` we read.

    Mirrors ``cli_init._resolve_root``: with no ``--root`` we fall back to
    the enclosing git work tree; with ``--root`` we resolve it (relative to
    cwd), require it to exist and to live inside a git work tree. Returns
    ``None`` on failure so the caller can emit a clear error and exit
    non-zero.
    """
    if explicit is None:
        return find_repo_root()
    candidate = Path(explicit)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    if not candidate.exists() or not candidate.is_dir():
        print(
            f"--root path does not exist or is not a directory: {candidate}",
            file=sys.stderr,
        )
        return None
    if find_repo_root(candidate) is None:
        print(
            f"--root path is not inside a git work tree: {candidate}",
            file=sys.stderr,
        )
        return None
    return candidate


def _coord_env(local_env: dict[str, str]) -> dict[str, str]:
    """Build the MCP server env block from a parsed ``local.env``.

    ``COORD_AUTH_TOKEN`` is mandatory and is validated by the caller before
    we get here. ``COORD_API_URL`` (falling back to ``COORD_SERVICE_URL``,
    which ``coord init`` also writes) and ``COORD_REPO_ID`` are included
    only when present, so we never embed an empty placeholder that would
    shadow a value the wrapper could otherwise resolve.
    """
    env: dict[str, str] = {}
    api_url = local_env.get("COORD_API_URL") or local_env.get("COORD_SERVICE_URL")
    if api_url:
        env["COORD_API_URL"] = api_url
    env["COORD_AUTH_TOKEN"] = local_env["COORD_AUTH_TOKEN"]
    repo_id = local_env.get("COORD_REPO_ID")
    if repo_id:
        env["COORD_REPO_ID"] = repo_id
    return env


def _server_entry(coord_env: dict[str, str]) -> dict[str, object]:
    """The canonical coord server entry for a JSON-config tool."""
    return {"command": _MCP_COMMAND, "args": [], "env": dict(coord_env)}


# ---------------------------------------------------------------------------
# tool detection + config paths
# ---------------------------------------------------------------------------


def _claude_desktop_config_path() -> Path:
    """Per-platform location of Claude Desktop's config file.

    Derived from ``Path.home()`` (and ``%APPDATA%`` on Windows) so a test
    that monkeypatches ``HOME`` redirects it without any extra hook.
    """
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _config_path(tool: str) -> Path:
    """Absolute config-file path coord manages for ``tool``."""
    home = Path.home()
    if tool == "claude-code":
        return home / ".claude.json"
    if tool == "claude-desktop":
        return _claude_desktop_config_path()
    if tool == "codex":
        return home / ".codex" / "config.toml"
    if tool == "cursor":
        return home / ".cursor" / "mcp.json"
    raise ValueError(f"unknown tool {tool!r}")


def _is_detected(tool: str) -> bool:
    """Heuristic presence check for ``tool``.

    A tool counts as present when its CLI is on PATH or its config
    directory/file already exists -- either signal means the operator uses
    it and would want coord wired in.
    """
    home = Path.home()
    if tool == "claude-code":
        return shutil.which("claude") is not None or (home / ".claude.json").exists()
    if tool == "claude-desktop":
        return _claude_desktop_config_path().exists()
    if tool == "codex":
        return (home / ".codex").exists() or shutil.which("codex") is not None
    if tool == "cursor":
        return (home / ".cursor").exists()
    return False


# ---------------------------------------------------------------------------
# JSON-config tools (claude-code, claude-desktop, cursor)
# ---------------------------------------------------------------------------


def _is_coord_server(name: str, value: object) -> bool:
    """True when an mcpServers entry is coord's.

    Either it is keyed ``coord`` or its command runs ``coord-mcp`` under any
    key -- both forms are treated as the same logical server so a re-run
    converges them onto the single ``coord`` key instead of duplicating.
    """
    if name == "coord":
        return True
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and _MCP_COMMAND in command:
            return True
    return False


def _write_json_config(
    path: Path, coord_env: dict[str, str], dry_run: bool
) -> str:
    """Create/update a JSON MCP config, returning ``"created"``/``"updated"``.

    Loads the existing file (refusing to proceed on invalid JSON so we never
    clobber it), drops every pre-existing coord server (by key or by
    command), then sets a single ``mcpServers.coord`` entry. All other keys
    and servers are preserved. On ``--dry-run`` nothing is written.
    """
    existed = path.exists()
    if existed:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise McpConfigError(f"cannot read {path}: {exc}") from exc
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise McpConfigError(
                f"{path} is not valid JSON ({exc}); refusing to overwrite it. "
                "Fix or remove the file and re-run."
            ) from exc
        if not isinstance(data, dict):
            raise McpConfigError(
                f"{path} does not contain a JSON object; refusing to overwrite it."
            )
    else:
        data = {}

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    # Drop any existing coord server (the "coord" key or a coord-mcp command
    # under any key) so the rewrite converges onto a single entry.
    for name in [n for n, v in servers.items() if _is_coord_server(n, v)]:
        del servers[name]
    servers["coord"] = _server_entry(coord_env)
    data["mcpServers"] = servers

    if dry_run:
        return "updated" if existed else "created"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return "updated" if existed else "created"


# ---------------------------------------------------------------------------
# Codex (TOML)
# ---------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    """Render ``value`` as a TOML basic string with the two escapes that
    matter for the URL / token / repo-id values we emit (backslash and
    double-quote). coord tokens are URL-safe base64 so this is belt-and-
    braces, but a hand-edited local.env could contain either character."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _codex_coord_block(coord_env: dict[str, str]) -> str:
    """The fresh ``[mcp_servers.coord]`` table text (mirrors the layout
    ``coord init`` writes), carrying the real env values."""
    lines = [
        "[mcp_servers.coord]",
        f"command = {_toml_str(_MCP_COMMAND)}",
        "args = []",
        "enabled = true",
        "required = false",
        "tool_timeout_sec = 30",
        "",
        "[mcp_servers.coord.env]",
    ]
    for key in ("COORD_API_URL", "COORD_AUTH_TOKEN", "COORD_REPO_ID"):
        if key in coord_env:
            lines.append(f"{key} = {_toml_str(coord_env[key])}")
    return "\n".join(lines) + "\n"


_TABLE_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _strip_codex_coord_tables(text: str, coord_keys: set[str]) -> str:
    """Remove coord's TOML tables from ``text`` by block surgery.

    Splits ``text`` into blocks at top-level ``[header]`` lines and drops any
    block whose header is ``[mcp_servers.<key>]`` or
    ``[mcp_servers.<key>.env]`` for a key in ``coord_keys``. Every other
    block -- including unrelated servers, comments, and top-level keys -- is
    preserved verbatim. Using text surgery (rather than re-serialising the
    parsed dict) is what keeps the rest of the operator's config byte-for-
    byte intact without pulling in a TOML writer dependency.
    """
    drop_headers = set()
    for key in coord_keys:
        drop_headers.add(f"mcp_servers.{key}")
        drop_headers.add(f"mcp_servers.{key}.env")

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    dropping = False
    for line in lines:
        match = _TABLE_HEADER_RE.match(line)
        if match is not None:
            header = match.group(1).strip()
            dropping = header in drop_headers
            if dropping:
                continue
        if dropping:
            continue
        out.append(line)
    return "".join(out)


def _codex_coord_keys(parsed: dict[str, object]) -> set[str]:
    """Server keys under ``[mcp_servers]`` that belong to coord.

    The ``coord`` key always qualifies; so does any server whose ``command``
    runs ``coord-mcp``. Returning the set lets the strip pass remove a coord
    server that an earlier hand-edit stored under a different name.
    """
    keys = {"coord"}
    servers = parsed.get("mcp_servers")
    if isinstance(servers, dict):
        for name, value in servers.items():
            if isinstance(value, dict):
                command = value.get("command")
                if isinstance(command, str) and _MCP_COMMAND in command:
                    keys.add(name)
    return keys


def _write_codex_config(
    path: Path, coord_env: dict[str, str], dry_run: bool
) -> str:
    """Create/update Codex's ``config.toml`` coord table in place.

    Parses the existing file with ``tomllib`` to validate it and to locate
    any coord server (refusing on a parse error rather than clobbering),
    strips coord's table(s) via :func:`_strip_codex_coord_tables`, then
    appends a single fresh ``[mcp_servers.coord]`` table. Everything else in
    the file is preserved. Nothing is written on ``--dry-run``.
    """
    existed = path.exists()
    if existed:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise McpConfigError(f"cannot read {path}: {exc}") from exc
        try:
            parsed = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            raise McpConfigError(
                f"{path} is not valid TOML ({exc}); refusing to overwrite it. "
                "Fix or remove the file and re-run."
            ) from exc
        stripped = _strip_codex_coord_tables(raw, _codex_coord_keys(parsed))
    else:
        stripped = ""

    block = _codex_coord_block(coord_env)
    # Separate the preserved body from our appended table by exactly one
    # blank line, with no leading blank lines on a brand-new file.
    body = stripped.rstrip("\n")
    new_text = f"{body}\n\n{block}" if body else block

    if dry_run:
        return "updated" if existed else "created"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return "updated" if existed else "created"


# ---------------------------------------------------------------------------
# per-tool dispatch
# ---------------------------------------------------------------------------


def _install_tool(
    tool: str, coord_env: dict[str, str], dry_run: bool
) -> str:
    """Write ``tool``'s config and return the ``created``/``updated`` verb."""
    path = _config_path(tool)
    if tool == "codex":
        return _write_codex_config(path, coord_env, dry_run)
    return _write_json_config(path, coord_env, dry_run)


def _select_tools(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Resolve which tools to act on.

    Returns ``(targets, skipped)``. With ``--all`` every tool is a target.
    With one or more ``--tool`` those (deduped, in canonical order) are
    targets. With neither we auto-detect: detected tools become targets and
    the rest are reported as skipped.
    """
    if args.all:
        return list(TOOLS), []
    if args.tool:
        chosen = [t for t in TOOLS if t in set(args.tool)]
        return chosen, []
    targets = [t for t in TOOLS if _is_detected(t)]
    skipped = [t for t in TOOLS if t not in targets]
    return targets, skipped


def run_mcp(args: argparse.Namespace) -> int:
    """Top-level dispatch for ``coord mcp``.

    Only ``install`` exists today; the nested subparser guarantees an action
    is present, so an empty action is an internal misconfiguration.
    """
    action = getattr(args, "mcp_action", None)
    if action != "install":
        print("Unknown mcp subcommand", file=sys.stderr)
        return 1

    repo_root = _resolve_root(getattr(args, "root", None))
    if repo_root is None:
        if getattr(args, "root", None) is None:
            print("Not inside a git repository.", file=sys.stderr)
        # _resolve_root already printed a specific message for --root failures.
        return 1

    env_path = repo_root / ".coordination" / "local.env"
    local_env = read_env_file(env_path)
    if not local_env.get("COORD_AUTH_TOKEN", "").strip():
        print(
            f"No COORD_AUTH_TOKEN found in {env_path}. "
            "Run 'coord init' in this repo first to generate the coordination "
            "config, then re-run 'coord mcp install'.",
            file=sys.stderr,
        )
        return 1

    coord_env = _coord_env(local_env)
    targets, skipped = _select_tools(args)

    if not targets and not skipped:
        # --tool with no valid choice cannot happen (argparse validates
        # choices) and --all always yields targets; this only triggers when
        # auto-detect found nothing.
        print(
            "No supported AI coding tools detected "
            f"({', '.join(TOOLS)}). Pass --tool or --all to force a target.",
            file=sys.stderr,
        )
        return 1

    if not args.tool and not args.all:
        # Auto-detect mode: tell the operator what we saw before acting.
        print(
            "Detected: " + (", ".join(targets) if targets else "(none)")
        )
        if skipped:
            print("Skipped (not detected): " + ", ".join(skipped))

    dry = bool(args.dry_run)
    rc = 0
    for tool in targets:
        path = _config_path(tool)
        try:
            verb = _install_tool(tool, coord_env, dry)
        except McpConfigError as exc:
            print(f"  {tool}: error -- {exc}", file=sys.stderr)
            rc = 1
            continue
        prefix = "DRY RUN: would " + verb if dry else verb
        print(f"  {tool}: {prefix} {path}")

    if not dry and rc == 0:
        print("")
        print("Registered the coord MCP server (command: coord-mcp).")
    return rc


# ---------------------------------------------------------------------------
# parser registration
# ---------------------------------------------------------------------------


def add_mcp_subparser(sub: argparse._SubParsersAction) -> None:
    """Wire ``coord mcp install`` onto ``sub``.

    Kept as a registration helper so ``cli.build_parser`` stays the single
    place subcommand registration lives -- this module owns the flag
    surface, the cli module owns ordering.
    """
    mcp = sub.add_parser(
        "mcp",
        help="Register the coord MCP server into your AI coding tools",
        description=(
            "Manage the coord MCP server registration in supported AI coding "
            "tools (Claude Code, Claude Desktop, Codex, Cursor) so you never "
            "hand-edit their config files."
        ),
    )
    nested = mcp.add_subparsers(dest="mcp_action", required=True)

    install = nested.add_parser(
        "install",
        help="Install/update the coord MCP server in detected tools",
        description=(
            "Register an MCP server named 'coord' (command: coord-mcp) into "
            "each target tool, filling its connection env from this repo's "
            ".coordination/local.env. With no --tool/--all the command auto-"
            "detects which supported tools are installed and wires each one. "
            "Idempotent: an existing coord entry is updated in place, never "
            "duplicated, and re-running converges to the same config."
        ),
    )
    install.add_argument(
        "--tool",
        action="append",
        choices=list(TOOLS),
        help=(
            "Target a specific tool (repeatable). Omit --tool and --all to "
            "auto-detect installed tools."
        ),
    )
    install.add_argument(
        "--all",
        action="store_true",
        help="Install into all four supported tools regardless of detection",
    )
    install.add_argument(
        "--root",
        help=(
            "Repo root holding .coordination/local.env (absolute or relative "
            "to cwd). Defaults to the enclosing git repo root."
        ),
    )
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing any file",
    )
    install.set_defaults(func=run_mcp)

    mcp.set_defaults(func=run_mcp)
