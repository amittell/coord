from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coordination import deps
from coordination.config import Settings
from coordination.dashboard import (
    _bucket,
    _esc,
    _recent_activity,
    _remaining,
    render_dashboard,
)
from coordination.db import Database
from coordination.service import CoordinationService


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def svc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CoordinationService]:
    """Fresh CoordinationService backed by a temp sqlite DB, wired into
    coordination.deps.get_service for the duration of the test.
    """
    db_path = tmp_path / "dashboard.sqlite"
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")

    database = Database(db_path)
    await database.init()

    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
        repo_scope=None,
    )
    service = CoordinationService(db=database, settings=settings)

    deps.get_service.cache_clear()
    monkeypatch.setattr(deps, "get_service", lambda: service)
    # render_dashboard() imports get_service at module import time, so also
    # patch the binding inside coordination.dashboard.
    import coordination.dashboard as dashboard_mod

    monkeypatch.setattr(dashboard_mod, "get_service", lambda: service)

    yield service

    # No explicit teardown: monkeypatch restores the original
    # lru_cache-wrapped get_service automatically. The pre-yield
    # cache_clear() already cleared it for this test's lifetime.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _insert_claim_raw(
    svc: CoordinationService,
    *,
    engineer: str,
    pattern: str,
    created_at: str,
    expires_at: str,
    claim_type: str = "file",
    severity: str = "soft",
    description: str | None = None,
    branch: str | None = None,
    released_at: str | None = None,
    claim_id: str = "",
) -> str:
    """Insert a claim with a fully-controlled created_at via raw SQL.

    Database.insert_claims_batch always stamps created_at = now, so it can't
    be used to seed the 24h activity window with old claims.
    """
    import aiosqlite
    from uuid import uuid4

    cid = claim_id or str(uuid4())
    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            """
            INSERT INTO claims (
                id, engineer, branch, description, claim_type, pattern,
                severity, created_at, expires_at, released_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                engineer,
                branch,
                description,
                claim_type,
                pattern,
                severity,
                created_at,
                expires_at,
                released_at,
            ),
        )
        await conn.commit()
    return cid


async def _insert_conflict_raw(
    svc: CoordinationService,
    *,
    claim_id: str,
    attempted_by: str,
    attempted_pattern: str,
    created_at: str,
    resolution: str | None = None,
) -> None:
    """Insert a conflict_log row with a controlled created_at."""
    import aiosqlite
    from uuid import uuid4

    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            """
            INSERT INTO conflict_log (
                id, claim_id, attempted_by, attempted_pattern, resolution, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), claim_id, attempted_by, attempted_pattern, resolution, created_at),
        )
        await conn.commit()


async def _insert_claim(
    svc: CoordinationService,
    *,
    engineer: str,
    pattern: str,
    description: str | None = None,
    severity: str = "soft",
    expires_at: str | None = None,
    claim_id: str = "",
) -> str:
    """Insert a single claim directly via Database.insert_claims_batch.

    Bypasses CoordinationService.create_claims so we can precisely control
    the expiry timestamp (past, near-future, etc.) without tripping ownership
    or scope validation.
    """
    from uuid import uuid4

    cid = claim_id or str(uuid4())
    exp = expires_at or _iso(datetime.now(UTC) + timedelta(hours=4))
    await svc.db.insert_claims_batch(
        engineer=engineer,
        branch=None,
        description=description,
        items=[(cid, "file", pattern, severity, exp)],
    )
    return cid


# ---------------------------------------------------------------------------
# render_dashboard - integration style
# ---------------------------------------------------------------------------


async def test_empty_state_renders_placeholder_rows(svc: CoordinationService) -> None:
    html_out = await render_dashboard()
    assert "No active claims" in html_out
    assert "No recent conflict attempts logged" in html_out
    assert "No claim history yet" in html_out


async def test_active_claims_appear_in_table(svc: CoordinationService) -> None:
    exp = _iso(datetime.now(UTC) + timedelta(hours=4))
    await _insert_claim(
        svc,
        engineer="alice",
        pattern="src/auth/**",
        description="auth refactor",
        expires_at=exp,
    )
    html_out = await render_dashboard()
    assert "alice" in html_out
    assert "src/auth/**" in html_out
    assert exp in html_out
    assert "auth refactor" in html_out


async def test_html_escapes_claim_fields(svc: CoordinationService) -> None:
    hostile = "<script>alert(1)</script>"
    await _insert_claim(svc, engineer="eve", pattern=hostile)
    html_out = await render_dashboard()
    # The literal hostile string must never appear verbatim.
    assert hostile not in html_out
    # The escaped form must appear instead.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out


