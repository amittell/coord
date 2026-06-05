# GitHub Actions workflows

This directory contains the CI and release automation for `coord`.

## `ci.yml`

Runs on every push to `main` and every pull request.

Jobs:

1. `test` (matrix: Ubuntu + Python 3.11, Ubuntu + Python 3.12,
   Ubuntu + Python 3.14, macOS + Python 3.12, Windows + Python 3.12) -
   installs the project with dev extras, runs `ruff check .`, then
   `pytest -q`. All runners use `bash` as the default shell so quoting
   and pipelines behave consistently on Windows (via Git for Windows'
   bash). The 3.14 row matches the Python version shipped in the
   release container (`python:3.14-slim`) so CI exercises what we
   ship; 3.11 is kept as the baseline per `requires-python = ">=3.11"`
   in `pyproject.toml`. 3.14 is Ubuntu-only to avoid tripling runner
   usage for incremental coverage.
2. `lint-workflows` - runs `actionlint` (via `reviewdog/action-actionlint@v1`)
   over `.github/workflows/` to catch YAML and Actions mistakes.
3. `type-check` - runs `mypy coordination` against Python 3.12 with a
   permissive starter config (see `[tool.mypy]` in `pyproject.toml`).
   Tests are excluded, missing import stubs are ignored, and strict
   mode is off. The intent is to guard against regressions on the
   production package without blocking PRs on unrelated type-noise.
   Tighten the config in follow-up PRs as the codebase gains annotations.
4. `docker-build` - depends on `test`; builds the container image with
   `docker/build-push-action@v6` (no push, `load: true`) to prove the
   Dockerfile still works. Uses GitHub Actions cache (`type=gha`).

`test`, `lint-workflows`, and `type-check` run in parallel; `docker-build`
is gated on `test` so a broken test suite does not waste Docker build
minutes.

Concurrency: superseded CI runs on the same ref are cancelled
(`cancel-in-progress: true`).

Permissions: `contents: read` only.

## `release.yml`

Runs on git tag pushes matching `v*`, and can also be triggered manually
via `workflow_dispatch`.

Jobs:

- `publish-image` - builds a multi-arch (`linux/amd64`, `linux/arm64`)
  container image and pushes to `ghcr.io/<owner>/coord`, emits an
  SPDX SBOM plus SLSA provenance attestations (BuildKit-native and
  GitHub-native), signs the image keyless with cosign, and (for real
  tag pushes) creates a GitHub Release with auto-generated notes.
- `publish-pypi` - on real tag pushes only (skipped on
  `workflow_dispatch`), builds the sdist + wheel, validates that the
  tag matches `pyproject.toml`'s `version` field, and publishes to
  PyPI via OIDC trusted publishing (no API token stored in repo
  secrets). Bootstrap requires a one-time pending-publisher
  registration on PyPI; see "PyPI trusted publishing" below.
- `bump-manifest` - on real tag pushes only (gated on the same
  condition as `publish-pypi`), rewrites
  `deploy/k8s/prod/deployment.yaml` to pin the image tag + digest
  that `publish-image` just published, then commits and pushes the
  result back to `main`. The commit message ends with `[skip ci]`
  so the manifest bump does not retrigger the CI matrix; ArgoCD
  watches `main` directly and reconciles regardless. Inputs
  (image name, version tag, digest) flow through `env:` blocks
  rather than `${{ ... }}` interpolation inside `run:`, eliminating
  the workflow-injection surface. Job permission: `contents: write`.

  Edge cases the job handles cleanly:
  - Manifest already at the target digest: detected via `git diff
    --quiet`; the job exits without a commit.
  - Concurrent push to `main` while the release was building: the
    job runs `git pull --rebase origin main` before pushing.
  - Manual rebuild via `workflow_dispatch`: skipped via the same
    `if: github.event_name == 'push' && startsWith(github.ref,
    'refs/tags/')` guard used by `publish-pypi`. Manual rebuilds
    of an old tag or release candidates do not silently flip
    production.

Tag behaviour:

| Trigger             | Pushed tags                         | GitHub Release |
|---------------------|-------------------------------------|----------------|
| `push` (git tag)    | `:<tag>` and `:latest`              | yes            |
| `workflow_dispatch` | `:<inputs.version>` only (no latest)| no             |

