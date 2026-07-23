#!/usr/bin/env bash
#
# Release coord without GitHub: PyPI (twine) + whcr.io image + writhub tag.
#
# The GitHub Actions release pipeline (release.yml: OIDC trusted publishing
# to PyPI + ghcr.io image push) died with the account suspension. This is
# the GitHub-free path against the same artifacts:
#
#   sdist+wheel -> PyPI            (API token; OIDC is GitHub/GitLab-only,
#                                   re-enable trusted publishing when GH returns)
#   multi-arch image -> whcr.io    (WritHub Container Registry)
#   vX.Y.Z tag -> writhub origin
#
# SAFE BY DEFAULT: with no flag it validates + builds and PRINTS the publish
# plan. Nothing leaves the machine without --publish.
#
# Credentials:
#   PyPI:  $PYPI_TOKEN, or ~/.config/pypi/token (0600). Mint at
#          pypi.org -> Account settings -> API tokens, scope it to project
#          "coord-mcp-server", store in k3s Vault secret/infra/pypi too.
#   Image signing: $COSIGN_SIGNING_KEY names the release KMS key. The upstream
#          GitHub-free release uses hashivault://coord-release in the dedicated
#          transit Vault and commits only its public key at
#          release/coord-release.pub. The private key never leaves Vault.
#   whcr:  docker login whcr.io (user alexm; password in k3s Vault
#          secret/infra/whcr). Big layers: login to the LAN NodePort
#          (kebabrack-node1.lan:30502) instead -- Cloudflare caps ~100MB bodies.
#
# Multi-arch builds need docker buildx with a qemu-enabled builder
# (kebab-rtx6000 works; Apple Silicon Macs build arm64 natively + amd64 via
# qemu). Usage:
#   scripts/release.sh              # validate + build + show plan
#   scripts/release.sh --publish    # actually upload/push/tag
#   scripts/release.sh --publish --skip-tests --skip-image
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PUBLISH=0 SKIP_TESTS=0 SKIP_IMAGE=0 SKIP_PYPI=0
for a in "$@"; do
  case "$a" in
    --publish) PUBLISH=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-image) SKIP_IMAGE=1 ;;
    --skip-pypi) SKIP_PYPI=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

REGISTRY="${COORD_IMAGE_REGISTRY:-whcr.io}"
IMAGE="${COORD_IMAGE_NAME:-${REGISTRY}/alexm/coord}"
PLATFORMS="${COORD_IMAGE_PLATFORMS:-linux/amd64,linux/arm64}"
COSIGN_MIN_VERSION="3.0.6"
CRANE_VERSION="0.21.7"
SBOM_GENERATOR="docker.io/docker/buildkit-syft-scanner@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"

VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
TAG="v${VERSION}"

fail() { echo "release: $*" >&2; exit 1; }

# ---- preflight ---------------------------------------------------------------
[ "$(git branch --show-current)" = "main" ] || fail "not on main"
[ -z "$(git status --porcelain)" ] || fail "working tree not clean"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || fail "main not synced with origin (writhub)"
git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null && fail "tag ${TAG} already exists"
git ls-remote --exit-code origin "refs/tags/${TAG}" >/dev/null 2>&1 && fail "tag ${TAG} already on origin"
grep -q "${VERSION}" CHANGELOG.md || echo "release: WARNING -- ${VERSION} not mentioned in CHANGELOG.md" >&2

if [ "$SKIP_IMAGE" = 0 ] && [ "$PUBLISH" = 1 ]; then
  command -v cosign >/dev/null 2>&1 || fail "cosign is required to publish a signed image"
  command -v crane >/dev/null 2>&1 || fail "crane is required to verify attestations and promote tags"
  [ -n "${COSIGN_SIGNING_KEY:-}" ] || fail "set COSIGN_SIGNING_KEY to the release KMS key"
  [ -f release/coord-release.pub ] || fail "missing release public key: release/coord-release.pub"
  [ "$(crane version)" = "$CRANE_VERSION" ] \
    || fail "crane $(crane version) does not match required ${CRANE_VERSION}"
  cosign_version="$(
    cosign version --json \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["gitVersion"].removeprefix("v"))'
  )" || fail "could not determine the installed cosign version"
  if ! python3 - "$cosign_version" "$COSIGN_MIN_VERSION" <<'PY'
import re
import sys

def version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise SystemExit(1)
    return tuple(map(int, match.groups()))

raise SystemExit(0 if version(sys.argv[1]) >= version(sys.argv[2]) else 1)
PY
  then
    fail "cosign ${cosign_version} is older than required ${COSIGN_MIN_VERSION}"
  fi
  image_probe_error="$(mktemp)"
  if crane digest "${IMAGE}:${TAG}" >/dev/null 2>"$image_probe_error"; then
    fail "official image tag already exists: ${IMAGE}:${TAG}"
  fi
  if ! grep -Fq "MANIFEST_UNKNOWN" "$image_probe_error"; then
    cat "$image_probe_error" >&2
    fail "could not prove official image tag is absent: ${IMAGE}:${TAG}"
  fi
  rm -f "$image_probe_error"
fi

