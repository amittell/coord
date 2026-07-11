# Runbook: coord SQLite -> Postgres HA hard-drain cutover

Status: beta/operator-gated runbook for the coord HA re-architecture
(`docs/designs/coord-ha-rearchitecture.md`, Section 8). Last reviewed
2026-07-11.

This procedure is only for migrating an existing SQLite deployment. A new
third-party PostgreSQL install has no SQLite state to drain or import; use the
fresh-install quickstart in `docs/deployment.md` instead.

> **WARNING -- ArgoCD (learned from the 2026-07-05 v0.44.0 incident):** the HA
> manifests live in `deploy/k8s/ha-cutover/`, deliberately OUTSIDE ArgoCD's
> watched path (`deploy/k8s/prod/`). Never move them into the watched path
> ahead of the cutover: ArgoCD auto-applies on merge, and doing so activated
> the cutover prematurely against an image that could not speak Postgres,
> 401ing the whole fleet. During the cutover itself, ArgoCD's
> `selfHeal` will fight any manual `kubectl apply` that diverges from
> `deploy/k8s/prod/` -- disable auto-sync first (step 4) and re-enable it only
> after the prod manifest in git matches the HA state.

This is a **hard-drain** cutover, not a clean break and not a live canary.
There must be **no window where a SQLite-backed pod and a Postgres-backed pod
both serve claim writes** -- two independent write truths produce mutual
overlap (split-brain). The procedure stops the old writer before starting the
new one.

Durable state (`engineer_tokens`, `ownership_config`) is migrated first.
Ephemeral state (active claims, queue waiters, open requests, history) is
**not** migrated: agents re-announce their claims within a TTL after cutover,
and idempotent client retry (design Section 10.1) makes that safe. Undelivered
`webhook_outbox` rows are **drained on the old instance before** cutover, not
carried across.

---

## 0. Preconditions (do these before the maintenance window)

- [ ] A PostgreSQL-enabled coord image, based on the exact reviewed coord
      release, is built and published under a distinct immutable tag. The
      standard production image is intentionally SQLite-only; merely setting
      `COORD_DATABASE_URL` on it fails fast because asyncpg is absent.
- [ ] The cutover image actually bundles the pinned Postgres driver from
      `requirements-postgres.txt`. One minimal derivative Dockerfile (built
      from this repository so the requirements file is in context) is:

  ```dockerfile
  ARG COORD_BASE=ghcr.io/amittell/coord:<release>@sha256:<digest>
  FROM ${COORD_BASE}
  USER root
  COPY requirements-postgres.txt /tmp/requirements-postgres.txt
  RUN /opt/venv/bin/pip install --no-cache-dir \
        -r /tmp/requirements-postgres.txt \
      && rm /tmp/requirements-postgres.txt
  USER coord
  ```

  Build/publish that variant, repin
  `deploy/k8s/ha-cutover/deployment.yaml` to its immutable digest, and verify:

  ```bash
  docker run --rm ghcr.io/amittell/coord:<postgres-tag>@sha256:<digest> \
    python -c "import asyncpg; print('asyncpg', asyncpg.__version__)"
  ```

- [ ] The real `coord-pg` Secret is created (sealed-secrets / External Secrets
      / Vault, or imperatively -- see
      `deploy/k8s/ha-cutover/secret.example.yaml` for the required keys and a
      bootstrap command). Never `kubectl apply` the example file itself: its
      placeholder values would clobber the live credentials.
- [ ] The Postgres `StatefulSet`, PVC, and `NetworkPolicy`
      (`deploy/k8s/ha-cutover/postgres.yaml` -- it deliberately contains no
      Secret) are applied and the pod is `Ready`. coord creates its
      consolidated v1 schema on first connect; the database is otherwise empty.
- [ ] `scripts/migrate_tokens_ownership.py` is present on the machine that has
      read access to the copied SQLite file and authenticated `kubectl`
      port-forward access to the cluster, with `psql` and the `sqlite3` CLI
      installed (the CLI is only needed locally; the coord image does not ship
      it). Run this from the exact reviewed coord release with its `[postgres]`
      extra installed; the one-shot bootstrap below imports that release's
      schema code.
- [ ] You know the database/user/password from the target
      `COORD_DATABASE_URL` Secret. URL-encode reserved characters in the local
      DSN used below.
- [ ] You chose one `COORD_POSTGRES_SCHEMA` and set the same value in the
      future Deployment. This runbook uses the default `coord`; changing the
      name selects a different logical dataset in the same database. It must
      match `[a-z_][a-z0-9_]{0,62}` and cannot be `public`,
      `information_schema`, or start with `pg_`.
