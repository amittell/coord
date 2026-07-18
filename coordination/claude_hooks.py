"""Claude Code hooks for coord (v20) -- enforcement, not just reminders.

The managed CLAUDE.md block asks agents to claim before editing; the live
fleet showed mandates alone decay under context pressure (2026-07-17: two
initialized repos, zero claims filed). These hooks make the protocol
self-executing for Claude Code sessions:

- ``sessionstart``  advisory: injects the repo's active claims and a
  token-scope-vs-repo mismatch warning as session context.
- ``pretool``       enforcing: before Edit/Write, checks the target file
  against OTHER holders' claims -- blocks (exit 2) on conflict -- and
  otherwise auto-claims it for this session (intent derived from action;
  no ritual to remember). Claim ids are remembered per Claude session.
- ``sessionend``    hygiene: releases every claim this session auto-filed.

Every network failure fails OPEN (exit 0, no output): coordination being
down must never block editing. Installed by ``coord init --tool claude``
into ``.claude/settings.json``; the commands run ``coord-claude-hook``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from coordination.envfile import parse_env

_TIMEOUT_S = 3.0
_AUTO_TTL_HOURS = 4
_AUTO_DESC = "auto-claim (coord Claude Code hook)"

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
        os.environ.get("COORD_API_URL")
        or env.get("COORD_API_URL")
        or env.get("COORD_SERVICE_URL")
    )
    if not base:
        return None
    token = os.environ.get("COORD_AUTH_TOKEN") or env.get("COORD_AUTH_TOKEN")
    if token in (None, "", "set-me", "SET-ME", "changeme", "CHANGEME", "REPLACE_ME", "TODO"):
        token = None
    return CoordClient(base, token)


def _session_state_path() -> Path:
    sid = os.environ.get("CLAUDE_SESSION_ID") or f"ppid-{os.getppid()}"
    return Path(tempfile.gettempdir()) / f"coord-claude-hook-{sid}.json"


def _load_session_claims() -> list[str]:
    try:
        return json.loads(_session_state_path().read_text())["claim_ids"]
    except Exception:
        return []


def _store_session_claims(ids: list[str]) -> None:
    try:
        path = _session_state_path()
        path.write_text(json.dumps({"claim_ids": ids}))
        os.chmod(path, 0o600)
    except Exception:
        pass


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
    tool_input = payload.get("tool_input") or {}
    return tool_input.get("file_path") or tool_input.get("notebook_path")


def foreign_conflicts(conflicts: list[dict], self_engineer: str | None) -> list[dict]:
    """Conflicts held by someone other than this session's identity."""
    return [
        c
        for c in conflicts
        if not self_engineer or c.get("engineer") != self_engineer
    ]


def _whoami_engineer(client: CoordClient) -> str | None:
    try:
        return client.get("/whoami").get("engineer")
    except Exception:
        return None


def cmd_sessionstart(env: dict[str, str], client: CoordClient) -> int:
    lines: list[str] = []
    repo_id = os.environ.get("COORD_REPO_ID") or env.get("COORD_REPO_ID")
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
        claims = client.get("/claims?active_only=true").get("claims", [])
        if claims:
            lines.append(f"Active coord claims in this repo ({len(claims)}):")
            for c in claims[:10]:
                lines.append(
                    f"  - {c.get('engineer')}: {c.get('pattern')} "
                    f"(expires {c.get('expires_at')})"
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


def cmd_pretool(env: dict[str, str], client: CoordClient, repo_root: Path) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    target = extract_target_file(payload)
    if not target:
        return 0
    rel = _relative_to_repo(repo_root, target)
    if rel is None or rel.startswith(".coordination/"):
        return 0

    engineer = _whoami_engineer(client)
    if not engineer:
        # Identity unavailable (older server without /whoami, unauthenticated
        # token, network hiccup): enforcement without identity would treat the
        # session's OWN claims as foreign and self-block, so fail open.
        return 0
    try:
        enc = urllib.parse.quote(rel)
        qs = f"pattern={enc}"
        if engineer:
            qs += f"&engineer={urllib.parse.quote(engineer)}"
        result = client.get(f"/conflicts?{qs}")
    except Exception:
        return 0  # fail open

    foreign = foreign_conflicts(result.get("conflicts", []), engineer)
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
    try:
        body = {
            "engineer": engineer or "claude-code",
            "description": _AUTO_DESC,
            "ttl_hours": _AUTO_TTL_HOURS,
            "claims": [{"type": "file", "pattern": rel}],
        }
        out = client.post("/claims", body)
        ids = out.get("claim_ids") or []
        if ids:
            _store_session_claims(_load_session_claims() + ids)
    except Exception:
        pass
    return 0


def cmd_sessionend(env: dict[str, str], client: CoordClient) -> int:
    ids = _load_session_claims()
    if not ids:
        return 0
    try:
        client.post("/claims/release", {"claim_ids": ids})
    except Exception:
        pass
    try:
        _session_state_path().unlink()
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
    repo_root, env = _find_local_env(Path.cwd())
    client = _client(env)
    if client is None or repo_root is None:
        return 0  # not a coord-wired repo: no-op
    if args[0] == "sessionstart":
        return cmd_sessionstart(env, client)
    if args[0] == "pretool":
        return cmd_pretool(env, client, repo_root)
    return cmd_sessionend(env, client)


if __name__ == "__main__":
    raise SystemExit(main())