if [ "$SKIP_PYPI" = 0 ]; then
  PYPI_TOKEN="${PYPI_TOKEN:-$(cat ~/.config/pypi/token 2>/dev/null || true)}"
  [ -n "${PYPI_TOKEN}" ] || fail "no PyPI token: set \$PYPI_TOKEN or ~/.config/pypi/token (see header)"
  curl -sf -m 10 "https://pypi.org/pypi/coord-mcp-server/json" \
    | python3 -c "import json,sys; vs=json.load(sys.stdin)['releases']; sys.exit(1 if '${VERSION}' in vs else 0)" \
    || fail "coord-mcp-server ${VERSION} already on PyPI (PyPI versions are immutable -- bump pyproject)"
fi

# ---- tests -------------------------------------------------------------------
if [ "$SKIP_TESTS" = 0 ]; then
  echo "release: running test suite..."
  .venv/bin/python -m pytest -q || fail "tests failed"
fi

# ---- build -------------------------------------------------------------------
echo "release: building sdist + wheel..."
rm -rf dist/
python3 -m build --outdir dist/ >/dev/null 2>&1 || .venv/bin/python -m build --outdir dist/ >/dev/null
ls -1 dist/

# ---- plan / publish ----------------------------------------------------------
echo
echo "release plan for ${TAG}:"
[ "$SKIP_PYPI" = 0 ] && echo "  1. twine upload dist/*  -> PyPI coord-mcp-server ${VERSION}"
[ "$SKIP_IMAGE" = 0 ] && echo "  2. buildx ${PLATFORMS} -> candidate (SBOM + provenance)"
[ "$SKIP_IMAGE" = 0 ] && echo "     validate + sign + verify, then promote the digest to ${IMAGE}:${TAG} + :latest"
echo "  3. git tag ${TAG} + push to origin (writhub)"
echo "  4. THEN: update each downstream cluster's Deployment manifest"
echo "     (kebabrack: build the -pg derivative and update k8s/10b-coord-app.yaml)"

if [ "$PUBLISH" = 0 ]; then
  echo
  echo "DRY RUN complete -- nothing published. Re-run with --publish."
  exit 0
fi

if [ "$SKIP_PYPI" = 0 ]; then
  echo "release: uploading to PyPI..."
  TWINE_USERNAME=__token__ TWINE_PASSWORD="${PYPI_TOKEN}" \
    python3 -m twine upload --non-interactive dist/* \
    || TWINE_USERNAME=__token__ TWINE_PASSWORD="${PYPI_TOKEN}" .venv/bin/python -m twine upload --non-interactive dist/*
fi

if [ "$SKIP_IMAGE" = 0 ]; then
  echo "release: building + pushing attested image ${IMAGE}:${TAG} (${PLATFORMS})..."
  candidate_tag="${IMAGE}:candidate-${TAG}-$(git rev-parse --short=12 HEAD)"
  image_metadata="$(mktemp)"
  cleanup_image_files() {
    rm -f "$image_metadata"
  }
  trap cleanup_image_files EXIT
  docker buildx build --platform "${PLATFORMS}" \
    -t "$candidate_tag" \
    --attest="type=sbom,generator=${SBOM_GENERATOR}" \
    --provenance=mode=max \
    --metadata-file "$image_metadata" \
    --push .
  image_digest="$(
    python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' \
      "$image_metadata"
  )"
  printf '%s\n' "$image_digest" \
    | grep -Eq '^sha256:[0-9a-f]{64}$' \
    || fail "buildx returned an invalid image digest: $image_digest"
  attestation_args=()
  IFS=',' read -r -a release_platforms <<< "$PLATFORMS"
  for platform in "${release_platforms[@]}"; do
    attestation_args+=(--platform "$platform")
  done
  python3 scripts/verify_image_attestations.py \
    --image "${IMAGE}@${image_digest}" \
    "${attestation_args[@]}"
  echo "release: signing ${IMAGE}@${image_digest}..."
  cosign sign --yes --key "${COSIGN_SIGNING_KEY}" "${IMAGE}@${image_digest}"
  cosign verify \
    --key release/coord-release.pub \
    "${IMAGE}@${image_digest}" >/dev/null
  echo "release: promoting verified digest to official tags..."
  crane tag "${IMAGE}@${image_digest}" "$TAG"
  crane tag "${IMAGE}@${image_digest}" latest
  [ "$(crane digest "${IMAGE}:${TAG}")" = "$image_digest" ] \
    || fail "${IMAGE}:${TAG} did not resolve to the verified digest"
  [ "$(crane digest "${IMAGE}:latest")" = "$image_digest" ] \
    || fail "${IMAGE}:latest did not resolve to the verified digest"
  printf '%s@%s\n' "$IMAGE" "$image_digest" \
    > "dist/coord-${TAG}-image-digest.txt"
  echo "release: verified signed image ${IMAGE}@${image_digest}"
  cleanup_image_files
  trap - EXIT
fi

git tag -a "${TAG}" -m "coord ${TAG}"
git push origin "refs/tags/${TAG}"
echo "release: ${TAG} published. Next: update downstream Deployment manifests."