- [ ] A low-traffic maintenance window is agreed and the two human operators
      are aware their ~40 agents will briefly 503 and then re-announce.
- [ ] Rollback image tag (current SQLite image) and the SQLite PVC are
      recorded; the PVC is **retained**, not deleted, for at least ~2 weeks.
- [ ] The exact currently deployed SQLite manifest is saved before maintenance
      (for this repo/ArgoCD layout:
      `cp deploy/k8s/prod/deployment.yaml ./previous-sqlite-deployment.yaml`)
      and reviewed to confirm it pins the current image, one replica, and the
      `coord-data` PVC.
- [ ] Token create/rotate/revoke and ownership-config changes are frozen from
      the final step-4 snapshot until the rollback window closes. This is what
      makes the retained SQLite PVC an exact rollback source; there is no safe
      automatic PostgreSQL-to-SQLite reverse importer.

Run every subsequent command block in this same Bash session. The fail-fast
options are part of the split-brain guard: any failed patch, drain, snapshot,
import, or apply stops the procedure instead of falling through to the next
writer transition. Set shared variables (adjust to your environment):

```bash
set -euo pipefail

NS=coord                      # kubernetes namespace
# Pods are labeled app.kubernetes.io/name=coord (see the Deployment manifests);
# there is no bare `app=coord` label.
SQLITE_POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=coord -o jsonpath='{.items[0].metadata.name}')
test -n "$SQLITE_POD" || { echo 'no coord pod found'; exit 1; }
SQLITE_IMAGE=$(kubectl -n "$NS" get deploy coord -o jsonpath='{.spec.template.spec.containers[?(@.name=="coord")].image}')
test -n "$SQLITE_IMAGE" || { echo 'could not resolve current coord image'; exit 1; }
SQLITE_PATH=/data/coordination.db
PG_LOCAL_PORT=15432
PG_URL='postgresql://coord:URL_ENCODED_PASSWORD@127.0.0.1:15432/coord'
PG_SCHEMA=coord                 # must match COORD_POSTGRES_SCHEMA in deployment.yaml
HOST='https://coord.example.com'
TOKEN=replace-with-real-coord-token
SQLITE_ROLLBACK_MANIFEST=./previous-sqlite-deployment.yaml
test -s "$SQLITE_ROLLBACK_MANIFEST" || {
  echo "missing saved SQLite rollback manifest: $SQLITE_ROLLBACK_MANIFEST" >&2
  exit 1
}

wait_for_no_coord_pods() {
  current="$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=coord -o name)" \
    || return 1
  if [ -n "$current" ]; then
    kubectl -n "$NS" wait --for=delete pod \
      -l app.kubernetes.io/name=coord --timeout=120s || return 1
  fi
  current="$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=coord -o name)" \
    || return 1
  [ -z "$current" ] || {
    echo "coord pods still exist after scale-to-zero: $current" >&2
    return 1
  }
}
```

The operator commands run on this machine, while `coord-postgres` is
cluster-local and protected by a NetworkPolicy. In the same shell, open an API
server port-forward and keep it alive for the bootstrap, imports, verification,
and any immediate rollback:

```bash
kubectl -n "$NS" port-forward pod/coord-postgres-0 \
  "$PG_LOCAL_PORT:5432" >./coord-pg-port-forward.log 2>&1 &
PG_FORWARD_PID=$!
trap 'kill "$PG_FORWARD_PID" >/dev/null 2>&1 || true' EXIT

for _ in {1..30}; do
  psql "$PG_URL" -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1 && break
  sleep 1
done
kill -0 "$PG_FORWARD_PID" 2>/dev/null || {
  cat ./coord-pg-port-forward.log >&2
  exit 1
}
psql "$PG_URL" -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null || {
  echo 'PostgreSQL port-forward did not become ready' >&2
  exit 1
}
```

---

## 1. Snapshot the source SQLite (safety net)

Take a consistent copy of the live database before touching anything. This is
both your migration source and a rollback artifact.

```bash
# Use the SQLite backup API for a consistent copy even while coord is running.
# The image ships the Python stdlib sqlite3 module but NOT the sqlite3 CLI
# (python:slim + git only), so drive the backup through python -c.
kubectl -n "$NS" exec "$SQLITE_POD" -- python -c "
import sqlite3
src = sqlite3.connect('$SQLITE_PATH')
dst = sqlite3.connect('/data/coordination.pre-cutover.db')
with dst:
    src.backup(dst)
dst.close()
src.close()
"

kubectl -n "$NS" cp "$SQLITE_POD:/data/coordination.pre-cutover.db" \
  ./coordination.pre-cutover.db
```

