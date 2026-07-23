#!/usr/bin/env bash
#
# Release coord from WritHub: PyPI (GitHub OIDC or token fallback) +
# whcr.io image + WritHub tag.
#
# This is the authoritative release coordinator. GitHub Actions is manual
# candidate validation for images and a narrow OIDC transport for PyPI. It
# cannot authorize a release, sign or promote an official image, create the
# WritHub version tag, or deploy:
#
#   sdist+wheel -> PyPI            (default: existing GitHub Trusted Publisher,
#                                   authorized by a Vault-signed release
#                                   statement; explicit token fallback remains)
#   multi-arch image -> whcr.io    (WritHub Container Registry)
#   vX.Y.Z tag -> writhub origin
#
# SAFE BY DEFAULT: with no flag it validates + builds and PRINTS the publish
# plan. Nothing leaves the machine without --publish.
#
# Credentials:
#   PyPI:  the default `github-oidc` publisher uses the existing PyPI Trusted
#          Publisher for amittell/coord, release.yml, environment pypi. GitHub
#          receives no long-lived PyPI or WritHub secret: a trigger tag carries
#          a Vault-signed statement that binds the exact release lock.
#          Set COORD_PYPI_PUBLISHER=token only for the explicit fallback, using
#          $PYPI_TOKEN or ~/.config/pypi/token (0600).
#   Image signing: $COSIGN_SIGNING_KEY names the release KMS key. The upstream
#          release uses hashivault://coord-release in the dedicated transit
#          Vault and commits only its public key at
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
PYPI_PUBLISHER="${COORD_PYPI_PUBLISHER:-github-oidc}"
PYPI_GITHUB_REMOTE="${COORD_PYPI_GITHUB_REMOTE:-github}"
COSIGN_MIN_VERSION="3.0.6"
CRANE_VERSION="0.21.7"
SBOM_GENERATOR="docker.io/docker/buildkit-syft-scanner@sha256:79e7b013cbec16bbb436f312819a49a4a57752b2270c1a9332ae1a10fcc82a68"
BUILDER_ID="https://writhub.io/alexm/coord/builders/github-free-release/v1"
PYPI_OIDC_PUBLISHER="github-oidc:amittell/coord:.github/workflows/release.yml:pypi"

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
      && "$candidate" -c 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] <= (3, 14)))'
    then
      command -v "$candidate"
      return
    fi
  done
  return 1
}

PYTHON_BIN="$(select_release_python)" \
  || fail "release requires CPython 3.11-3.14; set COORD_RELEASE_PYTHON to a supported interpreter"
BUILD_PYTHON_VERSION="$(
  "$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)"
VERSION=$("$PYTHON_BIN" -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
TAG="v${VERSION}"
LOCK_TAG="coord-release-lock-${TAG}"
RELEASE_RESUME_ID="${COORD_RELEASE_RESUME_ID:-}"
if [ -n "$RELEASE_RESUME_ID" ] \
  && ! printf '%s\n' "$RELEASE_RESUME_ID" | grep -Eq '^[0-9a-f]{32}$'
then
  fail "COORD_RELEASE_RESUME_ID must be exactly 32 lowercase hex characters"
fi
RELEASE_OPERATION_ID="${RELEASE_RESUME_ID:-$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(16))')}"
PYPI_OIDC_TRIGGER_TAG="coord-pypi-${TAG}-${RELEASE_OPERATION_ID}"
case "$PYPI_PUBLISHER" in
  github-oidc | token) ;;
  *)
    fail "COORD_PYPI_PUBLISHER must be github-oidc or token"
    ;;
esac

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

release_lock_payload() {
  local digest="$1"
  printf '%s\n' \
    "coord-release-lock-v2" \
    "operation_id=${RELEASE_OPERATION_ID}" \
    "commit=$(git rev-parse HEAD)" \
    "image=${IMAGE}@${digest}" \
    "packages=${PACKAGE_FINGERPRINT}"
}

