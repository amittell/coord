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
    # Placeholders are lowercase since the dashboard moved to the
    # phosphor-terminal aesthetic (all-lowercase typography).
    assert "no active claims" in html_out.lower()
    assert "no recent conflict attempts logged" in html_out.lower()
    assert "no claim history yet" in html_out.lower()


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
    # The table now shows relative time-left ("3h ...") rather than the
    # absolute expires_at; the absolute is only on the title attribute.
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

    # Extract the heatmap table body. The new dashboard uses lowercase
    # section headers ("module heatmap" / "recent conflicts").
    start = html_out.index("module heatmap")
    end = html_out.index("recent conflicts")
    heat = html_out[start:end]

    # Patterns are now wrapped in <span class='pattern'> rather than <code>.
    assert "src</span>" in heat
    assert "tests</span>" in heat
    # The src bucket has three claims, tests has one. The numeric cell
    # carries class='num-col' for right-alignment.
    assert ">3<" in heat
    assert ">1<" in heat


async def test_remaining_shows_time_until_expiry(svc: CoordinationService) -> None:
    exp = _iso(datetime.now(UTC) + timedelta(hours=2, minutes=5))
    await _insert_claim(
        svc, engineer="alice", pattern="src/auth/**", expires_at=exp
    )
    html_out = await render_dashboard()
    # Time-left cell shows "2h ..." or "1h ..." (clock drift). The new
    # dashboard renders it as plain text in a <td>, no <strong> wrap.
    assert ">2h" in html_out or ">1h" in html_out


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
    assert "no claim history yet" not in html_out.lower()
    assert "alice" in html_out
    assert "no active claims" in html_out.lower()
    # Helper contract: expired mapping.
    assert _remaining(past) == "expired"


async def test_recent_conflicts_appear_in_table(svc: CoordinationService) -> None:
    """The conflict log surfaces the requester's pattern. Resolution is
    derived from the claim state (covered by the dedicated resolution
    tests); this test only checks that the conflict row reaches the
    page at all."""
    cid = await _insert_claim(svc, engineer="alice", pattern="src/auth/**")
    await svc.db.log_conflict(
        claim_id=cid,
        attempted_by="bob",
        attempted_pattern="src/auth/login.ts",
        resolution=None,
    )
    html_out = await render_dashboard()
    assert "no recent conflict attempts logged" not in html_out.lower()
    assert "bob" in html_out
    assert "src/auth/login.ts" in html_out
    # Holder-engineer column was added in v0.8 -- conflict rows now
    # tell you who was holding the conflicting claim.
    assert "alice" in html_out


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
    # v0.8 moved the headline numbers into a top-of-page stats block
    # (4 big numbers) and demoted the breakdown to a dedicated "top
    # modules · 24h" panel. The numbers and the modules are still
    # there -- the layout just changed.
    assert "active claims" in html_out  # stats block label
    assert "conflicts 24h" in html_out

    stats_start = html_out.index('class="stats"')
    # v0.36 reordered the hero row and inserted the "needs attention"
    # rollup immediately after it; slice up to that banner.
    stats_end = html_out.index('class="attention')
    stats = html_out[stats_start:stats_end]
    # 2 claims created in the window, 2 distinct engineers, 1 conflict.
    # The delta text reads "<N> engineers active 24h" / "<N> created 24h".
    assert "2 engineers active 24h" in stats
    assert "2 created 24h" in stats
    # v0.36: the 24h conflict count moved from an amber hero number to the
    # delta line under the "blocked now" stat.
    assert "1 conflicts 24h" in stats

    # Top-modules panel renders as a <ul class="top-modules">. Slice
    # from the <ul> tag itself, not the literal class name (which also
    # appears in the inlined CSS earlier on the page).
    modules_start = html_out.index('<ul class="top-modules"')
    modules_end = html_out.index("</ul>", modules_start)
    modules = html_out[modules_start:modules_end]
    assert "services" in modules
    assert "l7-mitre-thread-agent" in modules
    assert "l7-host-tokens-agent" in modules


async def test_dashboard_recent_activity_renders_zero_state(svc: CoordinationService) -> None:
    """Empty database: stats block + top-modules panel still render
    with zeros / placeholder (the page does not disappear)."""
    html_out = await render_dashboard()
    # Stats block always renders.
    assert 'class="stats"' in html_out
    # Top-modules panel has a "no activity" placeholder when empty.
    assert "no activity in the last 24h" in html_out.lower()


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
            ("example-org/bastionx",),
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
    # Lowercase "repositories" header in the new aesthetic.
    assert "repositories" in html_out.lower()
    repos_start = html_out.lower().index("repositories")
    # The next panel is "top modules · 24h"; use that as the slice end.
    repos_end = html_out.lower().index("top modules", repos_start)
    section = html_out[repos_start:repos_end]
    assert "example-org/bastionx" in section
    assert "amittell/coord" in section


