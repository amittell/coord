from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pathspec


def _git_ls_files_sync(repo_root: Path, scope: str | None = None) -> list[str]:
    cmd = ["git", "-C", str(repo_root), "ls-files"]
    if scope:
        cmd += ["--", scope]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return []
    text = proc.stdout.strip()
    if not text:
        return []
    return text.splitlines()


@dataclass
class _LsFilesCacheEntry:
    files: list[str]
    expires_at: float  # time.monotonic() deadline


_LS_FILES_CACHE: dict[tuple[str, str | None], _LsFilesCacheEntry] = {}


def _ls_files_ttl_default() -> float:
    raw = os.environ.get("COORD_LS_FILES_CACHE_TTL_SEC")
    if raw is None:
        return 10.0
    try:
        return float(raw)
    except ValueError:
        return 10.0


_LS_FILES_TTL_SEC: float = _ls_files_ttl_default()


def _clear_ls_files_cache() -> None:
    """Drop all cached ls-files results. Intended for test isolation."""
    _LS_FILES_CACHE.clear()


async def git_ls_files(repo_root: Path, scope: str | None = None) -> list[str]:
    key = (str(repo_root), scope)
    now = time.monotonic()
    entry = _LS_FILES_CACHE.get(key)
    if entry is not None and entry.expires_at > now:
        return entry.files
    files = await asyncio.to_thread(_git_ls_files_sync, repo_root, scope)
    # Only cache successful non-empty results; empty could mean transient failure.
    if files:
        _LS_FILES_CACHE[key] = _LsFilesCacheEntry(
            files=files, expires_at=now + _LS_FILES_TTL_SEC
        )
    return files


def _normalize_pattern(pattern: str) -> str:
    """Normalize a user-supplied pattern to a canonical form.

    - Replace backslashes with forward slashes (Windows inputs)
    - Strip a leading "./"
    - Collapse a trailing "/" to "/**" (directory shorthand)
    - Remove a leading "/" (gitignore anchors to repo root regardless)
    """
    p = pattern.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    if p.startswith("/"):
        p = p[1:]
    if p.endswith("/"):
        p += "**"
    return p


def _pattern_to_pathspec_line(pattern: str) -> str:
    return _normalize_pattern(pattern)


def files_matching_pattern(all_files: list[str], pattern: str) -> list[str]:
    line = _pattern_to_pathspec_line(pattern)
    spec = pathspec.PathSpec.from_lines("gitignore", [line])
    return [f for f in all_files if spec.match_file(f)]


def _reject_negation(pattern: str) -> None:
    """Raise ValueError if the pattern is a gitignore negation.

    Negation patterns (leading '!') mean "exclude these paths" in gitignore
    semantics. They have no defensible overlap interpretation: asking whether
    an exclusion overlaps with an inclusion (or another exclusion) is a
    category error. We reject at the boundary rather than produce misleading
    results.
    """
    if pattern.startswith("!"):
        raise ValueError(
            f"Pattern {pattern!r} is a gitignore negation; negation patterns "
            "are not supported for overlap checks"
        )


def _parse_char_class(seg: str, start: int) -> tuple[str, int] | None:
    """Parse a `[...]` character class starting at position `start` in `seg`.

    Returns (representative_char, end_index_exclusive) on success, else None.
    The representative char is a literal character guaranteed to satisfy the
    class. For a positive class we pick the first character (or first char of
    the first range). For a negated class (`[!...]`) we pick a character that
    is NOT in the negation set.
    """
    if start >= len(seg) or seg[start] != "[":
        return None
    i = start + 1
    if i >= len(seg):
        return None
    negated = False
    if seg[i] == "!":
        negated = True
        i += 1
    # Collect the members of the class as a flat set of literal chars (ranges
    # expanded). `]` as the first char after `[` or `[!` is literal in
    # gitignore / POSIX glob semantics.
    members: set[str] = set()
    first = True
    while i < len(seg):
        c = seg[i]
        if c == "]" and not first:
            # End of class.
            if negated:
                # Pick any printable ASCII letter not in the excluded set.
                for candidate in "abcdefghijklmnopqrstuvwxyz0123456789_":
                    if candidate not in members:
                        return candidate, i + 1
                # Fallback: a character extremely unlikely to be excluded.
                return "_", i + 1
            # Positive class: pick first member (sorted for determinism).
            if not members:
                return None
            return sorted(members)[0], i + 1
        # Check for a range like a-c
        if (
            i + 2 < len(seg)
            and seg[i + 1] == "-"
            and seg[i + 2] != "]"
        ):
            lo, hi = seg[i], seg[i + 2]
            if ord(lo) <= ord(hi):
                for code in range(ord(lo), ord(hi) + 1):
                    members.add(chr(code))
            else:
                members.add(lo)
                members.add(hi)
            i += 3
        else:
            members.add(c)
            i += 1
        first = False
    # Unterminated class.
    return None


