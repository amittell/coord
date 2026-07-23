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

4. The GitHub-free release path once pushed an unsigned image with no
   SBOM/provenance while the README claimed every image was keyless-signed.
   The release script, committed public key, CI multi-arch build, and deploy
   verification commands must remain one fail-closed contract.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from coordination.assets import PRE_PUSH_SCRIPT
from coordination.cli_init import (
    PLACEHOLDER_API_URL,
    PLACEHOLDER_AUTH_TOKEN,
    PLACEHOLDER_REPO_ID,
    _update_codex_config,
    _update_mcp_json,
)
from coordination.mcp_server import _PLACEHOLDER_VALUES
from scripts.verify_image_attestations import verify_image_attestations

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"

MCP_JSON_TEMPLATES = (
    TEMPLATES / ".mcp.json.example",
    TEMPLATES / ".cursor" / "mcp.json.example",
)
CODEX_TEMPLATE = TEMPLATES / ".codex" / "config.toml.example"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"
GITHUB_FREE_RELEASE_DOC = REPO_ROOT / "docs" / "releasing-without-github.md"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release.sh"
RELEASE_PUBLIC_KEY = REPO_ROOT / "release" / "coord-release.pub"


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


def test_github_free_release_is_attested_signed_and_verified() -> None:
    script = RELEASE_SCRIPT.read_text(encoding="utf-8")
    for required in (
        "--platform \"${PLATFORMS}\"",
        '--attest="type=sbom,generator=${SBOM_GENERATOR}"',
        "--provenance=mode=max",
        "--metadata-file \"$image_metadata\"",
        "scripts/verify_image_attestations.py",
        "cosign sign --yes --key",
        "cosign verify",
        "release/coord-release.pub",
        "dist/coord-${TAG}-image-digest.txt",
        'COSIGN_MIN_VERSION="3.0.6"',
    ):
        assert required in script, (
            f"scripts/release.sh lost required supply-chain fence {required!r}"
        )
    assert "COSIGN_SIGNING_KEY" in script
    assert 'COSIGN_PUBLIC_KEY="release/coord-release.pub"' in script
    assert "${COSIGN_PUBLIC_KEY:-" not in script
    assert re.search(r"sha256:\[0-9a-f\]\{64\}", script)


def test_release_identity_commits_only_a_public_key() -> None:
    public_key = RELEASE_PUBLIC_KEY.read_text(encoding="utf-8")
    assert public_key.startswith("-----BEGIN PUBLIC KEY-----\n")
    assert public_key.endswith("-----END PUBLIC KEY-----\n")
    assert "PRIVATE KEY" not in public_key
    private_key_candidates = tuple((REPO_ROOT / "release").glob("*.key"))
    assert not private_key_candidates, (
        "release/ must never contain an exportable signing key; "
        f"found {private_key_candidates}"
    )


def test_ci_and_deploy_docs_exercise_the_signed_multiarch_contract() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    release_workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    docs = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    release_docs = GITHUB_FREE_RELEASE_DOC.read_text(encoding="utf-8")

    assert "127.0.0.1:5000/coord:ci" in workflow
    assert "steps.base-build.outputs.digest" in workflow
    assert "docker image inspect coord:ci" not in workflow
    assert workflow.count("platforms: linux/amd64,linux/arm64") >= 1
    assert "--platform linux/amd64,linux/arm64" in workflow
    assert "docker/buildkit-syft-scanner@sha256:" in workflow
    assert "scripts/verify_image_attestations.py" in workflow
    assert "--provenance=mode=max" in workflow
    assert "tonistiigi/binfmt:qemu-v10.2.3@sha256:" in workflow
    assert "tonistiigi/binfmt:latest" not in workflow
    assert "tonistiigi/binfmt:qemu-v10.2.3@sha256:" in release_workflow
    assert "tonistiigi/binfmt:latest" not in release_workflow
    assert "docker/buildkit-syft-scanner@sha256:" in release_workflow

    assert docs.count("cosign verify") >= 2
    assert "cosign sign --yes" in docs
    assert "release/coord-release.pub" in docs
    assert "docker/buildkit-syft-scanner@sha256:" in docs
    assert "scripts/verify_image_attestations.py" in docs
    assert "--provenance=mode=max" in docs
    assert 'path "transit/sign/coord-release/*"' in release_docs
    assert "Never give the release process a\n   Vault root token" in release_docs
    assert "Revoke that token when the release finishes" in release_docs


