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
import os
import threading
import time
import urllib.error
import urllib.parse
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


class _FakeHookClient:
    def __init__(self, *, fail_paths: set[str] | None = None):
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.fail_paths = fail_paths or set()
        self.claim_rows: list[dict] = []
        self.closed_sessions: set[str] = set()

    def get(self, path: str) -> dict:
        self.gets.append(path)
        if path == "/whoami":
            return {"engineer": "alex/claude/main"}
        if path.startswith("/conflicts?"):
            return {"conflicts": []}
        if path.startswith("/claims?"):
            return {"claims": self.claim_rows}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        if path in self.fail_paths:
            raise OSError("coord unavailable")
        route = path.split("?", 1)[0]
        if route.startswith("/sessions/"):
            session_id = urllib.parse.unquote(route.split("/")[2])
            if route.endswith("/open"):
                self.closed_sessions.discard(session_id)
                return {"opened": True}
            if route.endswith("/check"):
                if session_id in self.closed_sessions:
                    raise urllib.error.HTTPError(
                        path, 409, "session_closed", {}, None
                    )
                return {"open": True}
            if route.endswith("/release"):
                self.closed_sessions.add(session_id)
                return {"released": 1}
        if path == "/claims":
            if body.get("session_id") in self.closed_sessions:
                raise urllib.error.HTTPError(path, 409, "session_closed", {}, None)
            return {"claim_ids": [f"claim-{len(self.posts)}"]}
        return {"released": 1}


