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


# ---------------------------------------------------------------------------
# v0.21 soft auto-promote: YAML patch helpers
# ---------------------------------------------------------------------------
#
# These helpers patch an owners.yaml document with operator-driven
# hotspot promotions surfaced by the dashboard. They do NOT validate
# the resulting document against ``parse_ownership_yaml`` -- the new
# top-level keys (``shared_files`` and ``suggested_splits``) are
# advisory only and intentionally outside the strict modules/areas
# schema. The HTTP upload path (/config/ownership) still enforces a
# valid modules/areas section for any caller that exercises it
# directly; the promote endpoint writes through ``set_ownership_yaml``
# at the service layer, which is the unvalidated DB-backed path.


def patch_owners_yaml_with_shared_file(yaml_text: str, pattern: str) -> str:
    """Insert ``pattern`` into the top-level ``shared_files:`` list of
    ``yaml_text``. Creates the list if absent. Returns the patched YAML.
    Idempotent: re-adding a present pattern is a no-op.

    The ``shared_files`` key is a flat list of glob patterns that the
    dashboard's hotspot panel promotes when a file accumulates enough
    409s to suggest it belongs in a shared-rule scope. Example shape::

        shared_files:
          - src/router.ts
          - src/api/index.ts
        modules:
          ...

    Empty / missing input is treated as an empty document so a
    first-ever promotion seeds the file cleanly. Non-mapping documents
    raise :class:`ValueError`; we refuse to silently clobber a list-
    rooted YAML.
    """
    try:
        data = yaml.safe_load(yaml_text) if yaml_text else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("Ownership config must be a YAML mapping")
    existing = data.get("shared_files")
    if existing is None:
        data["shared_files"] = [pattern]
    elif not isinstance(existing, list):
        raise ValueError("`shared_files` must be a list")
    elif pattern not in existing:
        existing.append(pattern)
    return yaml.safe_dump(data, sort_keys=False)


def patch_owners_yaml_with_split_suggestion(
    yaml_text: str, *, pattern: str, note: str | None, suggested_at: str
) -> str:
    """Insert a split-suggestion entry into the top-level
    ``suggested_splits:`` list. Creates the list if absent. Returns the
    patched YAML. Idempotent: re-adding the same pattern is a no-op
    (note / suggested_at on the existing entry are left untouched so a
    re-promotion does not silently rewrite the original justification).

    The ``suggested_splits`` key is advisory only -- coord does not
    enforce splits. It records operator intent surfaced from the
    hotspot panel so future review has the context::

        suggested_splits:
          - pattern: src/router.ts
            note: too central; should be split per route family
            suggested_at: 2026-06-02T12:00:00Z

    Empty / missing input is treated as an empty document. Non-mapping
    documents raise :class:`ValueError`; ``suggested_splits`` must
    already be a list of mappings or absent.
    """
    try:
        data = yaml.safe_load(yaml_text) if yaml_text else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("Ownership config must be a YAML mapping")
    existing = data.get("suggested_splits")
    entry: dict[str, str] = {"pattern": pattern, "suggested_at": suggested_at}
    if note is not None:
        entry["note"] = note
    if existing is None:
        data["suggested_splits"] = [entry]
    elif not isinstance(existing, list):
        raise ValueError("`suggested_splits` must be a list")
    else:
        for item in existing:
            if isinstance(item, dict) and item.get("pattern") == pattern:
                return yaml.safe_dump(data, sort_keys=False)
        existing.append(entry)
    return yaml.safe_dump(data, sort_keys=False)
