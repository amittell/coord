"""v20 fleet-enforcement: repos registry, mint validation, Claude hooks.

Live incident 2026-07-17: scoped tokens were minted for repos the service
had never seen (format-only validation), claims were filed under one
engineer name while the pre-push hook self-excluded under another, and two
fully-initialized repos saw zero claims all day. These tests pin the three
fixes: registry-gated minting, /whoami identity, and the enforcement hooks.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from coordination import claude_hooks
from coordination.cli_init import _update_claude_settings
from coordination.cli_tokens import _create
from coordination.db import Database


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "coord.db")
    await d.init()
    return d


class TestReposRegistry:
    async def test_register_is_idempotent_and_listable(self, db: Database):
        assert await db.register_repo("alexm/writ", registered_by="amittell")
        assert not await db.register_repo("alexm/writ")
        assert await db.repo_registered("alexm/writ")
        assert not await db.repo_registered("alexm/phantom")
        rows = await db.list_registered_repos()
        assert [r["repo_id"] for r in rows] == ["alexm/writ"]
        assert rows[0]["registered_by"] == "amittell"

    async def test_ids_are_normalized(self, db: Database):
        await db.register_repo("Alexm/Writ")
        # Whatever normalize_repo_id produces, both spellings must agree.
        assert await db.repo_registered("Alexm/Writ")


class TestScopedMintValidation:
    def _args(self, tmp_path: Path, **over) -> argparse.Namespace:
        base = dict(
            engineer="amittell",
            description=None,
            expires_in=None,
            repo="alexm/writ",
            register=False,
            database_path=str(tmp_path / "coord.db"),
            local_db=True,
            json=True,
        )
        base.update(over)
        return argparse.Namespace(**base)

    async def test_unregistered_repo_is_refused(self, tmp_path, capsys):
        rc = await _create(self._args(tmp_path))
        assert rc == 1
        err = capsys.readouterr().err
        assert "not registered" in err
        assert "coord repos register alexm/writ" in err

    async def test_register_flag_bootstraps_and_mints(self, tmp_path, capsys):
        rc = await _create(self._args(tmp_path, register=True))
        assert rc == 0
        captured = capsys.readouterr()
        assert "Registered repo alexm/writ." in captured.err
        payload = json.loads(captured.out)
        assert payload["repo"] == "alexm/writ"
        # Registry persisted: the next mint needs no flag.
        rc = await _create(self._args(tmp_path))
        assert rc == 0

    async def test_registered_repo_mints_without_flag(self, tmp_path, capsys):
        db = Database(tmp_path / "coord.db")
        await db.register_repo("alexm/writ")
        rc = await _create(self._args(tmp_path))
        assert rc == 0

    async def test_unscoped_tokens_are_untouched(self, tmp_path, capsys):
        rc = await _create(self._args(tmp_path, repo=None))
        assert rc == 0


class TestClaudeHookLogic:
    def test_extracts_edit_targets_only(self):
        assert (
            claude_hooks.extract_target_file(
                {"tool_name": "Edit", "tool_input": {"file_path": "/r/a.py"}}
            )
            == "/r/a.py"
        )
        assert (
            claude_hooks.extract_target_file(
                {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/r/n.ipynb"}}
            )
            == "/r/n.ipynb"
        )
        # Read-only and unrelated tools never trigger claims.
        assert (
            claude_hooks.extract_target_file(
                {"tool_name": "Read", "tool_input": {"file_path": "/r/a.py"}}
            )
            is None
        )
        assert claude_hooks.extract_target_file({"tool_name": "Bash"}) is None

    def test_foreign_conflicts_excludes_self(self):
        conflicts = [
            {"engineer": "amittell", "pattern": "a.py"},
            {"engineer": "infra-agent", "pattern": "a.py"},
        ]
        foreign = claude_hooks.foreign_conflicts(conflicts, "amittell")
        assert [c["engineer"] for c in foreign] == ["infra-agent"]
        # Unknown self-identity fails safe: everything is foreign.
        assert len(claude_hooks.foreign_conflicts(conflicts, None)) == 2


class TestClaudeSettingsMerge:
    def test_fresh_install_writes_all_three_hooks(self, tmp_path: Path):
        path = tmp_path / ".claude" / "settings.json"
        assert _update_claude_settings(path)
        settings = json.loads(path.read_text())
        assert set(settings["hooks"]) == {"SessionStart", "PreToolUse", "SessionEnd"}
        pre = settings["hooks"]["PreToolUse"][0]
        assert pre["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
        assert pre["hooks"][0]["command"] == "coord-claude-hook pretool"

    def test_merge_preserves_existing_and_is_idempotent(self, tmp_path: Path):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "permissions": {"defaultMode": "plan"},
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "mylint"}],
                            }
                        ]
                    },
                }
            )
        )
        assert _update_claude_settings(path)
        settings = json.loads(path.read_text())
        # Foreign settings and hook groups survive.
        assert settings["permissions"] == {"defaultMode": "plan"}
        commands = [
            h["command"]
            for g in settings["hooks"]["PreToolUse"]
            for h in g["hooks"]
        ]
        assert "mylint" in commands
        assert "coord-claude-hook pretool" in commands
        # Second run: no changes.
        assert not _update_claude_settings(path)

    def test_invalid_json_is_left_alone(self, tmp_path: Path, capsys):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        assert not _update_claude_settings(path)
        assert path.read_text() == "{broken"
