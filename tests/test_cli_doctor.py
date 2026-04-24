from __future__ import annotations

import httpx

from coordination import cli_doctor
from coordination.repo_config import RepoConfig


def _config(tmp_path) -> RepoConfig:
    return RepoConfig(
        version=1,
        tool="claude",
        mode="remote",
        service_url="http://coord.example",
        ownership_file=".coordination/owners.yaml",
        local_env_file=".coordination/local.env",
    )


def _mock_transport(captured_requests: list[httpx.Request], status_by_path: dict[str, int]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_by_path.get(request.url.path, 200))

    return httpx.MockTransport(handler)


def test_check_service_with_token_sends_bearer(monkeypatch, tmp_path):
    captured: list[httpx.Request] = []
    transport = _mock_transport(captured, {"/readyz": 200, "/claims": 200})
    monkeypatch.setattr(
        cli_doctor.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )
    results = cli_doctor._check_service(_config(tmp_path), token="abc123")
    assert [r.label for r in results] == [
        "coordination service reachable",
        "auth token works",
    ]
    assert all(r.ok for r in results)
    claims_request = next(r for r in captured if r.url.path == "/claims")
    assert claims_request.headers["authorization"] == "Bearer abc123"


def test_check_service_without_token_omits_header(monkeypatch, tmp_path):
    captured: list[httpx.Request] = []
    transport = _mock_transport(captured, {"/readyz": 200, "/claims": 200})
    monkeypatch.setattr(
        cli_doctor.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )
    results = cli_doctor._check_service(_config(tmp_path), token="")
    # Second result is renamed when no token is configured.
    assert [r.label for r in results] == [
        "coordination service reachable",
        "unauthenticated access works",
    ]
    assert all(r.ok for r in results)
    claims_request = next(r for r in captured if r.url.path == "/claims")
    # httpx drops headers passed as empty dict, so Authorization must be absent.
    assert "authorization" not in {k.lower() for k in claims_request.headers}


def test_check_service_unauthenticated_failure_explains_insecure_flag(monkeypatch, tmp_path):
    captured: list[httpx.Request] = []
    transport = _mock_transport(captured, {"/readyz": 200, "/claims": 401})
    monkeypatch.setattr(
        cli_doctor.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )
    results = cli_doctor._check_service(_config(tmp_path), token="")
    auth_result = results[1]
    assert auth_result.label == "unauthenticated access works"
    assert auth_result.ok is False
    assert "COORD_ALLOW_INSECURE_NO_AUTH" in auth_result.hint


def test_check_service_unreachable_reports_both(monkeypatch, tmp_path):
    def raising_get(url, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(cli_doctor.httpx, "get", raising_get)
    results = cli_doctor._check_service(_config(tmp_path), token="abc123")
    assert len(results) == 2
    assert results[0].label == "coordination service reachable"
    assert results[0].ok is False
    assert results[1].label == "auth token works"
    assert results[1].ok is False