class _BlockingHookClient(_FakeHookClient):
    def __init__(self):
        super().__init__()
        self.entered_claim = threading.Event()
        self.release_claim = threading.Event()

    def post(self, path: str, body: dict) -> dict:
        if path == "/claims" and not self.entered_claim.is_set():
            self.entered_claim.set()
            assert self.release_claim.wait(timeout=2)
        return super().post(path, body)


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
        assert (
            claude_hooks.extract_target_file({"tool_name": "Edit", "tool_input": "malformed"})
            is None
        )
        assert (
            claude_hooks.extract_target_file(
                {"tool_name": "Write", "tool_input": {"file_path": 42}}
            )
            is None
        )

    def test_foreign_conflicts_excludes_self(self):
        conflicts = [
            {"engineer": "amittell", "pattern": "a.py"},
            {"engineer": "infra-agent", "pattern": "a.py"},
        ]
        foreign = claude_hooks.foreign_conflicts(conflicts, "amittell")
        assert [c["engineer"] for c in foreign] == ["infra-agent"]
        # Unknown self-identity fails safe: everything is foreign.
        assert len(claude_hooks.foreign_conflicts(conflicts, None)) == 2

    def test_pretool_uses_payload_session_and_claims_each_path_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "src" / "agent.py"
        target.parent.mkdir()
        target.write_text("pass\n")
        payload = {
            "session_id": "session / unsafe?",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _FakeHookClient()

        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0

        claim_posts = [post for post in client.posts if post[0] == "/claims"]
        assert len(claim_posts) == 1
        assert claim_posts[0][1]["session_id"] == "session / unsafe?"
        assert claim_posts[0][1]["claims"][0]["pattern"] == "src/agent.py"
        assert len([path for path in client.gets if path.startswith("/conflicts?")]) == 2
        assert all(
            "session_id=session+%2F+unsafe%3F" in path
            for path in client.gets
            if path.startswith("/conflicts?")
        )
        # Caller input is hashed, never interpolated into the temp filename.
        state = claude_hooks._session_state_path("session / unsafe?")
        assert state.parent.parent == tmp_path
        assert state.parent.stat().st_mode & 0o777 == 0o700
        assert "/" not in state.name.removeprefix("session-")

    def test_repo_scope_propagates_and_partitions_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "shared-session",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _FakeHookClient()

        for repo in ("alexm/one", "alexm/two"):
            assert (
                claude_hooks.cmd_pretool(
                    {"COORD_REPO_ID": repo}, client, tmp_path, payload
                )
                == 0
            )

        claim_posts = [body for path, body in client.posts if path == "/claims"]
        assert [body["repo"] for body in claim_posts] == ["alexm/one", "alexm/two"]
        conflict_gets = [path for path in client.gets if path.startswith("/conflicts?")]
        assert "repo=alexm%2Fone" in conflict_gets[0]
        assert "repo=alexm%2Ftwo" in conflict_gets[1]
        assert claude_hooks._session_state_path(
            "shared-session", "alexm/one"
        ) != claude_hooks._session_state_path("shared-session", "alexm/two")

    def test_concurrent_hooks_create_only_one_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "concurrent-session",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _BlockingHookClient()
        results: list[int] = []

        def run_hook() -> None:
            results.append(claude_hooks.cmd_pretool({}, client, tmp_path, payload))

        first = threading.Thread(target=run_hook)
        second = threading.Thread(target=run_hook)
        first.start()
        assert client.entered_claim.wait(timeout=2)
        second.start()
        time.sleep(0.05)
        client.release_claim.set()
        first.join(timeout=2)
        second.join(timeout=2)

        assert sorted(results) == [0, 0]
        assert len([post for post in client.posts if post[0] == "/claims"]) == 1

    def test_sessionstart_without_prior_end_preserves_claim_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "live-session",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _FakeHookClient()

        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert claude_hooks.cmd_sessionstart({}, client, payload) == 0
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert len([path for path, _ in client.posts if path == "/claims"]) == 1

    def test_sessionstart_tolerates_pre_lifecycle_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))

        class OldServerClient(_FakeHookClient):
            def post(self, path: str, body: dict) -> dict:
                if path.endswith("/open"):
                    raise urllib.error.HTTPError(path, 404, "not found", {}, None)
                return super().post(path, body)

        client = OldServerClient()
        assert (
            claude_hooks.cmd_sessionstart(
                {}, client, {"session_id": "rolling-upgrade"}
            )
            == 0
        )
        output = json.loads(capsys.readouterr().out)
        assert "No active coord claims" in output["hookSpecificOutput"]["additionalContext"]

    def test_stale_cache_is_verified_after_idle_expiry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        now = [1.0]
        monkeypatch.setattr(claude_hooks.time, "time", lambda: now[0])
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "session-1",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _FakeHookClient()

        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert len([post for post in client.posts if post[0] == "/claims"]) == 1

        # Once the local cache ages out, verify against the server. A live
        # claim is retained without another POST.
        now[0] = 62.0
        client.claim_rows = [
            {"session_id": "session-1", "pattern": "agent.py", "scope_type": "file"}
        ]
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert len([post for post in client.posts if post[0] == "/claims"]) == 1

        # If idle expiry closed it, evict the stale cache entry and reclaim.
        now[0] = 123.0
        client.claim_rows = []
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert len([post for post in client.posts if post[0] == "/claims"]) == 2

    def test_new_path_does_not_refresh_an_expired_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        now = [1.0]
        monkeypatch.setattr(claude_hooks.time, "time", lambda: now[0])
        first = tmp_path / "first.py"
        second = tmp_path / "second.py"
        first.write_text("pass\n")
        second.write_text("pass\n")
        client = _FakeHookClient()

        def payload(path: Path) -> dict:
            return {
                "session_id": "per-path-session",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(path)},
            }

        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload(first)) == 0
        now[0] = 62.0
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload(second)) == 0

        # Adding second.py must not make first.py look freshly verified. The
        # server no longer reports first.py, so the next edit reclaims it.
        now[0] = 63.0
        client.claim_rows = [
            {
                "session_id": "per-path-session",
                "pattern": "second.py",
                "scope_type": "file",
            }
        ]
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload(first)) == 0
        assert len([post for post in client.posts if post[0] == "/claims"]) == 3

    def test_malformed_conflict_response_fails_open(self, tmp_path: Path):
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "session-1",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }

        class MalformedClient(_FakeHookClient):
            def get(self, path: str):
                if path == "/whoami":
                    return {"engineer": "alex/claude/main"}
                return {"conflicts": "not-a-list"}

        client = MalformedClient()
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert client.posts == []

    def test_claim_race_409_blocks_edit(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "race-session",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }

        class RacingClient(_FakeHookClient):
            def post(self, path: str, body: dict) -> dict:
                if path == "/claims":
                    raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)
                return super().post(path, body)

        assert claude_hooks.cmd_pretool({}, RacingClient(), tmp_path, payload) == 2
        assert "became claimed or its session closed" in capsys.readouterr().err

    def test_uncached_claim_403_blocks_edit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "foreign-owner",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }

        class ForbiddenClient(_FakeHookClient):
            def post(self, path: str, body: dict) -> dict:
                if path == "/claims":
                    raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
                return super().post(path, body)

        assert claude_hooks.cmd_pretool({}, ForbiddenClient(), tmp_path, payload) == 2
        assert "was forbidden" in capsys.readouterr().err

    def test_sessionend_bulk_releases_and_drains_legacy_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-live-session")
        claude_hooks._remember_claimed_path("new/session", "src/a.py")
        claude_hooks._legacy_session_state_path().write_text(
            json.dumps({"claim_ids": ["old-1", "old-2"]})
        )
        claude_hooks._legacy_session_state_path().chmod(0o600)
        client = _FakeHookClient()

        assert claude_hooks.cmd_sessionend({}, client, {"session_id": "new/session"}) == 0

        assert client.posts == [
            ("/sessions/new%2Fsession/release", {}),
            ("/claims/release", {"claim_ids": ["old-1", "old-2"]}),
        ]
        assert claude_hooks._session_state_path("new/session").exists()
        assert claude_hooks._session_is_ended("new/session")
        assert not claude_hooks._legacy_session_state_path().exists()

    def test_legacy_drain_never_unlinks_a_recreated_writer_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-race")
        legacy = claude_hooks._legacy_session_state_path()
        legacy.write_text(json.dumps({"claim_ids": ["old-1"]}))
        legacy.chmod(0o600)

        class RecreatingClient(_FakeHookClient):
            def post(self, path: str, body: dict) -> dict:
                result = super().post(path, body)
                if path == "/claims/release" and body["claim_ids"] == ["old-1"]:
                    legacy.write_text(json.dumps({"claim_ids": ["new-2"]}))
                    legacy.chmod(0o600)
                return result

        client = RecreatingClient()
        assert claude_hooks.cmd_sessionend({}, client, {}) == 0
        releases = [
            body["claim_ids"]
            for path, body in client.posts
            if path == "/claims/release"
        ]
        assert releases == [["old-1"], ["new-2"]]
        assert not legacy.exists()

    def test_legacy_drain_waits_for_writer_on_renamed_inode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-open-writer")
        legacy = claude_hooks._legacy_session_state_path()
        legacy.write_text(json.dumps({"claim_ids": ["initial"]}))
        legacy.chmod(0o600)
        truncated = threading.Event()
        finish_write = threading.Event()

        def legacy_writer() -> None:
            with legacy.open("w") as handle:
                os.chmod(legacy, 0o600)
                truncated.set()
                assert finish_write.wait(timeout=2)
                json.dump({"claim_ids": ["written-after-rename"]}, handle)
                handle.flush()
                os.fsync(handle.fileno())

        writer = threading.Thread(target=legacy_writer)
        writer.start()
        assert truncated.wait(timeout=2)
        client = _FakeHookClient()
        result: list[int] = []
        ending = threading.Thread(
            target=lambda: result.append(claude_hooks.cmd_sessionend({}, client, {}))
        )
        ending.start()

        deadline = time.monotonic() + 2
        while not claude_hooks._pending_legacy_snapshots():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        finish_write.set()
        writer.join(timeout=2)
        ending.join(timeout=2)

        assert result == [0]
        assert (
            "/claims/release",
            {"claim_ids": ["written-after-rename"]},
        ) in client.posts
        assert claude_hooks._pending_legacy_snapshots() == []

    def test_sessionend_budget_retains_legacy_work_instead_of_timing_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-budget")
        clock = [0.0]
        monkeypatch.setattr(claude_hooks.time, "monotonic", lambda: clock[0])
        legacy = claude_hooks._legacy_session_state_path()
        legacy.write_text(json.dumps({"claim_ids": ["keep-for-retry"]}))
        legacy.chmod(0o600)

        class TimedClient(_FakeHookClient):
            def post(self, path: str, body: dict) -> dict:
                clock[0] += claude_hooks._TIMEOUT_S
                return super().post(path, body)

        def consume_lock_budget(_sid: str, _repo=None):
            clock[0] += claude_hooks._LOCK_WAIT_S
            return None

        monkeypatch.setattr(
            claude_hooks, "_acquire_session_lock", consume_lock_budget
        )
        client = TimedClient()
        assert (
            claude_hooks.cmd_sessionend(
                {}, client, {"session_id": "budget-session"}
            )
            == 0
        )
        assert clock[0] <= claude_hooks._SESSIONEND_BUDGET_S
        assert legacy.exists()
        assert [path for path, _ in client.posts] == [
            "/sessions/budget-session/release"
        ]

    def test_failed_session_release_keeps_state_for_diagnosis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        claude_hooks._remember_claimed_path("session-1", "src/a.py")
        client = _FakeHookClient(fail_paths={"/sessions/session-1/release"})

        assert claude_hooks.cmd_sessionend({}, client, {"session_id": "session-1"}) == 0
        assert claude_hooks._session_state_path("session-1").exists()

    def test_server_release_survives_local_terminal_marker_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(
            claude_hooks, "_mark_session_ended", lambda _sid, _repo=None: False
        )
        client = _FakeHookClient()

        assert claude_hooks.cmd_sessionend({}, client, {"session_id": "session-1"}) == 0
        assert any(path.startswith("/sessions/") for path, _ in client.posts)
        assert "session-1" in client.closed_sessions

    def test_sessionend_before_late_pretool_leaves_no_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "ended-session",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _FakeHookClient()

        assert claude_hooks.cmd_sessionend({}, client, payload) == 0
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 2
        assert "ended-session" in client.closed_sessions

        # A resumed Claude session gets SessionStart with the same id. The
        # server reopens authoritatively, then the local cache follows.
        assert claude_hooks.cmd_sessionstart({}, client, payload) == 0
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert "ended-session" not in client.closed_sessions
        assert len([path for path, _ in client.posts if path == "/claims"]) == 2

    def test_fresh_cache_still_checks_remote_terminal_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "remote-close",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _FakeHookClient()

        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        client.closed_sessions.add("remote-close")
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 2
        assert len([path for path, _ in client.posts if path == "/claims"]) == 1

    def test_sessionend_waits_for_inflight_pretool_before_release(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "agent.py"
        target.write_text("pass\n")
        payload = {
            "session_id": "overlap-session",
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
        }
        client = _BlockingHookClient()
        results: list[int] = []

        edit = threading.Thread(
            target=lambda: results.append(claude_hooks.cmd_pretool({}, client, tmp_path, payload))
        )
        ending = threading.Thread(
            target=lambda: results.append(claude_hooks.cmd_sessionend({}, client, payload))
        )
        edit.start()
        assert client.entered_claim.wait(timeout=2)
        ending.start()
        time.sleep(0.05)
        client.release_claim.set()
        edit.join(timeout=2)
        ending.join(timeout=2)

        assert sorted(results) == [0, 2]
        assert [path for path, _ in client.posts] == [
            "/sessions/overlap-session/release",
            "/claims",
        ]
        assert claude_hooks._session_is_ended("overlap-session")
        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 2
        assert len([path for path, _ in client.posts if path == "/claims"]) == 2

    def test_kernel_lock_never_allows_stale_takeover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(claude_hooks, "_LOCK_WAIT_S", 0.05)
        first = claude_hooks._acquire_session_lock("lock-session")
        assert first is not None
        assert claude_hooks._acquire_session_lock("lock-session") is None

        claude_hooks._release_session_lock(first)
        second = claude_hooks._acquire_session_lock("lock-session")
        assert second is not None
        claude_hooks._release_session_lock(second)

    def test_kernel_lock_refuses_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        victim = tmp_path / "victim"
        victim.write_text("do-not-truncate")
        lock_path = claude_hooks._session_lock_path("link-session")
        lock_path.symlink_to(victim)

        assert claude_hooks._acquire_session_lock("link-session") is None
        assert victim.read_text() == "do-not-truncate"

    def test_missing_session_id_enforces_conflicts_without_auto_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(claude_hooks.tempfile, "gettempdir", lambda: str(tmp_path))
        target = tmp_path / "legacy.py"
        target.write_text("pass\n")
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target)},
        }
        client = _FakeHookClient()

        assert claude_hooks.cmd_pretool({}, client, tmp_path, payload) == 0
        assert not any(path == "/claims" for path, _ in client.posts)

        # Upgrade cleanup remains available for claim IDs written by v0.48.
        claude_hooks._legacy_session_state_path().write_text(
            json.dumps({"claim_ids": ["legacy-1"]})
        )
        claude_hooks._legacy_session_state_path().chmod(0o600)
        assert claude_hooks.cmd_sessionend({}, client, {}) == 0
        assert ("/claims/release", {"claim_ids": ["legacy-1"]}) in client.posts