async def test_heatmap_shows_prefix_bucket_counts(svc: CoordinationService) -> None:
    await _insert_claim(svc, engineer="a", pattern="src/auth/login.ts")
    await _insert_claim(svc, engineer="b", pattern="src/ui/button.ts")
    await _insert_claim(svc, engineer="c", pattern="src/api/routes.ts")
    await _insert_claim(svc, engineer="d", pattern="tests/unit/foo.test.ts")

    html_out = await render_dashboard()

    # Extract the heatmap table body.
    start = html_out.index("Module heatmap")
    end = html_out.index("Recent conflicts")
    heat = html_out[start:end]

    assert "<code>src</code>" in heat
    assert "<code>tests</code>" in heat
    # The src bucket has three claims, tests has one. The count cell is
    # rendered as "<td>N</td>" directly after the prefix cell.
    assert "<code>src</code></td><td>3</td>" in heat
    assert "<code>tests</code></td><td>1</td>" in heat


async def test_remaining_shows_time_until_expiry(svc: CoordinationService) -> None:
    exp = _iso(datetime.now(UTC) + timedelta(hours=2, minutes=5))
    await _insert_claim(
        svc, engineer="alice", pattern="src/auth/**", expires_at=exp
    )
    html_out = await render_dashboard()
    # Time-left cell is inside <strong>...</strong>. The value should be
    # "2h ..." or possibly "1h ..." if the clock drifted during the call.
    # Accept either to keep this stable against scheduling jitter.
    assert ("<strong>2h" in html_out) or ("<strong>1h" in html_out)


async def test_remaining_shows_expired_for_past_expiry(svc: CoordinationService) -> None:
    """Expired claims are filtered out of list_active_claims, but the claim
    timeline (list_recent_claims) still shows them. Their expires_at column
    contains the past ISO timestamp, and _remaining itself maps past expiry
    to 'expired' - exercise that via the helper directly plus confirm the
    timeline picks the claim up at all.
    """
    past = _iso(datetime.now(UTC) - timedelta(hours=1))
    await _insert_claim(
        svc, engineer="alice", pattern="src/auth/**", expires_at=past
    )
    html_out = await render_dashboard()
    # Claim timeline shows the expired claim (no "No claim history yet").
    assert "No claim history yet" not in html_out
    assert "alice" in html_out
    # Active-claims table does not use it: the placeholder stays.
    assert "No active claims" in html_out
    # Helper contract: expired mapping.
    assert _remaining(past) == "expired"


async def test_recent_conflicts_appear_in_table(svc: CoordinationService) -> None:
    cid = await _insert_claim(svc, engineer="alice", pattern="src/auth/**")
    await svc.db.log_conflict(
        claim_id=cid,
        attempted_by="bob",
        attempted_pattern="src/auth/login.ts",
        resolution="narrow_claim",
    )
    html_out = await render_dashboard()
    assert "No recent conflict attempts logged" not in html_out
    assert "bob" in html_out
    assert "src/auth/login.ts" in html_out
    assert "narrow_claim" in html_out


async def test_dashboard_returns_html_content_type_expected_shape(
    svc: CoordinationService,
) -> None:
    html_out = await render_dashboard()
    assert html_out.startswith("<!DOCTYPE html>")
    assert "<title>Coordination Dashboard</title>" in html_out
    assert html_out.rstrip().endswith("</html>")


# ---------------------------------------------------------------------------
# Helper unit tests - no DB required
# ---------------------------------------------------------------------------


def test_bucket_returns_first_path_segment() -> None:
    assert _bucket("src/auth/login.ts") == "src"
    assert _bucket("") == "(root)"
    assert _bucket("**/*") == "**"
    assert _bucket(None) == "(root)"


def test_esc_escapes_html_and_handles_none() -> None:
    assert _esc("<b>") == "&lt;b&gt;"
    assert _esc(None) == ""


def test_remaining_returns_question_mark_for_none() -> None:
    assert _remaining(None) == "?"


def test_remaining_returns_question_mark_for_invalid_iso_string() -> None:
    assert _remaining("not-a-timestamp") == "?"


def test_remaining_formats_seconds_under_a_minute() -> None:
    # 30 seconds out - the format is "<N>s" with no minute/hour component.
    exp = _iso(datetime.now(UTC) + timedelta(seconds=30))
    out = _remaining(exp)
    assert out.endswith("s")
    assert "h" not in out
    assert "m" not in out


def test_remaining_formats_minutes_under_an_hour() -> None:
    # 30 minutes out - format is "<M>m <S>s" with no hour component.
    exp = _iso(datetime.now(UTC) + timedelta(minutes=30))
    out = _remaining(exp)
    assert "m" in out
    assert "h" not in out


