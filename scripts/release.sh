#!/usr/bin/env bash
#
# Release coord without GitHub: PyPI (twine) + whcr.io image + writhub tag.
#
# This is the sole authoritative publisher. The GitHub Actions release
# workflow is manual candidate validation only and cannot publish official
# artifacts:
#
#   sdist+wheel -> PyPI            (project-scoped API token; the historical
#                                   GitHub trusted publisher must stay retired)
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
#   scripts/release.sh --publish --skip-tests
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
BUILDER_ID="https://writhub.io/alexm/coord/builders/github-free-release/v1"

fail() { echo "release: $*" >&2; exit 1; }

select_release_python() {
  local candidate
  local -a candidates
  if [ -n "${COORD_RELEASE_PYTHON:-}" ]; then
    candidates=("$COORD_RELEASE_PYTHON")
  else
    candidates=(
      .venv/bin/python
      python3.14
      python3.13
      python3.12
      python3.11
      python3
    )
  fi
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'
    then
      command -v "$candidate"
      return
    fi
  done
  return 1
}

PYTHON_BIN="$(select_release_python)" \
  || fail "release requires Python >=3.11; set COORD_RELEASE_PYTHON to a supported interpreter"
VERSION=$("$PYTHON_BIN" -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
TAG="v${VERSION}"
LOCK_TAG="coord-release-lock-${TAG}"

require_image_tag_absent() {
  local image_ref="$1"
  local image_probe_error
  image_probe_error="$(mktemp)"
  if crane digest "$image_ref" >/dev/null 2>"$image_probe_error"; then
    rm -f "$image_probe_error"
    fail "official image tag already exists: ${image_ref}"
  fi
  if ! grep -Fq "MANIFEST_UNKNOWN" "$image_probe_error"; then
    cat "$image_probe_error" >&2
    rm -f "$image_probe_error"
    fail "could not prove official image tag is absent: ${image_ref}"
  fi
  rm -f "$image_probe_error"
}

remote_tag_lines() {
  local tag="$1"
  git ls-remote --tags origin \
    "refs/tags/${tag}" \
    "refs/tags/${tag}^{}"
}

remote_tag_commit() {
  local lines="$1"
  local commit
  commit="$(
    printf '%s\n' "$lines" \
      | awk '$2 ~ /\^\{\}$/ { print $1; exit }'
  )"
  if [ -z "$commit" ]; then
    commit="$(printf '%s\n' "$lines" | awk 'NF { print $1; exit }')"
  fi
  printf '%s\n' "$commit"
}

acquire_release_lock() {
  local head remote_lines remote_commit
  head="$(git rev-parse HEAD)"
  remote_lines="$(remote_tag_lines "$LOCK_TAG")" \
    || fail "could not query release lock ${LOCK_TAG}"
  if [ -n "$remote_lines" ]; then
    remote_commit="$(remote_tag_commit "$remote_lines")"
    [ "$remote_commit" = "$head" ] \
      || fail "release lock ${LOCK_TAG} belongs to ${remote_commit}, not ${head}"
    echo "release: ${LOCK_TAG} already locks this exact commit; resuming."
    return
  fi

  if git rev-parse -q --verify "refs/tags/${LOCK_TAG}" >/dev/null; then
    [ "$(git rev-list -n 1 "$LOCK_TAG")" = "$head" ] \
      || fail "local release lock ${LOCK_TAG} does not point to ${head}"
  else
    git tag -a "$LOCK_TAG" \
      -m "Serialize coord ${TAG} publication at ${head}"
  fi

  if git push origin "refs/tags/${LOCK_TAG}"; then
    echo "release: acquired immutable release lock ${LOCK_TAG}."
    return
  fi

  # A simultaneous publisher may have won the create-only push. Accept that
  # race only when the winner locked the same exact commit.
  remote_lines="$(remote_tag_lines "$LOCK_TAG")" \
    || fail "release-lock push failed and the winner could not be queried"
  [ -n "$remote_lines" ] \
    || fail "could not acquire release lock ${LOCK_TAG}"
  remote_commit="$(remote_tag_commit "$remote_lines")"
  [ "$remote_commit" = "$head" ] \
    || fail "release lock race lost to ${remote_commit}, not ${head}"
  echo "release: concurrent publisher locked the same exact commit; resuming."
}