release_lock_field() {
  local payload="$1"
  local field="$2"
  printf '%s\n' "$payload" \
    | awk -F= -v field="$field" '$1 == field { sub(/^[^=]*=/, ""); print; exit }'
}

fetch_remote_release_lock_payload() {
  local observed_ref payload
  observed_ref="refs/coord-release/observed-${LOCK_TAG}-$$"
  git fetch -q --force origin "refs/tags/${LOCK_TAG}:${observed_ref}" \
    || fail "could not fetch release lock ${LOCK_TAG}"
  payload="$(git for-each-ref --format='%(contents)' "$observed_ref")"
  git update-ref -d "$observed_ref"
  printf '%s\n' "$payload"
}

validate_release_lock_payload() {
  local payload="$1"
  local expected_digest="${2:-}"
  local locked_commit locked_image locked_operation locked_packages
  [ "$(printf '%s\n' "$payload" | head -n 1)" = "coord-release-lock-v2" ] \
    || fail "release lock ${LOCK_TAG} has an unsupported payload"
  locked_operation="$(release_lock_field "$payload" operation_id)"
  [ "$locked_operation" = "$RELEASE_OPERATION_ID" ] \
    || fail "release lock ${LOCK_TAG} belongs to operation ${locked_operation:-unknown}, not ${RELEASE_OPERATION_ID}"
  locked_commit="$(release_lock_field "$payload" commit)"
  [ "$locked_commit" = "$(git rev-parse HEAD)" ] \
    || fail "release lock ${LOCK_TAG} belongs to ${locked_commit:-unknown}, not HEAD"
  locked_packages="$(release_lock_field "$payload" packages)"
  [ "$locked_packages" = "$PACKAGE_FINGERPRINT" ] \
    || fail "release lock ${LOCK_TAG} package hashes do not match the rebuilt artifacts"
  locked_image="$(release_lock_field "$payload" image)"
  case "$locked_image" in
    "${IMAGE}@"*) ;;
    *)
      fail "release lock ${LOCK_TAG} has an invalid image binding: ${locked_image:-missing}"
      ;;
  esac
  LOCKED_IMAGE_DIGEST="${locked_image#"${IMAGE}"@}"
  printf '%s\n' "$LOCKED_IMAGE_DIGEST" \
    | grep -Eq '^sha256:[0-9a-f]{64}$' \
    || fail "release lock ${LOCK_TAG} has an invalid image digest"
  if [ -n "$expected_digest" ] && [ "$LOCKED_IMAGE_DIGEST" != "$expected_digest" ]; then
    fail "release lock ${LOCK_TAG} binds ${LOCKED_IMAGE_DIGEST}, not ${expected_digest}"
  fi
}

load_release_lock_for_resume() {
  local remote_lines payload
  remote_lines="$(remote_tag_lines "$LOCK_TAG")" \
    || fail "could not query release lock ${LOCK_TAG}"
  [ -n "$remote_lines" ] \
    || fail "no remote release lock ${LOCK_TAG} exists to resume"
  payload="$(fetch_remote_release_lock_payload)"
  validate_release_lock_payload "$payload"
  image_digest="$LOCKED_IMAGE_DIGEST"
  echo "release: resuming operation ${RELEASE_OPERATION_ID} at ${image_digest}."
}

release_authorization_statement() {
  local digest="$1"
  printf '%s\n' \
    "coord-pypi-authorization-v2" \
    "operation_id=${RELEASE_OPERATION_ID}" \
    "version=${VERSION}" \
    "commit=$(git rev-parse HEAD)" \
    "image=${IMAGE}@${digest}" \
    "packages=${PACKAGE_FINGERPRINT}" \
    "build_python=${BUILD_PYTHON_VERSION}" \
    "publisher=${PYPI_OIDC_PUBLISHER}"
}

release_trigger_field() {
  local payload="$1"
  local field="$2"
  printf '%s\n' "$payload" \
    | awk -F= -v field="$field" '$1 == field { sub(/^[^=]*=/, ""); print; exit }'
}

