"""Guards the live `deploy/k8s/prod/` Argo overlay against placeholder regressions.

A prior "public readiness" sweep silently rewrote concrete prod values
(`coord.kebabrack.lan`, `secret/apps/k8s/coord`, the API token) to
examples (`coord.internal.example`, `apps/YOUR_CLUSTER/coord`, `set-me`)
and shipped them to the cluster via Argo, breaking ingress + Vault sync.
The overlay is the live source-of-truth -- fork-and-replace, don't
sanitise-in-place. This test exists so the next sweep fails CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

OVERLAY = Path(__file__).resolve().parent.parent / "deploy" / "k8s" / "prod"

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


@pytest.mark.parametrize("path", sorted(OVERLAY.glob("*.yaml")), ids=lambda p: p.name)
def test_no_placeholder_strings(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in FORBIDDEN:
        assert needle not in text, (
            f"{path.name} contains placeholder {needle!r}; this overlay "
            f"is the live kebabrack source for Argo and cannot ship example "
            f"values. Edit a fork-side overlay instead."
        )


@pytest.mark.parametrize("name,needles", sorted(EXPECTED.items()))
def test_concrete_values_present(name: str, needles: tuple[str, ...]) -> None:
    text = (OVERLAY / name).read_text(encoding="utf-8")
    for needle in needles:
        assert needle in text, f"{name} missing required line: {needle!r}"
