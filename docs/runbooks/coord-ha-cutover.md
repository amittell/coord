# Runbook: coord SQLite -> Postgres HA hard-drain cutover

Status: operational runbook for the coord HA re-architecture
(`docs/designs/coord-ha-rearchitecture.md`, Section 8). Last reviewed
2026-07-08.

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

- [ ] The dual-backend coord image (selects backend via `COORD_DATABASE_URL`,
      `sqlite://` default unchanged) is built, reviewed, released, and the tag
      is known. The current prod image still runs SQLite.
- [ ] The cutover image actually bundles the Postgres driver. Images built
      before the Dockerfile installed `requirements-postgres.txt` (v0.45.0 and
      earlier) do NOT ship asyncpg and CrashLoop the moment
      `COORD_DATABASE_URL` is set. Verify, and repin the image line in
      `deploy/k8s/ha-cutover/deployment.yaml` to this verified release:

  ```sh
  docker run --rm ghcr.io/amittell/coord:<cutover-tag>@sha256:<digest> \
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
      both (a) read access to the live SQLite file and (b) network access to
      Postgres, with `psql` and the `sqlite3` CLI installed (the CLI is only
      needed locally; the coord image does not ship it).
- [ ] You know the target `COORD_DATABASE_URL` (the value in the PG Secret).
- [ ] A low-traffic maintenance window is agreed and the two human operators
      are aware their ~40 agents will briefly 503 and then re-announce.
- [ ] Rollback image tag (current SQLite image) and the SQLite PVC are
      recorded; the PVC is **retained**, not deleted, for at least ~2 weeks.

Set shared variables (adjust to your environment):

```sh
NS=coord                      # kubernetes namespace
# Pods are labeled app.kubernetes.io/name=coord (see the Deployment manifests);
# there is no bare `app=coord` label.
SQLITE_POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=coord -o jsonpath='{.items[0].metadata.name}')
test -n "$SQLITE_POD" || { echo 'no coord pod found'; exit 1; }
SQLITE_PATH=/data/coordination.db
PG_URL='postgresql://coord:PASSWORD@coord-postgres:5432/coord'   # from the PG Secret
```

---

## 1. Snapshot the source SQLite (safety net)

Take a consistent copy of the live database before touching anything. This is
both your migration source and a rollback artifact.

```sh
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

```sh
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

First, review the SQL the migration will apply (dry run -- changes nothing):

```sh
python3 scripts/migrate_tokens_ownership.py \
  --sqlite ./coordination.pre-cutover.db \
  --postgres-url "$PG_URL" \
  --dry-run --out ./coord-durable-migration.sql
```

- [ ] The emitted SQL contains an `INSERT INTO engineer_tokens ... ON CONFLICT
      (token_sha256) DO UPDATE` and an `INSERT INTO ownership_config ... ON
      CONFLICT (id) DO UPDATE`, wrapped in `BEGIN; ... COMMIT;`, and references
      **no** ephemeral tables.

Then apply it for real (idempotent -- safe to re-run):

```sh
python3 scripts/migrate_tokens_ownership.py \
  --sqlite ./coordination.pre-cutover.db \
  --postgres-url "$PG_URL"
```

Verify the import landed:

```sh
psql "$PG_URL" -c "SELECT count(*) AS tokens FROM engineer_tokens;"
psql "$PG_URL" -c "SELECT count(*) AS active_tokens FROM engineer_tokens WHERE revoked_at IS NULL;"
psql "$PG_URL" -c "SELECT length(yaml_text) AS ownership_bytes FROM ownership_config WHERE id = 1;"
```

- [ ] Token count matches the source
      (`sqlite3 ./coordination.pre-cutover.db 'SELECT count(*) FROM engineer_tokens;'`).
- [ ] Active (non-revoked) token count is sane -- this is what keeps the ~40
      agents authenticated under `COORD_REQUIRE_PER_ENGINEER_TOKEN=true`.
- [ ] `ownership_config` row is present if the source had one.

> The migration runs while the old instance is still live. That is fine:
> tokens and ownership rules change rarely, and the import is idempotent. If a
> token is minted in the gap between this step and step 4, re-run the migration
> just before step 4 to pick it up.

---

## 4. Hard cutover

No mixed-backend writers. Stop the old writer, then start the new ones.

```sh
# 4a. Stop the SQLite writer completely (scale to 0, do NOT rolling-update).
kubectl -n "$NS" scale deploy/coord --replicas=0
kubectl -n "$NS" rollout status deploy/coord --timeout=120s   # waits for 0 ready

