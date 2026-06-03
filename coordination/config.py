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

    @property
    def auth_mode(self) -> str:
        if self.auth_token:
            return "bearer"
        if self.allow_insecure_no_auth:
            return "insecure-no-auth"
        return "misconfigured"


def get_settings() -> Settings:
    return Settings()