async def test_dashboard_repos_panel_zero_state(svc: CoordinationService) -> None:
    """Empty database renders the panel with a placeholder."""
    html_out = await render_dashboard()
    assert "repositories" in html_out.lower()
    repos_start = html_out.lower().index("repositories")
    repos_end = html_out.lower().index("top modules", repos_start)
    section = html_out[repos_start:repos_end]
    assert "no repos using this service yet" in section.lower()


async def test_dashboard_recent_activity_excludes_old_claims(svc: CoordinationService) -> None:
    """Claims older than the window must not appear in the stats block
    or the top-modules panel."""
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
    # Stats block + top-modules region: anything from > 24h ago must
    # not surface there.
    stats_start = html_out.index('class="stats"')
    # v0.36: slice the hero stats block (it now ends at the "needs
    # attention" rollup that follows it).
    activity_end = html_out.index('class="attention')
    activity_region = html_out[stats_start:activity_end]
    assert "ancient-agent" not in activity_region
    # The big claims-24h number must be 0 (rendered as ">0<").
    assert ">0<" in activity_region


# ---------------------------------------------------------------------------
# Conflict resolution column (v0.8.0)
# ---------------------------------------------------------------------------
#
# The pre-v0.8 dashboard had a "Resolution" column that was always empty
# because nothing in the codebase ever set conflict_log.resolution to a
# non-NULL value. v0.8 derives a useful resolution at render time by
# joining the conflict to its claim and reading the claim's current state
# (still held / voluntarily released / TTL-expired / idle-released /
# missing).


def test_resolution_blocked_when_claim_still_held() -> None:
    """If the conflicting claim is still active (released_at IS NULL,
    expires_at in the future), the conflict has not been resolved --
    the requester is still blocked."""
    from coordination.dashboard import _resolution_for_conflict

    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    claim = {
        "released_at": None,
        "expires_at": "2099-01-01T00:00:00Z",
        "last_activity": None,
    }
    status, _label = _resolution_for_conflict(
        conflict={}, claim=claim, idle_timeout_sec=1800, now=now
    )
    assert status == "blocked"


def test_resolution_released_when_voluntarily_released() -> None:
    """released_at set, well before TTL expiry, no idle reason.
    Holder responded to the conflict (or `release_session` was called)."""
    from coordination.dashboard import _resolution_for_conflict

    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    claim = {
        # Released 30s ago, well before TTL.
        "released_at": "2026-05-02T11:59:30Z",
        "expires_at": "2026-05-02T16:00:00Z",
        "last_activity": "2026-05-02T11:59:00Z",
    }
    status, _ = _resolution_for_conflict(
        conflict={}, claim=claim, idle_timeout_sec=1800, now=now
    )
    assert status == "released"


def test_resolution_ttl_expired_when_release_at_or_after_expires() -> None:
    """If released_at >= expires_at, the cleanup sweep TTL'd the claim.
    Different signal than a voluntary release: the holder didn't act."""
    from coordination.dashboard import _resolution_for_conflict

    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    claim = {
        "released_at": "2026-05-02T10:00:30Z",
        "expires_at": "2026-05-02T10:00:00Z",  # released_at past TTL
        "last_activity": "2026-05-02T09:30:00Z",
    }
    status, _ = _resolution_for_conflict(
        conflict={}, claim=claim, idle_timeout_sec=1800, now=now
    )
    assert status == "ttl-expired"


def test_resolution_idle_released_when_session_idle() -> None:
    """released_at occurred about idle_timeout_sec after last_activity,
    well before TTL. Activity-based auto-expiration kicked in -- holder
    walked away. Worth surfacing distinctly so an operator can see if
    the timeout is too aggressive."""
    from coordination.dashboard import _resolution_for_conflict

    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    claim = {
        # last_activity at 10:00; released at 10:30 (= idle_timeout_sec=1800);
        # expires_at far in the future.
        "released_at": "2026-05-02T10:30:00Z",
        "expires_at": "2026-05-02T20:00:00Z",
        "last_activity": "2026-05-02T10:00:00Z",
    }
    status, _ = _resolution_for_conflict(
        conflict={}, claim=claim, idle_timeout_sec=1800, now=now
    )
    assert status == "idle-released"


