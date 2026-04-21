from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coordination import deps
from coordination.config import Settings
from coordination.dashboard import _bucket, _esc, _remaining, render_dashboard
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
