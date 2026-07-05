from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COORD_", extra="ignore")

    database_path: Path = Path("data/coordination.db")
    # HA re-architecture (design Sections 4-7). When set to a
    # ``postgresql://`` (or ``postgres://``) DSN the service runs on the
    # PostgresStore backend; the default (None) keeps the unchanged
    # single-writer SQLite path at ``database_path``. ``COORD_DATABASE_PATH``
    # stays the deprecated alias for the SQLite file location.
    database_url: str | None = None
    auth_token: str | None = None
    allow_insecure_no_auth: bool = False
    repo_root: Path | None = None
    repo_scope: str | None = None
    api_url: str = "http://127.0.0.1:8080"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    max_claim_files: int = 100
    max_claim_ratio: float = 0.2
    cleanup_interval_sec: int = 900
    default_ttl_hours: int = 4
    shared_ttl_hours: int = 2
    # Session-tagged claims auto-release when their holder has been
    # silent for this many seconds. Catches agents that walked away
    # without releasing. Legacy NULL-session claims are unaffected;
    # they continue to use TTL only. Set to 0 to disable idle expiry
    # cluster-wide.
    idle_timeout_sec: int = 1800
    # Filing a release request shortens the holder's claim TTL to
    # min(remaining, this value). Forces a near-term decision: the
    # holder either responds (approve/deny) or the shortened TTL
    # fires and the claim auto-releases, freeing the requester.
    request_ttl_short_sec: int = 300
    # v0.22 hard auto-promote: when a file's blocked-claim attempts
    # cross ``auto_promote_threshold`` within the rolling
    # ``auto_promote_window_days`` window, the conflict pipeline
    # writes a ``shared_files`` entry for it into the active
    # ownership YAML and records an ``auto-promote`` request_event.
    # Set the threshold to 0 (default) to disable the feature; the
    # v0.21 soft-promote endpoint stays available either way.
    auto_promote_threshold: int = 0
    auto_promote_window_days: int = 7
    # v0.23 auto-demote: closes the v0.22 one-way ratchet. A
    # coord-managed shared_files entry (marked with the
    # ``# auto-promoted=YYYY-MM-DD`` comment suffix in owners.yaml) is
    # removed when its underlying hotspot count stays below
    # ``auto_promote_threshold`` for ``auto_demote_window_days`` days.
    # ``auto_demote_interval_sec`` controls the background sweep
    # cadence; set to 0 to disable the sweep entirely (the soft-promote
    # endpoint and v0.22 hard auto-promote remain available either way).
    auto_demote_interval_sec: int = 3600
    auto_demote_window_days: int = 14
    # v0.26 pattern-class granularity: when this many auto-promoted
    # files share a common directory ancestor, the conflict pipeline
    # promotes the subtree glob (``src/auth/**``) once instead of
    # writing each leaf as its own shared_files entry. Set to 0 to
    # disable subtree-level promotion (the v0.22 per-file behaviour
    # is then preserved exactly).
    auto_promote_subtree_min_files: int = 3
    # v0.26 queue age boost: a waiting queue entry whose age (now -
    # enqueued_at) exceeds this many seconds is treated as one
    # priority level higher than its declared priority for the
    # purposes of pop ordering. Prevents low/normal-priority
    # waiters from starving under a steady stream of high/blocking
    # entries. Set to 0 to disable the boost (strict declared
    # priority ordering, the v0.25 behaviour).
    queue_age_boost_seconds: int = 60
    # v0.27 webhook delivery. When ``webhook_url`` is set, the
    # conflict pipeline writes every emitted event (auto-coexist,
    # auto-narrow, auto-promote, auto-demote, claim_granted,
    # queue_grant, request_release, queue_cancel) to the
    # webhook_outbox table; a background delivery loop POSTs each
    # row with an ``X-Coord-Signature`` HMAC header. Empty URL
    # disables the feature. ``webhook_events`` is a comma-separated
    # filter (empty = all event types). ``webhook_secret`` is the
    # HMAC key. ``webhook_max_retries`` caps the exponential-backoff
    # retry chain before a row is marked exhausted.
    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_events: str = ""
    webhook_max_retries: int = 5
    webhook_retry_backoff_sec: int = 60
    webhook_delivery_interval_sec: int = 5
    # v0.34 GitHub PR-comment integration. When ``github_token`` is
    # set, ``push_bounced`` events are routed through the webhook
    # outbox (kind='github') and the delivery loop posts (or updates)
    # a de-duplicated comment on the open PR for the pushing branch
    # instead of POSTing to ``webhook_url``. Empty token disables the
    # feature: no github outbox row is enqueued and the whole path is
    # a no-op, mirroring how ``webhook_url`` gates webhooks.
    # ``github_api_base`` lets self-hosted GitHub Enterprise installs
    # point at ``https://github.example.com/api/v3``; it defaults to
    # the public REST API host.
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    # v0.28 queue ordering refinements (queue QoS, low-hanging from the
    # roadmap v0.29 section pulled forward).
    #
    # ``queue_fairness_interval``: every Nth call to
    # pop_next_waiting_queue_entry ignores priority entirely and pops
    # by raw FIFO position. Guarantees low/normal-priority waiters
    # eventually win against a steady stream of high/blocking entries
    # that age boost (v0.26) and priority decay (this version) might
    # otherwise let monopolise. Set to 0 to disable (strict
    # priority-then-position ordering preserved).
    queue_fairness_interval: int = 10
    # ``queue_priority_decay_sec``: a waiting entry's effective
    # priority drops one level per this many seconds in the queue
    # (blocking -> high -> normal -> low, with low as the floor).
    # Counterpart to v0.26 age boost: prevents a misclassified urgent
    # request from sitting at the head of the queue indefinitely. Set
    # to 0 to disable decay (v0.26 boost + v0.25 declared priority
    # remain in force).
    queue_priority_decay_sec: int = 300
    # ``backpressure_header``: when True, every response includes an
    # ``X-Coord-Queue-Depth`` header counting how many of the caller's
    # claims are currently queued waiting. Lets clients self-regulate
    # without an extra round trip to ``/requests?queued=true``. Set
    # to False to disable for receivers that strip unknown headers.
    backpressure_header: bool = True
    # ``stale_engineer_days``: an engineer whose most recent
    # last_activity is older than this many days surfaces in the
    # ``coord engineers stale`` CLI and the dashboard's stale-engineer
    # panel. ``--release`` on the CLI drops their lingering claims.
    # Set to 0 to disable the housekeeping surface.
    stale_engineer_days: int = 7
    # v0.29 per-engineer bearer tokens. When True, the legacy shared
    # ``COORD_AUTH_TOKEN`` no longer authenticates -- the only
    # accepted bearers are rows in the ``engineer_tokens`` table
    # (managed by ``coord tokens create / list / revoke``). This is
    # the migration kill switch: flip it on once every client repo
    # has switched to its own per-engineer token. Default False so
    # existing deployments keep working unchanged on upgrade.
    require_per_engineer_token: bool = False
    # #30 slice 2/3 hardening: when True, a per-engineer token WITHOUT a repo
    # binding is rejected, so every agent token must be repo-scoped and the
    # server's repo isolation is mandatory rather than opt-in. The shared
    # COORD_AUTH_TOKEN is deliberately exempt -- it stays the operator /
    # cross-repo escape hatch (keep one, or an operator loses all-repo access).
    # Default False so existing deployments are unaffected on upgrade.
    require_scoped_token: bool = False
    # ``warn_unscoped_token`` (v0.43): the middle-ground nudge before
    # ``require_scoped_token`` is turned on. When True, a request made with
    # an UNSCOPED per-engineer token (repo IS NULL) is still honored, but the
    # response carries an ``X-Coord-Token-Warning`` header telling the agent
    # its token is not repo-bound and how to switch. The MCP wrapper surfaces
    # it as a ``coord_notice`` in the tool result and ``coord status`` prints
    # it. The shared operator token is exempt (it is unscoped by design), and
    # the header is never emitted once ``require_scoped_token`` hard-blocks
    # unscoped tokens. Set False to silence the nudge.
    warn_unscoped_token: bool = True
    # v0.29 dashboard cookie session.
    # an HTTP-only cookie with the engineer's bearer token so the
    # browser doesn't have to keep retyping it. Lifetime is bounded
    # by this many seconds; default 8h matches a working day. Set
    # to 0 to disable the cookie session entirely (browsers will
    # have to send Authorization: Bearer ... on every request,
    # which curl can do but real browsers cannot).
    dashboard_session_lifetime_sec: int = 28800
    # v0.29.3 ``dashboard_cookie_force_secure``: when True, the
    # ``coord_session`` cookie is always written with the Secure
    # attribute even if the request reaches the origin over HTTP.
    # Use this when the origin sits behind a TLS-terminating proxy
    # that does NOT inject ``X-Forwarded-Proto`` or ``CF-Visitor``
    # in a form the auto-detection in ``_request_uses_https`` can
    # see (or when those headers are stripped downstream). Default
    # False; the auto-detection handles Cloudflare Tunnel + Traefik
    # + nginx + ALB out of the box.
    dashboard_cookie_force_secure: bool = False
    # v0.29.6 generic OIDC SSO for the dashboard. When the four core
    # fields below (issuer, client_id, client_secret, redirect_uri)
    # are all set, the login page grows a "Sign in with SSO" link and
    # the /auth/oidc/* routes come alive. The flow is the standard
    # authorization-code + PKCE dance: coord redirects the browser to
    # the IdP, validates the returned ID token against the IdP's JWKS,
    # maps a claim to an engineer name, and mints a normal
    # per-engineer token bound to the dashboard session cookie -- SSO
    # logins reuse the existing v0.29 token machinery wholesale rather
    # than inventing a parallel session type.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # The EXACT callback URL registered with the IdP, e.g.
    # ``https://coord.example.com/auth/oidc/callback``. Required for
    # the feature (no inference): behind Cloudflare/Traefik the origin
    # cannot reliably reconstruct its public URL, and IdPs enforce an
    # exact string match on redirect_uri -- a guessed value fails the
    # IdP-side check with an opaque error, so we make the operator
    # state it explicitly.
    oidc_redirect_uri: str = ""
    # Which ID-token claim becomes the coord engineer name. ``email``
    # (the default) gets extra handling: lowercased, and rejected when
    # the IdP explicitly says ``email_verified: false``.
    oidc_engineer_claim: str = "email"
    oidc_scopes: str = "openid email profile"
    # Comma-separated allowlist of principals (claim values) permitted
    # to log in. Empty means "no allowlist", which is fine for
    # tenant-scoped IdPs (Okta, Entra, Keycloak -- only your org's
    # users exist there) but dangerous for public issuers where anyone
    # on the internet holds a valid account; the login route refuses
    # to start the flow against a known public issuer until either
    # this is set or ``oidc_allow_any_principal`` is flipped on.
    oidc_allowed_principals: str = ""
    oidc_allow_any_principal: bool = False
    # Prefix applied to the mapped claim value when forming the
    # engineer name, e.g. ``sso/`` turns dev@example.com into
    # ``sso/dev@example.com``. Keeps SSO-minted identities visually
    # distinct from hand-minted ones in claims and token listings.
    oidc_engineer_prefix: str = ""
    # #30 slice 2/3: when set, the OIDC claim whose value binds the SSO-minted
    # token to a repo (e.g. ``coord_repo``), so SSO dashboard sessions are
    # repo-scoped rather than operator-wide. If configured and the claim is
    # absent/empty in a principal's token, that login is REFUSED rather than
    # silently granted all-repo access. Unset (default) keeps SSO sessions
    # unscoped (operator) -- the documented v1 default.
    oidc_repo_claim: str = ""
    # v0.30 per-engineer rate limiting + per-repo queue-depth quota.
    # All three knobs default to 0 = disabled, so an upgrade changes
    # nothing until the operator opts in. Limits key on the
    # request-body ``engineer`` field -- the worker identity that
    # claims are stored and counted under -- NOT the authenticated
    # bearer token. A client that lies about its engineer name can
    # therefore dodge its own bucket; tying limits to authenticated
    # token identity needs a token->engineer attribution column and is
    # deferred to a future migration (v0.30 ships with no schema
    # change).
    #
    # ``max_claims_per_engineer``: cap on simultaneously ACTIVE
    # (unreleased, non-TTL-expired) claims one engineer may hold.
    # Enforced at insert time only -- an at-cap engineer may still
    # queue future work with wait_seconds; the cap re-fires when the
    # queue grant would actually insert.
    max_claims_per_engineer: int = 0
    # ``max_queued_per_engineer``: cap on live (waiting or
    # in_progress) claim_queue entries one engineer may have. Stops a
    # single agent from carpeting the queue with speculative waits.
    max_queued_per_engineer: int = 0
    # ``max_queue_depth_per_repo``: cap on ``state='waiting'`` queue
    # rows per repo bucket (NULL-repo requests form their own bucket).
    # Admission control for a degraded-service situation: when the
    # repo's queue is this deep, new wait_seconds requests get an
    # immediate 429 instead of joining a line that will not clear.
    max_queue_depth_per_repo: int = 0
    # v0.31 LSP-aware symbol claims (wave 1). When ``lsp_enabled`` is
    # True AND ``repo_root`` is set, claim-time span persistence
    # upgrades parser line spans to exact LSP documentSymbol ranges,
    # and symbol validation falls back to the language server for
    # names the tree-sitter/regex extraction cannot see (conditional
    # defs, decorated factories, re-exports). Default False: no
    # subprocess is ever spawned and behaviour is byte-identical to
    # v0.30. Every LSP failure is silent -- parser spans / the v0.17
    # rejection path are always the fallback, never an error.
    lsp_enabled: bool = False
    # A pooled language-server process that has not served a request
    # for this many seconds is reaped by ``shutdown_idle``. Servers
    # respawn on demand, so this only trades cold-start latency
    # against resident memory.
    lsp_idle_shutdown_sec: int = 300
    # Ceiling for any single JSON-RPC roundtrip (the initialize
    # handshake included). On expiry the call fails soft and counts
    # one circuit-breaker failure.
    lsp_request_timeout_sec: float = 5.0
    # Circuit breaker, per (language, repo_root) pair: after this many
    # consecutive failures the circuit opens and every LSP call
    # short-circuits to None for ``lsp_circuit_cooldown_sec`` seconds.
    # A spawn failure (server binary not installed) opens the circuit
    # immediately -- retrying an uninstalled binary three times per
    # claim would only add latency.
    lsp_circuit_failure_threshold: int = 3
    lsp_circuit_cooldown_sec: int = 600
    # Server launch command lines. Each is shlex-split and exec'd
    # directly (never through a shell), so quoting works but shell
    # syntax does not. Override to pin versions or to wrap servers in
    # ``uvx`` / ``npx`` style launchers.
    lsp_command_python: str = "pylsp"
    lsp_command_typescript: str = "typescript-language-server --stdio"
    lsp_command_go: str = "gopls"
    # v0.33: per-language servers for the symbol backends added in #29.
    # JavaScript rides on the TypeScript server, so it has no field of its
    # own; ``_command_for`` maps it onto ``lsp_command_typescript``.
    lsp_command_rust: str = "rust-analyzer"
    lsp_command_java: str = "jdtls"
    lsp_command_c: str = "clangd"
    lsp_command_cpp: str = "clangd"
    lsp_command_csharp: str = "OmniSharp"
    lsp_command_ruby: str = "ruby-lsp"
    lsp_command_php: str = "intelephense --stdio"
    lsp_command_kotlin: str = "kotlin-language-server"
    lsp_command_swift: str = "sourcekit-lsp"
    lsp_command_scala: str = "metals"
    # v0.32 optional OpenTelemetry distributed tracing. When True (env
    # ``COORD_OTEL_ENABLED=true``), ``coordination.otel.setup_tracing``
    # lazily imports the OpenTelemetry SDK, installs a TracerProvider
    # exporting spans over OTLP/HTTP, and instruments the FastAPI app and
    # outbound httpx clients. Default False: the SDK is never imported,
    # no provider is installed, and behaviour is byte-identical to a
    # build without tracing -- coord takes no hard dependency on
    # OpenTelemetry being importable. The collector endpoint and service
    # name are NOT coord settings; the SDK reads the standard
    # ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_SERVICE_NAME`` env vars
    # directly. Setup fails open: any error (missing extra, bad endpoint)
    # logs a warning and leaves coord running untraced rather than
    # breaking startup, mirroring the webhook/github opt-in gates.
    otel_enabled: bool = False

    @property
    def oidc_enabled(self) -> bool:
        """SSO is all-or-nothing: every one of the four core fields
        must be set before the routes activate. A partially configured
        deployment (say, issuer without redirect_uri) keeps the
        feature dark rather than half-working."""
        return bool(
            self.oidc_issuer
            and self.oidc_client_id
            and self.oidc_client_secret
            and self.oidc_redirect_uri
        )

    @property
    def oidc_allowed_principal_set(self) -> frozenset[str]:
        """Parsed allowlist: split on commas, strip whitespace, drop
        empties, lowercase. Lowercasing here means operators can paste
        mixed-case emails and they still match the lowercased email
        claim; non-email principals should be configured in the exact
        (lowercase) form the IdP emits."""
        return frozenset(
            p.strip().lower()
            for p in self.oidc_allowed_principals.split(",")
            if p.strip()
        )

    @property
    def oidc_scopes_list(self) -> list[str]:
        return [s for s in self.oidc_scopes.split() if s]

    @property
    def auth_mode(self) -> str:
        """Human-readable auth posture for ``/readyz`` and ``/meta``.

        ``per_engineer`` wins over ``bearer``: once the operator flips
        ``require_per_engineer_token`` the shared token no longer
        authenticates, so reporting ``bearer`` would be misleading.
        The flag alone also makes a deployment viable -- v0.29.4
        per-engineer-only mode needs no ``auth_token`` at all, with
        the ``engineer_tokens`` table as the only credential store.
        """
        if self.require_per_engineer_token:
            return "per_engineer"
        if self.auth_token:
            return "bearer"
        if self.allow_insecure_no_auth:
            return "insecure-no-auth"
        return "misconfigured"


def get_settings() -> Settings:
    return Settings()
