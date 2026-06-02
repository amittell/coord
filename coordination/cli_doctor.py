from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import httpx

from packaging.version import InvalidVersion, Version

from coordination import __version__
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
    # ``level`` distinguishes a soft warning (regex fallback because the
    # native tree-sitter wheel is not installed) from a hard failure. Only
    # checks that explicitly set ``level='warn'`` participate; everything
    # else continues to use the boolean ok / fail contract unchanged.
    level: str = "fail"


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


def _check_sessions_live(repo_root: Path) -> list[CheckResult]:
    """Inspect ``.coordination/sessions.live`` for entries pointing at
    dead PIDs (or pre-v0.12 entries with no PID at all).

    Stale entries are not a hard failure -- the pre-push hook prunes
    them on read and the next coord-mcp startup sweeps them out -- but
    leaving them in the file means /conflicts gets dead session_ids
    forwarded as self-exclusions on every push. Functionally a no-op
    (they don't match any active claim), operationally noise. Surface
    so an operator can run a quick sweep ('pkill coord-mcp' followed
    by any agent activity) if they care.
    """
    import os as _os

    marker = repo_root / ".coordination" / "sessions.live"
    if not marker.exists():
        return []
    try:
        raw_lines = marker.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [
            CheckResult(
                "sessions.live readable",
                False,
                "could not read .coordination/sessions.live",
                "Inspect file permissions; the pre-push hook needs to read it.",
            )
        ]

    total = 0
    stale = 0
    legacy = 0
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        total += 1
        parts = line.split()
        if len(parts) < 2:
            stale += 1
            legacy += 1
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            stale += 1
            continue
        if pid <= 0:
            stale += 1
            continue
        try:
            _os.kill(pid, 0)
        except ProcessLookupError:
            stale += 1
        except PermissionError:
            # Process exists but is owned by another uid; treat as live.
            pass
        except OSError:
            stale += 1

    if total == 0:
        return []
    if stale == 0:
        return [
            CheckResult(
                f"sessions.live entries are live ({total}/{total})",
                True,
            )
        ]
    detail_bits = [f"{stale}/{total} entries point at dead PIDs"]
    if legacy:
        detail_bits.append(f"{legacy} use the pre-v0.12 format with no PID")
    return [
        CheckResult(
            f"sessions.live entries are live ({total - stale}/{total})",
            False,
            "; ".join(detail_bits),
            (
                "Stale entries are pruned automatically by the next coord-mcp "
                "startup in this repo (and harmlessly skipped by the pre-push "
                "hook). To clean up immediately: 'pkill -f coord-mcp' will "
                "force a fresh sweep on every running agent's next coord call."
            ),
        )
    ]


def _extract_managed_block(text: str) -> str | None:
    """Return the *content* (without the marker lines and the
    AUTO-GENERATED warning line, if present) of the managed block,
    or None if no block is found. Accepts either marker style so
    drift detection still fires on legacy or hand-edited blocks.
    """
    from coordination.cli_shared import MANAGED_HASH_BEGIN, MANAGED_HASH_END

    for begin, end in (
        (MANAGED_BEGIN, MANAGED_END),
        (MANAGED_HASH_BEGIN, MANAGED_HASH_END),
    ):
        start = text.find(begin)
        if start < 0:
            continue
        stop = text.find(end, start)
        if stop < 0:
            continue
        block = text[start + len(begin) : stop].strip()
        # Drop the AUTO-GENERATED warning line if it's the first line --
        # callers compare the remainder against the packaged snippet,
        # which doesn't carry the warning text.
        lines = block.splitlines()
        if lines and "AUTO-GENERATED" in lines[0]:
            block = "\n".join(lines[1:]).strip()
        return block
    return None


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

    # Multi-tool aware: check the managed block of every tool that's
    # wired into this repo, not just the one named in config.toml. A
    # repo with both claude and codex active should surface drift in
    # whichever doc has gone stale, since `coord upgrade` will refresh
    # both anyway.
    tool_targets: list[tuple[Path, str, str]] = []
    if (repo_root / ".mcp.json").exists() or config.tool == "claude":
        tool_targets.append(
            (repo_root / "CLAUDE.md", CLAUDE_SNIPPET, "CLAUDE.md")
        )
    if (repo_root / ".codex" / "config.toml").exists() or config.tool == "codex":
        tool_targets.append(
            (repo_root / "AGENTS.md", AGENTS_SNIPPET, "AGENTS.md")
        )
    if (
        (repo_root / ".cursor" / "mcp.json").exists()
        or config.tool == "cursor"
    ):
        tool_targets.append(
            (
                repo_root / ".cursor" / "rules" / "coordination.mdc",
                CURSOR_RULE,
                ".cursor/rules/coordination.mdc",
            )
        )

    for block_path, snippet, label_root in tool_targets:
        if not block_path.exists():
            continue
        existing_block = _extract_managed_block(
            block_path.read_text(encoding="utf-8")
        )
        if existing_block is None:
            continue
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