# ---------------------------------------------------------------------------
# _recent_activity - pure helper
# ---------------------------------------------------------------------------


def _claim(*, engineer: str, pattern: str, created_at: str) -> dict[str, str]:
    return {"engineer": engineer, "pattern": pattern, "created_at": created_at}


def _conflict(*, attempted_by: str, created_at: str) -> dict[str, str]:
    return {"attempted_by": attempted_by, "created_at": created_at}


def test_recent_activity_counts_claims_and_conflicts_in_window() -> None:
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    in_window = _iso(now - timedelta(hours=2))
    out_of_window = _iso(now - timedelta(hours=30))

    summary = _recent_activity(
        claims=[
            _claim(engineer="alice", pattern="src/auth.py", created_at=in_window),
            _claim(engineer="bob", pattern="src/auth.py", created_at=in_window),
            _claim(engineer="charlie", pattern="src/auth.py", created_at=out_of_window),
        ],
        conflicts=[
            _conflict(attempted_by="bob", created_at=in_window),
            _conflict(attempted_by="charlie", created_at=out_of_window),
        ],
        now=now,
    )
    assert summary["claims"] == 2
    assert summary["conflicts"] == 1
    assert summary["engineers"] == 2  # alice, bob


def test_recent_activity_empty_when_nothing_in_window() -> None:
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    old = _iso(now - timedelta(days=2))
    summary = _recent_activity(
        claims=[_claim(engineer="alice", pattern="src/a.py", created_at=old)],
        conflicts=[_conflict(attempted_by="bob", created_at=old)],
        now=now,
    )
    assert summary["claims"] == 0
    assert summary["conflicts"] == 0
    assert summary["engineers"] == 0
    # top_modules is a list and should be empty when there are no in-window claims
    assert summary["top_modules"] == []


def test_recent_activity_top_modules_ranked_by_count() -> None:
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    t = _iso(now - timedelta(hours=1))

    summary = _recent_activity(
        claims=[
            _claim(engineer="a", pattern="services/x.py", created_at=t),
            _claim(engineer="b", pattern="services/y.py", created_at=t),
            _claim(engineer="c", pattern="services/z.py", created_at=t),
            _claim(engineer="a", pattern="data_models/foo.py", created_at=t),
            _claim(engineer="d", pattern="tests/foo.py", created_at=t),
        ],
        conflicts=[],
        now=now,
    )
    # services has 3, data_models has 1, tests has 1. services should rank first.
    modules = summary["top_modules"]
    assert modules[0]["prefix"] == "services"
    assert modules[0]["count"] == 3
    # The remaining two are tied at 1; ordering between them isn't load-bearing,
    # but both must be present.
    remaining = {m["prefix"] for m in modules[1:]}
    assert remaining == {"data_models", "tests"}


def test_recent_activity_includes_distinct_engineers_per_module() -> None:
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    t = _iso(now - timedelta(hours=1))
    summary = _recent_activity(
        claims=[
            _claim(engineer="alice", pattern="services/x.py", created_at=t),
            _claim(engineer="bob", pattern="services/y.py", created_at=t),
            # Same engineer twice in one module should not double-count engineers.
            _claim(engineer="alice", pattern="services/z.py", created_at=t),
        ],
        conflicts=[],
        now=now,
    )
    services = next(m for m in summary["top_modules"] if m["prefix"] == "services")
    assert services["count"] == 3
    assert set(services["engineers"]) == {"alice", "bob"}


def test_recent_activity_skips_rows_with_invalid_timestamps() -> None:
    now = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    in_window = _iso(now - timedelta(hours=1))
    summary = _recent_activity(
        claims=[
            _claim(engineer="alice", pattern="src/a.py", created_at=in_window),
            # Bogus timestamp: must not crash and must not count.
            _claim(engineer="bob", pattern="src/b.py", created_at="not-a-date"),
            _claim(engineer="carol", pattern="src/c.py", created_at=""),
        ],
        conflicts=[
            _conflict(attempted_by="x", created_at="also-bogus"),
        ],
        now=now,
    )
    assert summary["claims"] == 1
    assert summary["conflicts"] == 0
    assert summary["engineers"] == 1


# ---------------------------------------------------------------------------
# Recent activity panel - rendered HTML
# ---------------------------------------------------------------------------