# ---- preflight ---------------------------------------------------------------
[ "$(git branch --show-current)" = "main" ] || fail "not on main"
[ -z "$(git status --porcelain)" ] || fail "working tree not clean"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || fail "main not synced with origin (writhub)"
git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null && fail "tag ${TAG} already exists"
remote_version_tag="$(remote_tag_lines "$TAG")" \
  || fail "could not query origin for ${TAG}"
[ -z "$remote_version_tag" ] || fail "tag ${TAG} already on origin"
grep -q "${VERSION}" CHANGELOG.md || echo "release: WARNING -- ${VERSION} not mentioned in CHANGELOG.md" >&2

if [ "$PUBLISH" = 1 ] && { [ "$SKIP_IMAGE" = 1 ] || [ "$SKIP_PYPI" = 1 ]; }; then
  fail "--publish requires both the authenticated image and PyPI artifact paths"
fi

if [ "$SKIP_IMAGE" = 0 ] && [ "$PUBLISH" = 1 ]; then
  command -v cosign >/dev/null 2>&1 || fail "cosign is required to publish a signed image"
  command -v crane >/dev/null 2>&1 || fail "crane is required to verify attestations and promote tags"
  [ -n "${COSIGN_SIGNING_KEY:-}" ] || fail "set COSIGN_SIGNING_KEY to the release KMS key"
  [ -f release/coord-release.pub ] || fail "missing release public key: release/coord-release.pub"
  [ "$(crane version)" = "$CRANE_VERSION" ] \
    || fail "crane $(crane version) does not match required ${CRANE_VERSION}"
  cosign_version="$(
    cosign version --json \
      | "$PYTHON_BIN" -c 'import json, sys; print(json.load(sys.stdin)["gitVersion"].removeprefix("v"))'
  )" || fail "could not determine the installed cosign version"
  if ! "$PYTHON_BIN" - "$cosign_version" "$COSIGN_MIN_VERSION" <<'PY'
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
  require_image_tag_absent "${IMAGE}:${TAG}"
fi

if [ "$SKIP_PYPI" = 0 ]; then
  PYPI_TOKEN="${PYPI_TOKEN:-$(cat ~/.config/pypi/token 2>/dev/null || true)}"
fi

# ---- tests -------------------------------------------------------------------
if [ "$SKIP_TESTS" = 0 ]; then
  echo "release: running test suite..."
  "$PYTHON_BIN" -m pytest -q || fail "tests failed"
fi

# ---- build -------------------------------------------------------------------
echo "release: building sdist + wheel..."
rm -rf dist/
SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"
export SOURCE_DATE_EPOCH
build_venv="$(mktemp -d)"
cleanup_build_venv() {
  rm -rf "$build_venv"
}
trap cleanup_build_venv EXIT
"$PYTHON_BIN" -m venv "$build_venv"
"$build_venv/bin/pip" install \
  --require-hashes \
  -r requirements-build.txt >/dev/null
"$build_venv/bin/python" -m build \
  --no-isolation \
  --outdir dist/ >/dev/null
cleanup_build_venv
trap - EXIT
ls -1 dist/

PYPI_STATE="skipped"
PYPI_MISSING=()
if [ "$SKIP_PYPI" = 0 ]; then
  pypi_result="$(
    "$PYTHON_BIN" scripts/check_pypi_release.py \
      --project coord-mcp-server \
      --version "$VERSION" \
      --dist-dir dist
  )" || fail "could not establish an exact PyPI release state"
  IFS='|' read -r -a pypi_fields <<< "$pypi_result"
  PYPI_STATE="${pypi_fields[0]}"
  PYPI_MISSING=("${pypi_fields[@]:1}")
  if [ "$PYPI_STATE" = "absent" ] || [ "$PYPI_STATE" = "partial" ]; then
    [ "${#PYPI_MISSING[@]}" -gt 0 ] \
      || fail "${PYPI_STATE} PyPI state did not identify missing artifacts"
    [ -n "${PYPI_TOKEN}" ] \
      || fail "no PyPI token: set \$PYPI_TOKEN or ~/.config/pypi/token (see header)"
  elif [ "$PYPI_STATE" != "exact" ]; then
    fail "unexpected PyPI release state: $PYPI_STATE"
  fi