- [ ] `coordination.pre-cutover.db` exists locally and is non-empty
      (`sqlite3 coordination.pre-cutover.db 'SELECT count(*) FROM engineer_tokens;'`).

---

## 2. Drain the webhook outbox on the old instance

Undelivered `webhook_outbox` rows are durable but are **not** migrated. Let the
old (still-SQLite) instance flush them before you stop it, so no
auto-coexist / auto-narrow / PR-comment notification is lost.

```bash
# Inspect how many rows are still pending/failed (not yet delivered/exhausted).
# Read-only URI mode; the sqlite3 CLI is not in the image, so use python -c.
kubectl -n "$NS" exec "$SQLITE_POD" -- python -c "
import sqlite3
db = sqlite3.connect('file:$SQLITE_PATH?mode=ro', uri=True)
for status, count in db.execute(
    'SELECT status, count(*) FROM webhook_outbox GROUP BY status'
):
    print(status, count)
db.close()
"
```

- [ ] Wait until `pending` reaches 0 (the delivery loop drains on its normal
      cadence), or confirm the remaining rows are terminal (`delivered`,
      `exhausted`). Rows stuck `failed` with retries remaining will be lost at
      cutover -- decide explicitly whether to wait them out or accept the loss,
      and record the decision.

> Active claims, `claim_queue` waiters, open `requests`, and history
> (`conflict_log`, `request_events`) are **acknowledged-as-dropped** at
> cutover. This is acceptable by design: claims re-announce within a TTL, and
> waiters/requests are re-issued by agents. No action needed here beyond
> communicating it.

---

## 3. Export + import durable state (tokens + ownership)

Bootstrap or upgrade the **exact target schema** before importing rows. This
one-shot calls the selected release's normal database initializer but does not
start an HTTP listener or serve claim writes, so the old SQLite service remains
the only live writer:

```bash
COORD_DATABASE_URL="$PG_URL" \
COORD_POSTGRES_SCHEMA="$PG_SCHEMA" \
python3 -c 'import asyncio; from coordination.service import build_service; asyncio.run(build_service().db.init())'
```

- [ ] The command exits 0. If it fails, stop: the PostgreSQL role may lack
      schema/table DDL permission, the driver may be absent, or the selected
      database/schema may be wrong. Do not run the import against an
      uninitialized target.

First, review the SQL the migration will apply (dry run -- changes nothing):

```bash
python3 scripts/migrate_tokens_ownership.py \
  --sqlite ./coordination.pre-cutover.db \
  --postgres-url "$PG_URL" \
  --postgres-schema "$PG_SCHEMA" \
  --dry-run --out ./coord-durable-migration.sql
```

- [ ] The emitted SQL contains an `INSERT INTO
      "<PG_SCHEMA>"."engineer_tokens" ... ON CONFLICT (token_sha256) DO UPDATE`
      and an `INSERT INTO "<PG_SCHEMA>"."ownership_config" ... ON CONFLICT
      (id) DO UPDATE`, wrapped in `BEGIN; ... COMMIT;`, and references **no**
      ephemeral tables. Unqualified target tables are a failure.

Then apply it for real (idempotent -- safe to re-run):

```bash
python3 scripts/migrate_tokens_ownership.py \
  --sqlite ./coordination.pre-cutover.db \
  --postgres-url "$PG_URL" \
  --postgres-schema "$PG_SCHEMA"
```

Verify the import landed:

```bash
psql "$PG_URL" -c "SELECT count(*) AS tokens FROM ${PG_SCHEMA}.engineer_tokens;"
psql "$PG_URL" -c "SELECT count(*) AS active_tokens FROM ${PG_SCHEMA}.engineer_tokens WHERE revoked_at IS NULL;"
psql "$PG_URL" -c "SELECT length(yaml_text) AS ownership_bytes FROM ${PG_SCHEMA}.ownership_config WHERE id = 1;"
```

- [ ] Token count matches the source
      (`sqlite3 ./coordination.pre-cutover.db 'SELECT count(*) FROM engineer_tokens;'`).
- [ ] Active (non-revoked) token count is sane -- this is what keeps the ~40
      agents authenticated under `COORD_REQUIRE_PER_ENGINEER_TOKEN=true`.
- [ ] `ownership_config` row is present if the source had one.

