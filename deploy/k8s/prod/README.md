# coord prod overlay (kebabrack k3s)

Argocd target manifests for running `coord` on the kebabrack k3s cluster.
Deployed via the `coord-prod` Application in the `argocd` namespace, which
syncs this directory from the `amittell/coord` repo (`main` branch).

## Cluster assumptions

These manifests are specific to kebabrack and will not apply cleanly to
other clusters without edits. They assume:

- Ingress controller: Traefik (`kubernetes.io/ingress.class: traefik`)
- Default StorageClass: `local-path`
- Vault Secrets Operator installed with a cluster-shared
  `VaultAuth` at `vault/vault-auth`, wired to an authenticated Vault
  that carries `secret/apps/k8s/coord` (kv-v2)
- GHCR image is private; pull creds are sourced from the same Vault path

The portable reference manifests (no namespace, no ingress, no VSO) live
one directory up in `deploy/k8s/`.

## Auth posture

`COORD_ALLOW_INSECURE_NO_AUTH=true` is set on the Deployment. The service
sits behind a LAN-only Traefik ingress (`coord.kebabrack.lan`) and is
not reachable from outside the network. This trades token-based access
control for a browser-accessible dashboard. To flip to bearer-required
mode instead, remove the env var and let the container read
`COORD_AUTH_TOKEN` from the `coord-auth` Secret (populated by the
VaultStaticSecret).

## Files

- `namespace.yaml` - `coord` namespace
- `vaultstaticsecret-auth.yaml` - syncs `COORD_AUTH_TOKEN` from Vault
  into the `coord-auth` Secret (unused while insecure mode is on, but
  kept ready for the flip)
- `vaultstaticsecret-ghcr.yaml` - renders a `kubernetes.io/dockerconfigjson`
  Secret `ghcr-pull` from the `ghcr_username` / `ghcr_pat` fields
- `pvc.yaml` - 1Gi `local-path` PVC for the SQLite DB
- `deployment.yaml` - single replica, non-root, pinned image digest,
  insecure-no-auth mode
- `service.yaml` - ClusterIP :8080
- `ingress.yaml` - Traefik ingress for `coord.kebabrack.lan`

## DNS

`coord.kebabrack.lan` must resolve to a Traefik LB IP (any of the
`192.168.210.126/139/177/197/53/75` addresses). This cluster's
`*.kebabrack.lan` DNS is served by the Firewalla at `192.168.210.1`
and is not managed from the cluster.
