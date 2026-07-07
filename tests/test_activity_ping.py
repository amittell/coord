"""Unit tests for v0.44 activity-ping coalescing (service._maybe_touch).

The ping is a liveness write issued on most reads; coalescing it is the first
half of the SQLite write-scaling work. These tests pin the contract at the
service layer with a stub store, so they are backend-agnostic:

1. Within the interval, repeat pings for the same (session, repo) coalesce to
   one write; a different session or repo is not suppressed.
2. After the interval elapses, the ping writes again.
3. A failed write rolls the stamp back so the next call retries immediately
   instead of being suppressed for a full interval.
4. The effective interval is clamped to idle_timeout_sec / 2 so coalescing can
   never out-pace idle expiry (a misconfiguration degrades to more frequent
   pings, not false idle expiry).
5. interval <= 0 preserves the legacy write-every-read behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import coordination.service as service_mod
from coordination.config import Settings
from coordination.service import CoordinationService


@dataclass
class _StubDb:
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    fail_next: bool = False

    async def touch_session_activity(self, session_id: str, *, repo=None) -> int:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("boom")
        self.calls.append((session_id, repo))
        return 1


def _svc(interval: int, idle: int = 1800) -> CoordinationService:
    settings = Settings(
        activity_ping_min_interval_sec=interval,
        idle_timeout_sec=idle,
        _env_file=None,
    )
    return CoordinationService(db=_StubDb(), settings=settings)  # type: ignore[arg-type]


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch):
    """Controllable monotonic clock for the service module."""
    now = [1000.0]
    monkeypatch.setattr(service_mod._time, "monotonic", lambda: now[0])
    return now


async def test_repeat_pings_coalesce_within_interval(clock) -> None:
    svc = _svc(interval=30)
    await svc._maybe_touch("sess-a", "org/repo")
    await svc._maybe_touch("sess-a", "org/repo")
    clock[0] += 29
    await svc._maybe_touch("sess-a", "org/repo")
    assert svc.db.calls == [("sess-a", "org/repo")]


async def test_distinct_session_or_repo_not_suppressed(clock) -> None:
    svc = _svc(interval=30)
    await svc._maybe_touch("sess-a", "org/repo")
    await svc._maybe_touch("sess-b", "org/repo")  # other session
    await svc._maybe_touch("sess-a", "org/other")  # other repo
    assert len(svc.db.calls) == 3


async def test_ping_writes_again_after_interval(clock) -> None:
    svc = _svc(interval=30)
    await svc._maybe_touch("sess-a", "org/repo")
    clock[0] += 31
    await svc._maybe_touch("sess-a", "org/repo")
    assert len(svc.db.calls) == 2


async def test_failed_ping_rolls_stamp_back(clock) -> None:
    svc = _svc(interval=30)
    svc.db.fail_next = True
    with pytest.raises(RuntimeError):
        await svc._maybe_touch("sess-a", "org/repo")
    # The failed attempt must not suppress the retry.
    await svc._maybe_touch("sess-a", "org/repo")
    assert svc.db.calls == [("sess-a", "org/repo")]


async def test_interval_clamped_to_half_idle_timeout(clock) -> None:
    # interval (3600) far exceeds idle_timeout (600) -> effective 300, so a
    # read-only session pings at least twice per idle window.
    svc = _svc(interval=3600, idle=600)
    await svc._maybe_touch("sess-a", "org/repo")
    clock[0] += 301
    await svc._maybe_touch("sess-a", "org/repo")
    assert len(svc.db.calls) == 2


async def test_zero_interval_writes_every_read(clock) -> None:
    svc = _svc(interval=0)
    await svc._maybe_touch("sess-a", "org/repo")
    await svc._maybe_touch("sess-a", "org/repo")
    assert len(svc.db.calls) == 2
