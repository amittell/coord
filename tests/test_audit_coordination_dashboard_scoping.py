"""Audit regression tests for dashboard repo scoping and render fixes.

Covers:

- repo filtering applied in SQL before LIMIT for the dashboard's windowed
  fetches (``Database.list_requests``, ``Database.list_recent_claims``,
  ``Database.list_queued_with_holder`` wiring via ``render_dashboard``);
- the "release asked" contention flag keyed on claim_id instead of the
  (holder, pattern) tuple that cross-flagged same-named claims in other
  repos;
- the claim-timeline "updated" column no longer rendering the literal
  "now" for every still-active claim;
- the stale-engineers panel rendering plain text instead of a dead
  ``?engineer=`` drill-down link;
- the dashboard stylesheet no longer importing Google Fonts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from coordination import deps
from coordination.config import Settings
from coordination.dashboard import _CSS, render_dashboard
from coordination.db import Database
from coordination.service import CoordinationService


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture()
async def svc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[CoordinationService]:
    """Fresh CoordinationService backed by a temp sqlite DB, wired into
    coordination.deps.get_service for the duration of the test."""
    db_path = tmp_path / "audit-dashboard.sqlite"
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")

    database = Database(db_path)
    await database.init()

    settings = Settings(
        database_path=db_path,
        allow_insecure_no_auth=True,
        repo_root=None,
        repo_scope=None,
        # Disable idle expiration so seeded old ``last_activity`` values
        # (the timeline and stale-engineer fixtures) survive the
        # expire_stale_claims sweep that runs on every list_claims call.
        idle_timeout_sec=0,
    )
    service = CoordinationService(db=database, settings=settings)

    deps.get_service.cache_clear()
    monkeypatch.setattr(deps, "get_service", lambda: service)
    import coordination.dashboard as dashboard_mod

    monkeypatch.setattr(dashboard_mod, "get_service", lambda: service)

    yield service


async def _insert_claim(
    svc: CoordinationService,
    *,
    engineer: str,
    pattern: str,
    repo: str | None = None,
    session_id: str | None = None,
    last_activity: str | None = None,
    expires_at: str | None = None,
) -> str:
    cid = str(uuid4())
    exp = expires_at or _iso(datetime.now(UTC) + timedelta(hours=4))
    await svc.db.insert_claims_batch(
        engineer=engineer,
        branch=None,
        description=None,
        items=[(cid, "file", pattern, "soft", exp)],
        repo=repo,
        session_id=session_id,
        last_activity=last_activity,
    )
    return cid


async def _enqueue(
    svc: CoordinationService,
    *,
    blocking_claim_id: str,
    requester: str,
    repo: str | None,
    pattern: str,
) -> dict:
    return await svc.db.enqueue_claim_request(
        blocking_claim_id=blocking_claim_id,
        requester_engineer=requester,
        requester_session_id=None,
        requester_branch=None,
        requester_description=None,
        repo=repo,
        claim_type="file",
        pattern=pattern,
        symbols=None,
        narrowable=None,
        ttl_hours=None,
        wait_seconds=600,
    )


# ---------------------------------------------------------------------------
# Repo scoping runs in SQL, before LIMIT
# ---------------------------------------------------------------------------


async def test_list_requests_repo_filter_applies_before_limit(
    svc: CoordinationService,
) -> None:
    """A repo-scoped viewer's release request must survive a LIMIT window
    smaller than the volume of newer other-repo requests. Post-hoc Python
    filtering (the pre-fix behaviour) returns zero rows here because the
    other repo's newer rows consume the entire window."""
    mine_cid = await _insert_claim(
        svc, engineer="alice", pattern="src/mine/**", repo="me/repo"
    )
    await svc.file_request(
        claim_id=mine_cid,
        requester="bob",
        requester_session_id=None,
        reason="need it",
        urgency="normal",
    )
    # Now file strictly newer requests in another repo, more than the limit.
    for i in range(6):
        other_cid = await _insert_claim(
            svc, engineer="eve", pattern=f"src/other-{i}/**", repo="them/repo"
        )
        await svc.file_request(
            claim_id=other_cid,
            requester="mallory",
            requester_session_id=None,
            reason="busy repo",
            urgency="normal",
        )

    scoped = await svc.db.list_requests(repo="me/repo", limit=5)
    assert len(scoped) == 1
    assert scoped[0]["claim_id"] == mine_cid
    assert scoped[0]["holder_repo"] == "me/repo"
    # Unscoped call keeps legacy behaviour: window is global.
    unscoped = await svc.db.list_requests(limit=5)
    assert len(unscoped) == 5


async def test_list_requests_service_wrapper_passes_repo(
    svc: CoordinationService,
) -> None:
    mine_cid = await _insert_claim(
        svc, engineer="alice", pattern="src/mine/**", repo="me/repo"
    )
    await svc.file_request(
        claim_id=mine_cid,
        requester="bob",
        requester_session_id=None,
        reason="need it",
        urgency="normal",
    )
    other_cid = await _insert_claim(
        svc, engineer="eve", pattern="src/other/**", repo="them/repo"
    )
    await svc.file_request(
        claim_id=other_cid,
        requester="mallory",
        requester_session_id=None,
        reason="unrelated",
        urgency="normal",
    )
    rows = await svc.list_requests(repo="me/repo")
    assert [r["claim_id"] for r in rows] == [mine_cid]


