from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

# TOML basic-string escapes (TOML v1.0 section on strings). Backslash and
# double quote would otherwise terminate/corrupt the quoted value; the
# control characters are forbidden raw inside basic strings, so a value
# carrying any of them would serialise to a config.toml that tomllib can
# no longer parse -- and RepoConfig.load's callers swallow that parse
# error, silently disabling remote-mode protections.
_TOML_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_str(value: str) -> str:
    """Serialise ``value`` as a TOML basic string, escaping everything a
    bare f-string interpolation would emit unparseably."""
    out: list[str] = []
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


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
            f"tool = {_toml_str(self.tool)}\n"
            f"mode = {_toml_str(self.mode)}\n"
            f"service_url = {_toml_str(self.service_url)}\n"
            f"ownership_file = {_toml_str(self.ownership_file)}\n"
            f"local_env_file = {_toml_str(self.local_env_file)}\n"
        )
        if self.repo_id:
            body += f"repo_id = {_toml_str(self.repo_id)}\n"
        return body

