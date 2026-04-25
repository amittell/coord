from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import httpx

from coordination.assets import (
    AGENTS_SNIPPET,
    CLAUDE_SNIPPET,
    CURSOR_RULE,
    PRE_PUSH_SCRIPT,
)
from coordination.cli_shared import (
    MANAGED_BEGIN,
    MANAGED_END,
    find_repo_root,
    local_coord_mcp_path,
)
from coordination.ownership import parse_ownership_yaml
from coordination.repo_config import RepoConfig


@dataclass
class CheckResult:
    label: str
    ok: bool
    detail: str = ""
    hint: str = ""


def _load_token(repo_root: Path, config: RepoConfig) -> str:
    env_path = repo_root / config.local_env_file
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("COORD_AUTH_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _check_service(config: RepoConfig, token: str) -> list[CheckResult]:
    out: list[CheckResult] = []
    reach_hint = (
        "Start it with 'coord start --background' or set the correct COORD_API_URL."
    )
    try:
        ready = httpx.get(f"{config.service_url}/readyz", timeout=5.0)
        ok = ready.status_code == 200
        out.append(
            CheckResult(
                "coordination service reachable",
                ok,
                "" if ok else f"status {ready.status_code}",
                "" if ok else reach_hint,
            )
        )
    except httpx.HTTPError as exc:
        out.append(
            CheckResult("coordination service reachable", False, str(exc), reach_hint)
        )
        out.append(
            CheckResult(
                "auth token works",
                False,
                "service unreachable",
                reach_hint,
            )
        )
        return out

    # When the local env defines no token, send no Authorization header at
    # all rather than an invalid `Bearer ` with a trailing space. A 200 from
    # /claims with no auth header means the server is running with
    # COORD_ALLOW_INSECURE_NO_AUTH=true (or is otherwise happy with
    # unauthenticated reads) -- that matches the user's stated configuration
    # and counts as a pass.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    label = "auth token works" if token else "unauthenticated access works"
    hint = (
        "Check COORD_AUTH_TOKEN or regenerate the token via 'coord start'."
        if token
        else (
            "Service rejected an unauthenticated request. Either set "
            "COORD_AUTH_TOKEN in .coordination/local.env or run the service "
            "with COORD_ALLOW_INSECURE_NO_AUTH=true."
        )
    )
    try:
        claims = httpx.get(
            f"{config.service_url}/claims",
            headers=headers,
            timeout=5.0,
        )
        ok = claims.status_code == 200
        out.append(
            CheckResult(
                label,
                ok,
                "" if ok else f"status {claims.status_code}",
                "" if ok else hint,
            )
        )
    except httpx.HTTPError as exc:
        out.append(
            CheckResult(
                label,
                False,
                str(exc),
                hint,
            )
        )
    return out


def _extract_managed_block(text: str) -> str | None:
    """Return the *content* (without the marker lines) of the managed
    block, or None if no block is found.
    """
    start = text.find(MANAGED_BEGIN)
    if start < 0:
        return None
    end = text.find(MANAGED_END, start)
    if end < 0:
        return None
    block = text[start + len(MANAGED_BEGIN) : end]
    return block.strip()


def _check_asset_drift(repo_root: Path, config: RepoConfig) -> list[CheckResult]:
    """Flag any managed artefact whose contents differ from the snippet
    this version of the coord package would generate. The fix is always
    `coord upgrade`, so the hint is identical for every drift result.
    """
    out: list[CheckResult] = []
    upgrade_hint = (
        "Run 'coord upgrade' to refresh managed artefacts to the version "
        "this coord package ships."
    )

    hook_path = repo_root / ".coordination" / "hooks" / "pre-push"
    if hook_path.exists():
        hook_text = hook_path.read_text(encoding="utf-8")
        if hook_text == PRE_PUSH_SCRIPT:
            out.append(CheckResult("pre-push hook is up to date", True))
        else:
            out.append(
                CheckResult(
                    "pre-push hook is up to date",
                    False,
                    "managed hook differs from packaged version",
                    upgrade_hint,
                )
            )

    if config.tool == "claude":
        block_path = repo_root / "CLAUDE.md"
        snippet = CLAUDE_SNIPPET
        label_root = "CLAUDE.md"
    elif config.tool == "codex":
        block_path = repo_root / "AGENTS.md"
        snippet = AGENTS_SNIPPET
        label_root = "AGENTS.md"
    else:
        block_path = repo_root / ".cursor" / "rules" / "coordination.mdc"
        snippet = CURSOR_RULE
        label_root = ".cursor/rules/coordination.mdc"

    if block_path.exists():
        existing_block = _extract_managed_block(
            block_path.read_text(encoding="utf-8")
        )
        if existing_block is not None:
            label = f"{label_root} managed block is up to date"
            if existing_block == snippet.strip():
                out.append(CheckResult(label, True))
            else:
                out.append(
                    CheckResult(
                        label,
                        False,
                        "block content differs from packaged snippet",
                        upgrade_hint,
                    )
                )

    return out


def _print_results(results: list[CheckResult]) -> None:
    for result in results:
        status = "OK" if result.ok else "FAIL"
        detail = f" ({result.detail})" if result.detail else ""
        print(f"{status}  {result.label}{detail}")
        if not result.ok and result.hint:
            print(f"      hint: {result.hint}")


def run_doctor(args) -> int:
    repo_root = find_repo_root()
    results: list[CheckResult] = []
    if repo_root is None:
        print("FAIL  not inside a git repository")
        return 1

    results.append(CheckResult("repo is initialized", True))
    config_path = repo_root / ".coordination" / "config.toml"
    if not config_path.exists():
        results[-1] = CheckResult("repo is initialized", False, "missing .coordination/config.toml")
        _print_results(results)
        return 1

    config = RepoConfig.load(config_path)
    ownership_path = repo_root / config.ownership_file
    try:
        parse_ownership_yaml(ownership_path.read_text(encoding="utf-8"))
        results.append(CheckResult("ownership file parses", True))
    except Exception as exc:  # pragma: no cover - broad to keep doctor resilient
        results.append(CheckResult("ownership file parses", False, str(exc)))

    if config.tool == "claude":
        mcp_ok = (repo_root / ".mcp.json").exists() and '"coord"' in (
            repo_root / ".mcp.json"
        ).read_text(encoding="utf-8")
        results.append(CheckResult("Claude Code MCP config found", mcp_ok))
        doc_ok = (repo_root / "CLAUDE.md").exists() and MANAGED_BEGIN in (
            repo_root / "CLAUDE.md"
        ).read_text(encoding="utf-8")
        results.append(CheckResult("CLAUDE.md coordination block found", doc_ok))
    elif config.tool == "codex":
        results.append(CheckResult("Codex MCP config found", (repo_root / ".codex" / "config.toml").exists()))
        results.append(CheckResult("AGENTS.md coordination block found", (repo_root / "AGENTS.md").exists()))
    else:
        results.append(CheckResult("Cursor MCP config found", (repo_root / ".cursor" / "mcp.json").exists()))
        results.append(CheckResult("Cursor rule found", (repo_root / ".cursor" / "rules" / "coordination.mdc").exists()))

    hook_ok = (repo_root / ".git" / "hooks" / "pre-push").exists()
    results.append(CheckResult("pre-push hook installed", hook_ok))

    mcp_bin = shutil.which("coord-mcp") or str(local_coord_mcp_path())
    results.append(CheckResult("coord-mcp command available", Path(mcp_bin).exists()))

    results.extend(_check_asset_drift(repo_root, config))

    token = _load_token(repo_root, config)
    results.extend(_check_service(config, token))
    _print_results(results)
    return 0 if all(r.ok for r in results) else 1