async def test_list_recent_claims_repo_filter_applies_before_limit(
    svc: CoordinationService,
) -> None:
    mine_cid = await _insert_claim(
        svc, engineer="alice", pattern="src/mine/**", repo="me/repo"
    )
    for i in range(8):
        await _insert_claim(
            svc, engineer="eve", pattern=f"src/other-{i}/**", repo="them/repo"
        )

    scoped = await svc.db.list_recent_claims(4, repo="me/repo")
    assert [r["id"] for r in scoped] == [mine_cid]
    assert all(r["repo"] == "me/repo" for r in scoped)
    unscoped = await svc.db.list_recent_claims(4)
    assert len(unscoped) == 4


async def test_dashboard_scopes_queue_and_requests_to_viewer_repo(
    svc: CoordinationService,
) -> None:
    """End-to-end wiring: a repo-scoped render must show the viewer's own
    queue entry, release request, and claim while excluding the other
    repo's, and the hero numbers must count only the viewer's rows."""
    mine_cid = await _insert_claim(
        svc, engineer="alice", pattern="src/mine/**", repo="me/repo"
    )
    await svc.file_request(
        claim_id=mine_cid,
        requester="bob-requester",
        requester_session_id=None,
        reason="need it",
        urgency="normal",
    )
    await _enqueue(
        svc,
        blocking_claim_id=mine_cid,
        requester="queued-quinn",
        repo="me/repo",
        pattern="src/mine/**",
    )
    other_cid = await _insert_claim(
        svc, engineer="eve-other", pattern="src/other/**", repo="them/repo"
    )
    await svc.file_request(
        claim_id=other_cid,
        requester="mallory-other",
        requester_session_id=None,
        reason="unrelated",
        urgency="normal",
    )
    await _enqueue(
        svc,
        blocking_claim_id=other_cid,
        requester="quentin-other",
        repo="them/repo",
        pattern="src/other/**",
    )

    html_out = await render_dashboard(viewer_repo="me/repo")
    assert "queued-quinn" in html_out
    assert "bob-requester" in html_out
    assert "alice" in html_out
    assert "quentin-other" not in html_out
    assert "mallory-other" not in html_out
    assert "eve-other" not in html_out
    # Hero numbers: exactly one waiting queue entry and one pending
    # release request for the scoped viewer.
    assert "1 release requests pending" in html_out


# ---------------------------------------------------------------------------
# "release asked" flag keyed on claim_id
# ---------------------------------------------------------------------------


async def test_release_asked_flag_does_not_cross_repos(
    svc: CoordinationService,
) -> None:
    """A pending release request against alice's claim in repo A must not
    flag alice's same-named claim in repo B."""
    cid_a = await _insert_claim(
        svc, engineer="alice", pattern="src/**", repo="repo-a"
    )
    await _insert_claim(svc, engineer="alice", pattern="src/**", repo="repo-b")
    await svc.file_request(
        claim_id=cid_a,
        requester="bob",
        requester_session_id=None,
        reason="need repo-a only",
        urgency="normal",
    )

    html_out = await render_dashboard()
    pill = '<span class="pill release-asked">release asked</span>'
    assert html_out.count(pill) == 1
    # The flagged row is the repo-a one: the pill lands on the same table
    # row as the repo cell. Rows render repo-a's claim and repo-b's claim
    # separately; only one <tr class="attn"> exists.
    assert html_out.count('<tr class="attn">') == 1


# ---------------------------------------------------------------------------
# Claim-timeline "updated" column
# ---------------------------------------------------------------------------


async def test_timeline_updated_uses_last_activity_for_active_claims(
    svc: CoordinationService,
) -> None:
    two_hours_ago = _iso(datetime.now(UTC) - timedelta(hours=2))
    await _insert_claim(
        svc,
        engineer="alice",
        pattern="src/active/**",
        session_id="sess-1",
        last_activity=two_hours_ago,
    )
    html_out = await render_dashboard()
    # Pre-fix, expires_at (a future timestamp) hit _ago's negative-delta
    # branch and every active claim rendered updated="now".
    assert "<td class='muted'>now</td>" not in html_out
    assert "<td class='muted'>2h ago</td>" in html_out


async def test_timeline_updated_falls_back_to_created_at(
    svc: CoordinationService,
) -> None:
    """Legacy claims without session tagging have last_activity = NULL;
    the updated column must fall back to created_at, not expires_at."""
    await _insert_claim(svc, engineer="alice", pattern="src/legacy/**")
    html_out = await render_dashboard()
    assert "<td class='muted'>now</td>" not in html_out
    # created_at was stamped seconds ago, so the cell shows a seconds age.
    assert "s ago</td>" in html_out


# ---------------------------------------------------------------------------
# Stale-engineers panel: no dead drill-down link
# ---------------------------------------------------------------------------


async def test_stale_engineer_rendered_as_plain_text_not_link(
    svc: CoordinationService,
) -> None:
    ten_days_ago = _iso(datetime.now(UTC) - timedelta(days=10))
    await _insert_claim(
        svc,
        engineer="abandoned-al",
        pattern="src/old/**",
        session_id="sess-old",
        last_activity=ten_days_ago,
    )
    html_out = await render_dashboard()
    # The engineer shows up in the stale panel...
    assert 'class="seengineer"' in html_out
    assert "abandoned-al" in html_out
    # ...but not as a link to a query parameter the dashboard route ignores.
    assert "?engineer=" not in html_out
    assert '<a class="seengineer"' not in html_out


# ---------------------------------------------------------------------------
# No external font import
# ---------------------------------------------------------------------------


def test_css_has_no_external_font_import() -> None:
    assert "fonts.googleapis" not in _CSS
    assert "@import" not in _CSS


async def test_rendered_page_has_no_external_font_import(
    svc: CoordinationService,
) -> None:
    html_out = await render_dashboard()
    assert "fonts.googleapis" not in html_out
    assert "@import" not in html_out