> This first import pre-stages the rarely changing durable rows while the old
> instance is live. It is not the authoritative final copy: step 4 stops all
> SQLite writers, takes a fresh consistent snapshot from the retained PVC, and
> imports that post-drain snapshot before any PostgreSQL-backed replica starts.

---

## 4. Hard cutover

No mixed-backend writers. Stop the old writer, then start the new ones.

```bash
# 4a. Disable ArgoCD auto-sync before the first manual mutation. Otherwise
#     selfHeal can scale the SQLite writer back up during the final snapshot.
kubectl -n argocd patch application coord-prod --type merge \
  -p '{"spec":{"syncPolicy":null}}'

# 4b. Stop the SQLite writer completely (scale to 0, do NOT rolling-update).
kubectl -n "$NS" scale deploy/coord --replicas=0
kubectl -n "$NS" rollout status deploy/coord --timeout=120s
wait_for_no_coord_pods  # rollout status alone does not prove pod deletion

# 4c. Mount the retained SQLite PVC in a non-serving helper pod and take a
#     fresh, consistent post-drain backup. Reusing the step-1 snapshot here
#     would silently miss any token/ownership change made after that snapshot.
SNAPSHOT_POD=coord-sqlite-final-snapshot
kubectl -n "$NS" delete pod "$SNAPSHOT_POD" --ignore-not-found --wait=true
cat <<EOF | kubectl -n "$NS" apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: ${SNAPSHOT_POD}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  imagePullSecrets:
    - name: ghcr-pull
  containers:
    - name: snapshot
      image: ${SQLITE_IMAGE}
      imagePullPolicy: IfNotPresent
      command: ["sh", "-c", "sleep 600"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: coord-data
          mountPath: /data
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: coord-data
      persistentVolumeClaim:
        claimName: coord-data
    - name: tmp
      emptyDir: {}
EOF
kubectl -n "$NS" wait --for=condition=Ready "pod/$SNAPSHOT_POD" --timeout=120s
kubectl -n "$NS" exec "$SNAPSHOT_POD" -- python -c "
import pathlib, sqlite3
source = sqlite3.connect('file:$SQLITE_PATH?mode=ro', uri=True)
target_path = pathlib.Path('/data/coordination.final-cutover.db')
target_path.unlink(missing_ok=True)
target = sqlite3.connect(target_path)
with target:
    source.backup(target)
target.close()
source.close()
"
kubectl -n "$NS" cp "$SNAPSHOT_POD:/data/coordination.final-cutover.db" \
  ./coordination.final-cutover.db
kubectl -n "$NS" delete pod "$SNAPSHOT_POD" --wait=true

# 4d. Import the authoritative post-drain snapshot. The importer is
#     idempotent, so this updates anything that changed after step 3.
python3 scripts/migrate_tokens_ownership.py \
  --sqlite ./coordination.final-cutover.db \
  --postgres-url "$PG_URL" \
  --postgres-schema "$PG_SCHEMA"
```

- [ ] `coordination.final-cutover.db` is non-empty and its token count matches
      `${PG_SCHEMA}.engineer_tokens` after the final import.
- [ ] No coord API pod was running while the final snapshot/import occurred.

Now flip the backend and bring up the stateless multi-replica Deployment. Apply
the HA manifest set (image tag = PostgreSQL-enabled image; `COORD_DATABASE_URL` ->
Postgres Secret; `coord-data` PVC removed; `replicas: 3`;
`RollingUpdate(maxUnavailable:0, maxSurge:1)`; `PodDisruptionBudget
minAvailable:2`):

```bash
# The HA manifest set lives OUTSIDE ArgoCD's watched path on purpose.
# postgres.yaml deliberately contains no coord-pg Secret, so this apply can
# never clobber the real credentials created in step 0.
kubectl -n "$NS" apply -f deploy/k8s/ha-cutover/postgres.yaml
kubectl -n "$NS" apply -f deploy/k8s/ha-cutover/pdb.yaml
kubectl -n "$NS" apply -f deploy/k8s/ha-cutover/deployment.yaml
kubectl -n "$NS" rollout status deploy/coord --timeout=300s
```

Once the cutover is verified (step 5), land the HA manifests in
`deploy/k8s/prod/` via a normal PR and re-enable ArgoCD auto-sync, so git
returns to being the source of truth for the now-Postgres prod.

- [ ] coord scaled to 0 and reported 0 ready **before** the PG-backed pods
      started (this is the split-brain guard -- verify the ordering held).
- [ ] All 3 replicas reach `Ready`; readiness includes a DB-reachability check,
      so `Ready` means Postgres is connected.

---

## 5. Verify

