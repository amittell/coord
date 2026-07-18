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
[ "$SKIP_IMAGE" = 0 ] && echo "  2. buildx ${PLATFORMS} -> ${IMAGE}:${TAG} + :latest (pushed)"
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
  echo "release: building + pushing image ${IMAGE}:${TAG} (${PLATFORMS})..."
  docker buildx build --platform "${PLATFORMS}" \
    -t "${IMAGE}:${TAG}" -t "${IMAGE}:latest" --push .
fi

git tag -a "${TAG}" -m "coord ${TAG}"
git push origin "refs/tags/${TAG}"
echo "release: ${TAG} published. Next: update downstream Deployment manifests."
