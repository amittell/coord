# Releasing coord without GitHub

The former GitHub release pipeline published to PyPI via OIDC trusted
publishing and pushed images to GHCR. `scripts/release.sh` is now the sole
authoritative publisher. `.github/workflows/release.yml` is manual,
candidate-only supply-chain validation and cannot publish official artifacts
or deploy.

## One-time setup

1. **PyPI token** (only Alex can do this): pypi.org → Account settings →
   API tokens → new token scoped to project `coord-mcp-server`. Store it:
   - k3s Vault: `secret/infra/pypi` (field `token`)
   - locally: `~/.config/pypi/token` (0600) — what the script reads.
   OIDC note: PyPI trusted publishing only accepts allowlisted CI issuers
   (GitHub Actions, GitLab.com, Google Cloud, ActiveState). Self-hosted
   WritHub CI cannot be one, so a token is the GitHub-free mechanism.
   Remove the historical GitHub trusted-publisher registration so it cannot
   become a second release authority later.
2. **whcr.io login**: `docker login whcr.io` as `alexm` (password: k3s Vault
   `secret/infra/whcr`). For layers >~100MB use the LAN endpoint instead
   (`kebabrack-node1.lan:30502`, needs docker `insecure-registries`) —
   Cloudflare's proxy caps request bodies.
3. **buildx**: a builder with qemu for `linux/amd64,linux/arm64`
   (kebab-rtx6000, or an Apple Silicon Mac with containerized builder).
4. **cosign 3.0.6+ and the release KMS key**: upstream GitHub-free releases
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
5. **crane 0.21.7 exactly**: the release verifier fetches each in-toto blob,
   hashes its bytes, and validates its predicate and subject before promotion.
   Pinning the verifier version keeps official tag mutation behavior stable.
6. **Python 3.11 or newer**: the script prefers `.venv/bin/python`, then
   searches versioned Python executables before `python3`. It fails closed on
   an older interpreter (notably macOS `/usr/bin/python3` 3.9). Set
   `COORD_RELEASE_PYTHON=/absolute/path/to/python` to select an explicit
   supported interpreter.

## Cutting a release

```sh
# bump [project].version in pyproject.toml + CHANGELOG entry, commit, push
scripts/release.sh              # dry run: validates, builds, prints plan
scripts/release.sh --publish    # uploads PyPI + pushes image + tags writhub
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
commit—has a different operation ID and must stop. The lock holder uploads the
immutable PyPI artifacts and idempotently ensures `latest`, `vX.Y.Z`, the local
digest receipt, and the WritHub git tag all resolve to the locked state.
The OCI Distribution API has no conditional tag-create operation, so registry
credentials must not be used to publish release tags outside this locked path.
A verifier or signer failure therefore cannot publish the PyPI version or move
an official image tag. If an interrupted run uploaded only part of the PyPI
release, a retry accepts the remote subset only for matching filename and SHA-256
pairs in the newly built `dist/` directory, uploads only the missing
files, and then requires the complete remote set to match exactly. Package
builds set `SOURCE_DATE_EPOCH` from the release commit so that comparison is
reproducible.

For an interrupted post-lock release, copy the operation ID printed by the
original run (it is also stored in the annotated
`coord-release-lock-vX.Y.Z` tag) and resume explicitly:

```sh
COORD_RELEASE_RESUME_ID=<32-lowercase-hex> scripts/release.sh --publish
```

Resume rebuilds and rehashes the package artifacts, validates every lock field,
re-verifies attestations and the committed-key signature on the locked image
digest without rebuilding it, accepts existing PyPI/image/git artifacts only
when they match, and continues the remaining steps. Merely sharing the same
source commit never grants permission to resume another publisher's operation.
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
`COORD_IMAGE_PLATFORMS`.