```bash
# 5a. Each replica is healthy and pointed at Postgres.
kubectl -n "$NS" get pods -l app.kubernetes.io/name=coord -o wide
kubectl -n "$NS" logs -l app.kubernetes.io/name=coord --tail=50 | grep -iE 'postgres|schema|listen'

# 5b. End-to-end claim round-trip through the Service (use a real token).
#     Replace HOST and TOKEN above as appropriate for your ingress.
curl -fsS -H "Authorization: Bearer $TOKEN" "$HOST/health"
# Make a throwaway claim, list it, release it -- confirm a write path works.

# 5c. Confirm tokens authenticate (no lockout) and ownership rules loaded.
psql "$PG_URL" -c "SELECT engineer, count(*) FROM ${PG_SCHEMA}.engineer_tokens WHERE revoked_at IS NULL GROUP BY engineer;"
```

- [ ] A claim create/list/release cycle succeeds through the Service.
- [ ] At least one agent from each operator authenticates (token migration
      worked; no `COORD_REQUIRE_PER_ENGINEER_TOKEN` lockout).
- [ ] Agents re-announce their active claims within one TTL; `gpufarm status` /
      `list_claims` repopulates. Transient 503s during the flip are expected and
      covered by client retry.
- [ ] Background loops (cleanup, auto-demote, rename sweep, webhook delivery)
      run on exactly one replica (leader election / `FOR UPDATE SKIP LOCKED`),
      not three -- check logs for duplicate webhook deliveries (there must be
      none).

> **Deploying the leader-lease fix later:** the first deploy of a release
> that changes the leader-lease logic should be a scale-to-zero bounce
> (`kubectl -n "$NS" scale deploy/coord --replicas=0`, wait for 0 ready,
> then scale back to 3) rather than a RollingUpdate if background-loop
> continuity matters. A rolling deploy overlaps replicas running the old
> and the fixed lease logic: the loops can double-run or stall until the
> stale lease TTL expires. A bounce guarantees a single lease generation,
> at the cost of a brief 503 window that client retry already covers.

Cutover is complete once 5 is fully checked.

---

## 6. Rollback (kept available ~2 weeks)

Roll back if: tokens fail to authenticate at scale, claim writes error, the
concurrency guarantee is visibly violated (double-active overlapping claims),
or Postgres is unhealthy. **No mixed-backend writers in either direction** --
the rollback is the same hard-drain in reverse.

The retained SQLite PVC is the rollback source. The step-4 hard drain left its
live database at the exact durable state imported into PostgreSQL, and the
precondition above freezes token and ownership mutations while rollback remains
available. Do **not** attempt a partial token dump: rotations/revocations,
ownership, and token-chain references must move atomically.

If any token or ownership mutation occurred after cutover, stop here. This
runbook's rollback guarantee is no longer valid until a separately reviewed
PostgreSQL-to-SQLite reconciliation is performed; blindly reattaching the stale
PVC can lock out newer agents or resurrect revoked credentials.

Then drain PG writers and bring SQLite back:

```bash
# 6a. Disable ArgoCD auto-sync again (it may have been re-enabled after a
#     successful verification) so selfHeal cannot restart PostgreSQL writers.
kubectl -n argocd patch application coord-prod --type merge \
  -p '{"spec":{"syncPolicy":null}}'

# 6b. Stop the PG-backed replicas entirely.
kubectl -n "$NS" scale deploy/coord --replicas=0
kubectl -n "$NS" rollout status deploy/coord --timeout=120s
wait_for_no_coord_pods

# 6c. Re-apply the saved SQLite manifest against the unchanged retained PVC:
#     old image tag,
#     COORD_DATABASE_PATH=/data/coordination.db (no COORD_DATABASE_URL),
#     coord-data PVC re-attached, replicas: 1, strategy: Recreate.
kubectl -n "$NS" apply -f "$SQLITE_ROLLBACK_MANIFEST"
kubectl -n "$NS" rollout status deploy/coord --timeout=180s
```

- [ ] coord scaled to 0 PG replicas **before** the SQLite pod started.
- [ ] The durable-mutation freeze held; no token or ownership change occurred
      after the final SQLite snapshot.
- [ ] SQLite pod is `Ready` (single replica) and serving.
- [ ] Tokens authenticate; agents re-announce claims within a TTL.
- [ ] Record why the rollback was triggered for the follow-up.

Keep the Postgres `StatefulSet`/PVC and the SQLite PVC for ~2 weeks after a
successful cutover before reclaiming either, so a late-discovered problem can
still be rolled back.
