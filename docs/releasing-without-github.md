# Releasing coord with WritHub authority and PyPI OIDC

`scripts/release.sh` is the authoritative release coordinator. GitHub Actions
has one narrow official role: the existing PyPI Trusted Publisher exchanges
the protected `pypi` environment's OIDC identity for a short-lived PyPI
credential. It may publish packages only after verifying a Vault-signed
authorization created by the local release coordinator. GitHub cannot acquire
the WritHub release lock, sign or promote WHCR images, create the WritHub
version tag, publish a GitHub Release, or deploy.

## One-time setup

1. **PyPI Trusted Publisher**: retain this exact existing registration for
   project `coord-mcp-server`:
   - owner: `amittell`
   - repository: `coord`
   - workflow: `release.yml`
   - environment: `pypi`

   Protect the `pypi` environment with the controls in the next step. The
   publisher job receives only `id-token: write` and `actions: read`; it has no
   checkout and no stored PyPI token.

   An explicit emergency token fallback remains available, but is not the
   default: store a project-scoped token at `~/.config/pypi/token` with mode
   `0600`, then set `COORD_PYPI_PUBLISHER=token`. Never put that token in the
   GitHub workflow.
2. **GitHub enforcement boundary**: these repository settings are part of the
   release mechanism, not optional hardening:
   - bootstrap the reviewed OIDC verifier onto GitHub `main`, then enforce
     protection for administrators too and lock the branch read-only;
   - require a pull request, stale-review dismissal, last-push approval, and
     code-owner approval; block deletion and force-push;
   - keep `.github/CODEOWNERS` ownership on `release.yml`, `release.sh`, and
     `release/coord-release.pub`;
   - restrict the `pypi` environment to the `main` branch only, configure
     exactly one reviewer (`amittell`), and disable administrator bypass;
   - never allow `coord-pypi-v*` tags to trigger Actions. They are inert signed
     authorization records. `scripts/release.sh` dispatches `release.yml` with
     `--ref main` through the already authenticated `gh` CLI.

   The environment branch restriction is what prevents a workflow from an
   attacker-selected tag or branch from obtaining the trusted-publisher
   identity. Admin-enforced protection plus the read-only lock keeps the
   verifier and committed public key immutable during releases. Future WritHub
   release commits do not move GitHub `main`; their inert authorization tags
   transfer the exact source commit for an unprivileged checkout. Verifier
   maintenance requires an explicit, separately reviewed unlock/update/relock
   operation, and no release may run during that window. Do not rely on YAML
   `if:` conditions as a security boundary.
   `scripts/release.sh` queries the live branch protection, reviewers,
   administrator enforcement, stale-review dismissal, read-only lock, and
   exact deployment-branch policy before it dispatches the OIDC workflow, and
   fails closed on any drift. Before building or acquiring the irreversible
   release lock, it also fetches GitHub `main` and byte-compares the workflow,
   CODEOWNERS, public key, and both protected verifier scripts to the reviewed
   release source.
3. **whcr.io login**: `docker login whcr.io` as `alexm` (password: k3s Vault
   `secret/infra/whcr`). For layers >~100MB use the LAN endpoint instead
   (`kebabrack-node1.lan:30502`, needs docker `insecure-registries`) —
   Cloudflare's proxy caps request bodies.
4. **buildx**: a builder with qemu for `linux/amd64,linux/arm64`
   (kebab-rtx6000, or an Apple Silicon Mac with containerized builder).
5. **cosign 3.0.6+ and the release KMS key**: upstream releases
   sign with the non-exportable key `hashivault://coord-release` in the
   dedicated transit Vault. Set the standard `VAULT_ADDR` / `VAULT_TOKEN`
   variables, plus:

   ```sh
   export COSIGN_SIGNING_KEY=hashivault://coord-release
   ```

   The private key is non-exportable and remains in Vault. Its public key is
   committed at `release/coord-release.pub`, so builders and deployers verify
   the same identity without Vault access. Never give the release process a
   Vault root token. Mint a short-lived token (one hour, two-hour explicit
   maximum) under a policy limited to:

   ```hcl
   path "transit/keys/coord-release" {
     capabilities = ["read"]
   }
   path "transit/sign/coord-release" {
     capabilities = ["update"]
   }
   path "transit/sign/coord-release/*" {
     capabilities = ["update"]
   }
   ```

   Revoke that token when the release finishes. A release must stop if either
   the image signature or the committed-key verification fails.
6. **crane 0.21.7 exactly**: the release verifier fetches each in-toto blob,
   hashes its bytes, and validates its predicate and subject before promotion.
   Pinning the verifier version keeps official tag mutation behavior stable.
7. **CPython 3.11–3.14**: the script prefers `.venv/bin/python`, then searches
   versioned Python executables before `python3`. It fails closed outside that
   bounded range (notably macOS `/usr/bin/python3` 3.9, and future 3.15 until
   explicitly qualified). Set
   `COORD_RELEASE_PYTHON=/absolute/path/to/python` to select an explicit
   supported interpreter.

   The selected major/minor version is part of the signed PyPI authorization.
   GitHub installs that exact interpreter for its unprivileged rebuild; it
   never assumes cross-version wheel or sdist byte identity.

## Cutting a release

```sh
# bump [project].version in pyproject.toml + CHANGELOG entry, commit, push
scripts/release.sh              # dry run: validates, builds, prints plan
scripts/release.sh --publish    # authorizes OIDC + promotes image + tags WritHub
```

