"""Audit guards for the manual-copy templates and README version pins.

Three drift classes shipped historically and are pinned here:

1. The manual-copy MCP wiring templates (``templates/.mcp.json.example``,
   ``templates/.cursor/mcp.json.example``,
   ``templates/.codex/config.toml.example``) once carried ``REPLACE_ME`` /
   ``https://YOUR_COORD_SERVICE.example`` values that the wrapper's
   placeholder detection did not recognize, so a copied template
   permanently shadowed the repo's ``.coordination/local.env`` and sent
   ``Authorization: Bearer REPLACE_ME`` instead of dropping the header.
   The templates must only ever ship the exact strings
   ``coordination.mcp_server._PLACEHOLDER_VALUES`` treats as unset.

2. ``templates/.coordination/hooks/pre-push`` shipped a legacy fail-open
   hook (silently exiting 0 on missing jq/token, never sourcing
   ``local.env``, no ``sessions.live`` forwarding) long after ``coord
   init`` installed the fixed version. The template must stay
   byte-identical to ``coordination.assets.PRE_PUSH_SCRIPT``.

3. README install/upgrade examples pinned ``coord-mcp-server==0.28.2``
   on a v0.45 repo. Every version pin in README.md must match
   ``pyproject.toml``'s ``[project].version``.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from coordination.assets import PRE_PUSH_SCRIPT
from coordination.cli_init import (
    PLACEHOLDER_API_URL,
    PLACEHOLDER_AUTH_TOKEN,
    PLACEHOLDER_REPO_ID,
    _update_codex_config,
    _update_mcp_json,
)
from coordination.mcp_server import _PLACEHOLDER_VALUES

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"

MCP_JSON_TEMPLATES = (
    TEMPLATES / ".mcp.json.example",
    TEMPLATES / ".cursor" / "mcp.json.example",
)
CODEX_TEMPLATE = TEMPLATES / ".codex" / "config.toml.example"


def test_placeholder_constants_are_recognized_by_the_wrapper() -> None:
    """The cli_init placeholder constants are the contract the templates
    are written against; they must all be in the wrapper's unset-set."""
    for value in (PLACEHOLDER_API_URL, PLACEHOLDER_AUTH_TOKEN, PLACEHOLDER_REPO_ID):
        assert value in _PLACEHOLDER_VALUES, (
            f"{value!r} is written by coord init but not treated as a "
            f"placeholder by coordination.mcp_server ({_PLACEHOLDER_VALUES!r})"
        )


def test_mcp_json_templates_carry_only_recognized_placeholders() -> None:
    for path in MCP_JSON_TEMPLATES:
        data = json.loads(path.read_text(encoding="utf-8"))
        env = data["mcpServers"]["coord"]["env"]
        assert env, f"{path} has no env block"
        for key, value in env.items():
            assert value in _PLACEHOLDER_VALUES, (
                f"{path} env[{key!r}] = {value!r} is not one of the "
                f"wrapper-recognized placeholder strings "
                f"{sorted(_PLACEHOLDER_VALUES)}. A real-looking value here "
                f"permanently shadows .coordination/local.env."
            )


def test_mcp_json_templates_match_coord_init_output(tmp_path: Path) -> None:
    """The manual-copy template must produce the same coord server entry
    coord init's _update_mcp_json writes, so both rollout paths agree."""
    generated_path = tmp_path / ".mcp.json"
    _update_mcp_json(generated_path)
    generated = json.loads(generated_path.read_text(encoding="utf-8"))
    for path in MCP_JSON_TEMPLATES:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["coord"] == generated["mcpServers"]["coord"], (
            f"{path} drifted from cli_init._update_mcp_json output"
        )


def test_codex_template_matches_coord_init_env_block(tmp_path: Path) -> None:
    """Every non-comment config line coord init writes must appear in the
    manual-copy Codex template (the template may add explanatory
    comments, but the wiring itself must not drift)."""
    generated_path = tmp_path / "config.toml"
    _update_codex_config(generated_path)
    generated_lines = [
        line
        for line in generated_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    template_lines = {
        line
        for line in CODEX_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for line in generated_lines:
        assert line in template_lines, (
            f"{CODEX_TEMPLATE} is missing the coord init config line "
            f"{line!r}"
        )


def test_templates_carry_no_unrecognized_placeholder_strings() -> None:
    """REPLACE_ME / YOUR_COORD_SERVICE.example were the exact strings that
    caused the local.env shadowing failure; they must never reappear
    anywhere under templates/."""
    offenders: list[str] = []
    for path in TEMPLATES.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "REPLACE_ME" in text or "YOUR_COORD_SERVICE" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"unrecognized placeholder strings found in {offenders}; use the "
        f"documented placeholders {sorted(_PLACEHOLDER_VALUES)} instead"
    )


def test_template_pre_push_hook_matches_assets_script() -> None:
    """The manual-rollout hook template must be byte-identical to the
    hook coord init installs, so manual adopters get the same fail-closed
    jq check, inert local.env loading, and sessions.live forwarding."""
    template = (TEMPLATES / ".coordination" / "hooks" / "pre-push").read_text(
        encoding="utf-8"
    )
    assert template == PRE_PUSH_SCRIPT, (
        "templates/.coordination/hooks/pre-push drifted from "
        "coordination.assets.PRE_PUSH_SCRIPT; regenerate it with\n"
        "  python -c \"from pathlib import Path; "
        "from coordination.assets import PRE_PUSH_SCRIPT; "
        "Path('templates/.coordination/hooks/pre-push')"
        ".write_text(PRE_PUSH_SCRIPT, encoding='utf-8')\""
    )


def test_readme_version_pins_match_pyproject() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    version = str(pyproject["project"]["version"])
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    pip_pins = re.findall(r"coord-mcp-server==(\d+\.\d+\.\d+)", readme)
    image_pins = re.findall(r"whcr\.io/alexm/coord:v(\d+\.\d+\.\d+)", readme)
    version_outputs = re.findall(r"# coord (\d+\.\d+\.\d+)", readme)
    readyz_outputs = re.findall(r'"version"\s*:\s*"(\d+\.\d+\.\d+)"', readme)

    pins = pip_pins + image_pins + version_outputs + readyz_outputs
    assert pins, "expected at least one version pin in README.md"
    assert "ghcr.io/amittell/coord" not in readme, (
        "README.md points at the retired GHCR publication path; "
        "use the canonical whcr.io/alexm/coord image"
    )
    stale = sorted({p for p in pins if p != version})
    assert not stale, (
        f"README.md pins {stale} but pyproject.toml says {version}; "
        f"update the install/upgrade/verify examples"
    )