def _extract_mcp_json_token(path: Path) -> str | None:
    """Read the embedded COORD_AUTH_TOKEN from a Claude/Cursor-style
    .mcp.json. Returns None if the file or env entry is absent so the
    caller can skip the check rather than reporting spurious drift."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    env = (
        data.get("mcpServers", {})
        .get("coord", {})
        .get("env", {})
    )
    if not isinstance(env, dict):
        return None
    val = env.get("COORD_AUTH_TOKEN")
    return val if isinstance(val, str) else None


def _extract_codex_token(path: Path) -> str | None:
    """Read the embedded COORD_AUTH_TOKEN out of a Codex-format TOML
    config. We do a string-level extraction rather than a full TOML
    parse so doctor stays dependency-free; the line we look for is
    always emitted exactly as ``COORD_AUTH_TOKEN = "<value>"`` by
    `_update_codex_config`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    in_env_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_env_section = line == "[mcp_servers.coord.env]"
            continue
        if not in_env_section:
            continue
        if line.startswith("COORD_AUTH_TOKEN"):
            _, _, rhs = line.partition("=")
            return rhs.strip().strip('"').strip("'")
    return None


def _check_token_consistency(
    repo_root: Path, config: RepoConfig, token: str
) -> list[CheckResult]:
    """Surface drift between ``.coordination/local.env``'s
    ``COORD_AUTH_TOKEN`` and the copy embedded in each tool's MCP
    config. The hook reads local.env directly, but Claude/Codex/Cursor
    spawn the MCP child with the env baked into their tool config -- if
    the user rotates the token in local.env without running
    ``coord upgrade``, the MCP child silently authenticates with the
    old key. The fix is always ``coord upgrade``.
    """
    out: list[CheckResult] = []
    upgrade_hint = (
        "Run 'coord upgrade' so the embedded token matches the one in "
        ".coordination/local.env."
    )

    for path, label, extractor in (
        (
            repo_root / ".mcp.json",
            ".mcp.json token matches local.env",
            _extract_mcp_json_token,
        ),
        (
            repo_root / ".codex" / "config.toml",
            ".codex/config.toml token matches local.env",
            _extract_codex_token,
        ),
        (
            repo_root / ".cursor" / "mcp.json",
            ".cursor/mcp.json token matches local.env",
            _extract_mcp_json_token,
        ),
    ):
        if not path.exists():
            continue
        embedded = extractor(path)
        if embedded is None:
            continue
        if embedded == token:
            out.append(CheckResult(label, True))
        else:
            out.append(CheckResult(label, False, "embedded token differs", upgrade_hint))
    return out


def _check_server_version(config: RepoConfig, client_version: str) -> CheckResult | None:
    """Compare the running service's version (via /meta) to the locally
    installed coord package. Returns None when no actionable signal is
    available (server unreachable, /meta returns no version, version
    strings unparseable) so we don't double-report or spuriously fail.
    """
    try:
        meta = httpx.get(f"{config.service_url}/meta", timeout=5.0)
    except httpx.HTTPError:
        return None
    if meta.status_code != 200:
        return None
    try:
        body = meta.json()
    except ValueError:
        return None
    server_version_str = body.get("version") if isinstance(body, dict) else None
    if not server_version_str:
        return None
    try:
        server_v = Version(server_version_str)
        client_v = Version(client_version)
    except InvalidVersion:
        return None

    if server_v == client_v:
        return CheckResult(
            f"client/server versions match ({client_version})",
            True,
        )
    if server_v > client_v:
        return CheckResult(
            "coord CLI matches running service",
            False,
            f"server is newer ({server_version_str}) than CLI ({client_version})",
            "Update your local coord install (e.g. 'cd ~/git/coord && git pull' "
            "for an editable install) so 'coord upgrade' renders artefacts at "
            "the version the cluster runs.",
        )
    return CheckResult(
        "coord CLI matches running service",
        False,
        f"CLI is newer ({client_version}) than server ({server_version_str})",
        "Cluster pod is older than your local CLI. Bump the image tag in "
        "deploy/k8s/prod/deployment.yaml and let argocd roll, or accept the "
        "version skew if intentional.",
    )