fi

# ---- plan / publish ----------------------------------------------------------
echo
echo "release plan for ${TAG}:"
[ "$SKIP_IMAGE" = 0 ] && echo "  1. buildx ${PLATFORMS} -> candidate (SBOM + provenance)"
[ "$SKIP_IMAGE" = 0 ] && echo "     validate + sign + verify while official tags remain absent"
[ "$SKIP_IMAGE" = 0 ] && echo "     acquire create-only git lock ${LOCK_TAG}"
[ "$SKIP_PYPI" = 0 ] && echo "  2. twine upload missing dist artifacts -> PyPI coord-mcp-server ${VERSION}"
[ "$SKIP_IMAGE" = 0 ] && echo "     promote the authenticated digest to ${IMAGE}:${TAG} + :latest"
echo "  3. git tag ${TAG} + push to origin (writhub)"
echo "  4. THEN: update each downstream cluster's Deployment manifest"
echo "     (kebabrack: build the -pg derivative and update k8s/10b-coord-app.yaml)"

if [ "$PUBLISH" = 0 ]; then
  echo
  echo "DRY RUN complete -- nothing published. Re-run with --publish."
  exit 0
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
    --provenance="mode=max,builder-id=${BUILDER_ID}" \
    --metadata-file "$image_metadata" \
    --push .
  image_digest="$(
    "$PYTHON_BIN" -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' \
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
  "$PYTHON_BIN" scripts/verify_image_attestations.py \
    --image "${IMAGE}@${image_digest}" \
    --builder-id "${BUILDER_ID}" \
    "${attestation_args[@]}"
  echo "release: signing ${IMAGE}@${image_digest}..."
  cosign sign --yes --key "${COSIGN_SIGNING_KEY}" "${IMAGE}@${image_digest}"
  cosign verify \
    --key release/coord-release.pub \
    "${IMAGE}@${image_digest}" >/dev/null
fi

acquire_release_lock

if [ "$SKIP_PYPI" = 0 ]; then
  if [ "$PYPI_STATE" = "absent" ] || [ "$PYPI_STATE" = "partial" ]; then
    pypi_paths=()
    for filename in "${PYPI_MISSING[@]}"; do
      [ -f "dist/${filename}" ] \
        || fail "PyPI state checker returned a missing local artifact: ${filename}"
      pypi_paths+=("dist/${filename}")
    done
    if "$PYTHON_BIN" -c 'import twine' >/dev/null 2>&1; then
      twine_command=("$PYTHON_BIN" -m twine)
    else
      fail "twine is not installed in the release interpreter ${PYTHON_BIN}"
    fi
    echo "release: uploading ${#pypi_paths[@]} missing PyPI artifact(s)..."
    TWINE_USERNAME=__token__ TWINE_PASSWORD="${PYPI_TOKEN}" \
      "${twine_command[@]}" upload --non-interactive "${pypi_paths[@]}"
    post_upload_state="$(
      "$PYTHON_BIN" scripts/check_pypi_release.py \
        --project coord-mcp-server \
        --version "$VERSION" \
        --dist-dir dist
    )" || fail "could not verify PyPI after upload"
    [ "$post_upload_state" = "exact" ] \
      || fail "PyPI upload did not produce the exact complete artifact set"
  else
    echo "release: PyPI ${VERSION} already matches dist/ exactly; resuming."
  fi
fi

if [ "$SKIP_IMAGE" = 0 ]; then
  # The registry has no standard conditional-create operation for a tag.
  # Recheck immediately before the only official manifest PUT; the immutable
  # git lock above serializes every supported publisher for this repository.
  require_image_tag_absent "${IMAGE}:${TAG}"
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