def test_resolution_missing_when_claim_not_in_dict() -> None:
    """The conflict_log row references a claim id we don't have.
    Could be schema drift, manual deletion, or a very old conflict
    whose claim aged out of the recent-claims window. Don't crash;
    surface the state so the operator sees something is off."""
    from coordination.dashboard import _resolution_for_conflict

    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    status, _ = _resolution_for_conflict(
        conflict={}, claim=None, idle_timeout_sec=1800, now=now
    )
    assert status == "missing"


async def test_dashboard_shows_holder_and_resolution_for_conflicts(
    svc: CoordinationService,
) -> None:
    """End-to-end: a conflict where the holder still has the claim
    surfaces the holder's name, the holder's pattern, and the
    'blocked' resolution pill. The pre-v0.8 dashboard surfaced none
    of these -- the resolution column was always empty and the holder
    side of the conflict was never named."""
    holder_id = await _insert_claim(
        svc, engineer="holder-alice", pattern="src/auth/**"
    )
    await svc.db.log_conflict(
        claim_id=holder_id,
        attempted_by="bob",
        attempted_pattern="src/auth/login.ts",
        resolution=None,
    )
    html_out = await render_dashboard()
    # Section header is now lowercase per the new aesthetic.
    start = html_out.lower().index("recent conflicts")
    end = html_out.lower().index("claim timeline")
    conflicts_section = html_out[start:end]

    # Holder's identity must appear -- the missing piece pre-v0.8.
    assert "holder-alice" in conflicts_section
    # Computed resolution shows the "blocked" pill since the claim is
    # still active.
    assert 'class="pill blocked"' in conflicts_section


# ---------------------------------------------------------------------------
# Release requests panel (v0.9.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_release_requests_panel(
    svc: CoordinationService,
) -> None:
    """v0.9 added a 'release requests' panel below the conflict log
    that surfaces every filed request with its decision pill and
    time-to-decision latency."""
    cid = await _insert_claim(svc, engineer="holder-alice", pattern="src/auth/**")
    request = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="hot fix",
        urgency="high",
    )
    # Approve the request so the panel has a non-pending row to render.
    await svc.respond_to_request(
        request_id=request["id"],
        decision="approved",
        actor_engineer="holder-alice",
        actor_session_id=None,
    )

    html_out = await render_dashboard()
    # Panel header is present.
    assert "release requests" in html_out.lower()
    # Slice the panel content.
    start = html_out.lower().index("release requests")
    end = html_out.lower().index("claim timeline")
    panel = html_out[start:end]
    # Both parties appear, and the approved decision pill is rendered.
    assert "bob" in panel
    assert "holder-alice" in panel
    assert 'class="pill approved"' in panel
    # Urgency pill is also rendered.
    assert 'class="pill urgency-high"' in panel


@pytest.mark.asyncio
async def test_dashboard_release_requests_panel_zero_state(
    svc: CoordinationService,
) -> None:
    html_out = await render_dashboard()
    assert "no release requests filed yet" in html_out.lower()


# ---------------------------------------------------------------------------
# Release-requests panel: scope column + narrowed/coexist pill (v0.11.0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_requested_scope_column_in_requests_panel(
    svc: CoordinationService,
) -> None:
    """v0.11 added a `requested_scope` column between `their pattern`
    and `holder` so an operator can see what the requester actually
    needed (often a sub-pattern of what the holder claimed)."""
    cid = await _insert_claim(
        svc, engineer="holder-eve", pattern="src/api/**"
    )
    await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="just the auth file",
        urgency="normal",
        requested_scope="src/api/auth.py",
    )

    html_out = await render_dashboard()
    start = html_out.lower().index("release requests")
    end = html_out.lower().index("claim timeline")
    panel = html_out[start:end]
    assert "src/api/auth.py" in panel  # requested_scope
    assert "src/api/**" in panel  # original requested_pattern (the holder's claim)


@pytest.mark.asyncio
async def test_dashboard_renders_narrowed_decision_pill(
    svc: CoordinationService,
) -> None:
    """A request resolved via `decision=narrowed` shows the narrowed
    pill (dashed phosphor) instead of the regular approved pill."""
    cid = await _insert_claim(
        svc, engineer="holder-narrow", pattern="src/auth/**"
    )
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="just utils",
        urgency="normal",
        requested_scope="src/auth/utils.py",
    )
    await svc.respond_to_request(
        request_id=req["id"],
        decision="narrowed",
        actor_engineer="holder-narrow",
        actor_session_id=None,
        narrowed_pattern="src/auth/login.py",
    )

    html_out = await render_dashboard()
    start = html_out.lower().index("release requests")
    end = html_out.lower().index("claim timeline")
    panel = html_out[start:end]
    assert 'class="pill narrowed"' in panel


