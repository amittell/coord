"""Claude Code hooks for coord (v20) -- enforcement, not just reminders.

The managed CLAUDE.md block asks agents to claim before editing; the live
fleet showed mandates alone decay under context pressure (2026-07-17: two
initialized repos, zero claims filed). These hooks make the protocol
self-executing for Claude Code sessions:

- ``sessionstart``  advisory: injects the repo's active claims and a
  token-scope-vs-repo mismatch warning as session context.
- ``pretool``       enforcing: before Edit/Write, checks the target file
  against OTHER holders' claims -- blocks (exit 2) on conflict -- and
  otherwise auto-claims it once per Claude session (intent derived from
  action; no ritual to remember). Claimed paths are cached per session so
  repeated edits do not create repeated server rows.
- ``sessionend``    hygiene: releases the session in one bulk API call.

Every network failure fails OPEN (exit 0, no output): coordination being
down must never block editing. Installed by ``coord init --tool claude``
into ``.claude/settings.json``; the commands run ``coord-claude-hook``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from coordination.envfile import parse_env

_TIMEOUT_S = 3.0
_AUTO_TTL_HOURS = 4
_AUTO_DESC = "auto-claim (coord Claude Code hook)"
_CACHE_VERIFY_S = 60.0
_LOCK_WAIT_S = 7.0
_SESSIONEND_BUDGET_S = 12.0

# Tools whose input names a file this session is about to mutate.
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _find_local_env(start: Path) -> tuple[Path | None, dict[str, str]]:
    """Walk ``start`` -> filesystem root for ``.coordination/local.env``."""
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        path = candidate / ".coordination" / "local.env"
        if path.is_file():
            try:
                return candidate, parse_env(path.read_text(encoding="utf-8"))
            except Exception:
                return candidate, {}
    return None, {}


class CoordClient:
    """Minimal stdlib HTTP client (hooks must start fast; no httpx import)."""

    def __init__(self, base_url: str, token: str | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body)


def _client(env: dict[str, str]) -> CoordClient | None:
    base = (
        os.environ.get("COORD_API_URL") or env.get("COORD_API_URL") or env.get("COORD_SERVICE_URL")
    )
    if not base:
        return None
    token = os.environ.get("COORD_AUTH_TOKEN") or env.get("COORD_AUTH_TOKEN")
    if token in (None, "", "set-me", "SET-ME", "changeme", "CHANGEME", "REPLACE_ME", "TODO"):
        token = None
    return CoordClient(base, token)


def _session_endpoint(session_id: str, action: str, env: dict[str, str]) -> str:
    encoded = urllib.parse.quote(session_id, safe="")
    path = f"/sessions/{encoded}/{action}"
    repo = _repo_id(env)
    if repo:
        path += "?" + urllib.parse.urlencode({"repo": repo})
    return path


def _repo_id(env: dict[str, str]) -> str | None:
    """Return the configured repo identity used by every hook request."""
    value = os.environ.get("COORD_REPO_ID") or env.get("COORD_REPO_ID")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _payload_session_id(payload: dict[str, Any]) -> str | None:
    """Return Claude's stable session id, rejecting malformed hook input."""
    value = payload.get("session_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 1024 else None


def _session_key(session_id: str, repo: str | None = None) -> str:
    material = json.dumps([session_id, repo or ""], separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _state_dir() -> Path:
    """Return a private per-user state directory without following links."""
    path = Path(tempfile.gettempdir()) / f"coord-claude-hook-{os.getuid()}"
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"unsafe Claude hook state directory: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def _session_state_path(session_id: str, repo: str | None = None) -> Path:
    """Safe state path scoped by both session and repository identity."""
    return _state_dir() / f"session-{_session_key(session_id, repo)}.json"


def _session_lock_path(session_id: str, repo: str | None = None) -> Path:
    return _state_dir() / f"session-{_session_key(session_id, repo)}.lock"


def _acquire_session_lock(
    session_id: str, repo: str | None = None
) -> tuple[Path, int] | None:
    """Take a kernel-owned session lock, bounded so hooks still fail open."""
    try:
        path = _session_lock_path(session_id, repo)
    except OSError:
        return None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            return None
        os.fchmod(fd, 0o600)
    except OSError:
        return None
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return path, fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.02)
        except OSError:
            os.close(fd)
            return None


def _release_session_lock(lock: tuple[Path, int] | None) -> None:
    if lock is None:
        return
    _path, fd = lock
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


def _legacy_session_state_path() -> Path:
    """v0.48.0/v0.48.1 claim-id state, retained only for upgrade cleanup."""
    sid = os.environ.get("CLAUDE_SESSION_ID")
    if not sid or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in sid
    ):
        sid = f"ppid-{os.getppid()}"
    return Path(tempfile.gettempdir()) / f"coord-claude-hook-{sid}.json"


