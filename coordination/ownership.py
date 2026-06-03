from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
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


# Marker suffix written onto coord-managed ``shared_files`` entries so
# the v0.23 auto-demote sweep can distinguish them from operator-added
# entries. The marker is a YAML comment so it has no semantic effect on
# parsing -- ``parse_owners_yaml_with_shared_file`` round-trips a
# managed entry through PyYAML and gets back just the pattern string.
_MANAGED_MARKER_RE = re.compile(r"#\s*auto-promoted=(\d{4}-\d{2}-\d{2})\b")

# Regex that finds the top-level ``shared_files:`` key. Anchored to the
# start of a line and rejects any leading whitespace so it never matches
# a nested ``shared_files:`` mapping inside ``modules`` / ``areas``.
# Tolerates the inline empty-list form ``shared_files: []`` and an
# optional trailing comment.
_SHARED_FILES_HEADER_RE = re.compile(
    r"^shared_files\s*:(?:\s*\[\s*\])?\s*(?:#.*)?$", re.MULTILINE
)


def _list_item_pattern(line: str) -> str | None:
    """Return the bare pattern string from a YAML list-item line such as
    ``  - src/router.ts  # auto-promoted=2026-06-02``, or ``None`` if the
    line is not a list item. Strips quotes and trailing comments."""
    stripped = line.lstrip()
    if not stripped.startswith("- "):
        return None
    rest = stripped[2:]
    # Strip any trailing comment.
    if "#" in rest:
        rest = rest.split("#", 1)[0]
    rest = rest.strip()
    # Strip surrounding quotes (single or double); YAML allows either.
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ("'", '"'):
        rest = rest[1:-1]
    return rest


def _shared_files_block_range(
    lines: list[str], header_line_no: int
) -> tuple[int, int]:
    """Return the ``(start, end)`` exclusive bounds (in ``lines``) of the
    list-item block that follows the ``shared_files:`` header at
    ``header_line_no``. The block ends at the first line that is not a
    list item AND not blank AND not a comment with the same-or-deeper
    indent as the items. Empty list ``shared_files: []`` short-circuits
    to ``(header_line_no + 1, header_line_no + 1)``."""
    start = header_line_no + 1
    i = start
    while i < len(lines):
        raw = lines[i]
        stripped = raw.lstrip()
        if not stripped:
            # Blank line ends the block; the next top-level key would
            # be flush left and a non-list anyway.
            break
        if stripped.startswith("- "):
            i += 1
            continue
        # A comment line at deeper-or-equal indent than the list item
        # we'd insert still belongs to the block visually; bail at
        # anything else.
        if stripped.startswith("#") and (len(raw) - len(stripped)) >= 2:
            i += 1
            continue
        # Anything else (a new top-level key, a deeper nested mapping)
        # ends the block.
        break
    return start, i