@pytest.mark.asyncio
async def test_dashboard_renders_coexist_decision_pill(
    svc: CoordinationService,
) -> None:
    """A request resolved via `decision=coexist` shows the cyan
    coexist pill, distinct from approved/narrowed."""
    cid = await _insert_claim(
        svc, engineer="holder-coex", pattern="src/auth.py"
    )
    req = await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="logout function",
        urgency="normal",
        requested_scope="logout function",
    )
    await svc.respond_to_request(
        request_id=req["id"],
        decision="coexist",
        actor_engineer="holder-coex",
        actor_session_id=None,
        coexist_pattern="src/auth.py",
    )

    html_out = await render_dashboard()
    start = html_out.lower().index("release requests")
    end = html_out.lower().index("claim timeline")
    panel = html_out[start:end]
    assert 'class="pill coexist"' in panel


# ---------------------------------------------------------------------------
# v0.14.1: scope column + auto-resolutions panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_active_claims_show_symbol_names_for_symbol_scope(
    svc: CoordinationService,
) -> None:
    """v0.14.1: the active-claims table gained a `scope` column. For
    symbol-scope claims, the symbol names render inline so an operator
    can tell which parts of a file are locked without a round trip to
    the API."""
    import aiosqlite
    from uuid import uuid4

    cid = await _insert_claim(
        svc, engineer="alice", pattern="src/auth/login.ts"
    )
    # Flip the claim to symbol scope and seed two symbols. The DB layer
    # tolerates this composition: scope_type lives on claims, the symbol
    # list lives on claim_symbols.
    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            "UPDATE claims SET scope_type = 'symbol' WHERE id = ?", (cid,)
        )
        await conn.commit()
    await svc.db.insert_claim_symbols(
        rows=[
            (str(uuid4()), cid, "src/auth/login.ts", "handleLogin", "function", None),
            (str(uuid4()), cid, "src/auth/login.ts", "validateCredentials", "function", None),
        ]
    )

    html_out = await render_dashboard()
    # Slice to the active-claims panel so a symbol-named token in
    # another section can't accidentally satisfy the assertion.
    start = html_out.lower().index("active claims")
    end = html_out.lower().index("module heatmap")
    panel = html_out[start:end]
    assert "handleLogin" in panel
    assert "validateCredentials" in panel
    # The scope cell labels the row as a symbol claim.
    assert "symbol" in panel


@pytest.mark.asyncio
async def test_dashboard_symbol_spans_render_line_ranges_and_lsp_marker(
    svc: CoordinationService,
) -> None:
    """v0.31: symbol claims with persisted spans render their claim-time
    line range inline, with a subtle ``lsp`` marker only when the span
    came from a language server. Rows with NULL spans (pre-v16 rows, no
    repo root) render the bare name exactly as before."""
    import aiosqlite
    from uuid import uuid4

    cid = await _insert_claim(
        svc, engineer="alice", pattern="src/auth/login.ts"
    )
    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            "UPDATE claims SET scope_type = 'symbol' WHERE id = ?", (cid,)
        )
        await conn.commit()
    await svc.db.insert_claim_symbols(
        rows=[
            # Parser-resolved span: lines only, columns NULL.
            (
                str(uuid4()), cid, "src/auth/login.ts", "handleLogin",
                "function", None, 3, None, 9, None, "parser",
            ),
            # LSP-resolved span: full precision plus the marker.
            (
                str(uuid4()), cid, "src/auth/login.ts", "validateCredentials",
                "function", None, 12, 0, 20, 1, "lsp",
            ),
            # Legacy six-column row: NULL spans, bare-name rendering.
            (
                str(uuid4()), cid, "src/auth/login.ts", "legacyHelper",
                "function", None,
            ),
        ]
    )

    html_out = await render_dashboard()
    start = html_out.lower().index("active claims")
    end = html_out.lower().index("module heatmap")
    panel = html_out[start:end]

    assert "handleLogin (lines 3-9)" in panel
    assert "handleLogin (lines 3-9, lsp)" not in panel, (
        "lsp marker must only attach to lsp-resolved spans"
    )
    assert "validateCredentials (lines 12-20, lsp)" in panel
    assert "legacyHelper" in panel
    assert "legacyHelper (" not in panel, (
        "NULL spans must render the bare name exactly as pre-v0.31"
    )