def _load_session_state(
    session_id: str,
    repo: str | None = None,
) -> tuple[dict[str, float], float | None]:
    try:
        raw = json.loads(_session_state_path(session_id, repo).read_text())
        if raw.get("session_id") != session_id or raw.get("repo") != repo:
            return {}, None
        version = raw.get("version")
        if version == 2:
            # Development builds briefly wrote one timestamp for the whole
            # cache. Read it once so upgrades neither leak nor duplicate claims.
            verified_at = float(raw.get("verified_at") or 0.0)
            paths = raw.get("claimed_paths") or []
            return (
                {path: verified_at for path in paths if isinstance(path, str) and path},
                None,
            )
        if version not in {3, 4}:
            return {}, None
        paths = raw.get("claimed_paths") or {}
        claimed = {
            path: float(verified_at)
            for path, verified_at in paths.items()
            if isinstance(path, str) and path
        }
        ended = raw.get("ended_at")
        ended_at = float(ended) if ended is not None else None
        return claimed, ended_at
    except Exception:
        return {}, None


def _load_claimed_paths(session_id: str, repo: str | None = None) -> set[str]:
    return set(_load_session_state(session_id, repo)[0])


def _store_session_state(
    session_id: str,
    claimed: dict[str, float],
    ended_at: float | None = None,
    repo: str | None = None,
) -> bool:
    """Atomically persist per-path freshness and the terminal end marker."""
    try:
        path = _session_state_path(session_id, repo)
        payload = {
            "version": 4,
            "session_id": session_id,
            "repo": repo,
            "claimed_paths": dict(sorted(claimed.items())),
            "ended_at": ended_at,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
            return True
        finally:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass
    except Exception:
        return False


def _remember_claimed_path(
    session_id: str, claimed_path: str, repo: str | None = None
) -> None:
    claimed, ended_at = _load_session_state(session_id, repo)
    if ended_at is not None:
        return
    claimed[claimed_path] = time.time()
    _store_session_state(session_id, claimed, repo=repo)


def _session_is_ended(session_id: str, repo: str | None = None) -> bool:
    return _load_session_state(session_id, repo)[1] is not None


def _mark_session_ended(session_id: str, repo: str | None = None) -> bool:
    claimed, _ = _load_session_state(session_id, repo)
    return _store_session_state(session_id, claimed, time.time(), repo=repo)


def _reopen_session(session_id: str, repo: str | None = None) -> bool:
    """Clear a real SessionEnd tombstone; preserve an already-live cache."""
    lock = _acquire_session_lock(session_id, repo)
    if lock is None:
        return False
    try:
        claimed, ended_at = _load_session_state(session_id, repo)
        if ended_at is None:
            return True
        return _store_session_state(session_id, {}, repo=repo)
    finally:
        _release_session_lock(lock)


def _session_claim_is_cached(
    client: CoordClient,
    session_id: str,
    claimed_path: str,
    repo: str | None = None,
) -> bool:
    """Use per-path freshness, then reconcile expired cache with the server."""
    claimed, ended_at = _load_session_state(session_id, repo)
    if ended_at is not None:
        # The server lifecycle is authoritative. Clear a stale local marker
        # and let POST /claims either succeed (resumed/open) or return the
        # terminal session_closed 409 (late hook after SessionEnd).
        _store_session_state(session_id, {}, repo=repo)
        return False
    verified_at = claimed.get(claimed_path)
    if verified_at is None:
        return False
    if time.time() - verified_at <= _CACHE_VERIFY_S:
        return True
    try:
        query_params = {
            "active_only": "true",
            "session_id": session_id,
        }
        if repo:
            query_params["repo"] = repo
        query = urllib.parse.urlencode(query_params)
        response = client.get(f"/claims?{query}")
        rows = response.get("claims", []) if isinstance(response, dict) else []
        live_paths: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            pattern = row.get("pattern")
            if (
                row.get("session_id") == session_id
                and row.get("scope_type") in (None, "file")
                and isinstance(pattern, str)
            ):
                live_paths.add(pattern)
    except Exception:
        return True  # fail open without creating a duplicate during an outage
    refreshed = {path: time.time() for path in live_paths}
    _store_session_state(session_id, refreshed, repo=repo)
    return claimed_path in refreshed


def _safe_private_file(path: Path) -> bool:
    try:
        info = os.lstat(path)
        return not (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        )
    except OSError:
        return False


def _legacy_snapshot_prefix() -> str:
    source = str(_legacy_session_state_path()).encode("utf-8")
    return f"legacy-{hashlib.sha256(source).hexdigest()[:16]}-"


def _take_legacy_snapshot() -> Path | None:
    """Atomically move the mutable v0.48 file into our private directory."""
    source = _legacy_session_state_path()
    if not _safe_private_file(source):
        return None
    try:
        fd, name = tempfile.mkstemp(
            prefix=_legacy_snapshot_prefix(), suffix=".json", dir=_state_dir()
        )
        os.close(fd)
        snapshot = Path(name)
        snapshot.unlink()
        os.replace(source, snapshot)
        if not _safe_private_file(snapshot):
            snapshot.unlink(missing_ok=True)
            return None
        return snapshot
    except OSError:
        return None


def _pending_legacy_snapshots() -> list[Path]:
    try:
        paths = sorted(_state_dir().glob(f"{_legacy_snapshot_prefix()}*.json"))
    except OSError:
        return []
    return [path for path in paths if _safe_private_file(path)]


def _load_stable_legacy_claims(
    path: Path, *, deadline: float
) -> list[str] | None:
    """Read valid legacy JSON only after one unchanged quiescence interval.

    A v0.48 writer may still hold the inode open after we rename it. Invalid or
    changing content is in-progress work, not an empty claim set, and must
    never be unlinked.
    """
    previous: bytes | None = None
    while time.monotonic() + 0.05 <= deadline:
        try:
            if not _safe_private_file(path):
                return None
            content = path.read_bytes()
            raw = json.loads(content)
            claim_ids = raw.get("claim_ids") if isinstance(raw, dict) else None
            if not isinstance(claim_ids, list):
                raise ValueError("legacy claim_ids is not a list")
            parsed = [cid for cid in claim_ids if isinstance(cid, str)]
        except Exception:
            previous = None
        else:
            if content == previous:
                return parsed
            previous = content
        time.sleep(0.05)
    return None


def _relative_to_repo(repo_root: Path, file_path: str) -> str | None:
    """Repo-relative form of ``file_path``; None when outside the repo."""
    try:
        return str(Path(file_path).resolve().relative_to(repo_root))
    except Exception:
        return None


def extract_target_file(payload: dict) -> str | None:
    """The file a PreToolUse payload is about to mutate, if any."""
    if payload.get("tool_name") not in _EDIT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    return target if isinstance(target, str) and target.strip() else None


def foreign_conflicts(conflicts: list[dict], self_engineer: str | None) -> list[dict]:
    """Conflicts held by someone other than this session's identity."""
    return [c for c in conflicts if not self_engineer or c.get("engineer") != self_engineer]


def _whoami_engineer(client: CoordClient) -> str | None:
    try:
        return client.get("/whoami").get("engineer")
    except Exception:
        return None


def _cached_session_is_open(
    client: CoordClient, session_id: str, env: dict[str, str]
) -> bool | None:
    """Check cached work against the server lifecycle authority.

    False is an authoritative 403/409 and must block. None is a transport or
    rolling-upgrade failure and keeps the hook's fail-open contract.
    """
    try:
        client.post(_session_endpoint(session_id, "check", env), {})
    except urllib.error.HTTPError as exc:
        return False if exc.code in {403, 409} else None
    except Exception:
        return None
    return True


def cmd_sessionstart(env: dict[str, str], client: CoordClient, payload: dict[str, Any]) -> int:
    session_id = _payload_session_id(payload)
    repo_id = _repo_id(env)
    if session_id:
        try:
            client.post(_session_endpoint(session_id, "open", env), {})
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                return 0
            # Rolling upgrade: older servers have bulk release but no
            # terminal lifecycle/open endpoint. Keep the advisory context and
            # client-side cache behavior working until the server catches up.
        except Exception:
            return 0
        _reopen_session(session_id, repo_id)
    lines: list[str] = []
    try:
        who = client.get("/whoami")
        scope = who.get("repo_scope")
        if repo_id and scope and scope != repo_id:
            lines.append(
                f"WARNING: coord token is scoped to '{scope}' but this repo is "
                f"'{repo_id}' -- claims will land under the wrong repo. "
                f"Mint a matching token (coord tokens create --repo {repo_id})."
            )
    except Exception:
        pass
    try:
        query = {"active_only": "true"}
        if repo_id:
            query["repo"] = repo_id
        claims = client.get(f"/claims?{urllib.parse.urlencode(query)}").get("claims", [])
        if claims:
            lines.append(f"Active coord claims in this repo ({len(claims)}):")
            for c in claims[:10]:
                lines.append(
                    f"  - {c.get('engineer')}: {c.get('pattern')} (expires {c.get('expires_at')})"
                )
        else:
            lines.append(
                "No active coord claims in this repo. Files you edit will be "
                "auto-claimed for this session."
            )
    except Exception:
        return 0  # coord down: stay silent, never block session start
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "\n".join(lines),
                }
            }
        )
    )
    return 0


