# coord prod overlay (kebabrack)

Live GitOps overlay for the `kebabrack` cluster, synced by the Argo CD
`coord-prod` Application. It owns the service, ingress, auth projection,
backup, and observability resources, but deliberately does not own the
Deployment. The cluster-specific Deployment and PostgreSQL topology live in
`kebabrack-lab`; keeping one Deployment owner prevents two Argo Applications
from rolling different images over each other.

Concrete hostnames and Vault paths are
intentionally checked in -- they are not sensitive and Argo needs them
to reconcile. If you fork this repo, point Argo at your own overlay
rather than editing these files, otherwise an Argo sync will replace
your values with kebabrack's.

The portable, environment-neutral reference manifests (no namespace,
no ingress, no VSO) live one directory up in `deploy/k8s/`. Use those
as the starting point for a new overlay.

`tests/test_deploy_overlay.py` guards this directory against
placeholder regressions (`YOUR_CLUSTER`, `coord.internal.example`,
`set-me`) so a future "public readiness" pass cannot silently break
production by sanitising live values back to examples.

## Cluster assumptions

The overlay assumes:

- Ingress controller: Traefik (`kubernetes.io/ingress.class: traefik`)
- Default StorageClass: `local-path`
- Vault Secrets Operator installed with a cluster-shared `VaultAuth`
  at `vault/vault-auth` and a kv-v2 entry at `secret/apps/k8s/coord`
  with key `auth_token`
- A separately managed Deployment in the cluster configuration repository

## Auth posture

The cluster-owned Deployment reads `COORD_AUTH_TOKEN` from the `coord-auth`
Secret populated by the VaultStaticSecret. Keep the service behind
private ingress, VPN, or another access-control layer unless you have
reviewed the dashboard and API exposure model for your environment.

## Files

- `namespace.yaml` - `coord` namespace
- `vaultstaticsecret-auth.yaml` - syncs `COORD_AUTH_TOKEN` from Vault
  into the `coord-auth` Secret
- `pvc.yaml` - 1Gi `local-path` PVC for the SQLite DB
- `service.yaml` - ClusterIP :8080
- `ingress.yaml` - Traefik ingress for `coord.kebabrack.lan`

The live kebabrack Deployment is `k8s/10b-coord-app.yaml` in
`kebabrack-lab`, reconciled by `cluster-coord-deployment`. Do not add a second
Deployment or image-pull projection to this overlay.

## DNS

`coord.kebabrack.lan` must resolve to a Traefik LB IP for the kebabrack
cluster. If you reuse this overlay outside kebabrack, replace it with a
private hostname that resolves to your ingress controller.
