from __future__ import annotations

from coordination.envfile import parse_env, read_env_file


def test_basic_key_value():
    assert parse_env("COORD_AUTH_TOKEN=coordt_abc") == {"COORD_AUTH_TOKEN": "coordt_abc"}


def test_strips_matching_quotes():
    # coord's local.env template ships COORD_AUTH_TOKEN="set-me" quoted.
    assert parse_env('COORD_AUTH_TOKEN="coordt_abc"')["COORD_AUTH_TOKEN"] == "coordt_abc"
    assert parse_env("COORD_AUTH_TOKEN='coordt_abc'")["COORD_AUTH_TOKEN"] == "coordt_abc"
    # A lone/mismatched quote is left alone (not a surrounding pair).
    assert parse_env('COORD_AUTH_TOKEN="coordt_abc')["COORD_AUTH_TOKEN"] == '"coordt_abc'


def test_strips_surrounding_whitespace_and_export():
    assert (
        parse_env("  export COORD_AUTH_TOKEN =  coordt_abc  ")["COORD_AUTH_TOKEN"]
        == "coordt_abc"
    )


def test_blank_and_comment_lines_ignored():
    text = "\n# a comment\n\nCOORD_AUTH_TOKEN=coordt_abc\n\n"
    assert parse_env(text) == {"COORD_AUTH_TOKEN": "coordt_abc"}


def test_last_assignment_wins():
    # The exact shape that bit a real user: a stale token left above the
    # fresh one (separated by a blank line). Shell `source` uses the last
    # assignment; parse_env matches that, so the fresh token wins.
    text = "COORD_AUTH_TOKEN=coordt_stale\n\nCOORD_AUTH_TOKEN=coordt_fresh\n"
    assert parse_env(text)["COORD_AUTH_TOKEN"] == "coordt_fresh"


def test_crlf_line_endings():
    text = 'COORD_AUTH_TOKEN="coordt_abc"\r\nCOORD_API_URL=https://x\r\n'
    parsed = parse_env(text)
    assert parsed["COORD_AUTH_TOKEN"] == "coordt_abc"
    assert parsed["COORD_API_URL"] == "https://x"


def test_value_containing_equals_is_preserved():
    assert parse_env("COORD_API_URL=https://x?a=b")["COORD_API_URL"] == "https://x?a=b"


def test_lines_without_equals_ignored():
    assert parse_env("COORD_AUTH_TOKEN\njust some text\n") == {}


def test_read_env_file_missing_returns_empty(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


def test_read_env_file(tmp_path):
    p = tmp_path / "local.env"
    p.write_text('COORD_AUTH_TOKEN="coordt_abc"\nCOORD_REPO_ID=owner/repo\n', encoding="utf-8")
    parsed = read_env_file(p)
    assert parsed["COORD_AUTH_TOKEN"] == "coordt_abc"
    assert parsed["COORD_REPO_ID"] == "owner/repo"
