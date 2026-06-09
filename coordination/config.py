from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COORD_", extra="ignore")

    database_path: Path = Path("data/coordination.db")
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
    # v0.29 dashboard cookie session. The dashboard login form sets
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

    @property
    def auth_mode(self) -> str:
        if self.auth_token:
            return "bearer"
        if self.allow_insecure_no_auth:
            return "insecure-no-auth"
        return "misconfigured"


def get_settings() -> Settings:
    return Settings()
