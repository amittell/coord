"""Best-effort proactive update notice.

After most CLI commands finish, check whether the configured service is
running a newer version of the coord package than the local install.
When it is, emit a one-line stderr banner pointing at `coord upgrade`.
The check is throttled to once per 24h via a timestamp file under
COORD_HOME and is silent on any failure path so it can never break a
working command.

Skip rules (must be cheap, must not surprise):

- COORD_NO_UPDATE_CHECK=1 short-circuits before any I/O.
- Subcommands `init`, `start`, `_serve`, `doctor` skip: init is too
  early in the lifecycle to be useful, start/_serve is the server
  itself, doctor already runs an explicit version check.
- If the cwd is not inside a coord-initialised repo, there is no
  service URL to query -- skip silently.
- If the cache file is younger than 24h, skip.
- Any network or parsing exception is swallowed; we still touch the
  cache so a flaky service doesn't make every command pay the timeout.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
from packaging.version import InvalidVersion, Version

from coordination.cli_shared import coord_home, find_repo_root
from coordination.repo_config import RepoConfig

_CACHE_NAME = "last_update_check"
_REFRESH_SECONDS = 24 * 3600
_NETWORK_TIMEOUT = 1.5  # seconds; this runs after the user's command finishes
_SKIP_SUBCOMMANDS = frozenset({"init", "start", "_serve", "doctor"})


def _cache_path() -> Path:
    return coord_home() / _CACHE_NAME


def _cache_is_fresh() -> bool:
    path = _cache_path()
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < _REFRESH_SECONDS


def _touch_cache() -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        os.utime(path, None)
    except OSError:
        # If we can't even touch the cache, the worst case is we re-run
        # the network check every command. Better than crashing the CLI.
        pass


def _service_url_from_repo() -> str | None:
    repo_root = find_repo_root()
    if repo_root is None:
        return None
    config_path = repo_root / ".coordination" / "config.toml"
    if not config_path.exists():
        return None
    try:
        config = RepoConfig.load(config_path)
    except Exception:
        return None
    return config.service_url


def maybe_emit_update_notice(*, client_version: str, subcommand: str) -> None:
    if os.environ.get("COORD_NO_UPDATE_CHECK"):
        return
    if subcommand in _SKIP_SUBCOMMANDS:
        return
    service_url = _service_url_from_repo()
    if service_url is None:
        return
    if _cache_is_fresh():
        return

    # Always touch the cache before/after the network call so a flaky
    # endpoint can't make every CLI invocation hang on a 1.5s timeout.
    _touch_cache()

    try:
        response = httpx.get(f"{service_url}/meta", timeout=_NETWORK_TIMEOUT)
        if response.status_code != 200:
            return
        body = response.json()
    except Exception:
        return

    server_version_str = body.get("version") if isinstance(body, dict) else None
    if not server_version_str:
        return
    try:
        server = Version(server_version_str)
        client = Version(client_version)
    except InvalidVersion:
        return

    # Only flag the case the user actually needs to act on: their CLI is
    # behind the cluster. The reverse case (CLI ahead of cluster) is for
    # ops to action via image tag bump and is already covered by doctor.
    if server <= client:
        return

    msg = (
        f"coord: a newer version is running on {service_url} "
        f"({server_version_str}, you have {client_version}). "
        f"Run 'coord upgrade' after updating your install. "
        f"Silence with COORD_NO_UPDATE_CHECK=1."
    )
    print(msg, file=sys.stderr)