class TestClaudeSettingsMerge:
    def test_fresh_install_writes_all_three_hooks(self, tmp_path: Path):
        path = tmp_path / ".claude" / "settings.json"
        assert _update_claude_settings(path)
        settings = json.loads(path.read_text())
        assert set(settings["hooks"]) == {"SessionStart", "PreToolUse", "SessionEnd"}
        pre = settings["hooks"]["PreToolUse"][0]
        assert pre["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
        assert pre["hooks"][0]["command"] == "coord-claude-hook pretool"
        end = settings["hooks"]["SessionEnd"][0]["hooks"][0]
        assert end["async"] is True
        assert end["timeout"] == 15

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
        commands = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
        assert "mylint" in commands
        assert "coord-claude-hook pretool" in commands
        # Second run: no changes.
        assert not _update_claude_settings(path)

    def test_upgrade_reconciles_existing_sessionend_options(self, tmp_path: Path):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "coord-claude-hook sessionend",
                                        "timeout": 1,
                                        "custom": "preserved",
                                    },
                                    {"type": "command", "command": "other-cleanup"},
                                ]
                            }
                        ]
                    }
                }
            )
        )

        assert _update_claude_settings(path)
        settings = json.loads(path.read_text())
        group = settings["hooks"]["SessionEnd"][0]
        coord_hook, foreign_hook = group["hooks"]
        assert coord_hook == {
            "type": "command",
            "command": "coord-claude-hook sessionend",
            "timeout": 15,
            "custom": "preserved",
            "async": True,
        }
        assert foreign_hook == {"type": "command", "command": "other-cleanup"}
        assert not _update_claude_settings(path)

    def test_compound_or_wrong_subcommand_is_not_rewritten(self, tmp_path: Path):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        compound = "coord-claude-hook sessionend && echo foreign"
        wrong_event = "coord-claude-hook sessionend"
        path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": compound,
                                        "timeout": 1,
                                    }
                                ]
                            }
                        ],
                        "PreToolUse": [{"hooks": [{"type": "command", "command": wrong_event}]}],
                    }
                }
            )
        )

        assert _update_claude_settings(path)
        settings = json.loads(path.read_text())
        end_hooks = [hook for group in settings["hooks"]["SessionEnd"] for hook in group["hooks"]]
        assert {hook["command"] for hook in end_hooks} == {
            compound,
            "coord-claude-hook sessionend",
        }
        assert next(hook for hook in end_hooks if hook["command"] == compound) == {
            "type": "command",
            "command": compound,
            "timeout": 1,
        }
        pre_commands = [
            hook["command"] for group in settings["hooks"]["PreToolUse"] for hook in group["hooks"]
        ]
        assert pre_commands.count(wrong_event) == 1
        assert "coord-claude-hook pretool" in pre_commands

    def test_invalid_managed_event_shape_is_left_alone(self, tmp_path: Path):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        original = {"hooks": {"SessionEnd": {"not": "a-list"}}}
        path.write_text(json.dumps(original))

        assert not _update_claude_settings(path)
        assert json.loads(path.read_text()) == original

    def test_invalid_json_is_left_alone(self, tmp_path: Path, capsys):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        assert not _update_claude_settings(path)
        assert path.read_text() == "{broken"