@pytest.mark.asyncio
async def test_dashboard_renders_auto_resolutions_panel_with_counts(
    svc: CoordinationService,
) -> None:
    """v0.14.1: a top-of-page panel surfaces the rolling 24h count of
    server-side auto-resolutions, broken out by `auto-coexist` and
    `auto-narrow`. Numbers come from
    Database.count_auto_resolutions_since(window_hours=24)."""
    # Two auto-coexist events and one auto-narrow event, all inside the
    # 24h window since record_request_event stamps created_at = now.
    await svc.db.record_request_event(
        "auto-coexist", actor_engineer="bob", detail={"holder_claim_id": "x"}
    )
    await svc.db.record_request_event(
        "auto-coexist", actor_engineer="bob", detail={"holder_claim_id": "y"}
    )
    await svc.db.record_request_event(
        "auto-narrow", actor_engineer="bob", detail={"holder_claim_id": "z"}
    )

    html_out = await render_dashboard()
    # Panel header is lowercase per the dashboard aesthetic.
    assert "auto-resolutions (24h)" in html_out.lower()

    start = html_out.lower().index("auto-resolutions (24h)")
    # v0.36 reorder moved this panel below repositories, so bound it by
    # its own closing </section> rather than the next panel header.
    end = html_out.index("</section>", start)
    panel = html_out[start:end]
    # Breakdown text shows 2 coexist + 1 narrow.
    assert ">2</strong> coexist" in panel
    assert ">1</strong> narrow" in panel
    # Sum (3) is shown as a headline number.
    assert ">3<" in panel
    # Legend / link to the design doc is present so operators know what
    # the two decisions mean.
    assert "sub-file-claims.md" in panel


@pytest.mark.asyncio
async def test_dashboard_renders_hotspots_panel(
    svc: CoordinationService,
) -> None:
    """v0.20: dashboard "hotspot files (30d)" panel renders rows with
    suggested-action tags driven by attempt-count thresholds."""
    from uuid import uuid4
    import aiosqlite
    from coordination.db import _configure_sqlite

    # Holder claim so the conflict_log JOIN finds a repo.
    await svc.db.insert_claims_batch(
        engineer="alice",
        branch="main",
        description="seed",
        items=[
            ("holder-h", "file", "src/router.ts", "soft",
             "2099-01-01T00:00:00Z"),
            ("holder-w", "file", "src/middleware.ts", "soft",
             "2099-01-01T00:00:00Z"),
        ],
        session_id="sess",
        repo="amittell/coord",
    )

    # 25 attempts on router.ts (-> "promote to shared_file"), 7 on
    # middleware.ts (-> "monitor"). cold.ts has 3 attempts (-> filtered).
    async with aiosqlite.connect(svc.db.path) as conn:
        await _configure_sqlite(conn)
        for i in range(25):
            await conn.execute(
                "INSERT INTO conflict_log (id, claim_id, attempted_by, "
                "attempted_pattern, resolution, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (str(uuid4()), "holder-h", f"eng-{i % 6}",
                 "src/router.ts", "2026-06-01T10:00:00Z"),
            )
        for i in range(7):
            await conn.execute(
                "INSERT INTO conflict_log (id, claim_id, attempted_by, "
                "attempted_pattern, resolution, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (str(uuid4()), "holder-w", f"eng-{i}",
                 "src/middleware.ts", "2026-06-01T11:00:00Z"),
            )
        await conn.commit()

    html_out = await render_dashboard()

    # Panel header present and both qualifying patterns surfaced.
    assert "hotspot files (30d)" in html_out
    assert "src/router.ts" in html_out
    assert "src/middleware.ts" in html_out
    # router.ts at 25 attempts -> "promote to shared_file" chip.
    assert "promote to shared_file" in html_out
    # middleware.ts at 7 attempts -> "monitor" chip.
    assert "monitor" in html_out
    # Cold patterns under min_attempts should not appear.
    assert "src/cold.ts" not in html_out


