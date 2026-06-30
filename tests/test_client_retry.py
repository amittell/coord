"""Unit tests for the coord-mcp client retry + idempotency layer.

Scope (design Section 10.1): the backoff math, the retry-eligibility
predicates, and the ``_request_with_retry`` executor. The transport is mocked
with ``httpx.MockTransport`` and ``asyncio.sleep`` is patched out -- nothing
here touches a real socket or actually sleeps. Server-side claim semantics are
explicitly NOT exercised here; this file is the client transport only.
"""

from __future__ import annotations

import random
from typing import Any

import httpx
import pytest

from coordination import mcp_server


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace asyncio.sleep (as used by the module) with a recorder.

    Returns the list of delays the executor asked to sleep for, so a test
    can assert "retried N times" without real wall-clock delay.
    """
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(mcp_server.asyncio, "sleep", fake_sleep)
    return slept


def _client_with_handler(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sequence_handler(outcomes: list[Any]):
    """Build a MockTransport handler that yields ``outcomes`` in order.

    Each outcome is either an ``int`` status code (-> JSON 200-shaped
    response with that status) or an ``Exception`` instance to raise. After
    the list is exhausted the last outcome repeats, so an all-failure
    sequence keeps failing.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(outcomes) - 1)
        calls["n"] += 1
        outcome = outcomes[i]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={"ok": True, "attempt": calls["n"]})

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# ---------------------------------------------------------------------------
# retry-eligibility predicates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [502, 503, 504])
def test_retryable_statuses(status: int) -> None:
    assert mcp_server._should_retry_status(status) is True


@pytest.mark.parametrize("status", [200, 201, 400, 404, 409, 429, 500, 501])
def test_non_retryable_statuses(status: int) -> None:
    # 500/501 are deliberately NOT retried: they are server-side logic
    # errors, not transient gateway blips, so replay would not help.
    assert mcp_server._should_retry_status(status) is False


def test_transient_exception_classification() -> None:
    request = httpx.Request("GET", "http://x/claims")
    assert mcp_server._is_transient_exc(httpx.ConnectError("boom", request=request))
    assert mcp_server._is_transient_exc(httpx.ConnectTimeout("boom", request=request))
    assert mcp_server._is_transient_exc(httpx.ReadTimeout("boom", request=request))


def test_non_transient_exceptions_not_classified() -> None:
    request = httpx.Request("GET", "http://x/claims")
    # A protocol error / write error is not in the safe-to-replay set.
    assert not mcp_server._is_transient_exc(
        httpx.WriteError("boom", request=request)
    )
    assert not mcp_server._is_transient_exc(ValueError("nope"))
    # A real HTTP status error is surfaced to the caller, not retried as a
    # transport blip.
    assert not mcp_server._is_transient_exc(
        httpx.HTTPStatusError(
            "500", request=request, response=httpx.Response(500)
        )
    )


# ---------------------------------------------------------------------------
# backoff math
# ---------------------------------------------------------------------------


def test_backoff_is_bounded_by_cap_with_full_jitter() -> None:
    rng = random.Random(1234)
    base, cap = 0.1, 5.0
    for attempt in range(0, 12):
        for _ in range(50):
            d = mcp_server._backoff_delay(attempt, base=base, cap=cap, rng=rng)
            assert 0.0 <= d <= cap


def test_backoff_grows_then_saturates_at_cap() -> None:
    # With jitter pinned to its maximum (uniform always returns its hi bound),
    # the pre-jitter target is base*2**attempt clamped to cap.
    class _MaxRng(random.Random):
        def uniform(self, a: float, b: float) -> float:  # noqa: D102
            return b

    rng = _MaxRng()
    base, cap = 0.1, 5.0
    assert mcp_server._backoff_delay(0, base=base, cap=cap, rng=rng) == pytest.approx(0.1)
    assert mcp_server._backoff_delay(1, base=base, cap=cap, rng=rng) == pytest.approx(0.2)
    assert mcp_server._backoff_delay(2, base=base, cap=cap, rng=rng) == pytest.approx(0.4)
    # 0.1 * 2**6 = 6.4 -> clamped to cap 5.0
    assert mcp_server._backoff_delay(6, base=base, cap=cap, rng=rng) == pytest.approx(5.0)
    assert mcp_server._backoff_delay(20, base=base, cap=cap, rng=rng) == pytest.approx(5.0)


