from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ALLOWED_SEVERITIES = {"soft", "hard"}


@dataclass
class PathRule:
    pattern: str
    severity: str  # soft | hard
    owners: list[str]


def parse_ownership_yaml(text: str) -> list[PathRule]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - specific message varies by parser
        raise ValueError(f"Invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Ownership config must be a YAML mapping")

    if "modules" not in data and "areas" not in data:
        raise ValueError("Ownership config must define `modules` or `areas`")

    rules: list[PathRule] = []
    for section_name in ("modules", "areas"):
        section = data.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ValueError(f"`{section_name}` must be a mapping")
        for key, block in section.items():
            if not isinstance(block, dict):
                raise ValueError(f"`{section_name}.{key}` must be a mapping")
            paths = block.get("paths") or []
            if not isinstance(paths, list) or not paths:
                raise ValueError(f"`{section_name}.{key}.paths` must be a non-empty list")
            owners = block.get("owners") or []
            if not isinstance(owners, list):
                raise ValueError(f"`{section_name}.{key}.owners` must be a list")
            severity = str(block.get("severity", "soft")).lower()
            if severity not in ALLOWED_SEVERITIES:
                allowed = ", ".join(sorted(ALLOWED_SEVERITIES))
                raise ValueError(
                    f"`{section_name}.{key}.severity` must be one of: {allowed}"
                )
            for path in paths:
                if not isinstance(path, str) or not path.strip():
                    raise ValueError(f"`{section_name}.{key}.paths` must only contain strings")
                rules.append(
                    PathRule(
                        pattern=path,
                        severity=severity,
                        owners=[str(owner) for owner in owners],
                    )
                )

    if not rules:
        raise ValueError("Ownership config must define at least one path rule")

    return rules


def severity_for_pattern(pattern: str, rules: list[PathRule]) -> str:
    """Return hardest matching severity for a claim pattern."""
    from coordination.engine import heuristic_overlap

    best = "soft"
    for rule in rules:
        if heuristic_overlap(pattern, rule.pattern) or heuristic_overlap(rule.pattern, pattern):
            if rule.severity == "hard":
                return "hard"
            best = "soft"
    return best


def load_ownership_from_file(path: Path) -> list[PathRule]:
    return parse_ownership_yaml(path.read_text(encoding="utf-8"))