def _synthesize_candidate(pattern: str) -> str | None:
    """Synthesize a single concrete path that `pattern` would match.

    Strategy:
    - `**` segments each contribute ONE unique synthetic directory component
      (e.g. `ds0`, `ds1`, ...). Since pathspec treats `**` as matching zero or
      more directories, substituting a single placeholder directory is a valid
      concrete instance of the pattern.
    - `*` (within a segment, not crossing `/`) is replaced with the literal
      placeholder `x`. A run of consecutive `*` characters within a segment
      collapses to a single `x`.
    - `?` is replaced with `a`.
    - `[...]` character classes are parsed and replaced with a literal member
      of the class (for negated classes, a character guaranteed NOT in the
      excluded set).
    - All other characters are preserved literally.

    Returns a single synthetic path that, by construction, is matched by this
    pattern's own PathSpec. Overlap against a peer pattern B is then decided
    by asking B's PathSpec whether it also matches this synthetic path.
    """
    norm = _normalize_pattern(pattern)
    if not norm:
        return None
    parts = norm.split("/")

    out_segments: list[str] = []
    star_counter = 0

    for seg in parts:
        if seg == "":
            continue
        if seg == "**":
            out_segments.append(f"ds{star_counter}")
            star_counter += 1
            continue
        # Build the segment character-by-character, handling character
        # classes, `*`, and `?`.
        chars: list[str] = []
        i = 0
        n = len(seg)
        while i < n:
            c = seg[i]
            if c == "[":
                parsed = _parse_char_class(seg, i)
                if parsed is None:
                    # Treat as literal bracket (defensive).
                    chars.append(c)
                    i += 1
                    continue
                rep, new_i = parsed
                chars.append(rep)
                i = new_i
            elif c == "*":
                while i < n and seg[i] == "*":
                    i += 1
                chars.append("x")
            elif c == "?":
                chars.append("a")
                i += 1
            else:
                chars.append(c)
                i += 1
        synth = "".join(chars)
        if not synth:
            synth = "x"
        out_segments.append(synth)

    if not out_segments:
        return None
    return "/".join(out_segments)


def heuristic_overlap(a: str, b: str) -> bool:
    """Decide whether two patterns can overlap without a repo file list.

    Strategy: each pattern generates exactly ONE synthetic concrete path that
    it provably matches (via `_synthesize_candidate`, which replaces each
    wildcard token with a deterministic literal). The patterns overlap iff
    either synthetic path is also matched by the peer's PathSpec. This uses
    pathspec's native `**` handling (zero or more directories) so it behaves
    correctly at arbitrary depth and for arbitrary numbers of `**` segments,
    with O(1) candidates per side.

    Empty patterns are rejected and return False (there is no file a truly
    empty pattern could match, so claiming overlap is indefensible).

    Negation patterns (leading `!`) are rejected with ValueError; exclusion
    semantics have no meaningful overlap interpretation.
    """
    _reject_negation(a)
    _reject_negation(b)
    if not a or not b:
        return False
    na = _normalize_pattern(a)
    nb = _normalize_pattern(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    spec_a = pathspec.PathSpec.from_lines("gitignore", [na])
    spec_b = pathspec.PathSpec.from_lines("gitignore", [nb])

    cand_a = _synthesize_candidate(a)
    cand_b = _synthesize_candidate(b)

    # Either direction sufficing means there is at least one conceivable
    # concrete path that both patterns can match, i.e. they overlap.
    if cand_b is not None and spec_a.match_file(cand_b):
        return True
    if cand_a is not None and spec_b.match_file(cand_a):
        return True
    return False


async def compute_overlap(
    pattern_a: str,
    pattern_b: str,
    *,
    repo_root: Path | None,
    scope: str | None = None,
) -> list[str]:
    _reject_negation(pattern_a)
    _reject_negation(pattern_b)
    if repo_root and repo_root.is_dir():
        files = await git_ls_files(repo_root, scope=scope)
        if files:
            fa = set(files_matching_pattern(files, pattern_a))
            fb = set(files_matching_pattern(files, pattern_b))
            return sorted(fa & fb)
    if heuristic_overlap(pattern_a, pattern_b):
        return ["<unknown>"]
    return []