read_github_trigger() {
  local observed_ref
  observed_ref="refs/coord-release/observed-${PYPI_OIDC_TRIGGER_TAG}-$$"
  git fetch -q --force "$PYPI_GITHUB_REMOTE" \
    "refs/tags/${PYPI_OIDC_TRIGGER_TAG}:${observed_ref}" \
    || fail "could not fetch GitHub OIDC trigger ${PYPI_OIDC_TRIGGER_TAG}"
  GITHUB_TRIGGER_COMMIT="$(git rev-parse "${observed_ref}^{}")"
  GITHUB_TRIGGER_PAYLOAD="$(git for-each-ref --format='%(contents)' "$observed_ref")"
  git update-ref -d "$observed_ref"
}

read_local_github_trigger() {
  local trigger_ref="refs/tags/${PYPI_OIDC_TRIGGER_TAG}"
  GITHUB_TRIGGER_COMMIT="$(git rev-parse "${trigger_ref}^{}")"
  GITHUB_TRIGGER_PAYLOAD="$(git for-each-ref --format='%(contents)' "$trigger_ref")"
}

validate_github_oidc_trigger_payload() {
  local payload="$1"
  local expected_digest="$2"
  local statement_b64 bundle_b64 expected_statement
  local statement_file bundle_file
  [ "$(printf '%s\n' "$payload" | head -n 1)" = "coord-pypi-trigger-v1" ] \
    || fail "GitHub OIDC trigger ${PYPI_OIDC_TRIGGER_TAG} has an unsupported payload"
  [ "$GITHUB_TRIGGER_COMMIT" = "$(git rev-parse HEAD)" ] \
    || fail "GitHub OIDC trigger ${PYPI_OIDC_TRIGGER_TAG} points to ${GITHUB_TRIGGER_COMMIT}, not HEAD"
  statement_b64="$(release_trigger_field "$payload" statement_base64)"
  bundle_b64="$(release_trigger_field "$payload" bundle_base64)"
  [ -n "$statement_b64" ] && [ -n "$bundle_b64" ] \
    || fail "GitHub OIDC trigger ${PYPI_OIDC_TRIGGER_TAG} is missing its signed authorization"

  statement_file="$(mktemp)"
  bundle_file="$(mktemp)"
  printf '%s' "$statement_b64" \
    | "$PYTHON_BIN" -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read(), validate=True))' \
      > "$statement_file" \
    || fail "GitHub OIDC trigger contains invalid statement base64"
  printf '%s' "$bundle_b64" \
    | "$PYTHON_BIN" -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.read(), validate=True))' \
      > "$bundle_file" \
    || fail "GitHub OIDC trigger contains invalid bundle base64"
  cosign verify-blob \
    --key release/coord-release.pub \
    --bundle "$bundle_file" \
    "$statement_file" >/dev/null \
    || fail "GitHub OIDC trigger release authorization signature is invalid"
  expected_statement="$(release_authorization_statement "$expected_digest")"
  [ "$(cat "$statement_file")" = "$expected_statement" ] \
    || fail "GitHub OIDC trigger release authorization does not match the locked release"
  rm -f "$statement_file" "$bundle_file"
}