def patch_owners_yaml_with_shared_file(
    yaml_text: str,
    pattern: str,
    *,
    managed: bool = False,
    promoted_at: str | None = None,
) -> str:
    """Insert ``pattern`` into the top-level ``shared_files:`` list of
    ``yaml_text``. Creates the list if absent. Returns the patched YAML.
    Idempotent: re-adding a present pattern is a no-op (comment marker
    on an existing entry is preserved as-is).

    When ``managed`` is True, the inserted line carries a YAML comment
    ``# auto-promoted=YYYY-MM-DD`` so the v0.23 auto-demote sweep can
    recognise coord-owned entries. ``promoted_at`` overrides the marker
    date (default: today UTC). The marker is a comment so YAML parsing
    yields the bare pattern.

    The ``shared_files`` key is a flat list of glob patterns that the
    dashboard's hotspot panel promotes when a file accumulates enough
    409s to suggest it belongs in a shared-rule scope. Example shape::

        shared_files:
          - src/router.ts                # auto-promoted=2026-06-02
          - src/api/index.ts
        modules:
          ...

    Empty / missing input is treated as an empty document so a
    first-ever promotion seeds the file cleanly. Non-mapping documents
    raise :class:`ValueError`; we refuse to silently clobber a list-
    rooted YAML.

    Implementation note: PyYAML drops comments on round-trip, so we
    parse the document only to validate shape, then do line-level
    string surgery to add the new entry while preserving every comment
    around it.
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
    if existing is not None and not isinstance(existing, list):
        raise ValueError("`shared_files` must be a list")

    marker_suffix = ""
    if managed:
        when = promoted_at or datetime.now(UTC).strftime("%Y-%m-%d")
        marker_suffix = f"  # auto-promoted={when}"

    new_line = f"  - {pattern}{marker_suffix}"

    # Empty document: seed a brand-new shared_files block.
    if not yaml_text or not yaml_text.strip():
        return f"shared_files:\n{new_line}\n"

    text = yaml_text if yaml_text.endswith("\n") else yaml_text + "\n"
    header_match = _SHARED_FILES_HEADER_RE.search(text)
    if header_match is None:
        # No shared_files section yet -- append one at the end.
        if existing is not None:
            # Defensive: parse said the list exists but the header
            # regex didn't find it. Should not happen; fall back to
            # a yaml.safe_dump rewrite to stay correct at the cost of
            # losing comments.
            existing_list = list(existing)
            if pattern not in existing_list:
                existing_list.append(pattern)
            data["shared_files"] = existing_list
            return yaml.safe_dump(data, sort_keys=False)
        # Make sure we separate from the prior block by a newline.
        sep = "" if text.endswith("\n") else "\n"
        return f"{text}{sep}shared_files:\n{new_line}\n"

    lines = text.split("\n")
    # split() on trailing newline produces a final empty string; track
    # whether to re-add a trailing newline at the end.
    trailing_newline = text.endswith("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines.pop()

    # Find the header line number in ``lines``.
    header_line_no = -1
    char_offset = 0
    for idx, ln in enumerate(lines):
        end_offset = char_offset + len(ln) + 1  # +1 for newline
        if char_offset <= header_match.start() < end_offset:
            header_line_no = idx
            break
        char_offset = end_offset
    if header_line_no < 0:
        # Shouldn't happen given the regex matched, but guard anyway.
        return yaml.safe_dump(data, sort_keys=False)

    # Inline empty-list form ``shared_files: []``. Replace with a
    # block-form list and insert the new entry.
    header_line = lines[header_line_no]
    if header_line.strip().endswith("[]"):
        lines[header_line_no] = "shared_files:"
        lines.insert(header_line_no + 1, new_line)
        out = "\n".join(lines)
        return out + "\n" if trailing_newline else out

    block_start, block_end = _shared_files_block_range(lines, header_line_no)

    # Idempotence: if the pattern is already in the block, return the
    # original text unchanged (ignore marker suffix when comparing).
    for i in range(block_start, block_end):
        if _list_item_pattern(lines[i]) == pattern:
            return text if trailing_newline else text.rstrip("\n")

    # Insert at the end of the block so existing comments stay attached
    # to the entries they annotate.
    lines.insert(block_end, new_line)
    out = "\n".join(lines)
    return out + "\n" if trailing_newline else out


def list_coord_managed_shared_files(yaml_text: str) -> list[tuple[str, str]]:
    """Return ``[(pattern, promoted_at_iso), ...]`` for every
    ``shared_files`` entry whose line carries the
    ``# auto-promoted=YYYY-MM-DD`` marker.

    Operator-added entries (no marker) are skipped so the v0.23 demote
    sweep can act only on what coord itself wrote. Returns an empty
    list when the document has no ``shared_files`` section or no
    managed entries.
    """
    if not yaml_text or not yaml_text.strip():
        return []
    text = yaml_text if yaml_text.endswith("\n") else yaml_text + "\n"
    header_match = _SHARED_FILES_HEADER_RE.search(text)
    if header_match is None:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # Resolve the header line index.
    header_line_no = -1
    char_offset = 0
    for idx, ln in enumerate(lines):
        end_offset = char_offset + len(ln) + 1
        if char_offset <= header_match.start() < end_offset:
            header_line_no = idx
            break
        char_offset = end_offset
    if header_line_no < 0:
        return []
    block_start, block_end = _shared_files_block_range(lines, header_line_no)
    out: list[tuple[str, str]] = []
    for i in range(block_start, block_end):
        line = lines[i]
        pattern = _list_item_pattern(line)
        if pattern is None:
            continue
        marker = _MANAGED_MARKER_RE.search(line)
        if marker is None:
            continue
        out.append((pattern, marker.group(1)))
    return out


def patch_owners_yaml_remove_shared_file(
    yaml_text: str, pattern: str
) -> str:
    """Remove ``pattern`` from the ``shared_files`` list. Idempotent:
    a pattern that isn't present (or a document with no
    ``shared_files`` section at all) returns the text unchanged.

    Preserves surrounding comments and other entries. If removing the
    last entry leaves an empty ``shared_files:`` list the header line
    is removed too -- a YAML document with ``shared_files:`` and no
    items is ambiguous (PyYAML reads it as ``None``) and would also
    confuse the next auto-promote.
    """
    if not yaml_text or not yaml_text.strip():
        return yaml_text
    try:
        yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc
    text = yaml_text if yaml_text.endswith("\n") else yaml_text + "\n"
    header_match = _SHARED_FILES_HEADER_RE.search(text)
    if header_match is None:
        return yaml_text

    lines = text.split("\n")
    trailing_newline = text.endswith("\n")
    if trailing_newline and lines and lines[-1] == "":
        lines.pop()

    header_line_no = -1
    char_offset = 0
    for idx, ln in enumerate(lines):
        end_offset = char_offset + len(ln) + 1
        if char_offset <= header_match.start() < end_offset:
            header_line_no = idx
            break
        char_offset = end_offset
    if header_line_no < 0:
        return yaml_text

    block_start, block_end = _shared_files_block_range(lines, header_line_no)
    # Find the line(s) to drop. A pattern in the list block can only
    # appear once after a normal patch flow, but we tolerate dupes.
    to_drop: list[int] = []
    for i in range(block_start, block_end):
        if _list_item_pattern(lines[i]) == pattern:
            to_drop.append(i)
    if not to_drop:
        return yaml_text
    for i in reversed(to_drop):
        del lines[i]

    # Recompute the block: if no list items remain (only blanks /
    # comments), drop the header too so the document doesn't become
    # ``shared_files:\n`` which YAML parses as ``shared_files: None``.
    block_start2, block_end2 = _shared_files_block_range(lines, header_line_no)
    remaining_items = any(
        _list_item_pattern(lines[i]) is not None
        for i in range(block_start2, block_end2)
    )
    if not remaining_items:
        # Remove header + everything in its block (comments + blanks).
        del lines[header_line_no:block_end2]
        # If we left a stray leading blank line where the section used
        # to be, collapse it.
        if (
            header_line_no < len(lines)
            and not lines[header_line_no].strip()
            and (header_line_no == 0 or not lines[header_line_no - 1].strip())
        ):
            del lines[header_line_no]
    out = "\n".join(lines)
    return out + "\n" if trailing_newline else out


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
