#!/usr/bin/env python3
"""Classify PyPI state against a deterministic local release directory.

Output is one shell-safe line: ``exact`` or ``absent|file...`` or
``partial|file...``. Existing remote files are accepted only when their names
and SHA-256 digests exactly match local artifacts; every listed filename is
validated as a basename before the release shell consumes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")


def classify_release(
    local: Mapping[str, str],
    remote: Mapping[str, str],
) -> tuple[str, list[str]]:
    if not local:
        raise ValueError("local release directory has no artifacts")
    unsafe = sorted(name for name in local if _SAFE_FILENAME.fullmatch(name) is None)
    if unsafe:
        raise ValueError(f"local release has unsafe filenames: {unsafe}")

    unexpected = sorted(set(remote) - set(local))
    if unexpected:
        raise ValueError(f"PyPI release has unexpected artifacts: {unexpected}")
    mismatched = sorted(
        name
        for name, digest in remote.items()
        if digest != local[name]
    )
    if mismatched:
        raise ValueError(
            f"PyPI artifacts do not match local SHA-256 digests: {mismatched}"
        )

    missing = sorted(set(local) - set(remote))
    if not remote:
        return "absent", missing
    if missing:
        return "partial", missing
    return "exact", []


def _local_artifacts(dist_dir: Path) -> dict[str, str]:
    return {
        artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()
        for artifact in sorted(dist_dir.iterdir())
        if artifact.is_file()
    }


def _remote_artifacts(project: str, version: str) -> dict[str, str]:
    url = f"https://pypi.org/pypi/{quote(project, safe='')}/json"
    try:
        with urlopen(url, timeout=10) as response:
            document: Any = json.load(response)
        release = document["releases"].get(version) or []
        return {
            item["filename"]: item["digests"]["sha256"]
            for item in release
        }
    except (HTTPError, URLError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"could not query PyPI release state: {exc}") from exc


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        state, missing = classify_release(
            _local_artifacts(args.dist_dir),
            _remote_artifacts(args.project, args.version),
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"PyPI release-state check failed: {exc}") from exc
    print("|".join([state, *missing]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