async def test_dashboard_renders_recent_activity_panel(svc: CoordinationService) -> None:
    """Even with no active claims, the dashboard summarises the last 24h."""
    now = datetime.now(UTC)
    fresh = _iso(now - timedelta(hours=2))
    expired = _iso(now - timedelta(hours=1))

    cid = await _insert_claim_raw(
        svc,
        engineer="l7-mitre-thread-agent",
        pattern="services/foo.py",
        created_at=fresh,
        expires_at=expired,
        released_at=expired,
    )
    await _insert_claim_raw(
        svc,
        engineer="l7-host-tokens-agent",
        pattern="services/bar.py",
        created_at=fresh,
        expires_at=expired,
        released_at=expired,
    )
    await _insert_conflict_raw(
        svc,
        claim_id=cid,
        attempted_by="l7-host-tokens-agent",
        attempted_pattern="services/foo.py",
        created_at=fresh,
    )

    html_out = await render_dashboard()
    # The new section appears with a recognisable header.
    assert "Recent activity (last 24h)" in html_out
    # Two claims, one conflict, two distinct engineers.
    # Cells live in <td>N</td>; check raw substrings to keep the test
    # resilient to minor formatting tweaks.
    activity_section_start = html_out.index("Recent activity (last 24h)")
    activity_section_end = html_out.index("Active claims")
    activity = html_out[activity_section_start:activity_section_end]
    assert "<td>2</td>" in activity  # claims and engineers both = 2
    assert "<td>1</td>" in activity  # conflicts = 1
    # Top module is "services" with 2 claims from 2 engineers.
    assert "<code>services</code>" in activity
    assert "l7-mitre-thread-agent" in activity
    assert "l7-host-tokens-agent" in activity


async def test_dashboard_recent_activity_renders_zero_state(svc: CoordinationService) -> None:
    """Empty database: panel still renders with zeros (does not disappear)."""
    html_out = await render_dashboard()
    assert "Recent activity (last 24h)" in html_out
    activity_start = html_out.index("Recent activity (last 24h)")
    activity_end = html_out.index("Active claims")
    activity = html_out[activity_start:activity_end]
    # Every numeric stat reads zero.
    assert "<td>0</td>" in activity
    # And there's a clear "no recent activity" message in the modules block.
    assert "No activity in the last 24h" in activity


async def test_dashboard_renders_repos_panel(svc: CoordinationService) -> None:
    """The Repositories panel summarises distinct repos using the service."""
    now = datetime.now(UTC)
    fresh = _iso(now - timedelta(hours=2))
    expires = _iso(now + timedelta(hours=2))

    # Two repos, with multiple claims and engineers each.
    await _insert_claim_raw(
        svc,
        engineer="alice",
        pattern="services/x.py",
        created_at=fresh,
        expires_at=expires,
    )
    # Tag this raw insert with a repo by updating the row directly.
    import aiosqlite

    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            "UPDATE claims SET repo = ? WHERE engineer = 'alice'",
            ("amittell/bastionx",),
        )
        await conn.commit()

    cid = await _insert_claim_raw(
        svc,
        engineer="bob",
        pattern="coordination/foo.py",
        created_at=fresh,
        expires_at=expires,
    )
    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            "UPDATE claims SET repo = ? WHERE id = ?",
            ("amittell/coord", cid),
        )
        await conn.commit()

    html_out = await render_dashboard()
    assert "Repositories" in html_out
    repos_start = html_out.index("Repositories")
    repos_end = html_out.index("Recent activity (last 24h)")
    section = html_out[repos_start:repos_end]
    assert "amittell/bastionx" in section
    assert "amittell/coord" in section


async def test_dashboard_repos_panel_zero_state(svc: CoordinationService) -> None:
    """Empty database renders the panel with a placeholder."""
    html_out = await render_dashboard()
    assert "Repositories" in html_out
    repos_start = html_out.index("Repositories")
    repos_end = html_out.index("Recent activity (last 24h)")
    section = html_out[repos_start:repos_end]
    assert "No repos using this service yet" in section


async def test_dashboard_recent_activity_excludes_old_claims(svc: CoordinationService) -> None:
    """Claims older than the window must not appear in the activity panel."""
    now = datetime.now(UTC)
    very_old = _iso(now - timedelta(days=3))
    await _insert_claim_raw(
        svc,
        engineer="ancient-agent",
        pattern="services/dusty.py",
        created_at=very_old,
        expires_at=very_old,
        released_at=very_old,
    )
    html_out = await render_dashboard()
    activity_start = html_out.index("Recent activity (last 24h)")
    activity_end = html_out.index("Active claims")
    activity = html_out[activity_start:activity_end]
    # The old engineer must not appear in the recent activity panel
    # (the timeline section below may still show them).
    assert "ancient-agent" not in activity
    assert "<td>0</td>" in activity
