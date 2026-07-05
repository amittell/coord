"""Unit tests for the central repo-id validator/normalizer (#61,
``coordination/repo_id.py``). Distinct from ``test_repo_id.py``, which covers
client-side repo-id *detection* from a git origin."""

from __future__ import annotations

import pytest

from coordination.repo_id import (
    MAX_REPO_ID_LEN,
    InvalidRepoId,
    normalize_repo_id,
)


@pytest.mark.parametrize(
    "value",
    [
        "owner/name",
        "amittell/coord",
        "amittell/repo-a",
        "app",  # bare basename fallback (no git origin)
        "org/team_repo.v2",
        "a",
        "A1/b2",
    ],
)
def test_valid_ids_pass_through(value: str) -> None:
    assert normalize_repo_id(value) == value


def test_none_passes_through() -> None:
    assert normalize_repo_id(None) is None


def test_trims_surrounding_whitespace() -> None:
    assert normalize_repo_id("  owner/name  ") == "owner/name"


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "   ",  # whitespace only
        "/owner/name",  # leading slash
        "owner/name/",  # trailing slash
        "owner//name",  # doubled slash
        "..",  # path traversal
        "../etc",  # path traversal
        "owner/..",  # traversal segment
        ".hidden",  # segment must start alphanumeric
        "-leading",  # segment must start alphanumeric
        "owner\nname",  # embedded control char (trailing is trimmed, so embed)
        "owner name",  # space in id
        "owner/na me",  # space in segment
        "owner:name",  # illegal char
        "owner/name;rm",  # shell metachar
    ],
)
def test_malformed_ids_raise(value: str) -> None:
    with pytest.raises(InvalidRepoId):
        normalize_repo_id(value)


def test_overlong_id_raises() -> None:
    with pytest.raises(InvalidRepoId):
        normalize_repo_id("a" * (MAX_REPO_ID_LEN + 1))


def test_max_length_ok() -> None:
    value = "a" * MAX_REPO_ID_LEN
    assert normalize_repo_id(value) == value


def test_normalization_is_idempotent() -> None:
    once = normalize_repo_id("owner/name")
    assert normalize_repo_id(once) == once


def test_invalid_repo_id_is_value_error() -> None:
    # Callers may catch the broad ValueError; InvalidRepoId must subclass it.
    assert issubclass(InvalidRepoId, ValueError)