def _check_symbol_parser_backend() -> CheckResult:
    """Verify which backend ``coord-mcp`` would resolve for each registered
    sub-file-claim file extension.

    Three outcomes:

    - **OK** -- every supported extension resolves to a tree-sitter backend.
    - **WARN** -- one or more extensions fall back to regex. The parser still
      works (regex is the documented fallback) but operators may want to
      install the native wheels via the ``symbols`` extra for stricter
      parsing.
    - **FAIL** -- ``COORD_SYMBOL_PARSER=treesitter`` is set in the
      environment and at least one extension has no native backend
      installed. In treesitter-only mode this would crash
      :func:`coordination.symbols.extract_symbols` at call time, so we
      surface it as a hard failure.
    """
    import os as _os

    from coordination.symbols import probe_backend, supported_extensions

    pref = _os.environ.get("COORD_SYMBOL_PARSER", "auto").strip().lower() or "auto"
    if pref not in {"treesitter", "regex", "auto"}:
        pref = "auto"

    fallbacks: list[str] = []
    missing: list[str] = []
    for ext in sorted(supported_extensions()):
        status, _detail = probe_backend(ext)
        if status == "treesitter":
            continue
        if status == "regex":
            fallbacks.append(ext)
        else:
            missing.append(ext)

    label = "symbol parser backend"

    if missing:
        return CheckResult(
            label,
            ok=False,
            detail=(
                f"COORD_SYMBOL_PARSER={pref} but no native backend for: "
                f"{', '.join(missing)}"
            ),
            hint=(
                "Install the 'symbols' extra (pip install "
                "'multi-agent-coordination[symbols]') or unset "
                "COORD_SYMBOL_PARSER to fall back to regex."
            ),
        )

    if fallbacks:
        return CheckResult(
            label,
            ok=False,
            level="warn",
            detail=(
                f"COORD_SYMBOL_PARSER={pref}; regex fallback for: "
                f"{', '.join(fallbacks)}"
            ),
            hint=(
                "Install the 'symbols' extra (pip install "
                "'multi-agent-coordination[symbols]') for stricter parsing."
            ),
        )

    return CheckResult(
        label,
        ok=True,
        detail=f"COORD_SYMBOL_PARSER={pref}; tree-sitter for all extensions",
    )


def _print_results(results: list[CheckResult]) -> None:
    for result in results:
        if result.ok:
            status = "OK"
        elif result.level == "warn":
            status = "WARN"
        else:
            status = "FAIL"
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

    # The .git/hooks/pre-push (or whatever the user wired up) typically
    # delegates to .coordination/hooks/pre-push. If that target file is
    # missing, every push silently no-ops -- which is exactly the
    # requesthub failure mode where commits stayed local because the
    # exec target had been deleted out from under the shim. Surface
    # this before it bites a deploy.
    managed_hook = repo_root / ".coordination" / "hooks" / "pre-push"
    results.append(
        CheckResult(
            ".coordination/hooks/pre-push exists",
            managed_hook.exists(),
            "" if managed_hook.exists() else "missing exec target for the pre-push shim",
            (
                ""
                if managed_hook.exists()
                else "Run 'coord upgrade' (or 'coord init --force' if config.toml is also missing) "
                "to restore the managed hook script."
            ),
        )
    )

    mcp_bin = shutil.which("coord-mcp") or str(local_coord_mcp_path())
    results.append(CheckResult("coord-mcp command available", Path(mcp_bin).exists()))

    # v0.12: stale entries in .coordination/sessions.live aren't a
    # functional break -- the pre-push hook prunes them on read and
    # the next coord-mcp startup sweeps them out -- but they're
    # operator-noise worth surfacing. Each stale entry forwards a
    # dead session_id to /conflicts which over-excludes harmlessly.
    results.extend(_check_sessions_live(repo_root))

    results.extend(_check_asset_drift(repo_root, config))

    token = _load_token(repo_root, config)
    results.extend(_check_token_consistency(repo_root, config, token))
    results.extend(_check_service(config, token))

    version_result = _check_server_version(config, client_version=__version__)
    if version_result is not None:
        results.append(version_result)

    # v0.15: sub-file claims are now stable, so doctor surfaces which
    # parser backend will handle each registered extension. Regex fallback
    # is a warning (parser still works); treesitter-only mode with a
    # missing grammar is a hard fail.
    results.append(_check_symbol_parser_backend())

    _print_results(results)
    # warn-level results don't sink the overall exit code -- they're
    # informational (e.g. tree-sitter not installed so the parser falls
    # back to regex). Only hard failures flip the exit code.
    return 0 if all(r.ok or r.level == "warn" for r in results) else 1

