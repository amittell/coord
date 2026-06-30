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
| `COORD_AUTH_TOKEN` | Shared bearer token for the HTTP API. May be omitted when `COORD_REQUIRE_PER_ENGINEER_TOKEN=true` (per-engineer-only mode, v0.29.4+) or when `COORD_ALLOW_INSECURE_NO_AUTH=true`; otherwise requests fail with a misconfiguration error. |
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
pip install 'coord-mcp-server[symbols]'
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

## LSP integration (v0.31+)

With `COORD_REPO_ROOT` set, symbol claims can additionally be resolved through real language servers instead of tree-sitter approximation. Off by default:

```bash
COORD_LSP_ENABLED=true
# Server commands (defaults shown); the binaries must be on PATH in the container:
COORD_LSP_COMMAND_PYTHON=pylsp
COORD_LSP_COMMAND_TYPESCRIPT="typescript-language-server --stdio"
COORD_LSP_COMMAND_GO=gopls
```

What it buys: claim-time definition spans come from the language server (`resolved_by: lsp` in the dashboard), symbol validation accepts constructs tree-sitter misses, claimed symbols record their callsites so overlapping work on callers surfaces as an advisory, renamed symbols auto-follow when the rename is unambiguous, and `POST /claims/refactor` (MCP tool `claim_refactor`) reserves a symbol plus every callsite in one shot.

Operational shape: language servers run as child processes of coord (one per language), lazily spawned, reaped after `COORD_LSP_IDLE_SHUTDOWN_SEC` (default 300) idle, requests bounded by `COORD_LSP_REQUEST_TIMEOUT_SEC` (default 5). A misbehaving or missing server trips a circuit breaker (`COORD_LSP_CIRCUIT_FAILURE_THRESHOLD` / `COORD_LSP_CIRCUIT_COOLDOWN_SEC`) and coord falls back to tree-sitter silently -- LSP can never make claim creation fail. The stock container image does not bundle language servers; either extend the image or run coord where the binaries exist.

## Backups

The SQLite file at `COORD_DATABASE_PATH` plus its `*-wal` and `*-shm` siblings together make up the live state. For a consistent snapshot:

- Point-in-time copy: run `sqlite3 /data/coordination.db ".backup /backup/coordination.db"` from a sidecar or cron.
- Volume snapshot: make sure you capture the `-wal` and `-shm` files at the same instant as the main DB.
- A plain `cp` of just `coordination.db` while the service is writing may produce an inconsistent file; prefer `.backup` or a filesystem-level snapshot.

Because claims are short-lived (TTL in hours), most teams do not need historical backups. The conflict log is the most lossy piece on restore.

## Token lifecycle

### Per-engineer tokens (v0.29+)

Day-to-day agent traffic should run on per-engineer bearer tokens rather than the shared `COORD_AUTH_TOKEN`. Mint and manage them with the `coord tokens` CLI on the server (or inside the pod):

```bash
# Mint, optionally with an expiry (v0.29.4+)
coord tokens create alex/claude/myrepo --description "laptop" --expires-in 90d

# Inspect: status, expiry, request count, last source IP
coord tokens list

# Kill switch: token stops authenticating immediately
coord tokens revoke <token-id>
```

The raw token is printed exactly once at creation; only its sha256 lands in the database. Tokens created without `--expires-in` never expire (matching pre-v0.29.4 behavior).

From v0.29.5 the same lifecycle is available in the dashboard: engineers logged in with a per-engineer token manage their own tokens (list, revoke, create with a capped expiry), and a shared-token session gets the operator view over all tokens.

### Zero-downtime rotation (v0.29.4+)

`coord tokens rotate <token-id> --grace 24h` mints a successor token for the same engineer and keeps the old token valid for the grace window, so every cached copy of the old token keeps working while you roll the new value out to MCP configs and worktrees. After the window closes the old token gets a specific 401 telling the caller it was rotated. Use `--grace 0h` for an immediate cutover, and `--expires-in` to give the successor an expiry.