ensure_github_oidc_trigger() {
  local image_digest="$1"
  local github_main trigger_lines payload
  local statement_file="" bundle_file="" statement_b64 bundle_b64
  git remote get-url "$PYPI_GITHUB_REMOTE" \
    | grep -Eq '^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)amittell/coord(\.git)?$' \
    || fail "${PYPI_GITHUB_REMOTE} is not the trusted publisher repository github.com/amittell/coord"
  github_main="$(
    git ls-remote "$PYPI_GITHUB_REMOTE" refs/heads/main \
      | awk 'NF { print $1; exit }'
  )" || fail "could not query the GitHub publisher main branch"
  [ -n "$github_main" ] || fail "GitHub publisher main is missing"

  trigger_lines="$(
    git ls-remote --tags "$PYPI_GITHUB_REMOTE" \
      "refs/tags/${PYPI_OIDC_TRIGGER_TAG}" \
      "refs/tags/${PYPI_OIDC_TRIGGER_TAG}^{}"
  )" || fail "could not query GitHub OIDC trigger ${PYPI_OIDC_TRIGGER_TAG}"
  if [ -n "$trigger_lines" ]; then
    read_github_trigger
    payload="$GITHUB_TRIGGER_PAYLOAD"
    validate_github_oidc_trigger_payload "$payload" "$image_digest"
    echo "release: GitHub OIDC trigger already matches the locked release."
    return
  fi

  if git rev-parse -q --verify "refs/tags/${PYPI_OIDC_TRIGGER_TAG}" >/dev/null; then
    read_local_github_trigger
    payload="$GITHUB_TRIGGER_PAYLOAD"
    validate_github_oidc_trigger_payload "$payload" "$image_digest"
    echo "release: retrying previously verified local GitHub OIDC trigger."
  else
    [ -n "${COSIGN_SIGNING_KEY:-}" ] \
      || fail "set COSIGN_SIGNING_KEY to create the Vault-signed GitHub OIDC authorization"
    statement_file="$(mktemp)"
    bundle_file="$(mktemp)"
    release_authorization_statement "$image_digest" > "$statement_file"
    cosign sign-blob --yes \
      --key "${COSIGN_SIGNING_KEY}" \
      --bundle "$bundle_file" \
      "$statement_file" >/dev/null
    cosign verify-blob \
      --key release/coord-release.pub \
      --bundle "$bundle_file" \
      "$statement_file" >/dev/null
    statement_b64="$(
      "$PYTHON_BIN" -c 'import base64,sys; print(base64.b64encode(open(sys.argv[1],"rb").read()).decode())' \
        "$statement_file"
    )"
    bundle_b64="$(
      "$PYTHON_BIN" -c 'import base64,sys; print(base64.b64encode(open(sys.argv[1],"rb").read()).decode())' \
        "$bundle_file"
    )"
    payload="$(
      printf '%s\n' \
        "coord-pypi-trigger-v1" \
        "statement_base64=${statement_b64}" \
        "bundle_base64=${bundle_b64}"
    )"
    git tag --no-sign -a "$PYPI_OIDC_TRIGGER_TAG" -m "$payload"
  fi
  if ! git push "$PYPI_GITHUB_REMOTE" "refs/tags/${PYPI_OIDC_TRIGGER_TAG}"; then
    trigger_lines="$(
      git ls-remote --tags "$PYPI_GITHUB_REMOTE" \
        "refs/tags/${PYPI_OIDC_TRIGGER_TAG}" \
        "refs/tags/${PYPI_OIDC_TRIGGER_TAG}^{}"
    )" || fail "OIDC trigger push failed and remote state could not be queried"
    [ -n "$trigger_lines" ] \
      || fail "could not publish GitHub OIDC trigger ${PYPI_OIDC_TRIGGER_TAG}"
  fi
  read_github_trigger
  payload="$GITHUB_TRIGGER_PAYLOAD"
  validate_github_oidc_trigger_payload "$payload" "$image_digest"
  if [ -n "$statement_file" ]; then
    rm -f "$statement_file" "$bundle_file"
  fi
  echo "release: published Vault-authorized GitHub OIDC trigger ${PYPI_OIDC_TRIGGER_TAG}."
}