@pytest.mark.asyncio
async def test_dashboard_hotspot_action_link_present(
    svc: CoordinationService,
) -> None:
    """v0.21: hotspot rows that pass the actionable thresholds render
    an "apply" link; pure-monitor rows do not."""
    from uuid import uuid4
    import aiosqlite
    from coordination.db import _configure_sqlite

    await svc.db.insert_claims_batch(
        engineer="alice",
        branch="main",
        description="seed",
        items=[
            ("h-promote", "file", "src/promote.ts", "soft",
             "2099-01-01T00:00:00Z"),
            ("h-monitor", "file", "src/monitor.ts", "soft",
             "2099-01-01T00:00:00Z"),
        ],
        session_id="sess",
        repo="amittell/coord",
    )

    # 25 attempts on promote.ts (-> "promote to shared_file"),
    # 7 on monitor.ts (-> "monitor").
    async with aiosqlite.connect(svc.db.path) as conn:
        await _configure_sqlite(conn)
        for i in range(25):
            await conn.execute(
                "INSERT INTO conflict_log (id, claim_id, attempted_by, "
                "attempted_pattern, resolution, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (str(uuid4()), "h-promote", f"eng-{i % 6}",
                 "src/promote.ts", "2026-06-01T10:00:00Z"),
            )
        for i in range(7):
            await conn.execute(
                "INSERT INTO conflict_log (id, claim_id, attempted_by, "
                "attempted_pattern, resolution, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (str(uuid4()), "h-monitor", f"eng-{i}",
                 "src/monitor.ts", "2026-06-01T11:00:00Z"),
            )
        await conn.commit()

    html_out = await render_dashboard()

    # v0.36 reorder put the active-claims panel above hotspots, and these
    # files are also held as active claims, so scope the search to the
    # hotspots panel to find the hotspot row (not the claim row).
    assert "src/promote.ts" in html_out
    hotspots_panel = html_out[html_out.index("hotspot files (30d)"):]
    # Promote.ts row carries an apply link with the right action.
    promote_idx = hotspots_panel.index("src/promote.ts")
    promote_row = hotspots_panel[promote_idx:promote_idx + 800]
    assert 'class="hsapply"' in promote_row
    assert 'data-action="shared_file"' in promote_row

    # Monitor.ts row exists but has NO apply link in its row slice.
    assert "src/monitor.ts" in html_out
    monitor_idx = hotspots_panel.index("src/monitor.ts")
    monitor_row = hotspots_panel[monitor_idx:monitor_idx + 800]
    # The row must end before the next .hsrow div opens; scope the
    # apply-check to the cell containing this row's pattern.
    next_row_start = monitor_row.find('<div class="hsrow"', 1)
    if next_row_start > 0:
        monitor_row = monitor_row[:next_row_start]
    assert 'class="hsapply"' not in monitor_row


# ---------------------------------------------------------------------------
# v0.22: pending queue panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_pending_queue_panel(
    svc: CoordinationService,
) -> None:
    """v0.22: a per-repo "pending queue" panel surfaces queue depth and
    the head-of-queue waiter so an operator can spot agents piling up on
    hot files. Source: Database.list_queued_with_holder."""
    # Holder claim in a real repo so the queue rows can JOIN to a
    # blocking_engineer / blocking_pattern.
    await svc.db.insert_claims_batch(
        engineer="alice-holder",
        branch="main",
        description="big refactor",
        items=[
            ("holder-q1", "file", "src/router.ts", "soft",
             "2099-01-01T00:00:00Z"),
        ],
        session_id="sess-holder",
        repo="amittell/coord",
    )

    # Enqueue three distinct requesters behind the holder, in order.
    for requester in ("bob", "carol", "dave"):
        await svc.db.enqueue_claim_request(
            blocking_claim_id="holder-q1",
            requester_engineer=requester,
            requester_session_id=None,
            requester_branch=None,
            requester_description=None,
            repo="amittell/coord",
            claim_type="file",
            pattern="src/router.ts",
            symbols=None,
            narrowable=None,
            ttl_hours=4,
            wait_seconds=60,
        )

    html_out = await render_dashboard()

    # Panel header is present.
    assert "<h2>pending queue</h2>" in html_out.lower()

    # Slice the panel for tighter assertions. Anchor on the rendered
    # <h2> so the CSS comment ("pending queue panel") in the inlined
    # stylesheet can't match. The next panel in the page order is
    # "repositories" (start of the split-7-5 row).
    start = html_out.lower().index("<h2>pending queue</h2>")
    end = html_out.lower().index("repositories", start)
    panel = html_out[start:end]

    # Repo is named and depth is 3.
    assert "amittell/coord" in panel
    assert ">3<" in panel
    # Head-of-queue is the first enqueued waiter (bob, position 1).
    assert "bob" in panel
    # Blocking holder is identified in the head-of-queue subline.
    assert "alice-holder" in panel


@pytest.mark.asyncio
async def test_dashboard_pending_queue_empty_state(
    svc: CoordinationService,
) -> None:
    """With no queue rows, the panel still renders with a friendly
    'no queued claims (good!)' placeholder."""
    html_out = await render_dashboard()
    assert "no queued claims" in html_out.lower()


