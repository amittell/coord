from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import stat
import sys

MANAGED_BEGIN = "<!-- coord:begin -->"
MANAGED_END = "<!-- coord:end -->"

# Fixed command-line fragment we expect for a coord background service.
# Used to verify via `ps` that a recorded PID actually belongs to us (and not
# to some unrelated process that reused the PID after ours exited).
COORD_SERVE_MARKER = "coordination.cli _serve"


def coord_home() -> Path:
    return Path(os.environ.get("COORD_HOME", "~/.coord")).expanduser()


def state_paths() -> dict[str, Path]:
    home = coord_home()
    return {
        "home": home,
        "data_dir": home / "data",
        "database_path": home / "data" / "coordination.db",
        "token_file": home / "token",
    }


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_token_file(path: Path) -> str:
    ensure_parent(path)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.write_text(f"{token}\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return token


def find_repo_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def ensure_managed_block(path: Path, content: str) -> None:
    ensure_parent(path)
    block = f"{MANAGED_BEGIN}\n{content.strip()}\n{MANAGED_END}\n"
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return
    existing = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(MANAGED_BEGIN)}.*?{re.escape(MANAGED_END)}\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing, count=1)
    else:
        suffix = "\n" if existing and not existing.endswith("\n") else ""
        updated = f"{existing}{suffix}\n{block}"
    path.write_text(updated, encoding="utf-8")


def has_managed_block(path: Path) -> bool:
    return path.exists() and MANAGED_BEGIN in path.read_text(encoding="utf-8")


def ensure_gitignore_entry(repo_root: Path, entry: str) -> None:
    path = repo_root / ".gitignore"
    block = f"{MANAGED_BEGIN}\n{entry}\n{MANAGED_END}\n"
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return
    existing = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(MANAGED_BEGIN)}.*?{re.escape(MANAGED_END)}\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing, count=1)
    elif entry not in existing.splitlines():
        suffix = "\n" if existing and not existing.endswith("\n") else ""
        updated = f"{existing}{suffix}\n{block}"
    else:
        updated = existing
    path.write_text(updated, encoding="utf-8")


def write_executable(path: Path, content: str) -> None:
    ensure_parent(path)
    path.write_text(content, encoding="utf-8")
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def local_coord_mcp_path() -> Path:
    return Path(sys.executable).parent / "coord-mcp"

