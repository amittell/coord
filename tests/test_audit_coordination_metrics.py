"""Audit regression tests for the metrics registry.

Covers:

- ``http_requests_total`` path-label cardinality: requests that do not
  match a route (404 scans) collapse to the constant ``<unmatched>``
  label instead of minting one process-lifetime series per probed raw
  URL path;
- ``Registry.register`` rejecting duplicate metric names so a duplicate
  ``# HELP``/``# TYPE`` family can never invalidate the whole scrape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination import metrics as metrics_mod
from coordination.metrics import Counter, Gauge, Registry


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    metrics_mod._reset_for_tests()
    yield
    metrics_mod._reset_for_tests()


@pytest.fixture()
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("COORD_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("COORD_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("COORD_DISABLE_BACKGROUND_CLEANUP", "1")
    monkeypatch.setenv("COORD_DISABLE_INSTANCE_LOCK", "1")
    monkeypatch.delenv("COORD_REPO_ROOT", raising=False)

    from coordination import deps
    from coordination.main import app

    deps.get_service.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    deps.get_service.cache_clear()


# ---------------------------------------------------------------------------
# Unmatched paths collapse to a constant label
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmatched_paths_collapse_to_single_sentinel_series(
    client: AsyncClient,
) -> None:
    """Two 404s on distinct raw paths must produce ONE series labeled
    <unmatched>, not one permanent series per probed path. Series live in
    a process-lifetime dict, so per-path labels let an unauthenticated
    scanner grow memory and the scrape body without bound."""
    r1 = await client.get("/scanner-probe-alpha")
    r2 = await client.get("/scanner-probe-beta")
    assert r1.status_code == 404
    assert r2.status_code == 404

    paths = {
        key[1]
        for key in metrics_mod.http_requests_total.values
    }
    assert "<unmatched>" in paths
    assert "/scanner-probe-alpha" not in paths
    assert "/scanner-probe-beta" not in paths
    key = ("GET", "<unmatched>", "404")
    assert metrics_mod.http_requests_total.values[key] == 2.0


@pytest.mark.asyncio
async def test_matched_routes_still_use_route_template(
    client: AsyncClient,
) -> None:
    # The middleware increments after the response body is rendered, so
    # the first scrape does not include its own series; scrape twice.
    r = await client.get("/metrics")
    assert r.status_code == 200
    r = await client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert 'path="/metrics"' in body
    assert 'path="<unmatched>"' not in body


@pytest.mark.asyncio
async def test_scrape_renders_sentinel_label_for_404s(client: AsyncClient) -> None:
    await client.get("/does-not-exist-anywhere")
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert 'path="<unmatched>"' in r.text
    assert 'path="/does-not-exist-anywhere"' not in r.text


# ---------------------------------------------------------------------------
# Duplicate metric names fail fast at registration
# ---------------------------------------------------------------------------


def test_duplicate_counter_name_raises() -> None:
    reg = Registry()
    Counter("dup_total", "first", registry=reg)
    with pytest.raises(ValueError, match="dup_total"):
        Counter("dup_total", "second copy", registry=reg)


def test_duplicate_name_across_metric_types_raises() -> None:
    reg = Registry()
    Counter("family_total", "counter first", registry=reg)
    with pytest.raises(ValueError, match="family_total"):
        Gauge("family_total", "gauge with the same name", registry=reg)


def test_distinct_names_register_and_render_one_block_each() -> None:
    reg = Registry()
    Counter("one_total", "one", registry=reg)
    Counter("two_total", "two", registry=reg)
    body = reg.render()
    assert body.count("# TYPE one_total counter") == 1
    assert body.count("# TYPE two_total counter") == 1


def test_reset_for_tests_does_not_break_uniqueness_tracking() -> None:
    reg = Registry()
    c = Counter("resettable_total", "help", registry=reg)
    c.inc()
    reg._reset_for_tests()
    # Still registered (reset clears samples, not registration), so a
    # re-registration attempt must still be rejected.
    with pytest.raises(ValueError, match="resettable_total"):
        Counter("resettable_total", "help again", registry=reg)
    assert "resettable_total 0.0" in reg.render()