def _attested_index_fixture() -> tuple[str, dict[str, dict[str, object]]]:
    repository = "registry.example/coord"
    index_digest = "sha256:" + "0" * 64
    amd64_digest = "sha256:" + "1" * 64
    arm64_digest = "sha256:" + "2" * 64
    amd64_attestation = "sha256:" + "3" * 64
    arm64_attestation = "sha256:" + "4" * 64

    def platform(digest: str, architecture: str) -> dict[str, object]:
        return {
            "digest": digest,
            "platform": {"os": "linux", "architecture": architecture},
        }

    def link(subject: str, digest: str) -> dict[str, object]:
        return {
            "digest": digest,
            "platform": {"os": "unknown", "architecture": "unknown"},
            "annotations": {
                "vnd.docker.reference.type": "attestation-manifest",
                "vnd.docker.reference.digest": subject,
            },
        }

    def predicates() -> dict[str, object]:
        return {
            "layers": [
                {
                    "mediaType": "application/vnd.in-toto+json",
                    "annotations": {
                        "in-toto.io/predicate-type": "https://spdx.dev/Document"
                    },
                },
                {
                    "mediaType": "application/vnd.in-toto+json",
                    "annotations": {
                        "in-toto.io/predicate-type": (
                            "https://slsa.dev/provenance/v1"
                        )
                    },
                },
            ]
        }

    image = f"{repository}@{index_digest}"
    fixtures = {
        image: {
            "manifests": [
                platform(amd64_digest, "amd64"),
                link(amd64_digest, amd64_attestation),
                platform(arm64_digest, "arm64"),
                link(arm64_digest, arm64_attestation),
            ]
        },
        f"{repository}@{amd64_attestation}": predicates(),
        f"{repository}@{arm64_attestation}": predicates(),
    }
    return image, fixtures


def test_attestation_verifier_requires_both_predicates_per_platform() -> None:
    image, fixtures = _attested_index_fixture()
    verify_image_attestations(
        image,
        {"linux/amd64", "linux/arm64"},
        inspect_raw=fixtures.__getitem__,
    )

    arm64_attestation = "registry.example/coord@sha256:" + "4" * 64
    layers = fixtures[arm64_attestation]["layers"]
    assert isinstance(layers, list)
    layers.pop(0)
    with pytest.raises(ValueError, match="linux/arm64.*spdx"):
        verify_image_attestations(
            image,
            {"linux/amd64", "linux/arm64"},
            inspect_raw=fixtures.__getitem__,
        )


def test_release_docker_inputs_are_digest_pinned() -> None:
    dockerfiles = (
        REPO_ROOT / "Dockerfile",
        REPO_ROOT / "Dockerfile.postgres",
    )
    for path in dockerfiles:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert re.fullmatch(
            r"# syntax=docker/dockerfile:1\.7@sha256:[0-9a-f]{64}",
            first_line,
        ), f"{path.name} has a mutable Dockerfile frontend: {first_line}"

    base = dockerfiles[0].read_text(encoding="utf-8")
    from_lines = [line for line in base.splitlines() if line.startswith("FROM ")]
    assert from_lines
    assert all(
        re.match(r"FROM python:3\.14-slim@sha256:[0-9a-f]{64}", line)
        for line in from_lines
    ), f"Dockerfile has a mutable Python base: {from_lines}"