# ---------------------------------------------------------------------------
# v0.27: webhook delivery panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_webhook_delivery_panel(
    svc: CoordinationService,
) -> None:
    """v0.27: a "webhook delivery (24h)" panel surfaces per-event-type
    delivery counts (delivered / failed / pending / exhausted). Each
    event_type renders as a row; non-zero counts pop against muted
    zeros so an operator spots stuck endpoints at a glance."""
    oid_promote = await svc.db.enqueue_webhook(
        url="https://example.test/wh",
        event_type="auto-promote",
        payload_json="{}",
        hmac_signature="sig1",
    )
    oid_grant = await svc.db.enqueue_webhook(
        url="https://example.test/wh",
        event_type="queue_grant",
        payload_json="{}",
        hmac_signature="sig2",
    )
    await svc.db.enqueue_webhook(
        url="https://example.test/wh",
        event_type="auto-demote",
        payload_json="{}",
        hmac_signature="sig3",
    )

    # Flip the first to delivered, the second to failed; third remains
    # pending. Gives us one of each status (plus zero-exhausted) for
    # the panel to render.
    await svc.db.mark_webhook_delivered(oid_promote)
    await svc.db.mark_webhook_failed(
        oid_grant,
        last_error="boom",
        next_attempt_at="2099-01-01T00:00:00Z",
    )

    html_out = await render_dashboard()

    # Panel header is present.
    assert "webhook delivery (24h)" in html_out.lower()

    # Slice the panel: anchor on the <h2> so the CSS comment cannot
    # match. v0.36 reorder moved webhooks below repositories, so bound the
    # panel by its own closing </section>.
    start = html_out.lower().index("<h2>webhook delivery (24h)</h2>")
    end = html_out.index("</section>", start)
    panel = html_out[start:end]

    # All three event types appear as rows.
    assert "auto-promote" in panel
    assert "queue_grant" in panel
    assert "auto-demote" in panel

    # delivered=1 row (auto-promote): the row contains the event name
    # and a >1< delivered cell. Easiest check: locate the row and
    # confirm the per-status counts are present in the panel.
    assert ">1</div>" in panel  # at least one non-zero status cell

    # The header label tokens are visible.
    for label in ("delivered", "failed", "pending", "exhausted"):
        assert label in panel.lower()


@pytest.mark.asyncio
async def test_dashboard_webhook_panel_empty_state(
    svc: CoordinationService,
) -> None:
    """With no outbox rows, the panel still renders with a placeholder
    rather than disappearing."""
    html_out = await render_dashboard()
    assert "webhook delivery (24h)" in html_out.lower()
    assert "no webhook events" in html_out.lower()


# ---------------------------------------------------------------------------
# v0.28: stale engineers panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_renders_stale_engineers_panel(
    svc: CoordinationService,
) -> None:
    """v0.28: a "stale engineers" panel surfaces engineers whose most
    recent ``last_activity`` is older than ``stale_engineer_days``. The
    panel shows the engineer name, an age relative-time, and the
    active claim count so an operator can spot abandoned worktrees at
    a glance.

    We disable the idle-expiry sweep for this test by pinning
    ``idle_timeout_sec = 0`` on the service. Without that, the seed
    claim's 15-day-old last_activity would trip the idle path inside
    ``list_claims`` -- the dashboard calls that on its way in -- and
    the row would be released before ``list_stale_engineers`` saw it.
    """
    import aiosqlite

    svc.settings = svc.settings.model_copy(update={"idle_timeout_sec": 0})

    # Seed one stale engineer (15 days old) and one fresh engineer.
    # We backdate last_activity directly on the row so the test isn't
    # at the mercy of insert_claims_batch stamping "now".
    stale_exp = _iso(datetime.now(UTC) + timedelta(days=14))
    fresh_exp = _iso(datetime.now(UTC) + timedelta(days=14))
    await svc.db.insert_claims_batch(
        engineer="stale-engineer",
        branch=None,
        description=None,
        items=[("stale-1", "file", "src/x.py", "soft", stale_exp)],
        repo="demo",
        session_id="session-stale",
    )
    await svc.db.insert_claims_batch(
        engineer="fresh-engineer",
        branch=None,
        description=None,
        items=[("fresh-1", "file", "src/y.py", "soft", fresh_exp)],
        repo="demo",
        session_id="session-fresh",
    )
    backdated = _iso(datetime.now(UTC) - timedelta(days=15))
    async with aiosqlite.connect(svc.db.path) as conn:
        await conn.execute(
            "UPDATE claims SET last_activity = ? WHERE id = ?",
            (backdated, "stale-1"),
        )
        await conn.commit()

    html_out = await render_dashboard()

    # Panel header is present.
    assert "<h2>stale engineers</h2>" in html_out.lower()

    # Slice the panel for tighter assertions. The panel after stale is
    # webhooks (start of the per-event-type rows). Anchor on the
    # rendered <h2> so the CSS comment can't match.
    start = html_out.lower().index("<h2>stale engineers</h2>")
    end = html_out.lower().index("<h2>webhook delivery", start)
    panel = html_out[start:end]

    assert "stale-engineer" in panel
    # Fresh engineer must NOT appear in the panel slice.
    assert "fresh-engineer" not in panel
    # Active claim count surfaces as a numeric cell.
    assert ">1<" in panel