audit_github_oidc_controls() {
  local branch_protection environment branch_policies
  command -v gh >/dev/null 2>&1 \
    || fail "GitHub OIDC publication requires the authenticated gh CLI"
  gh auth status --hostname github.com >/dev/null 2>&1 \
    || fail "GitHub OIDC publication requires gh auth login for github.com"
  branch_protection="$(gh api repos/amittell/coord/branches/main/protection)" \
    || fail "could not audit GitHub main branch protection"
  environment="$(gh api repos/amittell/coord/environments/pypi)" \
    || fail "could not audit the GitHub pypi environment"
  branch_policies="$(
    gh api repos/amittell/coord/environments/pypi/deployment-branch-policies
  )" || fail "could not audit the GitHub pypi deployment branch policy"
  "$PYTHON_BIN" - "$branch_protection" "$environment" "$branch_policies" <<'PY' \
    || fail "GitHub trusted-publisher protections do not match the release contract"
import json
import sys

branch = json.loads(sys.argv[1])
environment = json.loads(sys.argv[2])
policies = json.loads(sys.argv[3])

reviews = branch.get("required_pull_request_reviews") or {}
if not (
    reviews.get("require_code_owner_reviews") is True
    and reviews.get("dismiss_stale_reviews") is True
    and reviews.get("require_last_push_approval") is True
    and reviews.get("required_approving_review_count", 0) >= 1
    and (branch.get("enforce_admins") or {}).get("enabled") is True
    and (branch.get("required_linear_history") or {}).get("enabled") is True
    and (branch.get("allow_force_pushes") or {}).get("enabled") is False
    and (branch.get("allow_deletions") or {}).get("enabled") is False
    and (branch.get("lock_branch") or {}).get("enabled") is True
):
    raise SystemExit("main branch protection is weaker than required")

reviewers = {
    (
        row.get("type"),
        row.get("reviewer", {}).get("login")
        or row.get("reviewer", {}).get("slug"),
    )
    for rule in environment.get("protection_rules", [])
    if rule.get("type") == "required_reviewers"
    for row in rule.get("reviewers", [])
}
deployment = environment.get("deployment_branch_policy") or {}
if not (
    environment.get("can_admins_bypass") is False
    and reviewers == {("User", "amittell")}
    and deployment.get("custom_branch_policies") is True
    and deployment.get("protected_branches") is False
):
    raise SystemExit("pypi environment protection is weaker than required")

observed_policies = {
    (row.get("name"), row.get("type"))
    for row in policies.get("branch_policies", [])
}
if observed_policies != {("main", "branch")}:
    raise SystemExit("pypi environment is not restricted to the main branch")
PY
}

verify_github_oidc_verifier() {
  local verifier_ref
  git remote get-url "$PYPI_GITHUB_REMOTE" \
    | grep -Eq '^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)amittell/coord(\.git)?$' \
    || fail "${PYPI_GITHUB_REMOTE} is not the trusted publisher repository github.com/amittell/coord"
  verifier_ref="refs/coord-release/github-verifier-main-$$"
  git fetch -q --force "$PYPI_GITHUB_REMOTE" \
    "refs/heads/main:${verifier_ref}" \
    || fail "could not fetch the protected GitHub OIDC verifier"
  if ! git diff --quiet HEAD "$verifier_ref" -- \
    .github/CODEOWNERS \
    .github/workflows/release.yml \
    release/coord-release.pub \
    scripts/check_pypi_release.py \
    scripts/verify_image_attestations.py
  then
    git update-ref -d "$verifier_ref"
    fail "protected GitHub main does not contain this exact reviewed OIDC verifier"
  fi
  git update-ref -d "$verifier_ref"
}

dispatch_github_oidc_publish() {
  audit_github_oidc_controls
  gh workflow run release.yml \
    --repo amittell/coord \
    --ref main \
    --field "oidc_trigger_tag=${PYPI_OIDC_TRIGGER_TAG}" \
    || fail "could not dispatch the protected-main GitHub OIDC workflow"
  echo "release: dispatched protected-main GitHub OIDC publication."
}

