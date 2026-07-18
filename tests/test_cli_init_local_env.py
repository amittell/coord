"""Force-reinit must never destroy an unrecoverable remote credential.

Live incident 2026-07-18: ``coord init --tool claude --mode remote --force``
(run to refresh the pre-push hook to v0.48 semantics) downgraded the pasted
per-engineer tokens of BOTH fleet repos to the ``set-me`` placeholder.
Remote mode cannot re-mint — the raw token exists only in local.env — so a
forced refresh with no exported replacement must preserve what is there.
"""

from pathlib import Path

import pytest

from coordination import cli_init


def _seed(tmp_path: Path, token: str) -> Path:
    envfile = tmp_path / ".coordination" / "local.env"
    envfile.parent.mkdir(parents=True)
    envfile.write_text(
        "COORD_API_URL=https://coord.example\n"
        "COORD_SERVICE_URL=https://coord.example\n"
        f"COORD_AUTH_TOKEN={token}\n",
        encoding="utf-8",
    )
    return envfile


def test_force_remote_reinit_preserves_token_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    envfile = _seed(tmp_path, "real-pasted-token")
    preserved = cli_init._write_local_env(
        tmp_path, "remote", "https://coord.example", force=True
    )
    assert preserved is True
    assert "COORD_AUTH_TOKEN=real-pasted-token" in envfile.read_text(
        encoding="utf-8"
    )


def test_force_remote_reinit_replaces_token_with_exported_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORD_AUTH_TOKEN", "replacement-token")
    envfile = _seed(tmp_path, "old-token")
    preserved = cli_init._write_local_env(
        tmp_path, "remote", "https://coord.example", force=True
    )
    assert preserved is False
    assert "COORD_AUTH_TOKEN=replacement-token" in envfile.read_text(
        encoding="utf-8"
    )


def test_unforced_remote_reinit_ignores_exported_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --force the pasted token wins even when the env exports one:
    re-running init for a second --tool must stay additive."""
    monkeypatch.setenv("COORD_AUTH_TOKEN", "ambient-token")
    envfile = _seed(tmp_path, "pasted-token")
    preserved = cli_init._write_local_env(
        tmp_path, "remote", "https://coord.example", force=False
    )
    assert preserved is True
    assert "COORD_AUTH_TOKEN=pasted-token" in envfile.read_text(
        encoding="utf-8"
    )


def test_fresh_remote_init_writes_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORD_AUTH_TOKEN", raising=False)
    preserved = cli_init._write_local_env(
        tmp_path, "remote", "https://coord.example", force=False
    )
    envfile = tmp_path / ".coordination" / "local.env"
    assert preserved is False
    assert (
        f"COORD_AUTH_TOKEN={cli_init.PLACEHOLDER_AUTH_TOKEN}"
        in envfile.read_text(encoding="utf-8")
    )
