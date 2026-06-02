# coord prod overlay example

Example manifests for running `coord` on a private Kubernetes cluster.
Copy these into your own deployment repo or GitOps path and replace the
placeholder hostnames, image reference, Vault references, and storage class.

## Cluster assumptions

These manifests are intentionally concrete enough to show the moving parts,
but they are not intended to apply unchanged. They assume:

- Ingress controller: Traefik (`kubernetes.io/ingress.class: traefik`)
- Default StorageClass: `local-path`
- Vault Secrets Operator installed with a cluster-shared
  `VaultAuth` at `vault/vault-auth`, wired to a kv-v2 path that you own
- GHCR image is private; pull creds are sourced from the same secret path

The portable reference manifests (no namespace, no ingress, no VSO) live
one directory up in `deploy/k8s/`.

## Auth posture

The example Deployment reads `COORD_AUTH_TOKEN` from the `coord-auth`
Secret populated by the VaultStaticSecret. Keep the service behind
private ingress, VPN, or another access-control layer unless you have
reviewed the dashboard and API exposure model for your environment.

## Files

- `namespace.yaml` - `coord` namespace
- `vaultstaticsecret-auth.yaml` - syncs `COORD_AUTH_TOKEN` from Vault
  into the `coord-auth` Secret
- `vaultstaticsecret-ghcr.yaml` - renders a `kubernetes.io/dockerconfigjson`
  Secret `ghcr-pull` from the `ghcr_username` / `ghcr_pat` fields
- `pvc.yaml` - 1Gi `local-path` PVC for the SQLite DB
- `deployment.yaml` - single replica, non-root, pinned image digest,
  bearer-token auth
- `service.yaml` - ClusterIP :8080
- `ingress.yaml` - Traefik ingress for `coord.kebabrack.lan`

## DNS

`coord.kebabrack.lan` must resolve to a Traefik LB IP for the kebabrack
cluster. If you reuse this overlay outside kebabrack, replace it with a
private hostname that resolves to your ingress controller.
