"""Guards committed config against the two opposite sanitisation hazards:

1. ``deploy/k8s/prod/`` is the LIVE Argo overlay -- a "public readiness"
   sweep that swaps the host or Vault path for an example string (e.g.
   ``coord.internal.example``, ``apps/YOUR_CLUSTER/coord``) breaks
   ingress + secret sync in prod. We assert placeholders are ABSENT
   and the known-good concrete values are PRESENT.

2. ``.mcp.json`` is LOCAL machine config -- gitignored and untracked
   under the v0.32 untracked-machine-config model, so it never exists
   in a CI checkout and the guard below always skips there. The check
   is a local-dev lint: on a machine where ``coord init`` has written
   an ``.mcp.json``, it flags a real bearer token sitting in the env
   block, because that file is one careless ``git add -f`` (or a
   dropped ``.gitignore`` entry) away from a public GitHub repo.

The wrapper's ``_load_local_env`` (see coordination/mcp_server.py)
overrides placeholders with values from ``.coordination/local.env``
on startup, so the placeholder-in-.mcp.json pattern is functionally
correct -- this lint just stops a real token from accumulating in the
local file at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAY = REPO_ROOT / "deploy" / "k8s" / "prod"
MCP_JSON = REPO_ROOT / ".mcp.json"

FORBIDDEN = (
    "YOUR_CLUSTER",
    "coord.internal.example",
    "set-me",
)

EXPECTED = {
    "ingress.yaml": ("host: coord.kebabrack.lan",),
    "vaultstaticsecret-auth.yaml": ("path: apps/k8s/coord",),
    "vaultstaticsecret-ghcr.yaml": ("path: apps/k8s/coord",),
}

# A coord auth token is a 64-char hex string. Any committed config that
# matches this shape is almost certainly a real credential leak.
_TOKEN_SHAPED = re.compile(r"\b[0-9a-f]{40,}\b")
_TEMPLATE_TOKEN = "set-me"


@pytest.mark.parametrize("path", sorted(OVERLAY.glob("*.yaml")), ids=lambda p: p.name)
def test_overlay_has_no_placeholder_strings(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in FORBIDDEN:
        assert needle not in text, (
            f"{path.name} contains placeholder {needle!r}; this overlay "
            f"is the live kebabrack source for Argo and cannot ship example "
            f"values. Edit a fork-side overlay instead."
        )


@pytest.mark.parametrize("name,needles", sorted(EXPECTED.items()))
def test_overlay_has_concrete_values(name: str, needles: tuple[str, ...]) -> None:
    text = (OVERLAY / name).read_text(encoding="utf-8")
    for needle in needles:
        assert needle in text, f"{name} missing required line: {needle!r}"


def test_mcp_json_does_not_leak_real_token() -> None:
    """Local-dev lint (see module docstring point 2): ``.mcp.json`` is
    gitignored machine config, so this always skips in CI where the file
    does not exist. On dev machines the env block (if present) must use
    the documented ``set-me`` placeholder, never a real token, so the
    file stays safe to un-ignore or force-add by mistake."""
    if not MCP_JSON.exists():
        pytest.skip(
            ".mcp.json not present (gitignored machine config; expected "
            "absent in CI -- this lint only runs on configured dev machines)"
        )
    data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    env = data.get("mcpServers", {}).get("coord", {}).get("env", {})
    token = env.get("COORD_AUTH_TOKEN")
    if token is None:
        return  # env block omitted entirely is fine
    assert token == _TEMPLATE_TOKEN, (
        f".mcp.json COORD_AUTH_TOKEN must stay {_TEMPLATE_TOKEN!r}; the "
        f"wrapper auto-loads .coordination/local.env (gitignored) at "
        f"startup. Got {token[:8]!r}..."
    )
    assert not _TOKEN_SHAPED.search(token), (
        ".mcp.json COORD_AUTH_TOKEN looks like a real credential -- "
        "never commit one. Use .coordination/local.env instead."
    )