wait_for_github_oidc_publish() {
  local attempt result state
  for attempt in $(seq 1 90); do
    result="$(
      "$PYTHON_BIN" scripts/check_pypi_release.py \
        --project coord-mcp-server \
        --version "$VERSION" \
        --dist-dir dist
    )" || fail "could not establish PyPI state while waiting for GitHub OIDC"
    state="${result%%|*}"
    if [ "$state" = "exact" ]; then
      echo "release: PyPI ${VERSION} exactly matches the Vault-authorized OIDC build."
      return
    fi
    [ "$state" = "absent" ] || [ "$state" = "partial" ] \
      || fail "unexpected PyPI state while waiting for GitHub OIDC: $state"
    if [ "$attempt" -eq 90 ]; then
      fail "GitHub OIDC publication did not complete; resume operation ${RELEASE_OPERATION_ID} after checking the pypi environment"
    fi
    sleep 10
  done
}

acquire_release_lock() {
  local image_digest="$1"
  local lock_payload remote_lines observed_payload
  remote_lines="$(remote_tag_lines "$LOCK_TAG")" \
    || fail "could not query release lock ${LOCK_TAG}"
  if [ -n "$remote_lines" ]; then
    observed_payload="$(fetch_remote_release_lock_payload)"
    fail "release lock ${LOCK_TAG} already belongs to operation $(release_lock_field "$observed_payload" operation_id); explicit resume requires COORD_RELEASE_RESUME_ID"
  fi

  if git rev-parse -q --verify "refs/tags/${LOCK_TAG}" >/dev/null; then
    fail "local release lock ${LOCK_TAG} already exists without a remote lock; resolve it before publishing"
  fi
  lock_payload="$(release_lock_payload "$image_digest")"
  git tag --no-sign -a "$LOCK_TAG" -m "$lock_payload"

  if git push origin "refs/tags/${LOCK_TAG}"; then
    echo "release: acquired ${LOCK_TAG} for operation ${RELEASE_OPERATION_ID}."
    return
  fi

  # A transport failure may hide a successful create. Accept only our exact
  # operation payload; a concurrent publisher always has a different nonce.
  remote_lines="$(remote_tag_lines "$LOCK_TAG")" \
    || fail "release-lock push failed and the winner could not be queried"
  [ -n "$remote_lines" ] \
    || fail "could not acquire release lock ${LOCK_TAG}"
  observed_payload="$(fetch_remote_release_lock_payload)"
  [ "$observed_payload" = "$lock_payload" ] \
    || fail "release lock race lost to operation $(release_lock_field "$observed_payload" operation_id)"
  echo "release: lock push was ambiguous, but the remote contains this exact operation payload."
}

# ---- preflight ---------------------------------------------------------------
[ "$(git branch --show-current)" = "main" ] || fail "not on main"
[ -z "$(git status --porcelain)" ] || fail "working tree not clean"
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || fail "main not synced with origin (writhub)"
if [ -z "$RELEASE_RESUME_ID" ]; then
  git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null \
    && fail "tag ${TAG} already exists"
  remote_version_tag="$(remote_tag_lines "$TAG")" \
    || fail "could not query origin for ${TAG}"
  [ -z "$remote_version_tag" ] || fail "tag ${TAG} already on origin"
fi
grep -q "${VERSION}" CHANGELOG.md || echo "release: WARNING -- ${VERSION} not mentioned in CHANGELOG.md" >&2

if [ "$PUBLISH" = 1 ] && { [ "$SKIP_IMAGE" = 1 ] || [ "$SKIP_PYPI" = 1 ]; }; then
  fail "--publish requires both the authenticated image and PyPI artifact paths"
fi

if [ "$SKIP_IMAGE" = 0 ] && [ "$PUBLISH" = 1 ]; then
  command -v cosign >/dev/null 2>&1 || fail "cosign is required to publish a signed image"
  command -v crane >/dev/null 2>&1 || fail "crane is required to verify attestations and promote tags"
  if [ -z "$RELEASE_RESUME_ID" ]; then
    [ -n "${COSIGN_SIGNING_KEY:-}" ] \
      || fail "set COSIGN_SIGNING_KEY to the release KMS key"
  fi
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
  if [ -z "$RELEASE_RESUME_ID" ]; then
    require_image_tag_absent "${IMAGE}:${TAG}"
  fi