Rotation refuses revoked, expired, and already-rotated tokens; a rotation can never revive a dead credential. For a lost or leaked token, use `coord tokens revoke` followed by `coord tokens create`.

### SSO via OIDC (v0.29.6+)

Dashboard logins can go through any OIDC identity provider instead of pasted tokens:

```bash
COORD_OIDC_ISSUER=https://your-idp.example.com
COORD_OIDC_CLIENT_ID=coord-dashboard
COORD_OIDC_CLIENT_SECRET=...
# Must exactly match the redirect URI registered at the IdP:
COORD_OIDC_REDIRECT_URI=https://coord.example.com/auth/oidc/callback
# Who may log in (identity claim values, default claim: email):
COORD_OIDC_ALLOWED_PRINCIPALS=alice@example.com,bob@example.com
# Optional namespace for SSO-mapped engineer names:
COORD_OIDC_ENGINEER_PREFIX=sso/
```

A successful SSO login mints a per-engineer token that expires with the dashboard session lifetime, so SSO sessions show up in `coord tokens list` and the dashboard token panel like any other token. Public issuers (accounts.google.com) require either an allowlist or an explicit `COORD_OIDC_ALLOW_ANY_PRINCIPAL=true` -- without one of those the SSO login refuses, because "any Google account" is never a sane default for an operator surface.

### Retiring the shared token

Once every caller is on per-engineer tokens, set `COORD_REQUIRE_PER_ENGINEER_TOKEN=true` to reject the shared token cluster-wide. From v0.29.4 a deployment in this mode may omit `COORD_AUTH_TOKEN` entirely (per-engineer-only mode); `/readyz` reports `auth_mode: per_engineer`.

To rotate the shared token itself (legacy deployments):

1. Generate a new token with a secure random generator.
2. Redeploy the service with the new `COORD_AUTH_TOKEN`.
3. Update every engineer's `COORD_AUTH_TOKEN` (and `.coordination/local.env` in each application repo). The editor MCP configs pick up the updated value on next launch.
4. Discard the old token.

The shared token has no grace-window mechanism; if you need zero-downtime rotation, migrate to per-engineer tokens and use `coord tokens rotate`.

## Observability

