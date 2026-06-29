"""Tests for the optional OpenTelemetry tracing integration.

Mirrors the opt-in style of ``tests/test_webhooks.py`` /
``tests/test_github_adapter.py``: the feature is gated on a single
setting (``otel_enabled`` / ``COORD_OTEL_ENABLED``) and must be a
complete no-op when off, fail open when misconfigured, and instrument
the app when on with the SDK present.

Cases:

* ``otel_enabled`` defaults to False.
* ``setup_tracing`` is a no-op when disabled -- it returns without
  touching the OpenTelemetry SDK and installs no real tracer provider.
* When enabled but the SDK import fails (or the endpoint is bogus),
  ``setup_tracing`` does NOT raise -- it fails open.
* When enabled with the deps present, it instruments the FastAPI app
  (``FastAPIInstrumentor`` records the app) and installs a non-default
  tracer provider.
"""

from __future__ import annotations

import builtins
import importlib

import pytest
from fastapi import FastAPI

from coordination.config import Settings
from coordination.otel import setup_tracing

# The OpenTelemetry SDK is an optional extra. The enabled-path tests
# that need it are skipped when it is not installed so the suite stays
# green in a bare environment; the disabled-path and fail-open tests
# run regardless because they never require the SDK.
# find_spec raises ModuleNotFoundError (rather than returning None) when the
# top-level ``opentelemetry`` package is absent entirely, which is the case in
# the base [dev] test matrix. Guard it so this module still collects there --
# the disabled / fail-open tests run, and the enabled-path tests skip.
try:
    _HAS_OTEL = importlib.util.find_spec("opentelemetry.sdk.trace") is not None
except ModuleNotFoundError:
    _HAS_OTEL = False


@pytest.fixture()
def isolated_tracer_provider():
    """Isolate the process-global OTel tracer provider for one test.

    OpenTelemetry installs the tracer provider as set-once process-global
    state; without isolation an enabled-path test would leak its provider
    (and its background BatchSpanProcessor/exporter) into the rest of the
    suite and become order-dependent. Reset to a pristine state before the
    test and restore + shut down afterwards.
    """
    import opentelemetry.trace as _t
    from opentelemetry.util._once import Once

    saved_provider = _t._TRACER_PROVIDER
    saved_once = _t._TRACER_PROVIDER_SET_ONCE
    _t._TRACER_PROVIDER = None
    _t._TRACER_PROVIDER_SET_ONCE = Once()
    try:
        yield
    finally:
        installed = _t._TRACER_PROVIDER
        if installed is not None and hasattr(installed, "shutdown"):
            try:
                installed.shutdown()
            except Exception:
                pass
        _t._TRACER_PROVIDER = saved_provider
        _t._TRACER_PROVIDER_SET_ONCE = saved_once


def test_otel_enabled_defaults_false() -> None:
    """The feature is off unless explicitly enabled."""
    settings = Settings(otel_enabled=False)
    assert settings.otel_enabled is False
    # And a freshly constructed Settings with no env override is also off.
    assert Settings().otel_enabled is False


def test_setup_tracing_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled path must not import the SDK or install a provider.

    We sabotage the OpenTelemetry import so that *any* attempt to import
    it would raise; since the disabled path returns before importing, the
    call still succeeds. This proves the no-op does not touch the SDK.
    """
    real_import = builtins.__import__

    def guard(name: str, *args: object, **kwargs: object):
        if name.startswith("opentelemetry"):
            raise AssertionError(
                f"disabled setup_tracing imported {name!r}; expected no-op"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)

    app = FastAPI()
    settings = Settings(otel_enabled=False)

    # Returns None, raises nothing, imports no opentelemetry module.
    assert setup_tracing(app, settings) is None


def test_setup_tracing_fails_open_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled but SDK missing: log a warning, do not raise."""
    real_import = builtins.__import__

    def break_otel(name: str, *args: object, **kwargs: object):
        if name.startswith("opentelemetry"):
            raise ImportError(f"simulated missing dependency: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", break_otel)

    app = FastAPI()
    settings = Settings(otel_enabled=True)

    # Must swallow the ImportError and return cleanly.
    assert setup_tracing(app, settings) is None