fi

if [ "$SKIP_PYPI" = 0 ] && [ "$PYPI_PUBLISHER" = "token" ]; then
  PYPI_TOKEN="${PYPI_TOKEN:-$(cat ~/.config/pypi/token 2>/dev/null || true)}"
fi
if [ "$PUBLISH" = 1 ] \
  && [ "$SKIP_PYPI" = 0 ] \
  && [ "$PYPI_PUBLISHER" = "github-oidc" ]
then
  verify_github_oidc_verifier
  audit_github_oidc_controls
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
PACKAGE_FINGERPRINT="$(
  "$PYTHON_BIN" - <<'PY'
import hashlib
import json
from pathlib import Path

artifacts = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(Path("dist").iterdir())
    if path.is_file()
}
if not artifacts:
    raise SystemExit("dist/ contains no package artifacts")
print(json.dumps(artifacts, sort_keys=True, separators=(",", ":")))
PY
)" || fail "could not fingerprint release package artifacts"

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
    if [ "$PYPI_PUBLISHER" = "token" ]; then
      [ -n "${PYPI_TOKEN}" ] \
        || fail "no PyPI token: set \$PYPI_TOKEN or ~/.config/pypi/token (see header)"
    fi
  elif [ "$PYPI_STATE" != "exact" ]; then
    fail "unexpected PyPI release state: $PYPI_STATE"
  fi
fi

# ---- plan / publish ----------------------------------------------------------
echo
echo "release plan for ${TAG}:"
echo "  operation: ${RELEASE_OPERATION_ID}"
[ -n "$RELEASE_RESUME_ID" ] && echo "  mode: explicit validated resume"
[ "$SKIP_IMAGE" = 0 ] && echo "  1. buildx ${PLATFORMS} -> candidate (SBOM + provenance)"
[ "$SKIP_IMAGE" = 0 ] && echo "     validate + sign + verify while official tags remain absent"
[ "$SKIP_IMAGE" = 0 ] && echo "     acquire create-only git lock ${LOCK_TAG}"
if [ "$SKIP_PYPI" = 0 ]; then
  if [ "$PYPI_PUBLISHER" = "github-oidc" ]; then
    echo "  2. Vault-authorized GitHub OIDC -> PyPI coord-mcp-server ${VERSION}"
  else
    echo "  2. twine upload missing dist artifacts -> PyPI coord-mcp-server ${VERSION}"
  fi
fi
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
  attestation_args=()
  IFS=',' read -r -a release_platforms <<< "$PLATFORMS"
  for platform in "${release_platforms[@]}"; do
    attestation_args+=(--platform "$platform")
  done
  if [ -n "$RELEASE_RESUME_ID" ]; then
    load_release_lock_for_resume
  else
    echo "release: building + pushing attested image ${IMAGE}:${TAG} (${PLATFORMS})..."
    candidate_tag="${IMAGE}:candidate-${TAG}-$(git rev-parse --short=12 HEAD)-${RELEASE_OPERATION_ID}"
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
  fi
  "$PYTHON_BIN" scripts/verify_image_attestations.py \
    --image "${IMAGE}@${image_digest}" \
    --builder-id "${BUILDER_ID}" \
    "${attestation_args[@]}"
  if [ -z "$RELEASE_RESUME_ID" ]; then
    echo "release: signing ${IMAGE}@${image_digest}..."
    cosign sign --yes --key "${COSIGN_SIGNING_KEY}" "${IMAGE}@${image_digest}"
  fi
  cosign verify \
    --key release/coord-release.pub \
    "${IMAGE}@${image_digest}" >/dev/null
  if [ -z "$RELEASE_RESUME_ID" ]; then
    acquire_release_lock "$image_digest"
  fi
fi