- stdout/stderr: standard uvicorn logs at the level set by `COORD_LOG_LEVEL`.
- `/readyz`: includes version, auth mode, and database path for quick probing.
- `/meta`: name, version, auth mode, and whether `COORD_REPO_ROOT` is configured.
- `/metrics`: Prometheus-style text exposition (`text/plain; version=0.0.4`). Exposed unauthenticated by convention so standard Prometheus scrapers work without custom headers. If you need to restrict it, front the service with a reverse proxy that gates `/metrics` separately from the rest of the API.
- Per-request IDs: every response carries an `X-Request-ID` header. If the client sends one on the request the service echoes it; otherwise the service mints a 16-character hex id. Use it to correlate a client error with a specific server-side log line.
- Structured logs (default): the `coordination.*` loggers emit one-line JSON by default (`ts`, `level`, `logger`, `msg`, and `request_id` when set), which a log aggregator such as Loki ingests directly from the container stream. Set `COORD_LOG_JSON=false` to opt back out to a plain human-readable formatter for local development.

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
- Terminate TLS in front of the service; the app itself speaks plain HTTP. See [Transport security (TLS)](#transport-security-tls) below for the four supported patterns.

## Transport security (TLS)

The `coord-api` process speaks plain HTTP on port 8080 by design. TLS termination is the responsibility of whatever sits in front of it. Four supported patterns are documented below in roughly increasing setup effort. Pick by threat model, not by aesthetics.

### Threat model summary

| Threat | Plaintext | Self-signed CA | Cloudflare Tunnel | Let's Encrypt + cert-manager |
|--------|-----------|----------------|-------------------|------------------------------|
| Passive observer on the same LAN (e.g. shared office WiFi) | Token visible | Encrypted | Encrypted | Encrypted |
| Active MITM on the same LAN | Token visible | Encrypted (clients trust your CA) | Encrypted | Encrypted |
| Public internet exposure | N/A (LAN-only) | N/A (clients need CA distribution) | Encrypted + Cloudflare WAF | Encrypted |
| Token replay if leaked (regardless of transport) | Possible | Possible | Possible | Possible |
| Token leak via accidental log capture / browser cache | Possible | Possible | Possible | Possible |

TLS protects the token in transit; it does not address token leak through other channels (browser dev tools, shell history, application logs, CI artifact uploads, etc.). The placeholder-template pattern (see `tests/test_deploy_overlay.py`) is the orthogonal layer that prevents accidental commit-time leaks.

### Option 1: Plaintext HTTP (default)

`coord-api` accepts HTTP connections on port 8080 and reads the bearer token from the `Authorization` header. With no TLS layer, the token is sent in cleartext over whatever network sits between the client and the service.

When this is acceptable:

- Home lab or test environment on a LAN you fully control, where you trust every device on the network not to passively sniff or actively MITM.
- Loopback-only deployments (`127.0.0.1` or a UNIX socket) where the traffic never leaves the host.
- Throwaway test deployments behind a feature flag, where the token is rotated immediately after the test.

When it is not acceptable:

- Shared office, conference, or coffee-shop WiFi.
- Any network where you do not control every endpoint, including IoT devices or guest networks.
- Any deployment reachable from the public internet (in which case the service must not be exposed at all, regardless of TLS).

Setup: none. The container ships with HTTP on port 8080.

Client config: `COORD_API_URL=http://your-host:8080` (or `http://your-host` if you put a TLS-terminating proxy on 80, which would be plaintext for a different reason).

### Option 2: Cloudflare Tunnel + Universal SSL

Cloudflare Tunnel (`cloudflared`) opens an outbound long-poll connection from your origin to Cloudflare's edge, eliminating the need to expose an inbound port at all. Cloudflare presents its Universal SSL certificate to clients on the public hostname; traffic between Cloudflare's edge and your origin runs inside the tunnel's own encrypted channel.

When to use it:

- You already use Cloudflare for DNS or other services and have an account.
- You want clients (including off-LAN clients) to hit `https://coord.yourdomain.com` without managing certificates yourself.
- You want Cloudflare's WAF, rate limiting, and access policies in front of coord at no extra cost.
- Your origin runs in a homelab or behind NAT with no public IP.

Pros:

- Zero certificate management. Universal SSL auto-renews.
- No inbound firewall changes; the tunnel is outbound-only.
- Adds Cloudflare WAF / Access / Argo without extra wiring.
- Free for personal and small-business use.

Cons:

- Adds Cloudflare as a hard dependency. If Cloudflare's edge or your tunnel goes down, clients cannot reach coord. (LAN clients hitting the origin directly bypass this.)
- Cloudflare can see every coord HTTP request in cleartext at the edge. Acceptable for most coord workloads; not acceptable for high-secrecy environments where Cloudflare is in the threat model.
- The free Universal SSL cert is shared SAN; some clients with strict cert pinning may reject it. Most do not.
- Inbound traffic must arrive via Cloudflare; clients pointed at the origin IP bypass the encryption.

Setup (one-time, Kubernetes):

```bash
# 1. Install cloudflared as a Deployment in the cluster (commonly via the Helm chart or a plain manifest from cloudflare/cloudflare-helm-charts).
# 2. Create a tunnel in the Cloudflare dashboard or via cloudflared:
cloudflared tunnel create coord
# 3. Map your hostname to the tunnel in the dashboard's Public Hostname section:
#      Subdomain: coord
#      Domain: yourdomain.com
#      Type: HTTP
#      URL: http://coord.coord.svc.cluster.local (or whatever your in-cluster service DNS is)
# 4. Cloudflare creates a CNAME record on coord.yourdomain.com pointing at the tunnel.
# 5. Cloudflare provisions Universal SSL automatically (~30 seconds).
```

Client config: `COORD_API_URL=https://coord.yourdomain.com`.

Verify:

```bash
curl -fsS https://coord.yourdomain.com/readyz
# {"status":"ready","version":"0.28.x",...}
```

The origin port 8080 can stay closed at the host firewall; nothing inbound needs to reach it.

### Option 3: Let's Encrypt + cert-manager (DNS-01 challenge)

`cert-manager` is the standard Kubernetes-native cert-issuer. The DNS-01 challenge proves you control a domain by writing a TXT record, which works for both internal hostnames (e.g. `coord.internal.example.com`) and hostnames that are not publicly routable, as long as the public DNS zone is editable via API.

When to use it:

- You own a real public domain and have API access to its DNS provider (Cloudflare, Route53, Google Cloud DNS, DigitalOcean, etc.).
- You want a real TLS certificate, trusted by every client out of the box, on an internal hostname.
- Your origin is a Kubernetes ingress (Traefik, nginx-ingress, etc.) that integrates with cert-manager.
- You do not want Cloudflare (or anyone else) in the cleartext path.

Pros:

- Real, publicly trusted certificate. No CA distribution to clients.
- Works on internal hostnames as long as you control the public DNS zone.
- Auto-renewal handled by cert-manager.
- No third party can read coord traffic; TLS terminates at your ingress.
- Survives Cloudflare or any specific provider outage as long as Let's Encrypt and your DNS provider are up.

Cons:

- Requires you to own a public domain.
- Requires API credentials for your DNS provider (stored as a Kubernetes secret).
- More moving parts: cert-manager controller, ClusterIssuer or Issuer resource, Certificate resource, Ingress with TLS section.
- Let's Encrypt rate-limits cert issuance (50/week per registered domain, 5 duplicate-cert per week). Easy to hit during cert-manager debugging; use the staging issuer first.

Setup (Kubernetes, abbreviated):

```bash
# 1. Install cert-manager (one-time per cluster):
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

# 2. Store DNS provider credentials as a Secret (Cloudflare example):
kubectl create secret generic cloudflare-api-token \
  --from-literal=api-token=YOUR_CF_TOKEN \
  -n cert-manager

# 3. Create a ClusterIssuer:
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-account-key
    solvers:
      - dns01:
          cloudflare:
            apiTokenSecretRef:
              name: cloudflare-api-token
              key: api-token
EOF

# 4. Annotate your Ingress to request a cert, and add a `tls:` section:
#    metadata.annotations: cert-manager.io/cluster-issuer: letsencrypt-prod
#    spec.tls:
#      - hosts: [coord.yourdomain.com]
#        secretName: coord-tls
#    spec.rules.host: coord.yourdomain.com
# 5. Apply the Ingress; cert-manager solves the DNS-01 challenge and issues the cert within a couple of minutes.
```

Client config: `COORD_API_URL=https://coord.yourdomain.com`.

Verify:

```bash
curl -fsS https://coord.yourdomain.com/readyz
# {"status":"ready","version":"0.28.x",...}

# Confirm cert is from Let's Encrypt (not a self-signed CA):
echo | openssl s_client -connect coord.yourdomain.com:443 -servername coord.yourdomain.com 2>/dev/null | openssl x509 -noout -issuer
# issuer=C = US, O = Let's Encrypt, CN = R10
```

Tip: bootstrap against the Let's Encrypt staging issuer (`https://acme-staging-v02.api.letsencrypt.org/directory`) until the Certificate resource reports `Ready: True`. The staging cert is not browser-trusted, but it lets you debug DNS-01 wiring without burning prod rate limits.

### Option 4: Self-signed CA + cert distribution

You generate a CA, issue a cert for your internal hostname, and distribute the CA's public cert to every client machine that needs to connect. No external dependency, but every client must trust your CA before its connection succeeds.

When to use it:

- Your hostname is on a private DNS zone (e.g. `coord.kebabrack.lan`) and you cannot or do not want to use a public domain.
- You do not want any external CA (Cloudflare, Let's Encrypt) involved.
- The number of client machines is small enough that distributing the CA cert is tractable.

Pros:

- Zero external dependency. Works fully offline.
- Strong: the CA cert lives in trust stores you control, and only your CA can issue certs for your hostname.
- No rate limits, no renewal billing, no DNS provider account needed.

Cons:

- Every client machine must add the CA cert to its trust store. Operating-system-specific commands. Easy to skip on a new client and end up with `certificate verify failed` instead of a clean failure mode.
- The CA private key is now a credential you have to protect; if it leaks, anyone can issue certs for your internal hostnames.
- Cert renewal is on you (or your tooling like `step-ca` / `smallstep`).
- Browsers, curl, Python `requests`, Go `crypto/tls`, etc. all use independent trust stores. You may need to set per-language environment variables (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, etc.) on top of the OS trust store.

Setup (abbreviated, using `step-ca` for a friendlier UX than raw `openssl`):

```bash
# 1. Install step-ca and the step CLI (Homebrew example):
brew install step step-ca

# 2. Bootstrap a local CA:
step ca init --name="HomelabCA" --dns="ca.kebabrack.lan" --address=":9000" --provisioner=admin

# 3. Issue a cert for coord:
step certificate create coord.kebabrack.lan coord.crt coord.key \
  --profile leaf --ca ~/.step/certs/intermediate_ca.crt --ca-key ~/.step/secrets/intermediate_ca_key \
  --not-after 8760h --san coord.kebabrack.lan

# 4. Mount coord.crt and coord.key into your reverse proxy (Traefik, nginx, Caddy) and configure TLS.
# 5. Distribute ~/.step/certs/root_ca.crt to every client machine:
#    macOS: sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root_ca.crt
#    Linux: sudo cp root_ca.crt /usr/local/share/ca-certificates/homelab-ca.crt && sudo update-ca-certificates
#    Windows: certutil -addstore -f "ROOT" root_ca.crt
```

Client config: `COORD_API_URL=https://coord.kebabrack.lan`.

Verify:

```bash
curl -fsS https://coord.kebabrack.lan/readyz
# If you see `SSL certificate problem: unable to get local issuer certificate`,
# the CA cert has not been added to this client's trust store yet.
```

Tip: prefer Option 3 (Let's Encrypt + cert-manager) for any hostname where you can own a public domain. It removes the per-client trust-store work entirely. Reserve Option 4 for genuinely air-gapped or `.lan`-only deployments.

### Option 5: Dual-access (Cloudflare Tunnel + LAN-direct fallback)

This is a hybrid of Option 1 and Option 2 for operators who want coord reachable from outside their LAN (e.g. while travelling) without giving up direct LAN access when at home or in the office. Two parallel ingress paths share one backend; the client picks which one to use based on where it is.

When to use it:

- You self-host coord on a LAN you control AND you want it reachable when you're off-LAN (travel, working from a different site).
- You do not want Cloudflare in the path when you are on the LAN (latency, availability dependency).
- You are happy switching `COORD_API_URL` per location, OR you are willing to set up split-horizon DNS to do the switch automatically.

What it looks like in practice:

- Backend: one `coord-api` service in your cluster, listening on port 8080 (same as today).
- LAN path: an internal DNS record (e.g. `coord.kebabrack.lan`) and your internal ingress route the LAN client straight to the backend over HTTP. Plaintext, low latency.
- Off-LAN path: a Cloudflare Tunnel maps a public hostname (e.g. `coord.amittell.io`) to the same backend service. Cloudflare terminates Universal SSL at the edge; tunnel-encrypted between edge and origin.
- Client config: each engineer's `.coordination/local.env` carries either both URLs (with a switcher script) or the Cloudflare URL by default with a LAN override.

Setup (on top of an existing LAN-only deployment, abbreviated):

```bash
# 1. Install cloudflared in the cluster (Helm or plain Deployment manifest) and
#    create a tunnel:
cloudflared tunnel create coord
#    Or do it from the Cloudflare Zero Trust dashboard: Networks -> Tunnels -> Create a tunnel.

# 2. In the Cloudflare dashboard, under the tunnel's Public Hostnames:
#      Subdomain: coord
#      Domain: amittell.io
#      Type: HTTP
#      URL: http://coord.coord.svc.cluster.local:8080  (your in-cluster Service DNS)
#    Cloudflare adds the CNAME and provisions Universal SSL automatically.

# 3. Verify both paths resolve:
curl -fsS http://coord.kebabrack.lan/readyz      # LAN-direct
curl -fsS https://coord.amittell.io/readyz       # Cloudflare-fronted
```

Client side: two patterns. Pick one.

Pattern A: manual switch via a helper script. Each engineer has both URLs in `.coordination/local.env` (commented) and toggles when they leave or return:

```bash
# .coordination/local.env (gitignored), with manual switch convention:
COORD_API_URL=http://coord.kebabrack.lan
# COORD_API_URL=https://coord.amittell.io   # uncomment when traveling
COORD_SERVICE_URL=${COORD_API_URL}
COORD_AUTH_TOKEN=...
COORD_REPO_ID=amittell/yourrepo
```

A one-liner to flip:

```bash
sed -i.bak 's|^COORD_API_URL=.*|COORD_API_URL=https://coord.amittell.io|; s|^COORD_SERVICE_URL=.*|COORD_SERVICE_URL=https://coord.amittell.io|' .coordination/local.env
```

Pattern B: split-horizon DNS. Configure your home DNS (Firewalla, Pi-hole, Mikrotik, etc.) so that `coord.amittell.io` resolves to your LAN IP when queried from inside the LAN, and to Cloudflare's anycast IP when queried from outside. Client URL becomes `https://coord.amittell.io` always; the network does the switch invisibly.

The split-horizon catch: the LAN endpoint must also present a valid cert for `coord.amittell.io` or the client will get a TLS handshake error. Two ways to satisfy that:

1. Issue an internal cert for `coord.amittell.io` via Option 3 (cert-manager + DNS-01) at the LAN ingress. Let's Encrypt does not require the server to be publicly reachable for DNS-01.
2. Use Cloudflare Origin Certificates: download a long-lived cert from the Cloudflare dashboard and install it at the LAN ingress. Only clients that trust Cloudflare's Origin CA see it as valid, which is fine if every coord client is one you control.

Pattern A is what most solo operators land on (one config line to swap, no DNS engineering). Pattern B is cleaner if you have a managed DNS resolver on the LAN already and want a single client URL forever.

Tip: for travel, also confirm your Cloudflare tunnel is reachable from the kinds of networks you actually use (hotel WiFi, conference networks, cellular hotspots). Cloudflare's anycast usually works everywhere, but captive portals occasionally interfere with QUIC; falling back to TCP mode in cloudflared (`--protocol=http2`) often resolves it.

### Picking an option

Quick decision tree:

1. Will any client reach coord from off-LAN, including while travelling? -> Option 5 (dual-access) if you also want low-latency LAN access, otherwise Option 2 (Cloudflare-only) if you are happy routing LAN clients through the edge too.
2. All clients always on-LAN, no off-LAN access ever? -> Option 1 (plaintext) if the LAN is fully trusted, Option 4 (self-signed CA) if you want LAN TLS, Option 3 (Let's Encrypt) if you also own a public domain.
3. Do you own a public domain you can manage DNS for? -> Required for Options 2, 3, and 5. Not required for Option 1 or 4.
4. Loopback only, single trusted host, throwaway? -> Option 1 (plaintext) is fine.

Whichever you pick, the client URL ends up in:

- The repo's `.coordination/local.env` (`COORD_API_URL=...`) on each engineer's machine. `coord upgrade` rewrites this field from `.coordination/config.toml`, so update `config.toml.service_url` once per repo and re-run upgrade rather than editing every `local.env` by hand.
- The dashboard URL operators visit in a browser.

The `.mcp.json` / `.codex/config.toml` tracked templates keep their placeholder URL (`http://127.0.0.1:8080`); the MCP wrapper resolves the real URL from `.coordination/local.env` at startup. This means switching TLS strategies later does not require a tracked-template change.

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