# 4b. (Optional but recommended) re-run the durable migration to capture any
#     token/ownership change since step 3. Idempotent.
python3 scripts/migrate_tokens_ownership.py \
  --sqlite ./coordination.pre-cutover.db \
  --postgres-url "$PG_URL"
```

Now flip the backend and bring up the stateless multi-replica Deployment. Apply
the HA manifest set (image tag = dual-backend image; `COORD_DATABASE_URL` ->
Postgres Secret; `coord-data` PVC removed; `replicas: 3`;
`RollingUpdate(maxUnavailable:0, maxSurge:1)`; `PodDisruptionBudget
minAvailable:2`):

```sh
# Disable ArgoCD auto-sync first, or selfHeal reverts the manual applies
# back to the SQLite manifest still in deploy/k8s/prod/.
kubectl -n argocd patch application coord-prod --type merge \
  -p '{"spec":{"syncPolicy":null}}'

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

```sh
# 5a. Each replica is healthy and pointed at Postgres.
kubectl -n "$NS" get pods -l app.kubernetes.io/name=coord -o wide
kubectl -n "$NS" logs -l app.kubernetes.io/name=coord --tail=50 | grep -iE 'postgres|schema|listen'

# 5b. End-to-end claim round-trip through the Service (use a real token).
#     Replace HOST and TOKEN as appropriate for your ingress.
curl -fsS -H "Authorization: Bearer $TOKEN" https://<coord-host>/health
# Make a throwaway claim, list it, release it -- confirm a write path works.

# 5c. Confirm tokens authenticate (no lockout) and ownership rules loaded.
psql "$PG_URL" -c "SELECT engineer, count(*) FROM engineer_tokens WHERE revoked_at IS NULL GROUP BY engineer;"
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

The SQLite manifest must carry the **migrated** tokens so a flip back does not
lock anyone out. Because the migration is one-directional (SQLite -> PG), the
authoritative tokens after cutover live in Postgres. If tokens were created
*after* cutover (PG-only), back-port them to the retained SQLite file before
rolling back:

```sh
# Optional: dump PG-side tokens/ownership created since cutover back into the
# retained SQLite snapshot so a rollback keeps everyone authenticated.
psql "$PG_URL" -At -F '|' \
  -c "SELECT id, engineer, token_sha256, created_at FROM engineer_tokens WHERE revoked_at IS NULL;" \
  > ./post-cutover-tokens.txt
#   (apply these into ./coordination.pre-cutover.db with sqlite3 as needed;
#    inspect the file first -- only back-port tokens minted after the snapshot.)
```

Then drain PG writers and bring SQLite back:

```sh
# 6a. Stop the PG-backed replicas entirely.
kubectl -n "$NS" scale deploy/coord --replicas=0
kubectl -n "$NS" rollout status deploy/coord --timeout=120s

# 6b. Restore the (token-current) SQLite file onto the retained PVC if needed,
#     then re-apply the previous SQLite manifest: old image tag,
#     COORD_DATABASE_PATH=/data/coordination.db (no COORD_DATABASE_URL),
#     coord-data PVC re-attached, replicas: 1, strategy: Recreate.
kubectl -n "$NS" apply -f <previous-sqlite-deployment.yaml>
kubectl -n "$NS" rollout status deploy/coord --timeout=180s
```

- [ ] coord scaled to 0 PG replicas **before** the SQLite pod started.
- [ ] SQLite pod is `Ready` (single replica) and serving.
- [ ] Tokens authenticate; agents re-announce claims within a TTL.
- [ ] Record why the rollback was triggered for the follow-up.

Keep the Postgres `StatefulSet`/PVC and the SQLite PVC for ~2 weeks after a
successful cutover before reclaiming either, so a late-discovered problem can
still be rolled back.
