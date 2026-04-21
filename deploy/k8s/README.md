# Kubernetes reference manifests

These manifests are a starting point for running the coord service on
Kubernetes. They are not an opinionated, production-grade distribution.
The service itself is shipped as a container image only; how you wire it
into your cluster (namespace, ingress, TLS, storage class, registry auth,
observability) is up to you.

## What is here

- `deployment.yaml` - single-replica Deployment that runs the container as
  a non-root user, mounts `/data` from a PVC, loads `COORD_AUTH_TOKEN`
  from a Secret, and probes `/readyz` and `/health`.
- `service.yaml` - ClusterIP Service on port 8080.
- `pvc.yaml` - 1Gi PersistentVolumeClaim for the SQLite database.
- `secret.example.yaml` - template showing the expected shape of the
  `coord-auth` Secret. Do not commit real tokens; create the Secret
  imperatively with `kubectl create secret` or render it from a secrets
  manager.

## Before you apply

Edit the manifests to match your environment:

1. Set a namespace (or pass `-n my-namespace` to each `kubectl` call).
2. Replace `ghcr.io/YOUR_ORG/coord:latest` with the image path your
   release pipeline publishes to, and pin to a specific tag.
3. Adjust the PVC's `storageClassName` (or remove it to use the default).
4. Create the `coord-auth` Secret with a real random token rather than
   applying `secret.example.yaml` verbatim.
5. Put an Ingress, Gateway API route, or LoadBalancer in front of the
   Service and terminate TLS there. The app itself speaks plain HTTP.

## Apply order

```bash
kubectl apply -f pvc.yaml
# Create the Secret from a real random value - do not use the example file.
kubectl create secret generic coord-auth \
  --from-literal=COORD_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
```

## Scaling

Do not raise `replicas` above 1. The service keeps state in a local SQLite
file on the mounted volume and is not safe to run as multiple writers.
The Deployment uses `strategy.type: Recreate` so the PVC is released
before a new pod attaches during updates.

## Further reading

See `docs/deployment.md` in the repository root for the full container
contract, environment variables, backup guidance, and token rotation
workflow.
