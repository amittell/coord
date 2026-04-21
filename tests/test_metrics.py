from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from coordination import __version__
from coordination import metrics as metrics_mod


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    """Reset module-level metric singletons between tests so counter
    increments from one test do not leak into the next."""
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


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_openmetrics_format(client: AsyncClient) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/plain" in ct
    assert "version=0.0.4" in ct
    body = r.text
    assert "# HELP" in body
    assert "# TYPE" in body


@pytest.mark.asyncio
async def test_claim_create_increments_counter(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/claims",
        json={
            "engineer": "alice",
            "claims": [{"type": "file", "pattern": "src/feature/**"}],
        },
        headers=h,
    )
    assert r.status_code == 200
    m = await client.get("/metrics")
    assert m.status_code == 200
    assert "claims_created_total" in m.text
    # At least one non-zero reading of claims_created_total must appear.
    found = False
    for line in m.text.splitlines():
        if line.startswith("claims_created_total") and " 0.0" not in line:
            found = True
            break
    assert found, f"claims_created_total should be > 0, got:\n{m.text}"


@pytest.mark.asyncio
async def test_conflict_increments_counter(client: AsyncClient) -> None:
    h = {"Authorization": "Bearer test-token"}
    r = await client.post(
        "/claims",
        json={"engineer": "alice", "claims": [{"type": "file", "pattern": "src/auth/**"}]},
        headers=h,
    )
    assert r.status_code == 200
    r2 = await client.post(
        "/claims",
        json={"engineer": "bob", "claims": [{"type": "file", "pattern": "src/auth/login.ts"}]},
        headers=h,
    )
    assert r2.status_code == 409
    m = await client.get("/metrics")
    assert "claims_conflicts_total" in m.text
    for line in m.text.splitlines():
        if line.startswith("claims_conflicts_total ") and " 0.0" not in line:
            return
    raise AssertionError(f"claims_conflicts_total should be > 0, got:\n{m.text}")


@pytest.mark.asyncio
async def test_unauthenticated_request_increments_auth_failures(client: AsyncClient) -> None:
    r = await client.get("/claims")
    assert r.status_code == 401
    m = await client.get("/metrics")
    for line in m.text.splitlines():
        if line.startswith("auth_failures_total ") and " 0.0" not in line:
            return
    raise AssertionError(f"auth_failures_total should be > 0, got:\n{m.text}")


@pytest.mark.asyncio
async def test_build_info_gauge_is_set(client: AsyncClient) -> None:
    m = await client.get("/metrics")
    expected = f'build_info{{version="{__version__}"}} 1.0'
    assert expected in m.text, f"expected {expected!r} in metrics output"


def test_metric_rendering_escapes_label_values() -> None:
    reg = metrics_mod.Registry()
    c = metrics_mod.Counter("escape_test_total", "escaping check", labels=("label",), registry=reg)
    c.inc(label='a"b\\c\nd')
    out = reg.render()
    # OpenMetrics label value escaping: \\ for backslash, \" for quote, \n for newline.
    assert 'label="a\\"b\\\\c\\nd"' in out, f"escaping wrong, got:\n{out}"


def test_reset_for_tests_clears_counters() -> None:
    metrics_mod.claims_created_total.inc(severity="hard")
    body = metrics_mod.registry.render()
    assert "claims_created_total" in body
    metrics_mod._reset_for_tests()
    body2 = metrics_mod.registry.render()
    # After reset, counter should have no sample lines (or only zero if we
    # keep the HELP/TYPE header). Require that the hard label sample is gone.
    assert 'claims_created_total{severity="hard"}' not in body2
