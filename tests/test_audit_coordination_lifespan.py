"""Audit: lifespan background-work plumbing.

- Leader lease: renewal moved to a dedicated heartbeat task with a TTL of
  ~3 heartbeats (about a minute), instead of a TTL derived from the
  slowest work-loop interval (~3 hours with defaults), and the lease is
  voluntarily released in the lifespan finally block so a rolling deploy
  hands leadership off immediately.
- Webhook outbox delivery loop starts when COORD_GITHUB_TOKEN is set even
  without COORD_WEBHOOK_URL (github rows are enqueued gated on the token
  alone, and the loop is the outbox's only drain).
- db.expire_stale_queue_entries is wired into the cleanup loop so
  orphaned waiting/in_progress queue rows converge instead of leaking
  against the queue caps forever.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import coordination.main as main_mod
from coordination.main import (
    LEADER_HEARTBEAT_INTERVAL_SEC,
    LEADER_LEASE_NAME,
    LEADER_LEASE_TTL_SEC,
    app,
)


@pytest.fixture()
def lifespan_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_DISABLE_BACKGROUND_CLEANUP", raising=False)
    monkeypatch.delenv("COORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("COORD_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps

    deps.get_service.cache_clear()
    yield monkeypatch
    deps.get_service.cache_clear()


def test_lease_ttl_is_heartbeat_derived_not_work_loop_derived() -> None:
    # The audited failure: TTL = max(loop intervals) * 3 + 5 = 10805s with
    # defaults, stalling failover for ~3 hours after a leader crash. The
    # heartbeat-derived TTL must stay in the about-a-minute range.
    assert LEADER_LEASE_TTL_SEC == LEADER_HEARTBEAT_INTERVAL_SEC * 3 + 5
    assert LEADER_LEASE_TTL_SEC <= 120


async def test_lifespan_acquires_and_releases_leader_lease(
    lifespan_env: pytest.MonkeyPatch,
) -> None:
    from coordination.deps import get_service

    svc = get_service()
    calls: list[tuple] = []

    async def rec_acquire(*, lease_name, holder_id, ttl_sec):
        calls.append(("acquire", lease_name, holder_id, ttl_sec))
        return True

    async def rec_release(*, lease_name, holder_id):
        calls.append(("release", lease_name, holder_id))
        return True

    lifespan_env.setattr(svc.db, "acquire_leader_lease", rec_acquire)
    lifespan_env.setattr(svc.db, "release_leader_lease", rec_release)

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)

    acquires = [c for c in calls if c[0] == "acquire"]
    releases = [c for c in calls if c[0] == "release"]
    assert acquires, "lifespan never acquired the leader lease"
    assert acquires[0][1] == LEADER_LEASE_NAME
    assert acquires[0][3] == LEADER_LEASE_TTL_SEC
    assert len(releases) == 1, "lease must be released exactly once on shutdown"
    assert releases[0][1] == LEADER_LEASE_NAME
    # The release names the same holder that acquired, so a row another
    # replica re-acquired in the meantime would be left untouched.
    assert releases[0][2] == acquires[0][2]


async def test_lease_heartbeat_renews_on_its_own_cadence(
    lifespan_env: pytest.MonkeyPatch,
) -> None:
    from coordination.deps import get_service

    svc = get_service()
    renewals = []

    async def rec_acquire(*, lease_name, holder_id, ttl_sec):
        renewals.append(holder_id)
        return True

    async def rec_release(*, lease_name, holder_id):
        return True

    lifespan_env.setattr(svc.db, "acquire_leader_lease", rec_acquire)
    lifespan_env.setattr(svc.db, "release_leader_lease", rec_release)
    # The heartbeat loop reads the module global on each tick; shrink it
    # so the test observes several renewals without waiting 20s.
    lifespan_env.setattr(main_mod, "LEADER_HEARTBEAT_INTERVAL_SEC", 0.01)

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.2)

    # Initial acquire + several heartbeat renewals, all for one holder id
    # (the lease is stable across renew ticks).
    assert len(renewals) >= 3
    assert len(set(renewals)) == 1


async def test_cleanup_loop_reaps_stale_queue_entries(
    lifespan_env: pytest.MonkeyPatch,
) -> None:
    from coordination.deps import get_service

    svc = get_service()
    called = asyncio.Event()

    async def rec_expire(now_iso=None):
        called.set()
        return 0

    lifespan_env.setattr(svc.db, "expire_stale_queue_entries", rec_expire)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(called.wait(), timeout=2.0)


async def test_delivery_loop_starts_with_github_token_alone(
    lifespan_env: pytest.MonkeyPatch,
) -> None:
    # COORD_GITHUB_TOKEN set, COORD_WEBHOOK_URL unset: a documented-valid
    # config that enqueues kind='github' outbox rows. The delivery loop
    # must start or those rows sit 'pending' forever.
    lifespan_env.setenv("COORD_GITHUB_TOKEN", "ghp_test")

    from coordination.deps import get_service

    svc = get_service()
    called = asyncio.Event()

    async def rec_deliver():
        called.set()
        return {"delivered": 0, "failed": 0, "exhausted": 0}

    lifespan_env.setattr(svc, "deliver_pending_webhooks", rec_deliver)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(called.wait(), timeout=2.0)


async def test_delivery_loop_not_started_when_neither_transport_configured(
    lifespan_env: pytest.MonkeyPatch,
) -> None:
    from coordination.deps import get_service

    svc = get_service()
    delivered = []

    async def rec_deliver():
        delivered.append(1)
        return {"delivered": 0, "failed": 0, "exhausted": 0}

    lifespan_env.setattr(svc, "deliver_pending_webhooks", rec_deliver)

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.1)

    assert delivered == []


async def test_sqlite_release_leader_lease_is_a_true_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordination.db import Database

    # The PostgreSQL matrix exports COORD_DATABASE_URL globally, while this
    # test deliberately pins the SQLite implementation's no-op contract.
    monkeypatch.delenv("COORD_DATABASE_URL", raising=False)
    db = Database(tmp_path / "lease.sqlite")
    assert (
        await db.release_leader_lease(
            lease_name=LEADER_LEASE_NAME, holder_id="whoever"
        )
        is True
    )