def cmd_pretool(
    env: dict[str, str],
    client: CoordClient,
    repo_root: Path,
    payload: dict[str, Any],
) -> int:
    target = extract_target_file(payload)
    if not target:
        return 0
    rel = _relative_to_repo(repo_root, target)
    if rel is None or rel.startswith(".coordination/"):
        return 0

    session_id = _payload_session_id(payload)
    repo_id = _repo_id(env)
    engineer = _whoami_engineer(client)
    if not engineer:
        # Identity unavailable (older server without /whoami, unauthenticated
        # token, network hiccup): enforcement without identity would treat the
        # session's OWN claims as foreign and self-block, so fail open.
        return 0
    try:
        params = [("pattern", rel), ("engineer", engineer)]
        if repo_id:
            params.append(("repo", repo_id))
        if session_id:
            params.append(("session_id", session_id))
        result = client.get(f"/conflicts?{urllib.parse.urlencode(params)}")
        conflicts = result.get("conflicts", []) if isinstance(result, dict) else []
        if not isinstance(conflicts, list):
            return 0
        foreign = foreign_conflicts(
            [item for item in conflicts if isinstance(item, dict)], engineer
        )
    except Exception:
        return 0  # fail open

    if foreign:
        holder = foreign[0]
        print(
            f"coord: '{rel}' is claimed by {holder.get('engineer')} "
            f"({holder.get('pattern')}, expires {holder.get('expires_at')}). "
            f"Coordinate via coord (request_release / wait) or ask the user "
            f"before editing this file.",
            file=sys.stderr,
        )
        return 2  # block the tool call

    # No foreign holder: auto-claim so OTHER sessions now see this work.
    # A stable session id is mandatory: without it, a claim cannot be safely
    # deduplicated or bulk-released. Conflict enforcement still ran above.
    if not session_id:
        return 0
    claimed, ended_at = _load_session_state(session_id, repo_id)
    verified_at = claimed.get(rel) if ended_at is None else None
    if verified_at is not None and time.time() - verified_at <= _CACHE_VERIFY_S:
        if _cached_session_is_open(client, session_id, env) is False:
            print(
                f"coord: session {session_id!r} is closed or owned by another "
                "credential; resume/reopen it before editing.",
                file=sys.stderr,
            )
            return 2
        return 0

    lock = _acquire_session_lock(session_id, repo_id)
    if lock is None:
        return 0  # another hook is slow/unhealthy: fail open, never duplicate
    try:
        # A concurrent hook may have claimed this path while we waited, or
        # SessionEnd may have installed its tombstone before we got the lock.
        if _session_claim_is_cached(client, session_id, rel, repo_id):
            if _cached_session_is_open(client, session_id, env) is False:
                print(
                    f"coord: session {session_id!r} is closed or owned by "
                    "another credential; resume/reopen it before editing.",
                    file=sys.stderr,
                )
                return 2
            return 0
        body: dict[str, Any] = {
            "engineer": engineer,
            "description": _AUTO_DESC,
            "ttl_hours": _AUTO_TTL_HOURS,
            "session_id": session_id,
            "claims": [{"type": "file", "pattern": rel}],
        }
        if repo_id:
            body["repo"] = repo_id
        try:
            out = client.post("/claims", body)
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 409}:
                # The conflict-free GET and claim POST are necessarily two
                # requests. 409 is the race/terminal fence; 403 is an
                # authoritative ownership or repo-scope refusal. Neither may
                # degrade to an unclaimed edit.
                if exc.code == 403:
                    message = (
                        f"coord: claim admission for '{rel}' was forbidden "
                        "(session owner or repository scope mismatch)."
                    )
                else:
                    message = (
                        f"coord: '{rel}' became claimed or its session closed "
                        "before auto-claim completed; reopen or coordinate, "
                        "then retry."
                    )
                print(message, file=sys.stderr)
                return 2
        except Exception:
            pass
        else:
            ids = [
                cid
                for cid in ((out.get("claim_ids") or []) if isinstance(out, dict) else [])
                if isinstance(cid, str)
            ]
            if ids:
                _remember_claimed_path(session_id, rel, repo_id)
    finally:
        _release_session_lock(lock)
    return 0