@pytest.mark.skipif(not _HAS_OTEL, reason="opentelemetry SDK not installed")
def test_setup_tracing_fails_open_on_bad_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled with a malformed endpoint: still must not raise.

    The OTLP/HTTP exporter and provider are lazy about reaching the
    collector, but a malformed endpoint or any internal error in setup
    must be caught by the broad ``except Exception`` and swallowed.
    """
    monkeypatch.setenv("COORD_OTEL_ENABLED", "true")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://nonexistent.invalid:4318"
    )

    # Force a failure deep inside setup by making set_tracer_provider blow
    # up; the broad except must catch it.
    from opentelemetry import trace as _trace

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated provider install failure")

    monkeypatch.setattr(_trace, "set_tracer_provider", boom)

    app = FastAPI()
    settings = Settings(otel_enabled=True)

    assert setup_tracing(app, settings) is None


@pytest.mark.skipif(not _HAS_OTEL, reason="opentelemetry SDK not installed")
def test_setup_tracing_instruments_app_when_enabled(
    monkeypatch: pytest.MonkeyPatch, isolated_tracer_provider: None
) -> None:
    """Enabled + deps present: app is instrumented, provider installed."""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from coordination import __version__

    # Point at a plausible (unreachable) collector; the BatchSpanProcessor
    # exports in the background and never blocks setup, so an unreachable
    # endpoint is fine for this test.
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://tempo.tempo.svc.cluster.local:4318",
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "coord-test")

    app = FastAPI()
    settings = Settings(otel_enabled=True)

    try:
        setup_tracing(app, settings)

        # A real SDK TracerProvider is now the global provider (not the
        # default proxy/no-op provider).
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

        # The resource carries our service identity: the OTEL_SERVICE_NAME
        # override and coord's own version (not the SDK's unknown_service).
        attrs = dict(provider.resource.attributes)
        assert attrs.get("service.name") == "coord-test"
        assert attrs.get("service.version") == __version__

        # A BatchSpanProcessor exporting over OTLP/HTTP is wired up -- this
        # would fail if the exporter or processor line were dropped.
        processors = getattr(
            provider._active_span_processor, "_span_processors", ()
        )
        assert any(
            isinstance(p, BatchSpanProcessor)
            and isinstance(p.span_exporter, OTLPSpanExporter)
            for p in processors
        ), "expected a BatchSpanProcessor exporting via OTLP"

        # FastAPIInstrumentor recorded the app as instrumented.
        assert FastAPIInstrumentor._is_instrumented_by_opentelemetry or getattr(
            app, "_is_instrumented_by_opentelemetry", False
        )

        # httpx client instrumentation is active too (outbound spans) --
        # this would fail if the HTTPXClientInstrumentor().instrument() line
        # were removed.
        assert HTTPXClientInstrumentor()._is_instrumented_by_opentelemetry
    finally:
        # Clean up global httpx instrumentation so it does not leak into
        # other tests that build httpx clients.
        try:
            HTTPXClientInstrumentor().uninstrument()
        except Exception:
            pass
        try:
            FastAPIInstrumentor.uninstrument_app(app)
        except Exception:
            pass


@pytest.mark.skipif(not _HAS_OTEL, reason="opentelemetry SDK not installed")
def test_setup_tracing_attaches_to_existing_provider(
    monkeypatch: pytest.MonkeyPatch, isolated_tracer_provider: None
) -> None:
    """If a real TracerProvider is already installed (a foreign library, auto-
    instrumentation, or an earlier call), setup_tracing attaches its OTLP
    exporter to that provider rather than silently failing to replace the
    set-once global."""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    foreign = TracerProvider(resource=Resource.create({"service.name": "foreign"}))
    trace.set_tracer_provider(foreign)
    before = len(foreign._active_span_processor._span_processors)

    app = FastAPI()
    try:
        setup_tracing(app, Settings(otel_enabled=True))

        # The foreign provider is kept (not replaced) and gains exactly one
        # OTLP-exporting processor.
        assert trace.get_tracer_provider() is foreign
        procs = foreign._active_span_processor._span_processors
        assert len(procs) == before + 1
        assert any(
            isinstance(p, BatchSpanProcessor)
            and isinstance(p.span_exporter, OTLPSpanExporter)
            for p in procs
        ), "expected our OTLP exporter attached to the existing provider"
    finally:
        try:
            HTTPXClientInstrumentor().uninstrument()
        except Exception:
            pass
        try:
            FastAPIInstrumentor.uninstrument_app(app)
        except Exception:
            pass