The canonical image is `whcr.io/alexm/coord:vX.Y.Z`. The script first pushes a
non-official candidate tag, checks every in-toto layer's content hash, predicate,
and linked platform subject, signs the immutable index digest through Vault,
and verifies it against the literal committed public key (the official release
path has no public-key override). Only after the candidate is authenticated does
the script acquire a create-only `coord-release-lock-vX.Y.Z` tag on the exact
release commit. The annotated lock payload also binds a unique 128-bit
operation ID, the authenticated image digest, and the exact filename/SHA-256
map of the rebuilt package artifacts. A competing publisher—even from the same
commit—has a different operation ID and must stop.

After acquiring that lock, the script creates a canonical PyPI authorization
statement binding the same operation ID, version, admitted commit, immutable
WHCR digest, package hashes, local build-Python major/minor, and exact Trusted
Publisher identity. It signs the statement through the same non-exportable
Vault key used for the image, verifies the signature against
`release/coord-release.pub`, and pushes one operation-unique annotated
authorization tag:

```text
coord-pypi-vX.Y.Z-<32-lowercase-hex-operation-id>
```

That tag never triggers Actions. The local coordinator dispatches the locked
`release.yml` from protected GitHub `main` and passes the tag name as inert
input. The verifier checks the tag's Vault signature using the public key from
locked `main`, then checks out the signed source commit into a separate
credential-free directory. An unprivileged build job re-verifies the locked
WHCR attestations and committed-key signature, installs the signed Python
version, rebuilds the wheel and sdist deterministically, requires their
filename/SHA-256 map to equal the signed map, and uploads those exact bytes as
a one-day workflow artifact. Because package build code ran in that job, a
second fresh no-OIDC runner downloads and rehashes the artifact, checks out
only the locked-main verifier, and classifies existing PyPI state there.

Only after the fresh gate succeeds—and only when PyPI is absent or an exact
subset—does the `pypi` environment ask for approval. Its OIDC-enabled job has
no repository checkout, build backend, dependency installation, or repository
script execution. It downloads the verified artifact, rehashes it with runner
Python, rechecks with locked inline code that the live PyPI files are still an
exact subset, and calls the pinned PyPI publisher. A final unprivileged job and
the local coordinator both require the complete PyPI set to match. The
coordinator then idempotently ensures `latest`, `vX.Y.Z`, the local digest
receipt, and the WritHub git tag all resolve to the locked state.

The OCI Distribution API has no conditional tag-create operation, so registry
credentials must not be used to publish release tags outside this locked path.
A verifier, signer, or lock failure therefore cannot publish the PyPI version
or move an official image tag. Under the mandatory admin-enforced,
locked-main and main-only-environment settings, a malicious branch or tag
workflow cannot obtain the Trusted Publisher identity. GitHub has no Vault
signing credential, so it cannot forge a new authorization. Copying an old
signed authorization to another tag or commit fails the bound version,
operation-ID, commit, image, package, build-Python, and publisher checks. If an
interrupted run uploaded only part of the PyPI release, a retry accepts the
remote subset only for matching filename and SHA-256 pairs in the newly built
`dist/` directory, uploads only the missing files, and then requires the
complete remote set to match exactly. Package builds set `SOURCE_DATE_EPOCH`
from the release commit so that comparison is reproducible.

PyPI does not offer an atomic create-version transaction. The fresh gate and
immediate OIDC-job recheck minimize but cannot eliminate a race with some
other credential publishing the same version. No other credential may publish
`coord-mcp-server` releases outside this path. The final exact checks detect a
race but cannot remove an immutable bad file after the fact. Likewise, the
GitHub repository/environment administrator remains part of the Trusted
Publisher trust root because that administrator can rewrite GitHub protection
settings themselves; eliminating that residual requires an independently
operated custom deployment-protection GitHub App.

For an interrupted post-lock release, copy the operation ID printed by the
original run (it is also stored in the annotated
`coord-release-lock-vX.Y.Z` tag) and resume explicitly:

```sh
COORD_RELEASE_RESUME_ID=<32-lowercase-hex> scripts/release.sh --publish
```

Resume rebuilds and rehashes the package artifacts, validates every lock field,
re-verifies attestations and the committed-key signature on the locked image
digest without rebuilding it, validates or recreates the exact operation-bound
OIDC trigger, accepts existing PyPI/image/git artifacts only when they match,
and continues the remaining steps. Merely sharing the same source commit never
grants permission to resume another publisher's operation.
The authenticated reference is written to
`dist/coord-vX.Y.Z-image-digest.txt`; the corresponding operation receipt is
`dist/coord-vX.Y.Z-release-operation-id.txt`.

Deployment manifests are owned by each downstream cluster, not this
repository's `deploy/k8s/prod` overlay. For kebabrack, first verify the base
digest using `release/coord-release.pub`, then build, attest, sign, and verify
the PostgreSQL-enabled `vX.Y.Z-pg` derivative as documented in
`docs/deployment.md`. Update `kebabrack-lab/k8s/10b-coord-app.yaml` with the
verified `tag@sha256:digest`, and let the `cluster-coord-deployment` Argo CD
Application roll it out.

Registry/image knobs: `COORD_IMAGE_REGISTRY`, `COORD_IMAGE_NAME`,
`COORD_IMAGE_PLATFORMS`. PyPI transport knobs:
`COORD_PYPI_PUBLISHER=github-oidc|token` and
`COORD_PYPI_GITHUB_REMOTE` (default `github`).
