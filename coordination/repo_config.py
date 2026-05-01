from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass
class RepoConfig:
    version: int
    tool: str
    mode: str
    service_url: str
    ownership_file: str
    local_env_file: str = ".coordination/local.env"
    repo_id: str | None = None

    @classmethod
    def load(cls, path: Path) -> "RepoConfig":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        repo_id_raw = data.get("repo_id")
        return cls(
            version=int(data.get("version", 1)),
            tool=str(data["tool"]),
            mode=str(data["mode"]),
            service_url=str(data["service_url"]),
            ownership_file=str(data.get("ownership_file", ".coordination/owners.yaml")),
            local_env_file=str(data.get("local_env_file", ".coordination/local.env")),
            repo_id=str(repo_id_raw) if repo_id_raw else None,
        )

    def to_toml(self) -> str:
        body = (
            f"version = {self.version}\n"
            f'tool = "{self.tool}"\n'
            f'mode = "{self.mode}"\n'
            f'service_url = "{self.service_url}"\n'
            f'ownership_file = "{self.ownership_file}"\n'
            f'local_env_file = "{self.local_env_file}"\n'
        )
        if self.repo_id:
            body += f'repo_id = "{self.repo_id}"\n'
        return body

