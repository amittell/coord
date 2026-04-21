from __future__ import annotations

from pathlib import Path

import pytest

from coordination.ownership import (
    PathRule,
    load_ownership_from_file,
    parse_ownership_yaml,
    severity_for_pattern,
)


# ---------------------------------------------------------------------------
# parse_ownership_yaml
# ---------------------------------------------------------------------------


def test_parses_modules_section() -> None:
    text = """
modules:
  auth:
    paths: ["src/auth/**"]
    severity: hard
    owners: [alice, bob]
"""
    rules = parse_ownership_yaml(text)
    assert len(rules) == 1
    rule = rules[0]
    assert isinstance(rule, PathRule)
    assert rule.pattern == "src/auth/**"
    assert rule.severity == "hard"
    assert rule.owners == ["alice", "bob"]


def test_parses_areas_section() -> None:
    text = """
areas:
  billing:
    paths: ["src/billing/**"]
    severity: soft
    owners: [carol]
"""
    rules = parse_ownership_yaml(text)
    assert len(rules) == 1
    assert rules[0].pattern == "src/billing/**"
    assert rules[0].severity == "soft"
    assert rules[0].owners == ["carol"]


def test_parses_both_sections_when_present() -> None:
    text = """
modules:
  auth:
    paths: ["src/auth/**"]
    severity: hard
areas:
  billing:
    paths: ["src/billing/**"]
    severity: soft
"""
    rules = parse_ownership_yaml(text)
    patterns = sorted(r.pattern for r in rules)
    assert patterns == ["src/auth/**", "src/billing/**"]
    by_pat = {r.pattern: r.severity for r in rules}
    assert by_pat["src/auth/**"] == "hard"
    assert by_pat["src/billing/**"] == "soft"


def test_default_severity_is_soft_when_unspecified() -> None:
    text = """
modules:
  shared:
    paths: ["src/shared/**"]
"""
    rules = parse_ownership_yaml(text)
    assert len(rules) == 1
    assert rules[0].severity == "soft"


def test_severity_hard_is_preserved() -> None:
    text = """
modules:
  shared:
    paths: ["src/shared/**"]
    severity: hard
"""
    rules = parse_ownership_yaml(text)
    assert rules[0].severity == "hard"


def test_rejects_invalid_severity() -> None:
    text = """
modules:
  shared:
    paths: ["src/shared/**"]
    severity: critical
"""
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml(text)
    msg = str(excinfo.value)
    assert "severity" in msg
    # Allowed values must be surfaced so operators can fix the config.
    assert "hard" in msg
    assert "soft" in msg


def test_rejects_non_mapping_root() -> None:
    # YAML top-level list is not a mapping.
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml("- just\n- a\n- list\n")
    assert "mapping" in str(excinfo.value).lower()


def test_rejects_missing_modules_and_areas() -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml("something: else\n")
    msg = str(excinfo.value)
    assert "modules" in msg
    assert "areas" in msg


def test_rejects_empty_paths_list() -> None:
    text = """
modules:
  shared:
    paths: []
"""
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml(text)
    assert "paths" in str(excinfo.value)


def test_rejects_non_list_paths() -> None:
    text = """
modules:
  shared:
    paths: "src/**"
"""
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml(text)
    assert "paths" in str(excinfo.value)


def test_rejects_non_string_path_entry() -> None:
    text = """
modules:
  shared:
    paths: [123]
"""
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml(text)
    assert "paths" in str(excinfo.value)


def test_rejects_non_list_owners() -> None:
    text = """
modules:
  shared:
    paths: ["src/shared/**"]
    owners: "alice"
"""
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml(text)
    assert "owners" in str(excinfo.value)


def test_multiple_paths_create_multiple_rules() -> None:
    text = """
modules:
  shared:
    paths:
      - "src/a/**"
      - "src/b/**"
      - "src/c/**"
    severity: hard
    owners: [x]
"""
    rules = parse_ownership_yaml(text)
    assert len(rules) == 3
    assert {r.pattern for r in rules} == {"src/a/**", "src/b/**", "src/c/**"}
    # Each rule inherits the block-level severity and owners.
    for rule in rules:
        assert rule.severity == "hard"
        assert rule.owners == ["x"]


def test_owners_default_to_empty_list_when_unspecified() -> None:
    text = """
modules:
  shared:
    paths: ["src/shared/**"]
"""
    rules = parse_ownership_yaml(text)
    assert rules[0].owners == []


def test_rejects_config_that_yields_zero_rules() -> None:
    # modules present but empty mapping yields no rules.
    with pytest.raises(ValueError) as excinfo:
        parse_ownership_yaml("modules: {}\n")
    assert "rule" in str(excinfo.value).lower()


def test_yaml_error_wraps_as_valueerror() -> None:
    # Malformed YAML: unmatched flow-style brace.
    with pytest.raises(ValueError):
        parse_ownership_yaml("{unclosed")


# ---------------------------------------------------------------------------
# severity_for_pattern
# ---------------------------------------------------------------------------


def test_hard_wins_when_any_matching_rule_is_hard() -> None:
    rules = [
        PathRule(pattern="src/**", severity="soft", owners=[]),
        PathRule(pattern="src/auth/**", severity="hard", owners=[]),
    ]
    assert severity_for_pattern("src/auth/login.ts", rules) == "hard"


def test_returns_soft_when_only_soft_rules_match() -> None:
    rules = [
        PathRule(pattern="src/**", severity="soft", owners=[]),
        PathRule(pattern="src/ui/**", severity="soft", owners=[]),
    ]
    assert severity_for_pattern("src/ui/button.ts", rules) == "soft"


def test_returns_soft_when_no_rules_match_at_all() -> None:
    rules = [
        PathRule(pattern="tests/**", severity="hard", owners=[]),
    ]
    assert severity_for_pattern("src/auth/**", rules) == "soft"


def test_empty_rules_list_returns_soft() -> None:
    assert severity_for_pattern("src/auth/**", []) == "soft"


def test_hardest_match_wins_even_if_soft_rule_was_checked_first() -> None:
    # Same overlap semantics, but the hard rule sits later in the list.
    # severity_for_pattern must still upgrade to hard.
    rules = [
        PathRule(pattern="src/auth/**", severity="soft", owners=[]),
        PathRule(pattern="src/**", severity="hard", owners=[]),
    ]
    assert severity_for_pattern("src/auth/login.ts", rules) == "hard"


# ---------------------------------------------------------------------------
# load_ownership_from_file
# ---------------------------------------------------------------------------


def test_reads_from_path(tmp_path: Path) -> None:
    cfg = tmp_path / "ownership.yaml"
    cfg.write_text(
        """
modules:
  auth:
    paths: ["src/auth/**"]
    severity: hard
    owners: [alice]
""",
        encoding="utf-8",
    )
    rules = load_ownership_from_file(cfg)
    assert len(rules) == 1
    assert rules[0].pattern == "src/auth/**"
    assert rules[0].severity == "hard"
    assert rules[0].owners == ["alice"]


def test_raises_filenotfound_for_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(FileNotFoundError):
        load_ownership_from_file(missing)