# ---------------------------------------------------------------------------
# v0.36: live-first hero stats, needs-attention rollup, contention flags,
# auto-refresh
# ---------------------------------------------------------------------------


async def test_dashboard_hero_stats_are_live_first(svc: CoordinationService) -> None:
    """The hero row leads with the live operational picture (active claims,
    blocked now, waiting, repos) and no longer spends a slot on the static
    idle-timeout constant -- that moves to the status bar."""
    html_out = await render_dashboard()
    stats = html_out[
        html_out.index('class="stats"') : html_out.index('class="attention')
    ]
    for label in ("active claims", "blocked now", "waiting", "repos"):
        assert f">{label}</span>" in stats
    # idle-timeout is no longer a hero stat; it now lives in the status bar.
    assert "idle-timeout" not in stats
    assert "idle 30m" in html_out  # status-bar segment (default 1800s)


async def test_dashboard_attention_all_clear_when_idle(
    svc: CoordinationService,
) -> None:
    """With nothing blocked, queued, or awaiting a decision the rollup
    renders the calm 'all clear' state, not the alert state."""
    html_out = await render_dashboard()
    assert 'class="attention clear"' in html_out
    assert "all clear" in html_out
    assert "needs attention" not in html_out


async def test_dashboard_attention_alert_lists_actionable_signals(
    svc: CoordinationService,
) -> None:
    """A filed (pending) release request flips the rollup to the alert
    state and is summarised in the at-a-glance line."""
    now = datetime.now(UTC)
    cid = await _insert_claim_raw(
        svc,
        engineer="holder-alice",
        pattern="src/auth/login.py",
        created_at=_iso(now - timedelta(minutes=10)),
        expires_at=_iso(now + timedelta(hours=1)),
    )
    await svc.file_request(
        claim_id=cid,
        requester="bob",
        requester_session_id=None,
        reason="need it",
        urgency="high",
    )
    html_out = await render_dashboard()
    assert 'class="attention alert"' in html_out
    assert "needs attention" in html_out
    assert "1 release request pending" in html_out


async def test_dashboard_flags_contended_and_release_asked_claims(
    svc: CoordinationService,
) -> None:
    """Two engineers holding the same (repo, pattern) are both flagged
    'contended'; a held claim that is a pending release-request target is
    flagged 'release asked'."""
    now = datetime.now(UTC)
    fresh = _iso(now - timedelta(minutes=5))
    future = _iso(now + timedelta(hours=2))
    # Two active claims on the same pattern by different engineers.
    await _insert_claim_raw(
        svc, engineer="alice", pattern="services/shared.py",
        created_at=fresh, expires_at=future,
    )
    await _insert_claim_raw(
        svc, engineer="bob", pattern="services/shared.py",
        created_at=fresh, expires_at=future,
    )
    # A third active claim that someone files a release request against.
    cid = await _insert_claim_raw(
        svc, engineer="carol", pattern="services/lonely.py",
        created_at=fresh, expires_at=future,
    )
    await svc.file_request(
        claim_id=cid, requester="dave", requester_session_id=None,
        reason="hot", urgency="normal",
    )
    html_out = await render_dashboard()
    # Scope to the active-claims panel.
    start = html_out.index("<h2>active claims</h2>")
    panel = html_out[start : html_out.index("</section>", start)]
    assert panel.count('class="pill contended"') == 2
    assert 'class="pill release-asked"' in panel
    assert 'class="attn"' in panel


async def test_dashboard_includes_auto_refresh_script(
    svc: CoordinationService,
) -> None:
    """The page ships the progressive-enhancement auto-refresh script and
    the status-bar toggle it drives."""
    html_out = await render_dashboard()
    assert 'id="refresh-toggle"' in html_out
    assert "auto-refresh" in html_out
    assert "sessionStorage" in html_out  # scroll-position preservation

