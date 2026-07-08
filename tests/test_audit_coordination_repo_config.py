"""Audit-fix coverage for RepoConfig TOML serialisation (v0.45 audit).

``RepoConfig.to_toml`` interpolated values into quoted TOML strings
with no escaping, so any field containing a double quote, backslash,
or control character produced a config.toml that ``RepoConfig.load``
(tomllib) could no longer parse -- and load()'s callers swallow the
exception, silently disabling remote-mode protections. Serialisation
now escapes per TOML basic-string rules, making the round trip safe
for the full string domain of the dataclass fields.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from coordination.repo_config import RepoConfig, _toml_str


def _round_trip(config: RepoConfig, tmp_path: Path) -> RepoConfig:
    path = tmp_path / "config.toml"
    path.write_text(config.to_toml(), encoding="utf-8")
    return RepoConfig.load(path)


def test_plain_values_round_trip_byte_identical(tmp_path: Path) -> None:
    config = RepoConfig(
        version=1,
        tool="claude",
        mode="remote",
        service_url="http://coord.kebabrack.lan",
        ownership_file=".coordination/owners.yaml",
        repo_id="amittell/coord",
    )
    loaded = _round_trip(config, tmp_path)
    assert loaded == config


def test_double_quote_in_value_round_trips(tmp_path: Path) -> None:
    config = RepoConfig(
        version=1,
        tool="claude",
        mode="remote",
        service_url='http://coord.lan/path?q="quoted"',
        ownership_file='.coordination/"owners".yaml',
    )
    loaded = _round_trip(config, tmp_path)
    assert loaded.service_url == config.service_url
    assert loaded.ownership_file == config.ownership_file


def test_backslash_in_value_round_trips(tmp_path: Path) -> None:
    config = RepoConfig(
        version=1,
        tool="claude",
        mode="local",
        service_url="http://coord.lan",
        ownership_file="C:\\coord\\owners.yaml",
    )
    loaded = _round_trip(config, tmp_path)
    assert loaded.ownership_file == "C:\\coord\\owners.yaml"


def test_control_characters_round_trip(tmp_path: Path) -> None:
    hostile = "line1\nline2\ttabbed\x00nul"
    config = RepoConfig(
        version=1,
        tool="claude",
        mode="local",
        service_url="http://coord.lan",
        ownership_file=hostile,
    )
    loaded = _round_trip(config, tmp_path)
    assert loaded.ownership_file == hostile


def test_toml_str_output_is_always_parseable() -> None:
    for value in (
        "plain",
        'has "quotes"',
        "back\\slash",
        "new\nline",
        "\x01\x1f\x7f",
        "",
    ):
        doc = f"v = {_toml_str(value)}"
        assert tomllib.loads(doc)["v"] == value