The `workflow_dispatch` path deliberately does not push `:latest` so
release candidates and ad-hoc rebuilds cannot clobber the production
`:latest` pointer.

Concurrency: release runs are NOT cancelled in flight
(`cancel-in-progress: false`) so a half-pushed multi-arch image cannot
be left behind by a second run.

### Required permissions

Set on the `release.yml` workflow itself:

- `contents: write` - `softprops/action-gh-release@v2` needs this to
  create the GitHub Release.
- `packages: write` - push to GHCR.
- `id-token: write` - OIDC token for
  `actions/attest-build-provenance@v2`.
- `attestations: write` - store the attestation on the repo.

### Required repository secrets

None beyond the default `GITHUB_TOKEN`. GHCR login uses
`${{ secrets.GITHUB_TOKEN }}` directly.

### GitHub Enterprise

The release workflow defaults to `ghcr.io/<owner>/coord`, but both the
registry host and the repo path are overridable through repo or org
Actions variables (no secret values required):

- `IMAGE_REGISTRY` - registry hostname, for example
  `containers.ghe.example.com`. If unset, defaults to `ghcr.io`.
- `IMAGE_REPO` - path under the registry, for example
  `platform/coord`. If unset, defaults to
  `<github.repository_owner>/coord`.

Set them under Settings -> Secrets and variables -> Actions -> Variables.
The workflow resolves them at runtime and falls back to the GHCR
defaults when unset, so upstream behavior is preserved.

Login continues to use `${{ secrets.GITHUB_TOKEN }}` against the
configured registry. Some GHE setups require a separate Personal Access
Token (PAT) with `write:packages` (or the enterprise-equivalent) scope
if the default `GITHUB_TOKEN` cannot push to your registry; in that
case, store the PAT as a repo secret and swap `password:` in the
`docker/login-action` step accordingly. This is a common GHE gotcha
rather than a hard requirement - try the default token first.

`actions/attest-build-provenance@v2` requires **GitHub Enterprise
Server 3.10 or later** with the attestations feature enabled, OR a
public repository, OR a private repository owned by an organization
on a plan that supports attestations. It **does NOT work on personal
(user-owned) private repositories** - the step fails with
"Feature not available for user-owned private repositories".

In any of those unsupported cases, set the repo or org Actions
variable `SKIP_ATTESTATION=true` and the provenance step will be
skipped cleanly. The image push, BuildKit-native SBOM/provenance
attestations, and cosign signing are all unaffected. This project's
own `amittell/coord` repo (user-owned private) sets this var.

```
Settings -> Secrets and variables -> Actions -> Variables
    SKIP_ATTESTATION = true
```

### SBOM and image attestations

Every successful release now emits four independent supply-chain
signals against the pushed image:

1. **BuildKit SBOM attestation** (SPDX JSON) - produced by
   `docker/build-push-action` with `sbom: true`. Attached to the image
   manifest as an OCI referrer and retrievable with
   `docker buildx imagetools inspect <ref> --format '{{json .SBOM}}'`
   or `cosign download sbom <ref>`.
2. **BuildKit SLSA provenance attestation** (`mode=max`) - produced by
   the same action with `provenance: mode=max`. Includes the full build
   invocation, materials, and builder identity. Inspect with
   `docker buildx imagetools inspect <ref> --format '{{json .Provenance}}'`.
3. **GitHub-native provenance attestation** - produced by
   `actions/attest-build-provenance@v2` and stored in the repo-level
   attestations store (separate from the OCI-attached BuildKit output).
   Verifiable with `gh attestation verify`.
4. **Cosign keyless signature** - see below.

The BuildKit attestations (1, 2) and the GitHub attestation (3) are
independent. Keeping both is intentional: the OCI-attached artifacts
travel with the image across registry mirrors, while the GitHub-stored
attestation is queryable through the GitHub API without pulling the
image.

### Cosign signing

The workflow signs the pushed image with `cosign sign --yes` in
keyless mode. The signing identity is the workflow's ephemeral OIDC
token (no long-lived keys), and the signature is anchored in the
public Rekor transparency log.

Verify a signed image against this repository's workflow identity:

