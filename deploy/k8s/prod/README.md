# coord prod overlay (kebabrack)

Live GitOps overlay for the `kebabrack` cluster, synced by Argo CD
(`coord-prod` Application). Concrete hostnames and Vault paths are
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
- WHCR pull credentials at `secret/infra/whcr`; the Deployment consumes the
  rendered `whcr-pull` Secret

## Auth posture

The example Deployment reads `COORD_AUTH_TOKEN` from the `coord-auth`
Secret populated by the VaultStaticSecret. Keep the service behind
private ingress, VPN, or another access-control layer unless you have
reviewed the dashboard and API exposure model for your environment.

## Files

- `namespace.yaml` - `coord` namespace
- `vaultstaticsecret-auth.yaml` - syncs `COORD_AUTH_TOKEN` from Vault
  into the `coord-auth` Secret
- `vaultstaticsecret-whcr.yaml` - renders the `whcr-pull` registry Secret
- `pvc.yaml` - 1Gi `local-path` PVC for the SQLite DB
- `deployment.yaml` - single replica, non-root, pinned image digest,
  bearer-token auth
- `service.yaml` - ClusterIP :8080
- `ingress.yaml` - Traefik ingress for `coord.kebabrack.lan`

## DNS

`coord.kebabrack.lan` must resolve to a Traefik LB IP for the kebabrack
cluster. If you reuse this overlay outside kebabrack, replace it with a
private hostname that resolves to your ingress controller.
