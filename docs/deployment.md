# Deployment

The coordination service ships as a single OCI container image. It is a small FastAPI process backed by SQLite, designed to be hosted on whatever container infrastructure you already run (Kubernetes, ECS, Nomad, Docker on a VM, Docker Compose, etc.). This document describes the container contract so you can wire it into your platform of choice.

## Container contract

| Concern | Value |
|---------|-------|
| Image entrypoint | `coord-api` (runs `uvicorn coordination.main:app`) |
| Listen port | `8080` (bound to `0.0.0.0` inside the container) |
| Persistent state | SQLite at `COORD_DATABASE_PATH` (default `/data/coordination.db`) |
| Volume to mount | `/data` (for the SQLite file and its WAL/SHM sidecars) |
| Liveness probe | `GET /health` returns `ok` (HTTP 200) with no auth |
| Readiness probe | `GET /readyz` returns JSON (HTTP 200) with no auth |
| Graceful shutdown | Responds to `SIGTERM` by letting uvicorn drain in-flight requests |
| Built-in healthcheck | `HEALTHCHECK` in the Dockerfile probes `/readyz` every 30s |
| Container user | Non-root `coord` (uid/gid 1000); `/data` is owned by that user |

The runtime image is a multi-stage build on `python:3.12-slim` with pinned production dependencies from `requirements.txt`. It runs as a non-root user (`coord`, uid 1000), so the mounted `/data` volume must be writable by uid 1000 (Kubernetes users can set `spec.securityContext.fsGroup: 1000`; Docker volume drivers and bind mounts may need adjustment).

## Minimum production stance

At minimum:

- Set `COORD_AUTH_TOKEN` to a strong random value (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
- Mount persistent storage at `/data`.
- Expose the HTTP API on a stable hostname, terminated by your own TLS-capable reverse proxy (nginx, Caddy, ALB, Cloudflare, etc.). The service itself speaks plain HTTP.
- Distribute the bearer token to engineers through your normal secret channel.
- Keep `coord-mcp` local to each engineer's machine as an editor/CLI command. Do not expose it over the network.

## Required environment variables

| Variable | Purpose |
|----------|---------|
| `COORD_AUTH_TOKEN` | Bearer token for the HTTP API. Unset means the service refuses to start unless `COORD_ALLOW_INSECURE_NO_AUTH=true`. |
| `COORD_DATABASE_PATH` | SQLite path. Default `/data/coordination.db` in the shipped image. |

All other `COORD_*` variables (scope limits, TTLs, cleanup cadence) are optional and documented in the top-level README.

## Build the image

```bash
docker build -t coordination .
```

If you publish to a registry, tag and push with whatever scheme your team uses. The included `.github/workflows/release.yml` publishes the image to `ghcr.io/<github-owner>/coord` on every `v*` git tag, tagging both `:latest` and `:<tag>` (e.g. `ghcr.io/your-org/coord:v0.1.0`). Update the referenced image path in any downstream manifests to match your fork.

## Kubernetes

The repository ships a generic reference under `deploy/k8s/` (Deployment, Service, PVC, and an example Secret manifest). It is a starting point, not a recommended distribution - pin the image tag, set your namespace, storage class, ingress, and TLS termination to match your cluster before applying. See `deploy/k8s/README.md` for details.

## Run with `docker run`

```bash
docker run \
  -e COORD_AUTH_TOKEN=replace-me \
  -p 8080:8080 \
  -v coordination-data:/data \
  coordination
```

## Run with Docker Compose

The shipped `compose.yaml` is a local-dev reference; it is not a production recommendation but shows the expected shape:

```bash
cp .env.example .env
# edit .env to set COORD_AUTH_TOKEN
docker compose up --build -d
curl http://127.0.0.1:8080/readyz
```

## Run with uvicorn directly (no container)

If you want to run the ASGI app on bare metal or inside a different process supervisor, the app object is `coordination.main:app`:

```bash
pip install multi-agent-coordination
export COORD_AUTH_TOKEN=replace-me
export COORD_DATABASE_PATH=/var/lib/coord/coordination.db
uvicorn coordination.main:app --host 0.0.0.0 --port 8080
```

## Sizing

This service is meant for a small to medium team coordinating one repo or a small set of repos. Expect:

- Dozens to low hundreds of active claims at any time.
- SQLite + WAL behaves well for one API process. Running multiple replicas against the same SQLite file is not supported.
- Memory footprint stays under 200 MB in normal use; CPU is negligible.

If you need to run multiple replicas for HA, the storage layer needs to be replaced first. The HTTP and MCP contracts are stable enough that this is a local-only change.

## `COORD_REPO_ROOT` guidance

`COORD_REPO_ROOT` lets the service expand globs using `git ls-files`, which dramatically improves overlap detection accuracy for wide patterns like `src/auth/**`.

Use it when the container can see a checkout of the application repo on disk. Common patterns:

- A sidecar container that runs `git fetch` periodically into a shared volume, with `COORD_REPO_ROOT` pointing at the mount.
- A shared host running coord plus a mirror checkout in the same filesystem.
- A CI job that rebuilds the checkout on each push and redeploys the coord pod.

If none of those fit your setup, leave `COORD_REPO_ROOT` unset. The service falls back to pathspec-only matching, which is less accurate but still usable for teams that write narrow claims.

## Backups

The SQLite file at `COORD_DATABASE_PATH` plus its `*-wal` and `*-shm` siblings together make up the live state. For a consistent snapshot:

- Point-in-time copy: run `sqlite3 /data/coordination.db ".backup /backup/coordination.db"` from a sidecar or cron.
- Volume snapshot: make sure you capture the `-wal` and `-shm` files at the same instant as the main DB.
- A plain `cp` of just `coordination.db` while the service is writing may produce an inconsistent file; prefer `.backup` or a filesystem-level snapshot.

Because claims are short-lived (TTL in hours), most teams do not need historical backups. The conflict log is the most lossy piece on restore.

## Token rotation

There is currently one shared bearer token. To rotate:

1. Generate a new token with a secure random generator.
2. Redeploy the service with the new `COORD_AUTH_TOKEN`.
3. Update every engineer's `COORD_AUTH_TOKEN` (and `.coordination/local.env` in each application repo). The editor MCP configs pick up the updated value on next launch.
4. Discard the old token.

There is no in-service "allow two tokens during rollover" feature yet. If you need zero-downtime rotation, front the service with a proxy that rewrites the `Authorization` header during the changeover window.

## Observability

- stdout/stderr: standard uvicorn logs at the level set by `COORD_LOG_LEVEL`.
- `/readyz`: includes version, auth mode, and database path for quick probing.
- `/meta`: name, version, auth mode, and whether `COORD_REPO_ROOT` is configured.
- `/metrics`: Prometheus-style text exposition (`text/plain; version=0.0.4`). Exposed unauthenticated by convention so standard Prometheus scrapers work without custom headers. If you need to restrict it, front the service with a reverse proxy that gates `/metrics` separately from the rest of the API.
- Per-request IDs: every response carries an `X-Request-ID` header. If the client sends one on the request the service echoes it; otherwise the service mints a 16-character hex id. Use it to correlate a client error with a specific server-side log line.
- Structured logs (opt-in): set `COORD_LOG_JSON=true` to switch the `coordination.*` loggers to one-line JSON output with `ts`, `level`, `logger`, `msg`, and `request_id` (when set). The default remains human-readable so local development is unaffected.

### Metrics surfaced

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `claims_created_total` | counter | `severity` | Claims successfully inserted. |
| `claims_conflicts_total` | counter | - | Create attempts rejected as conflicting with an existing active claim. |
| `claims_released_total` | counter | - | Claims released (one tick per released id). |
| `auth_failures_total` | counter | - | 401s raised by the bearer-token guard. |
| `http_requests_total` | counter | `method`, `path`, `status` | Every response the app emits. `path` is the matched route template so cardinality is bounded. |
| `build_info` | gauge | `version` | Constant 1.0 with the running service version in the label. |

## Multi-instance detection

The coordination service assumes single-writer semantics for its in-process caches and background cleanup loop. Running two live replicas against the same SQLite file is almost always a misconfiguration. To catch that at startup, the service takes an advisory `fcntl.flock(LOCK_EX | LOCK_NB)` on `<COORD_DATABASE_PATH>.lock` during lifespan initialisation. If the lock is already held, the process refuses to start with a message identifying the current holder's PID.

- The lock file lives next to the database file (same directory, `.lock` suffix). Your volume mount must allow creating it.
- The file descriptor is held for the process lifetime; `fcntl` auto-releases on fd close or process exit, so crashed instances do not leave a stale lease behind.
- `COORD_DISABLE_INSTANCE_LOCK=true` bypasses the check. Use it when running on an NFS-backed shared volume where advisory flock semantics are unreliable, or during debugging. Do not set it blindly in production: if two live instances actually share the DB you will see duplicated background cleanup work and confusing log output.
- Windows host runs (dev only) skip the check because `fcntl` is POSIX-only. The shipped container image is Linux, so production always enforces the lock.
- **Docker Desktop for Mac and Windows**: the virtualised bind-mount filesystem (gRPC-FUSE / virtiofs) does not propagate `fcntl.flock` across containers sharing the same host directory. The lock engages inside one container but a second container mounting the same directory will not observe it. Verified on macOS 26 + Docker Desktop 29. On native Linux hosts (where production deployments run) `flock` works correctly across containers. If you need a dev-time guard on Mac, run both services against named Docker volumes on separate hosts, or add a Kubernetes readinessGate / deployment-level `replicas: 1` enforcement ahead of the service.

## Security notes

- Do not run with `COORD_ALLOW_INSECURE_NO_AUTH=true` outside explicit local demos.
- Treat the bearer token like any other shared secret.
- The dashboard is protected by the same bearer token as the API; do not expose it to unauthenticated networks.
- Terminate TLS in front of the service; the app itself speaks plain HTTP.

## Supply chain

Every image published by `.github/workflows/release.yml` ships with four independent supply-chain signals attached:

- An SPDX SBOM attestation produced by BuildKit (`sbom: true`), attached to the image manifest as an OCI referrer. Pull it with `cosign download sbom <image>@<digest>` or inspect with `docker buildx imagetools inspect <image> --format '{{json .SBOM}}'`.
- A SLSA build provenance attestation produced by BuildKit with `provenance: mode=max`, covering the builder identity, source materials, and full invocation metadata.
- A second provenance attestation written by `actions/attest-build-provenance@v2` into the repo-level GitHub attestations store. Verify with `gh attestation verify <image>@<digest> --owner <github-owner>`.
- A keyless cosign signature produced via the workflow's OIDC identity and anchored in the public Rekor transparency log. Verify with:

```
cosign verify \
  --certificate-identity-regexp '^https://github.com/<owner>/<repo>/\.github/workflows/release\.yml@' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  ghcr.io/<owner>/coord@sha256:<digest>
```

Consumers who want a minimal gate should verify at least the cosign signature on deployment; the SBOM and provenance records are additionally useful for vulnerability triage and incident response. See `.github/workflows/README.md` for the full list of signals, the `SKIP_ATTESTATION` and `SKIP_SIGNING` escape hatches for environments without Sigstore or GHE attestations support, and how the cosign-installer action is pinned.

## GitHub Enterprise

The shipped release workflow (`.github/workflows/release.yml`) publishes to `ghcr.io` by default, but the registry host and repo path are overridable via repo or org Actions variables so you can publish to your own GHE container registry without forking the workflow:

- `vars.IMAGE_REGISTRY` - registry hostname, for example `containers.ghe.example.com`. Defaults to `ghcr.io`.
- `vars.IMAGE_REPO` - path under the registry, for example `platform/coord`. Defaults to `<repository_owner>/coord`.

Set these under Settings -> Secrets and variables -> Actions -> Variables. Login uses `${{ secrets.GITHUB_TOKEN }}` against whatever registry you configure. Some GHE setups require a Personal Access Token with `write:packages` to push images to the configured registry; if the default `GITHUB_TOKEN` is rejected by your registry, store a PAT as a repo secret and swap it into the `docker/login-action` step. See `.github/workflows/README.md` for details.

`actions/attest-build-provenance@v2` requires **GitHub Enterprise Server 3.10 or later** with the attestations feature enabled. On older GHE, or where the feature is disabled, comment out the "Attest build provenance" step or the release workflow will fail at that point. The image push itself is unaffected by that step being removed.

Networking: the service itself is a plain HTTP API. Corporate TLS termination, SSO in front of the dashboard, and egress policies to reach the container registry all apply at the standard enterprise-ingress layer - there is nothing coord-specific to configure beyond the registry overrides above.

Restrictions observed:

- `actions/attest-build-provenance` is the only step that is GHE-version gated; everything else (checkout, buildx, login, build-push, softprops release) is available across all supported GHE versions at the pinned action versions.
- Outbound network from runners must reach the configured registry host; for air-gapped enterprises, use a self-hosted runner inside the same network as the registry.

## Monorepos and virtual file systems (Scalar, Git VFS)

`COORD_REPO_ROOT` enables `git ls-files` for accurate overlap detection. In a large monorepo, enumerating every tracked file can be expensive if unscoped, so the service also reads `COORD_REPO_SCOPE` (a gitignore-style pathspec prefix such as `apps/web/`) to narrow the listing to a single subtree.

A 10-second in-process result cache sits in front of `git ls-files`, so a burst of conflict-check requests against the same scope pays the enumeration cost at most once every 10 seconds.

For virtual-file-system checkouts (Scalar or legacy GVFS), `git ls-files` reads the index without pulling blob contents, so even very large trees enumerate in bounded time. Still, prefer to set `COORD_REPO_SCOPE` to the subtree that matters for your service; enumerating the entire monorepo index on every cold cache miss is wasted work even when blobs are virtual.

Per-service wiring: when multiple services live in one monorepo, run `coord init --root <service-dir>` inside each service directory so the generated `.coordination/` lives next to the service rather than at the repo root. See `docs/usage-guide.md` for the CLI usage.

## macOS

Verified on macOS 26 (the current Sequoia/Tahoe era release). Two notes:

- `coord stop` resolves the service PID via `ps -p <pid> -o command=`, which is the macOS BSD `ps` variant. This works as expected and does not require any GNU-ps compatibility shim.
- The container image runs under Docker Desktop on both Apple Silicon and Intel Macs; the release workflow publishes a multi-arch manifest (`linux/amd64` + `linux/arm64`) so `docker pull` picks the right architecture automatically.