```
cosign verify \
  --certificate-identity-regexp '^https://github.com/<owner>/<repo>/\.github/workflows/release\.yml@' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  ghcr.io/<owner>/coord@sha256:<digest>
```

Replace `<owner>/<repo>` and the digest to match your fork and target
image. The `--certificate-identity-regexp` value pins the expected
signer to this exact workflow file - accept no others.

The `cosign-installer` action is pinned by commit SHA
(`sigstore/cosign-installer@d7543c93...` = v3.10.0) and fetches a
fixed cosign CLI release (currently v3.0.6) via `cosign-release:`.

### Disabling individual signals

Each signing or attestation step is gated on a repo or org Actions
variable so operators can disable any one without editing the
workflow:

| Variable | Effect when set to `true` |
|----------|---------------------------|
| `SKIP_ATTESTATION` | Skips `actions/attest-build-provenance`. Required on GHES older than 3.10 or where the attestations feature is disabled. |
| `SKIP_SIGNING` | Skips both `Install cosign` and `Sign image with cosign (keyless)`. Use on runners that cannot reach the Sigstore public good instance (Fulcio / Rekor). |

The BuildKit SBOM and BuildKit provenance attestations are emitted
unconditionally by the build step and have no skip variable; they are
produced locally by BuildKit and pushed alongside the image manifest,
so they do not depend on external services.

```
Settings -> Secrets and variables -> Actions -> Variables
    SKIP_ATTESTATION = true     # older GHES without attestations API
    SKIP_SIGNING = true         # environments without Sigstore access
```

### PyPI trusted publishing

The `publish-pypi` job uploads to PyPI without an API token by
exchanging GitHub's OIDC identity for a short-lived PyPI token at
publish time. This requires a one-time pending-publisher
registration on PyPI before the first publish succeeds.

Bootstrap steps (one-time, by a project maintainer with PyPI
account access):

1. Sign in at `https://pypi.org`.
2. Go to "Your account" -> "Publishing" -> "Add a new pending
   publisher" (the form is at
   `https://pypi.org/manage/account/publishing/`).
3. Fill in:
   - PyPI Project Name: `coord-mcp-server`
   - Owner: `amittell`
   - Repository name: `coord`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
4. Save. PyPI now trusts this exact workflow file on this exact
   repo to publish under the named project. The next tagged push
   that runs the workflow will create the project on first
   publish.

GitHub side: create an environment called `pypi` under Settings
-> Environments. Optionally add deployment protection rules
(required reviewers, deployment branches, etc.) -- the
environment binding alone is enough for OIDC to work.

After bootstrap, every `git tag v0.X.Y && git push --tags`
triggers a build + publish. The workflow refuses to publish if
the tag version does not match the `version` field in
`pyproject.toml`, so an accidental tag without a version bump is
caught at the verification step.

To pause PyPI publishing without disabling the whole release:
remove the pending publisher in PyPI, or change the workflow's
environment to a name that PyPI does not trust. The `:latest`
container image and GitHub Release still publish from
`publish-image`.

### Manual trigger (workflow_dispatch)

Via the GitHub UI: Actions tab -> `release` -> Run workflow -> supply
`version` (for example `v0.1.0-rc1`).

Via `gh`:

```
gh workflow run release.yml -f version=v0.1.0-rc1
```

The resulting image will be pushed as
`ghcr.io/<owner>/coord:v0.1.0-rc1` and `:latest` will not be touched.

## Verification

Locally:

```
python -c "import yaml; list(yaml.safe_load_all(open('.github/workflows/ci.yml')))"
python -c "import yaml; list(yaml.safe_load_all(open('.github/workflows/release.yml')))"
```

If you have `actionlint` installed:

```
actionlint .github/workflows/*.yml
```

## Pinning policy

Third-party actions are pinned to a full 40-char commit SHA, with the
readable version tag preserved in a trailing comment, for example:

```
uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1
```

This blocks tag-retargeting supply-chain attacks: the workflow runs the
exact commit reviewed at pin time, regardless of what the upstream tag
later points at.

Updates are handled by Dependabot (`.github/dependabot.yml`), which
opens a weekly grouped PR that bumps both the SHA and the trailing
version comment in lockstep. The same config also keeps `requirements.txt`
and the Dockerfile base image current.
