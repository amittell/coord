# coord HA re-architecture: Postgres + stateless multi-replica

Status: IMPLEMENTED, SHIPPED DORMANT, CUTOVER SHELVED (2026-07-07). The
backend merged in v0.44.0 (#54): ``coordination/pg_backend.py`` selected by a
``postgresql://`` ``COORD_DATABASE_URL``, validated by the ``test-postgres``
CI job, cutover manifests in ``deploy/k8s/ha-cutover/`` (deliberately outside
ArgoCD's watched path after the 2026-07-05 premature-activation incident),
live cutover operator-gated per ``docs/runbooks/coord-ha-cutover.md``. The
cutover itself is SHELVED: v0.45.0's SQLite write-scaling (activity-ping
coalescing + in-process writer queue) was chosen as the scale path after load
tests showed it eliminates dropped writes at 200-300-agent concurrency.
Revisit when ``sqlite_writer_wait_seconds_total`` climbs fast relative to
wall time or topology needs multi-replica.

Prior status: DRAFT v2 (2026-06-29) -- revised after review by Codex, the
RepoPrompt oracle, and a fresh code-review agent. See Section 14 for the
review outcome.

Verdict from review: the direction (Postgres + stateless replicas + per-repo
advisory lock + client retry) is correct, but there are correctness gaps that
**must be closed before implementation**. They are folded into the design below
and called out as [R#] (review finding) where they reshaped a decision.

## 1. Problem

coord runs as a single pod backed by single-writer SQLite on an RWO PVC
(`deploy/k8s/prod/deployment.yaml`: `replicas: 1`, `strategy: Recreate`,
`COORD_DATABASE_PATH=/data/coordination.db`). Target load is **two humans with
~20 agents each (~40 clients)**, with coord on the hot path of every edit.

1. **Availability (acute).** `replicas: 1` + `Recreate`: every deploy/restart
   is a total outage for all agents (coord releases are frequent). This is what
   hurts today.
2. **Write concurrency (latent).** SQLite serializes writers; fine at this
   rate, but a single global write lock is a ceiling.

You cannot add replicas on SQLite-on-RWO (RWO mounts one pod; SQLite is
single-writer). HA requires a concurrent-writer shared store: Postgres.

## 2. Goals / non-goals

Goals: zero-downtime deploys + survive one pod/node loss; **conflict detection
preserved exactly** under concurrent multi-replica writers; local single-user
path stays on SQLite, unchanged; isolated branch + staged, reversible cutover.

Non-goals (v1): Postgres HA itself (single PG instance; Patroni/CloudNativePG
later); sharding/read-replicas; any public API or claim-semantics change.

## 3. Current architecture (corrected by review [R1])

The doc's original premise -- "SQLite's single writer makes check-overlap-then-
insert atomic" -- is **wrong**, and this reshapes the whole design:

- Overlap check and insert are **separate connections with no shared
  transaction**. `create_claims` reads active claims at `service.py:913`
  (`list_active_claims_rows`, its own `aiosqlite.connect`), filters to the repo
  at `service.py:915`, computes overlap in Python -- which **shells out to
  `git ls-files`** (`engine.py:304` -> `subprocess.run` in a thread). The insert
  is a *different* connection at `service.py:1063` -> `db.py:855`
  (`insert_claims_batch`) under its own `BEGIN IMMEDIATE` that **just INSERTs --
  it never re-checks overlap** (`db.py:886-909`).
- So the read -> compute -> insert sequence is **not** serialized today; only
  the two INSERT statements are. The double-grant race is **already latently
  present in-process** (two `create_claims` coroutines can interleave at any
  `await` between 913 and 1077). It is simply never hit: single uvicorn worker,
  one event loop (`main.py:2108`, no `workers`), low write rate.
- Multi-replica does not *introduce* the race; it makes the existing one
  **acutely reachable** across event loops and connections.
- Connection-per-op (`aiosqlite.connect` ~14x, no pool); inline SQL; versioned
  migrations to `CURRENT_SCHEMA_VERSION = 18`; ~64+ SQLite dialect spots.
- Timestamps are stored as **ISO-8601 TEXT with `Z`** (`_utcnow`, `db.py:603`),
  compared via `datetime()`/`strftime('%s',...)`/`julianday` at ~30 sites.
- Persistent (NOT ephemeral) tables: `engineer_tokens` (`db.py:464`),
  `ownership_config` (`db.py:133`), `webhook_outbox` (`db.py:426`),
  `claim_queue` waiters (`db.py:368`), plus `conflict_log`/`request_events`
  history and open `requests`.

## 4. Target architecture

```
            ~40 agents (coord-mcp clients, with retry)
                      |  Cloudflare -> cloudflared
            Service coord (ClusterIP)
             /        |        \
        coord-0    coord-1    coord-2     Deployment: stateless, replicas=3,
                      |                   RollingUpdate(maxUnavailable=0,
              Postgres (StatefulSet,      maxSurge=1), PDB minAvailable=2
              1 primary, own RWO PVC)
```

- coord Deployment **stateless** (drop `coord-data` PVC); `replicas: 3`;
  `RollingUpdate(maxUnavailable:0, maxSurge:1)`; `PodDisruptionBudget
  minAvailable:2`; `readinessProbe` must also check DB reachability.
- **Postgres**: single in-cluster `StatefulSet`, own RWO PVC, one primary. This
  is **app-tier HA, not full-system HA** [R-oracle9] -- PG is still a single
  write point (acceptable v1; PG restarts are fast and client retry covers
  blips). Name it accurately; Patroni/CloudNativePG is the follow-up.
- Backend by `COORD_DATABASE_URL` (`sqlite://` default unchanged for
  local/tests; `postgresql://` for prod). `COORD_DATABASE_PATH` stays as a
  deprecated alias.

## 5. Correctness: the claim grant must become ONE locked transaction

This is the core of the work, and bigger than "translate dialect spots" [R1].

### 5.1 Two-phase claim path

Holding the per-repo lock across `git ls-files`/LSP (hundreds of ms) would
serialize every claim in a repo behind subprocess latency while pinning a pool
connection [R-agent1, R-agent7]. So split it:

- **Phase A (lock-free, outside any txn):** expand patterns / run `git ls-files`
  / LSP to materialize the candidate file+symbol set. No DB write, no lock.
- **Phase B (one connection, one transaction):**
  1. `SELECT pg_advisory_xact_lock(hashtextextended(coalesce(repo,''), <ns>))`
     -- transaction-scoped, per-repo, namespaced [R-oracle10, R-agent2].
  2. **Re-SELECT** active claims for the repo and **recompute the claims-table
     overlap** (the cheap, DB-only part -- NOT git/LSP) [R-agent1].
  3. Enforce the per-engineer quota **inside this locked path** (see 5.3).
  4. Insert the **fully-finalized** file + symbol claims atomically -- today
     base claims are inserted first (`db.py:855`) then `_finalise_v14_scope`
     opens a new connection to set `scope_type` + insert symbol rows
     (`service.py:1419/1487`); under PG a peer could observe the incomplete
     claim [R-codex]. Do it all in this one txn.
  5. Auto-resolution bookkeeping in the same txn.
  6. Commit (lock releases). Recompute against git only happened in Phase A;
     if the file set changed between A and B it is acceptable to be slightly
     conservative -- the DB-only re-check under lock is the correctness anchor.

READ COMMITTED + the advisory lock is then **sufficient**; SERIALIZABLE is not
needed (keep it behind a flag for testing) [R-oracle3, R-agent1].

### 5.2 The lock must cover EVERY active-claim mutation [R-oracle2, R-codex]

Not just new claims. The same per-repo locked transaction is required for:
- `create_claims` (incl. queue grants that re-enter it, `db.py:1916`),
- `respond_to_request` narrowed/coexist grants (`db.py:3339/3445/3555`), which
  validate in service code (`service.py:2289`) then insert -- they must
  re-check third-party active claims under the lock,
- renew/extend/heartbeat (renew-after-expiry must re-check overlap or fail),
- release / force-release, and expiry materialization.
Implementation: a single `with_repo_claim_txn(repo)` unit-of-work is the ONLY
way to mutate the active-claim set; a test fails if any path skips it.

### 5.3 Per-engineer quota caps break under multi-replica [R-agent4] (NEW)

`_quota_lock` (`service.py:376`) is an in-process `asyncio.Lock` guarding
`max_claims_per_engineer` (`service.py:1072`) and the queue cap
(`service.py:2613`). Its comment says one lock suffices *because the flock
instance lock guarantees a single process* -- false under `replicas:3`: each
replica admits up to the cap, so caps overshoot ~3x. The cap is **per-engineer,
global across repos**, so the per-repo advisory lock does NOT cover it. Re-home
it DB-side: a per-engineer advisory lock (`hashtextextended('eng:'||engineer)`)
around count+insert, or a DB constraint/trigger. Must be in the design.

### 5.4 NULL repo is a real bucket and a lock foot-gun [R-codex, R-agent2]

`repo` is nullable (`db.py:174`); NULL is its own conflict bucket
(`service.py:672/915`). `pg_advisory_xact_lock(NULL)` **takes no lock**, so
NULL-repo claims would run unserialized -- a silent double-grant exactly in the
legacy bucket. Always key on `coalesce(repo,'')`. (Confirmed: no cross-repo or
global claim type escapes repo scoping; `shared_file`/`module`/symbol/callsite/
rename claims all carry/inherit `repo` [R-codex, R-agent Open-Q1].)

### 5.5 Backstop index (corrected) [R-codex, R-agent2]

The v1 proposal of a unique index on active `(repo_id, file_path)` is wrong:
`claims` has `pattern`, not `file_path`; symbol paths live in `claim_symbols`;
and NULLs are not equal in a unique index. A broad exact-path unique index
would also reject legitimately-disjoint symbol claims on the same file. Drop it;
rely on the locked transaction for correctness. If a backstop is wanted, scope
it to exact-pattern active rows with `coalesce(repo,'')` and `released_at IS
NULL`, and treat it as defense-in-depth only, never the prefix/symbol guarantee.

## 6. Multi-replica background loops need a single leader [R-codex] (NEW)

Every replica would run cleanup, auto-demote, rename sweep, and webhook
delivery. Webhook delivery lists due rows (`db.py:1979`) then POSTs
(`service.py:2983`) with no row-claiming -> **3x duplicate webhook/PR-comment
delivery**, breaking at-least-once-once semantics. Fix: either a singleton
leader-election lease (one replica runs the loops) or claim work rows with
`SELECT ... FOR UPDATE SKIP LOCKED` + an `in_progress` state so exactly one
replica delivers each row. The outbox is durable state, so this also matters
for cutover (Section 8).

## 7. Code design (transaction-oriented, not per-method) [R-oracle6, R-codex]

A method-by-method `Store` with `_acquire()` is too weak: the lock+overlap+
insert must span one connection across what is today service-layer logic. So:

1. Define a `Store` interface, but **transaction-oriented**: a
   `with_repo_claim_txn(repo)` unit-of-work yielding a bound connection, plus
   coarse grant operations (`grant_claims_in_txn(...)`) that do re-check +
   insert + finalize atomically. Move the overlap/grant decision out of
   `service.py` into a backend-neutral helper so the same logic runs in one txn
   on either backend; remove SQLite leakage from service code first.
2. `SqliteStore`: today's code, lightly refactored to acquire its connection
   via the unit-of-work. Behaviour-identical; keeps local/test green.
3. `PostgresStore`: asyncpg + pool (min2/max10 per replica; 3x = ~30 vs PG
   `max_connections`~100, leaving headroom for the canary 4th deployment).
   Dialect translation -- enumerated, including the spots review caught:
   - `INSERT OR REPLACE/IGNORE` -> `ON CONFLICT DO UPDATE/NOTHING`;
     `AUTOINCREMENT` -> identity; `?` -> `$n`; `BEGIN IMMEDIATE` -> txn+advisory.
   - **Timestamps** [R-agent5]: TEXT-ISO `'…Z'` params must become
     `datetime`/`timestamptz` (asyncpg will not accept the ISO string the way
     SQLite does); `strftime('%s', enqueued_at)` age-boost math (`db.py:1681-
     1687`) -> `extract(epoch from …)`; governs TTL/idle expiry + queue
     age-boost. Test-matrix, not hand-waved.
   - `json_extract(detail,'$.holder_claim_id')` (`db.py:2849/2904`) ->
     `detail::jsonb ->> 'holder_claim_id'` (or store `detail` as `jsonb`);
     `GROUP_CONCAT(DISTINCT repo)` (`db.py:3043`) -> `string_agg(DISTINCT
     repo, ',')` [R-agent6].
   - Migration runner: `_split_sql_statements` uses `sqlite3.complete_statement`
     + `executescript` (`db.py:703`) -- the PG path needs its own splitter/runner.
   - `acquire_instance_lock` flock (`db.py:27`, `main.py:75`) is meaningless
     across pods; the PG path must bypass it (return the sentinel) and must NOT
     imply single-writer. `build_service` hardwires `Database(database_path)`
     (`service.py:3349`) -- branch on backend.
4. PG schema: a **consolidated v1** = SQLite schema post-migration-18 (claims
   are ephemeral; no historical replay), but with a `schema_version` row and a
   real PG migration runner for v2+ [R-oracle11].

Dependency: `asyncpg` (prod extra only; SQLite path keeps zero new deps).
asyncpg-vs-SQLAlchemy: keep asyncpg (small, correctness-sensitive, advisory-
lock/pool are PG-specific) -- *contingent on* full backend contract tests
[R-all].

## 8. Cutover (NOT a clean break) [R-oracle1, R-codex, R-agent3]

The original "empty PG, no migration, canary alongside, instant rollback" is
unsafe on two counts:

- **Split-brain:** any window where SQLite-backed and PG-backed pods both serve
  *writes* gives two independent truths -> mutual overlap. So the canary may do
  health/schema/read-only checks only; it must NOT serve production claim
  writes. Cutover is a **hard drain**: stop old writers, then start PG writers.
- **Durable state must migrate:** an empty PG drops `engineer_tokens` -- and
  prod runs `COORD_REQUIRE_PER_ENGINEER_TOKEN=true` (`deployment.yaml:52`), so
  that **locks out all ~40 agents** until re-provisioned. It also drops
  `ownership_config` (auto-promoted shared-file rules), undelivered
  `webhook_outbox`, and `claim_queue` waiters.

Cutover plan:
1. Ship the dual-backend image (SQLite default) via normal review/CI/release.
2. Stand up the PG StatefulSet (empty; coord creates v1 schema on first connect).
3. Export+import `engineer_tokens` + `ownership_config` (small, scriptable);
   drain `webhook_outbox` on the old instance first.
4. Hard cutover in a low-traffic window: stop the SQLite pod, flip
   `COORD_DATABASE_URL` -> postgres, scale to 3 (RollingUpdate). Active claims
   are dropped; agents re-announce within a TTL (idempotent retry, 10.1, makes
   this safe). Open `requests`/queue waiters/history are acknowledged-as-dropped
   (acceptable; document it).
5. Rollback (kept ~2 weeks): the SQLite manifest must also carry the migrated
   tokens, so a flip back does not lock anyone out. No mixed-backend writers
   in either direction.

## 9. Testing

- Run the **entire test suite against PG** in CI (a `postgres` service
  container) alongside SQLite -- the primary guard against dialect bugs.
- **Concurrency regression test** [R-all]: many real PG connections (not one
  event loop) claiming overlapping paths/symbols in one repo, incl. NULL-repo;
  assert exactly one winner and no double-active row. Note it would pass against
  today's SQLite only because of the single event loop -- it must run against PG
  to be meaningful.
- Per-engineer cap test under simulated 3 replicas (asserts no ~3x overshoot).
- Webhook single-delivery test under 3 replicas.
- Load smoke: 40 simulated agents through a rolling restart; p99 + zero errors.

## 10. Interim mitigation (ships first, independent)

### 10.1 Client-side retry + idempotency
Retry the coord MCP wrapper on `502/503/conn-refused/read-timeout` with capped
backoff+jitter so deploy windows are invisible now. **But mutating retries need
idempotency** [R-oracle7]: a client-generated request id `(repo, engineer,
claim_request_id)`; on retry the server returns the already-created claim
instead of double-creating or returning a misleading overlap. This is also what
makes the cutover "re-announce" safe.

## 11. Risks

- Claim-grant race under MVCC -> the Section 5 single-locked-transaction over
  ALL grant paths + the concurrency test. Highest priority.
- Quota overshoot [R-agent4] -> DB-side per-engineer enforcement (5.3).
- Webhook/loop duplication [R-codex] -> leader/SKIP LOCKED (Section 6).
- Cutover lockout/loss -> migrate tokens+ownership, drain outbox, no mixed
  writers (Section 8).
- Timestamp/dialect drift -> PG CI matrix (Sections 7, 9).
- PG SPOF (v1) -> accepted; client retry + fast restart; Patroni follow-up.
- Lock held over slow work -> keep git/LSP in Phase A only (5.1, R-agent7).

## 12. Sequencing

0. Client retry + idempotency (Section 10) -- ship immediately, independent.
1. Backend-neutral grant helper + `Store` unit-of-work + `SqliteStore`
   extraction (behaviour-identical; tests green) -- separate reviewable commit.
2. Re-home quota caps DB-side (5.3); single-leader background loops (Section 6)
   -- both needed regardless of backend, do early.
3. `PostgresStore` (asyncpg) + the two-phase locked claim path (5.1) over all
   grant paths (5.2) + NULL key (5.4) + full dialect translation + PG CI matrix
   + concurrency/quota/webhook tests.
4. PG StatefulSet + secret + NetworkPolicy + stateless coord Deployment
   (replicas=3, RollingUpdate, PDB) via ArgoCD.
5. Token/ownership migration + drain + hard cutover + ~2-week rollback.

## 13. Open questions (resolved by review)

- Q (rowid/last_insert_rowid): **EMPTY risk** [R-agent] -- no `rowid`/`lastrowid`
  reliance; ids are app `uuid4` (`service.py:1042`); `insert_claims_batch`
  returns its input ids. No `RETURNING` needed. Dropped.
- Q (repo as conflict domain): **confirmed correct**, subject to the NULL fix
  (5.4) and the single-transaction fix (5.1).
- Q (asyncpg vs SQLAlchemy): asyncpg, contingent on contract tests.
- Q (single PG vs Patroni v1): single PG for v1; Patroni follow-up.
- Q (clean-break vs migrate): **migrate** tokens+ownership; drain outbox; claims
  may be re-announced (Section 8).

## 14. Review outcome (Codex + RepoPrompt oracle + code-review agent)

All three independently judged the direction correct and the original draft
**not safe to implement as written**, converging on: (1) the claim grant is not
atomic even today and must become one locked transaction spanning the
service-level overlap+insert, not a per-method store call; (2) the advisory lock
must cover every grant path and handle NULL repo; (3) the cutover must migrate
durable state (tokens/ownership) and avoid mixed-backend writers. Unique adds:
Codex -- atomic symbol finalization, `respond_to_request` paths, background-loop
double-delivery, build_service/flock coupling. Code-review agent -- the latent
in-process race + git-subprocess-in-lock-path, the `_quota_lock` ~3x overshoot
(net-new correctness regression), the TEXT-timestamp depth, and emptying the
rowid risk. Oracle -- split-brain cutover framing, idempotent retries, lock-key
namespacing, no-slow-work-under-lock. This v2 folds all of it in. Implementation
is gated on Sections 5-8; ship Section 10 now.