if [ "$SKIP_PYPI" = 0 ]; then
  if [ "$PYPI_PUBLISHER" = "github-oidc" ] \
    && { [ "$PYPI_STATE" = "absent" ] || [ "$PYPI_STATE" = "partial" ]; }
  then
    ensure_github_oidc_trigger "$image_digest"
    dispatch_github_oidc_publish
    echo "release: waiting for the protected GitHub pypi environment:"
    echo "  https://github.com/amittell/coord/actions/workflows/release.yml"
    wait_for_github_oidc_publish
  elif [ "$PYPI_STATE" = "absent" ] || [ "$PYPI_STATE" = "partial" ]; then
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
  # Every post-lock mutation is idempotent and content-checked. An explicit
  # resume uses the digest bound into the lock rather than rebuilding a
  # run-specific provenance index.
  version_probe_error="$(mktemp)"
  if existing_version_digest="$(
    crane digest "${IMAGE}:${TAG}" 2>"$version_probe_error"
  )"; then
    [ "$existing_version_digest" = "$image_digest" ] \
      || fail "${IMAGE}:${TAG} exists at ${existing_version_digest}, not ${image_digest}"
    version_tag_exists=1
  elif grep -Fq "MANIFEST_UNKNOWN" "$version_probe_error"; then
    version_tag_exists=0
  else
    cat "$version_probe_error" >&2
    fail "could not establish the state of ${IMAGE}:${TAG}"
  fi
  rm -f "$version_probe_error"

  echo "release: ensuring official tags resolve to the locked digest..."
  latest_probe_error="$(mktemp)"
  if existing_latest_digest="$(
    crane digest "${IMAGE}:latest" 2>"$latest_probe_error"
  )"; then
    if [ "$existing_latest_digest" != "$image_digest" ]; then
      crane tag "${IMAGE}@${image_digest}" latest
    fi
  elif grep -Fq "MANIFEST_UNKNOWN" "$latest_probe_error"; then
    crane tag "${IMAGE}@${image_digest}" latest
  else
    cat "$latest_probe_error" >&2
    fail "could not establish the state of ${IMAGE}:latest"
  fi
  rm -f "$latest_probe_error"
  [ "$(crane digest "${IMAGE}:latest")" = "$image_digest" ] \
    || fail "${IMAGE}:latest did not resolve to the locked digest"
  if [ "$version_tag_exists" = 0 ]; then
    crane tag "${IMAGE}@${image_digest}" "$TAG"
  fi
  [ "$(crane digest "${IMAGE}:${TAG}")" = "$image_digest" ] \
    || fail "${IMAGE}:${TAG} did not resolve to the locked digest"
  printf '%s@%s\n' "$IMAGE" "$image_digest" \
    > "dist/coord-${TAG}-image-digest.txt"
  printf '%s\n' "$RELEASE_OPERATION_ID" \
    > "dist/coord-${TAG}-release-operation-id.txt"
  echo "release: verified signed image ${IMAGE}@${image_digest}"
  if [ -z "$RELEASE_RESUME_ID" ]; then
    cleanup_image_files
    trap - EXIT
  fi
fi

head="$(git rev-parse HEAD)"
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  [ "$(git rev-list -n 1 "$TAG")" = "$head" ] \
    || fail "local tag ${TAG} does not point to ${head}"
else
  git tag --no-sign -a "${TAG}" -m "coord ${TAG}"
fi
remote_version_tag="$(remote_tag_lines "$TAG")" \
  || fail "could not query remote tag ${TAG}"
if [ -z "$remote_version_tag" ]; then
  if ! git push origin "refs/tags/${TAG}"; then
    remote_version_tag="$(remote_tag_lines "$TAG")" \
      || fail "tag push failed and remote state could not be queried"
  fi
fi
[ -n "$remote_version_tag" ] \
  || fail "tag ${TAG} was not published"
[ "$(remote_tag_commit "$remote_version_tag")" = "$head" ] \
  || fail "remote tag ${TAG} does not point to ${head}"
echo "release: ${TAG} published. Next: update downstream Deployment manifests."