def test_backoff_zero_base_is_zero() -> None:
    rng = random.Random(7)
    assert mcp_server._backoff_delay(3, base=0.0, cap=5.0, rng=rng) == 0.0


# ---------------------------------------------------------------------------
# settings parsing (defensive env handling)
# ---------------------------------------------------------------------------


def test_retry_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "COORD_RETRY_MAX_ATTEMPTS",
        "COORD_RETRY_BASE_DELAY",
        "COORD_RETRY_MAX_DELAY",
        "COORD_RETRY_MUTATIONS",
    ):
        monkeypatch.delenv(var, raising=False)
    attempts, base, cap, retry_mut = mcp_server._retry_settings()
    assert attempts == 4
    assert base == pytest.approx(0.1)
    assert cap == pytest.approx(5.0)
    assert retry_mut is False


def test_retry_settings_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_RETRY_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("COORD_RETRY_BASE_DELAY", "0.25")
    monkeypatch.setenv("COORD_RETRY_MAX_DELAY", "9")
    monkeypatch.setenv("COORD_RETRY_MUTATIONS", "TRUE")
    attempts, base, cap, retry_mut = mcp_server._retry_settings()
    assert (attempts, base, cap, retry_mut) == (7, 0.25, 9.0, True)


def test_retry_settings_malformed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_RETRY_MAX_ATTEMPTS", "not-an-int")
    monkeypatch.setenv("COORD_RETRY_BASE_DELAY", "")
    monkeypatch.setenv("COORD_RETRY_MAX_DELAY", "-3")  # negative -> default
    attempts, base, cap, _ = mcp_server._retry_settings()
    assert attempts == 4
    assert base == pytest.approx(0.1)
    assert cap == pytest.approx(5.0)


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("yes", True), ("on", True),
     ("0", False), ("false", False), ("", False), ("nope", False)],
)
def test_retry_mutations_flag_parsing(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("COORD_RETRY_MUTATIONS", value)
    *_, retry_mut = mcp_server._retry_settings()
    assert retry_mut is expected


# ---------------------------------------------------------------------------
# executor: idempotent reads ARE retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_retries_on_503_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept = _no_sleep(monkeypatch)
    handler = _sequence_handler([503, 503, 200])
    async with _client_with_handler(handler) as client:
        r = await mcp_server._request_with_retry(
            client, "GET", "http://svc/claims", idempotent=True
        )
    assert r.status_code == 200
    assert handler.calls["n"] == 3  # two failures + one success
    assert len(slept) == 2  # slept before each of the two retries


@pytest.mark.asyncio
async def test_idempotent_retries_on_connect_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept = _no_sleep(monkeypatch)
    req = httpx.Request("GET", "http://svc/claims")
    handler = _sequence_handler([httpx.ConnectError("down", request=req), 200])
    async with _client_with_handler(handler) as client:
        r = await mcp_server._request_with_retry(
            client, "GET", "http://svc/claims", idempotent=True
        )
    assert r.status_code == 200
    assert len(slept) == 1


@pytest.mark.asyncio
async def test_idempotent_exhausts_and_returns_last_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_RETRY_MAX_ATTEMPTS", "3")
    slept = _no_sleep(monkeypatch)
    handler = _sequence_handler([503])  # always 503
    async with _client_with_handler(handler) as client:
        r = await mcp_server._request_with_retry(
            client, "GET", "http://svc/claims", idempotent=True
        )
    # On exhaustion the retryable status is returned as-is so the caller's
    # raise_for_status() can surface it; it is NOT swallowed.
    assert r.status_code == 503
    assert handler.calls["n"] == 3  # max_attempts
    assert len(slept) == 2  # one fewer sleep than attempts


@pytest.mark.asyncio
async def test_idempotent_exhausts_and_reraises_transient_exc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORD_RETRY_MAX_ATTEMPTS", "2")
    _no_sleep(monkeypatch)
    req = httpx.Request("GET", "http://svc/claims")
    handler = _sequence_handler([httpx.ReadTimeout("slow", request=req)])
    async with _client_with_handler(handler) as client:
        with pytest.raises(httpx.ReadTimeout):
            await mcp_server._request_with_retry(
                client, "GET", "http://svc/claims", idempotent=True
            )


@pytest.mark.asyncio
async def test_idempotent_does_not_retry_non_transient_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept = _no_sleep(monkeypatch)
    handler = _sequence_handler([409, 200])
    async with _client_with_handler(handler) as client:
        r = await mcp_server._request_with_retry(
            client, "GET", "http://svc/conflicts", idempotent=True
        )
    assert r.status_code == 409  # returned immediately, no retry
    assert handler.calls["n"] == 1
    assert slept == []


# ---------------------------------------------------------------------------
# executor: mutations carry an idempotency key and are NOT auto-retried
# (unless the gate flag is explicitly enabled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_attaches_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sleep(monkeypatch)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client_with_handler(handler) as client:
        await mcp_server._request_with_retry(
            client, "POST", "http://svc/claims", json={"x": 1}, idempotent=False
        )
    key = captured[0].headers.get(mcp_server._IDEMPOTENCY_HEADER)
    assert key
    # Looks like a uuid4 hex-with-dashes.
    assert len(key) == 36 and key.count("-") == 4


@pytest.mark.asyncio
async def test_idempotent_read_has_no_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sleep(monkeypatch)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client_with_handler(handler) as client:
        await mcp_server._request_with_retry(
            client, "GET", "http://svc/claims", idempotent=True
        )
    assert mcp_server._IDEMPOTENCY_HEADER not in captured[0].headers


@pytest.mark.asyncio
async def test_mutation_not_retried_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COORD_RETRY_MUTATIONS", raising=False)
    slept = _no_sleep(monkeypatch)
    handler = _sequence_handler([503, 200])
    async with _client_with_handler(handler) as client:
        r = await mcp_server._request_with_retry(
            client, "POST", "http://svc/claims", json={}, idempotent=False
        )
    # Default OFF: the first 503 is returned without a retry, so the caller's
    # status handling decides -- no risk of double-create.
    assert r.status_code == 503
    assert handler.calls["n"] == 1
    assert slept == []


@pytest.mark.asyncio
async def test_mutation_transient_exc_not_retried_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COORD_RETRY_MUTATIONS", raising=False)
    _no_sleep(monkeypatch)
    req = httpx.Request("POST", "http://svc/claims")
    handler = _sequence_handler([httpx.ConnectError("down", request=req), 200])
    async with _client_with_handler(handler) as client:
        with pytest.raises(httpx.ConnectError):
            await mcp_server._request_with_retry(
                client, "POST", "http://svc/claims", json={}, idempotent=False
            )


@pytest.mark.asyncio
async def test_mutation_retried_when_flag_enabled_uses_stable_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Opt in to the experimental gate; the SAME idempotency key must be sent
    # on every retry so a future de-duping server collapses them to one.
    monkeypatch.setenv("COORD_RETRY_MUTATIONS", "1")
    slept = _no_sleep(monkeypatch)
    captured: list[httpx.Request] = []
    seq = _sequence_handler([503, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return seq(request)

    async with _client_with_handler(handler) as client:
        r = await mcp_server._request_with_retry(
            client, "POST", "http://svc/claims", json={}, idempotent=False
        )
    assert r.status_code == 200
    assert len(captured) == 3
    assert len(slept) == 2
    keys = {req.headers.get(mcp_server._IDEMPOTENCY_HEADER) for req in captured}
    assert len(keys) == 1 and next(iter(keys))  # identical, non-empty key


@pytest.mark.asyncio
async def test_kwargs_and_method_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sleep(monkeypatch)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client_with_handler(handler) as client:
        await mcp_server._request_with_retry(
            client,
            "DELETE",
            "http://svc/requests/q1",
            params={"engineer": "alex"},
            headers={"Authorization": "Bearer t"},
            idempotent=False,
        )
    req = captured[0]
    assert req.method == "DELETE"
    assert req.url.params.get("engineer") == "alex"
    assert req.headers.get("authorization") == "Bearer t"
    # mutation still gets a key even alongside caller headers
    assert mcp_server._IDEMPOTENCY_HEADER in req.headers
