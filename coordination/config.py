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

    @property
    def auth_mode(self) -> str:
        if self.auth_token:
            return "bearer"
        if self.allow_insecure_no_auth:
            return "insecure-no-auth"
        return "misconfigured"


def get_settings() -> Settings:
    return Settings()
