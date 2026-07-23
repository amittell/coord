# GitHub Actions workflows

This directory contains CI and non-authoritative release-candidate validation
for `coord`.

## `ci.yml`

CI runs on pushes to `main` and pull requests, except documentation-only
changes. Its bounded jobs are:

1. `quality`: Ruff, mypy, and the OpenTelemetry enabled-path tests on Python
   3.14.
2. `test`: the full SQLite suite on the supported floor (3.11) and production
   image runtime (3.14).
3. `platform`: focused OS-facing smoke tests on macOS and Windows.
4. `lint-workflows`: actionlint over `.github/workflows/`.
5. `docker-build`: a real multi-platform base and PostgreSQL derivative build
   against a private ephemeral registry, strict per-platform SBOM/SLSA
   verification, and runtime/readiness smoke tests.

Every pytest invocation uses `python -m pytest` so the checked-out repository
root remains importable on all runners. Superseded CI runs on the same ref are
cancelled. Workflow permissions are read-only.

## `release.yml`

This workflow is deliberately **not an official publisher**. It has only a
manual `workflow_dispatch` trigger and produces a commit-qualified
`candidate-<label>-<sha>` image for supply-chain validation. It cannot run from
a git tag and it cannot:

- publish an official version or `latest` image tag;
- publish to PyPI;
- create a GitHub Release;
- change the production deployment manifest.

The authoritative release path is [`scripts/release.sh`](../../scripts/release.sh),
which publishes the canonical `whcr.io/alexm/coord` image, signs with the
repository's committed release identity, and acquires the immutable WritHub
release lock. Production rollout remains a separate GitOps operation.

The manual candidate job:

1. reuses the real PostgreSQL gate;
2. builds `linux/amd64` and `linux/arm64` with a digest-pinned SBOM generator;
3. emits BuildKit SLSA provenance with a run-specific builder ID;
4. rehashes the top-level OCI index and each in-toto blob;
5. requires the exact expected builder ID, subject digest, statement type,
   SPDX 2.3 predicate, and both predicates on every platform;
6. keyless-signs and verifies the candidate digest with the exact workflow
   identity.

Candidate tags are retained as review evidence. Operators should apply the
registry's normal candidate-retention policy; candidates are never selected by
the production manifests.

### Manual invocation

```bash
gh workflow run release.yml -f version=v0.49.0-review1
```

The input is a candidate label, not an official release version.

### Configuration

The workflow defaults to `ghcr.io/<owner>/coord`. Forks may set:

- `IMAGE_REGISTRY`: registry hostname.
- `IMAGE_REPO`: repository path below that registry.

Login uses the workflow's `GITHUB_TOKEN`; a custom registry may require an
equivalent package-write credential. No repository secret is otherwise
required for the default GHCR path.

Required workflow permissions are:

- `contents: read`;
- `packages: write`;
- `id-token: write`;
- `attestations: write`.

## Local verification

Parse every workflow and run actionlint before committing workflow changes:

```bash
python -c "import yaml; list(yaml.safe_load_all(open('.github/workflows/release.yml')))"
actionlint .github/workflows/*.yml
```

The release contract has additional executable fences:

```bash
python -m pytest \
  tests/test_audit_coordination_hardening.py \
  tests/test_audit_templates_placeholders.py -q
```

## Pinning policy

Third-party actions, Dockerfile frontends, base images, QEMU/binfmt images, SBOM
generators, and registry helper archives are immutable-digest or exact-version
pinned. Update a pin only after reviewing the upstream release and checksum,
then run the workflow lint and audit tests above.
