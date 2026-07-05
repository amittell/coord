"""Central repo-id validation / normalization (#61).

A repo identifier buckets claims on a shared multi-repo coord service and
scopes a repo-bound token. It enters the system from several places -- the
``coord tokens create --repo <id>`` CLI, an OIDC repo claim, a request
``repo=`` param/body, and ``COORD_REPO_ID`` -- and enforcement is by
*comparison* (``token_repo`` vs the request value). Without a single shared
rule, a malformed or non-canonical id (trailing slash, doubled slash, control
chars, absurd length) is accepted as long as it happens to match on both
sides, and two spellings of "the same" repo become distinct buckets. This
module is the one validator every ingress calls so ids are well-formed and
canonical, and a bad one fails fast with the same clear error everywhere.
"""

from __future__ import annotations

import re

# A repo id is one or more path-like segments joined by a single '/':
# "owner/name" (the canonical GitHub form) or a bare "name" (the CLI's
# basename fallback when a checkout has no git origin). Each segment starts
# with an alphanumeric and then allows '.', '_' and '-'. That structure
# rejects empty segments, a leading/trailing or doubled '/', and bare '.'/'..'
# segments (path-traversal-shaped ids) without a separate check.
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_REPO_ID_RE = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
MAX_REPO_ID_LEN = 200


class InvalidRepoId(ValueError):
    """Raised when a repo identifier is malformed."""


def normalize_repo_id(value: str | None) -> str | None:
    """Trim and validate a repo identifier, returning its canonical form.

    ``None`` passes through unchanged (an unscoped / operator token, or a
    request that omits ``repo``). A non-None value is stripped of surrounding
    whitespace and must match the repo-id grammar; otherwise
    :class:`InvalidRepoId` (a ``ValueError`` subclass) is raised so callers can
    map it to a 400 / login refusal / CLI error. Normalization is idempotent,
    so re-validating an already-clean id is a no-op.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        raise InvalidRepoId("repo id must not be empty")
    if len(trimmed) > MAX_REPO_ID_LEN:
        raise InvalidRepoId(
            f"repo id too long ({len(trimmed)} > {MAX_REPO_ID_LEN} chars)"
        )
    if not _REPO_ID_RE.fullmatch(trimmed):
        raise InvalidRepoId(
            f"invalid repo id {value!r}: expected one or more segments of "
            "letters/digits/'.'/'_'/'-' (each starting alphanumeric) joined by "
            "a single '/', e.g. 'owner/name'"
        )
    return trimmed
