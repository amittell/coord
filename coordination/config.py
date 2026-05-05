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

    @property
    def auth_mode(self) -> str:
        if self.auth_token:
            return "bearer"
        if self.allow_insecure_no_auth:
            return "insecure-no-auth"
        return "misconfigured"


def get_settings() -> Settings:
    return Settings()
