# Releasing coord without GitHub

The GitHub release pipeline (`.github/workflows/release.yml`) published to
PyPI via **OIDC trusted publishing** and pushed images to **ghcr.io** — both
unavailable while the GitHub account is suspended. This is the replacement
path; the workflow file stays for the day GitHub returns.

## One-time setup

1. **PyPI token** (only Alex can do this): pypi.org → Account settings →
   API tokens → new token scoped to project `coord-mcp-server`. Store it:
   - k3s Vault: `secret/infra/pypi` (field `token`)
   - locally: `~/.config/pypi/token` (0600) — what the script reads.
   OIDC note: PyPI trusted publishing only accepts allowlisted CI issuers
   (GitHub Actions, GitLab.com, Google Cloud, ActiveState). Self-hosted
   WritHub CI cannot be one, so a token is the GitHub-free mechanism.
   When GitHub unsuspends, the existing trusted-publisher config on PyPI
   resumes working alongside the token.
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

## Cutting a release

```sh
# bump [project].version in pyproject.toml + CHANGELOG entry, commit, push
scripts/release.sh              # dry run: validates, builds, prints plan
scripts/release.sh --publish    # uploads PyPI + pushes image + tags writhub
```

The canonical image is `whcr.io/alexm/coord:vX.Y.Z`. The script publishes one
multi-platform index with BuildKit SBOM and maximum provenance attestations,
signs the immutable index digest through Vault, verifies it with the committed
public key, and writes the authenticated reference to
`dist/coord-vX.Y.Z-image-digest.txt`.

Deployment manifests are owned by each downstream cluster, not this
repository's `deploy/k8s/prod` overlay. For kebabrack, first verify the base
digest using `release/coord-release.pub`, then build, attest, sign, and verify
the PostgreSQL-enabled `vX.Y.Z-pg` derivative as documented in
`docs/deployment.md`. Update `kebabrack-lab/k8s/10b-coord-app.yaml` with the
verified `tag@sha256:digest`, and let the `cluster-coord-deployment` Argo CD
Application roll it out.

Registry/image knobs: `COORD_IMAGE_REGISTRY`, `COORD_IMAGE_NAME`,
`COORD_IMAGE_PLATFORMS`.
