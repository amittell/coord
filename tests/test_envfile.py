from __future__ import annotations

import os
import stat
import subprocess

import pytest

from coordination import envfile
from coordination.envfile import parse_env, read_env_file, update_env_file


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


def test_update_env_file_uses_same_directory_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path / "local.env"
    replaced: list[tuple[object, object]] = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replaced.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(envfile.os, "replace", recording_replace)
    update_env_file(path, {"COORD_AUTH_TOKEN": "coordt_secret"})

    assert len(replaced) == 1
    source, destination = replaced[0]
    assert destination == path
    assert source.parent == path.parent
    assert not source.exists()


def test_update_env_file_hardens_empty_temp_before_writing_secret(
    tmp_path, monkeypatch
):
    path = tmp_path / "local.env"
    observed: list[tuple[int, object]] = []
    real_harden = envfile._harden_private_temp

    def recording_harden(fd, temporary):
        observed.append((os.fstat(fd).st_size, temporary))
        real_harden(fd, temporary)

    monkeypatch.setattr(envfile, "_harden_private_temp", recording_harden)
    update_env_file(path, {"COORD_AUTH_TOKEN": "coordt_secret"})

    assert len(observed) == 1
    assert observed[0][0] == 0
    assert observed[0][1].parent == path.parent


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable")
@pytest.mark.parametrize("preexisting", [False, True])
def test_update_env_file_creates_and_repairs_private_mode(tmp_path, preexisting):
    path = tmp_path / "local.env"
    if preexisting:
        path.write_text("COORD_AUTH_TOKEN=old\n", encoding="utf-8")
        path.chmod(0o644)

    update_env_file(path, {"COORD_AUTH_TOKEN": "coordt_secret"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "COORD_AUTH_TOKEN=coordt_secret\n"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BAD=KEY", "value"),
        ("BAD\nKEY", "value"),
        ("", "value"),
        ("COORD_REPO_ID", "safe\nCOORD_PUSH_BASE_REF=HEAD"),
        ("COORD_REPO_ID", "safe\rCOORD_TOKEN=shadow"),
        ("COORD_REPO_ID", "safe\x00shadow"),
        ("COORD_REPO_ID", "safe\u2028COORD_TOKEN=shadow"),
    ],
)
def test_update_env_file_rejects_assignment_injection(tmp_path, key, value):
    path = tmp_path / "local.env"
    original = "COORD_AUTH_TOKEN=original\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises((TypeError, ValueError)):
        update_env_file(path, {key: value})

    assert path.read_text(encoding="utf-8") == original


def test_update_env_file_does_not_replace_an_unreadable_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "local.env"
    original = "COORD_AUTH_TOKEN=original\n# keep me\n"
    path.write_text(original, encoding="utf-8")
    real_read_text = type(path).read_text

    def denied_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied for regression test")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", denied_read)

    with pytest.raises(PermissionError):
        update_env_file(path, {"COORD_AUTH_TOKEN": "replacement"})

    assert real_read_text(path, encoding="utf-8") == original


def test_windows_acl_command_uses_current_sid_without_a_shell(
    tmp_path, monkeypatch
):
    path = tmp_path / "local.env"
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(envfile.subprocess, "run", fake_run)
    envfile._harden_windows_private_acl(path)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:5] == [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    assert argv[-1] == envfile._WINDOWS_PRIVATE_ACL_SCRIPT
    assert str(path) not in argv
    assert "COORD_PRIVATE_ACL_PATH" in argv[-1]
    assert "WindowsIdentity]::GetCurrent().User" in argv[-1]
    child_env = kwargs.pop("env")
    assert child_env["COORD_PRIVATE_ACL_PATH"] == str(path)
    assert kwargs == {"check": True, "capture_output": True, "text": True}


def test_acl_failure_leaves_existing_file_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "local.env"
    original = "COORD_AUTH_TOKEN=original\n"
    path.write_text(original, encoding="utf-8")

    def fail_hardening(fd, temporary):
        raise PermissionError("ACL failure")

    monkeypatch.setattr(envfile, "_harden_private_temp", fail_hardening)

    with pytest.raises(PermissionError, match="ACL failure"):
        update_env_file(path, {"COORD_AUTH_TOKEN": "replacement"})

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".local.env.*")) == []


@pytest.mark.platform
@pytest.mark.skipif(os.name != "nt", reason="requires Windows ACL APIs")
def test_update_env_file_has_only_current_user_allow_acl_on_windows(tmp_path):
    path = tmp_path / "local.env"
    update_env_file(path, {"COORD_AUTH_TOKEN": "coordt_secret"})
    verify_script = r"""
$ErrorActionPreference = 'Stop'
$path = $env:COORD_PRIVATE_ACL_TEST_PATH
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = Get-Acl -LiteralPath $path
if (-not $acl.AreAccessRulesProtected) { throw 'ACL inheritance is enabled' }
$allowSids = @(
    $acl.Access |
        Where-Object { $_.AccessControlType -eq 'Allow' } |
        ForEach-Object {
            $_.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
)
if ($allowSids.Count -ne 1 -or $allowSids[0] -ne $currentSid) {
    throw "unexpected allow ACL: $($allowSids -join ',')"
}
"""
    child_env = os.environ.copy()
    child_env["COORD_PRIVATE_ACL_TEST_PATH"] = str(path)
    subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            verify_script,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=child_env,
    )