def cmd_sessionend(env: dict[str, str], client: CoordClient, payload: dict[str, Any]) -> int:
    deadline = time.monotonic() + _SESSIONEND_BUDGET_S
    session_id = _payload_session_id(payload)
    repo_id = _repo_id(env)
    if session_id:
        try:
            # Server-side closure is terminal and ordered against claim
            # admission. Local state is only a cache/diagnostic optimization.
            client.post(_session_endpoint(session_id, "release", env), {})
        except Exception:
            pass
        else:
            lock = _acquire_session_lock(session_id, repo_id)
            if lock is not None:
                try:
                    _mark_session_ended(session_id, repo_id)
                finally:
                    _release_session_lock(lock)

    # One-time migration path: an upgraded, already-running Claude session
    # may still have v0.48 claim IDs keyed by its parent process. The managed
    # synchronous SessionEnd timeout gives this slow legacy endpoint time to
    # drain them once without leaving cleanup to a process Claude can cancel.
    # At most two batched requests: one for all currently pending snapshots,
    # then one for a legacy writer that recreated the source while batch one
    # was in flight. Never start a request unless its full network timeout fits
    # inside the total shutdown budget.
    for _ in range(2):
        if deadline - time.monotonic() < _TIMEOUT_S:
            break
        snapshots = _pending_legacy_snapshots()[:7]
        current = _take_legacy_snapshot()
        if current is not None and current not in snapshots:
            snapshots.append(current)
        if not snapshots:
            break
        loaded = [
            (snapshot, _load_stable_legacy_claims(snapshot, deadline=deadline))
            for snapshot in snapshots
        ]
        valid_snapshots = [snapshot for snapshot, ids in loaded if ids is not None]
        invalid_snapshots = [snapshot for snapshot, ids in loaded if ids is None]
        legacy_ids = list(
            dict.fromkeys(
                claim_id
                for _snapshot, ids in loaded
                if ids is not None
                for claim_id in ids
            )
        )
        if invalid_snapshots and not legacy_ids:
            break
        if not legacy_ids:
            for snapshot in valid_snapshots:
                snapshot.unlink(missing_ok=True)
            continue
        if deadline - time.monotonic() < _TIMEOUT_S:
            break
        try:
            client.post("/claims/release", {"claim_ids": legacy_ids})
        except Exception:
            break
        else:
            for snapshot in valid_snapshots:
                try:
                    snapshot.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in {"sessionstart", "pretool", "sessionend"}:
        print(
            "usage: coord-claude-hook {sessionstart|pretool|sessionend}",
            file=sys.stderr,
        )
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    repo_root, env = _find_local_env(Path.cwd())
    client = _client(env)
    if client is None or repo_root is None:
        return 0  # not a coord-wired repo: no-op
    if args[0] == "sessionstart":
        return cmd_sessionstart(env, client, payload)
    if args[0] == "pretool":
        return cmd_pretool(env, client, repo_root, payload)
    return cmd_sessionend(env, client, payload)


if __name__ == "__main__":
    raise SystemExit(main())
