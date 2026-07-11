"""Robust parsing of ``.coordination/local.env`` files.

A single parser shared by every consumer of ``local.env`` so they all agree
on the value of a key. Historically three readers disagreed: the MCP server's
loader stripped quotes, ``coord doctor``'s token reader did not (and returned
the *first* match), and the pre-push hook just ``source``-d the file in bash
(last assignment wins, quotes stripped). A token that was quoted, indented,
duplicated, or separated by blank lines would then work in some places and
401 in others -- a confusing failure mode for operators.

:func:`parse_env` is deliberately close to what POSIX shell ``source`` does
for the simple ``KEY=VALUE`` lines these files contain:

- blank lines and ``#`` comment lines are ignored;
- leading/trailing whitespace on the line and around the value is stripped;
- a leading ``export `` is allowed and ignored;
- one layer of matching surrounding quotes (``"`` or ``'``) is removed;
- the LAST assignment of a key wins (so an appended fresh token overrides a
  stale one above it, matching ``source``).

It does NOT attempt full shell semantics (variable expansion, escapes,
multi-line values); local.env is a flat list of ``KEY=VALUE`` lines.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


_WINDOWS_PRIVATE_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:COORD_PRIVATE_ACL_PATH
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = Get-Acl -LiteralPath $path
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) {
    [void]$acl.RemoveAccessRuleSpecific($rule)
}
$privateRule = [Security.AccessControl.FileSystemAccessRule]::new(
    $currentSid,
    [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
)
[void]$acl.AddAccessRule($privateRule)
Set-Acl -LiteralPath $path -AclObject $acl
"""


def _harden_windows_private_acl(path: Path) -> None:
    """Give only the current Windows SID access to ``path``.

    ``0o600`` does not control NTFS DACL inheritance. PowerShell and the ACL
    APIs used here are built into supported Windows installations; failure is
    fatal so coord never falls back to writing a bearer token under an
    inherited group/world-readable ACL.
    """

    child_env = os.environ.copy()
    child_env["COORD_PRIVATE_ACL_PATH"] = str(path)
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _WINDOWS_PRIVATE_ACL_SCRIPT,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=child_env,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise PermissionError(
            "could not apply a private Windows ACL to local.env"
        ) from exc


def _harden_private_temp(fd: int, path: Path) -> None:
    """Make an empty local.env tempfile private before secrets are written."""

    if os.name == "nt":
        _harden_windows_private_acl(path)
    else:
        os.fchmod(fd, 0o600)


def parse_env(text: str) -> dict[str, str]:
    """Parse ``text`` (the contents of a local.env file) into a dict.

    Last assignment of a key wins. See the module docstring for the exact
    normalisation applied to each value.
    """

    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, val = line.partition("=")
        if sep != "=":
            continue
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("\"", "'"):
            val = val[1:-1]
        out[key] = val
    return out


def read_env_file(path: Path) -> dict[str, str]:
    """Read and parse a local.env file. Returns an empty dict if the file is
    absent or unreadable (callers treat a missing file as "no config")."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_env(text)


def _line_key(raw: str) -> str | None:
    """The key a ``KEY=VALUE`` line assigns, normalised exactly the way
    :func:`parse_env` normalises it (whitespace stripped, a leading
    ``export `` tolerated). Returns None for blank lines, comments, and
    lines with no ``=``."""

    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    key, sep, _ = line.partition("=")
    if sep != "=":
        return None
    key = key.strip()
    return key or None


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Rewrite ``path`` with ``updates`` applied in place, preserving
    everything coord does not manage.

    This is the single writer both ``coord init`` and ``coord upgrade`` use
    to refresh ``.coordination/local.env``. Rules:

    - Comment lines, blank lines, and assignments of keys NOT in ``updates``
      (e.g. ``COORD_USER``, ``COORD_BRANCH``, ``COORD_REPO_ROOT`` -- every
      key the MCP wrapper's ``_LOCAL_ENV_KEYS`` bootstraps, plus anything
      else an operator added) are kept verbatim, in their original order.
    - The FIRST assignment line of each updated key is replaced with the
      canonical ``KEY=VALUE`` form; later duplicate assignments of the same
      key are dropped. :func:`parse_env` applies last-assignment-wins, so
      callers resolve the effective value first and the rewrite collapses
      the duplicates onto that single authoritative line.
    - Updated keys absent from the file are appended at the end, in the
      order ``updates`` lists them. A missing file is created fresh.
    """

    for update_key, update_value in updates.items():
        if not isinstance(update_key, str) or re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", update_key
        ) is None:
            raise ValueError(f"invalid environment key: {update_key!r}")
        if not isinstance(update_value, str):
            raise TypeError(
                f"environment value for {update_key!r} must be a string"
            )
        # local.env is deliberately a one-assignment-per-line data format.
        # Reject every separator recognised by str.splitlines(), plus NUL,
        # so one managed value can never smuggle a second assignment into
        # Python readers or the pre-push hook's allowlisted data loader.
        if (
            "\x00" in update_value
            or "".join(update_value.splitlines()) != update_value
        ):
            raise ValueError(
                f"environment value for {update_key!r} contains a line or NUL separator"
            )

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    out: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        key = _line_key(raw)
        if key is None or key not in updates:
            out.append(raw)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{key}={updates[key]}")
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(out) + ("\n" if out else "")

    # local.env normally contains a bearer token.  Write it through a private
    # same-directory temporary file so a new file never inherits a permissive
    # umask and readers never observe a partially rewritten file.  Replacing
    # an existing path also repairs legacy 0644 files without a window where
    # the replacement is world-readable.
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        _harden_private_temp(fd, temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
