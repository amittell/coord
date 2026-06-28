"""Optional OpenTelemetry distributed tracing for coord.

Tracing is **disabled by default** and is a complete no-op unless the
operator sets ``COORD_OTEL_ENABLED=true`` (``settings.otel_enabled``).
When disabled, nothing in the OpenTelemetry SDK is imported, no
``TracerProvider`` is installed, and coord behaves byte-for-byte as if
this module did not exist. coord therefore does NOT take a hard runtime
dependency on OpenTelemetry being importable: the SDK is imported
lazily, inside :func:`setup_tracing`, only on the enabled path.

This mirrors the opt-in gating used elsewhere in coord
(``COORD_WEBHOOK_URL`` for webhook delivery, ``COORD_GITHUB_TOKEN`` for
PR-comment delivery): a single env flag turns the feature on, and an
unconfigured deployment pays nothing.

Endpoint configuration is intentionally NOT a coord setting. The OTel
SDK reads the standard environment variables directly:

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` -- the OTLP/HTTP collector base URL,
  e.g. ``http://tempo.tempo.svc.cluster.local:4318``. The HTTP exporter
  appends ``/v1/traces`` to it.
* ``OTEL_SERVICE_NAME`` -- overrides the reported ``service.name``
  (defaults to ``coord``).

The whole setup is wrapped in a fail-open ``try/except`` that catches
both :class:`ImportError` (the SDK extra was not installed) and any
other :class:`Exception` (a malformed endpoint, an unreachable
collector at provider-build time, an instrumentation incompatibility).
A tracing misconfiguration must never take coord's startup down -- on
any failure we log a warning and return, leaving coord untraced but
fully operational.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from fastapi import FastAPI

    from coordination.config import Settings

logger = logging.getLogger(__name__)


def setup_tracing(app: "FastAPI", settings: "Settings") -> None:
    """Install OpenTelemetry tracing on ``app`` when enabled.

    No-op unless ``settings.otel_enabled`` is True. On the enabled path
    this builds a ``TracerProvider`` exporting spans over OTLP/HTTP and
    instruments both the inbound FastAPI app and outbound httpx clients.

    Fails open: any error (missing SDK, bad endpoint, instrumentation
    failure) is logged as a warning and swallowed so coord starts
    regardless.
    """
    if not settings.otel_enabled:
        return

    try:
        # Lazy imports: nothing here is touched on the disabled path, so
        # coord never requires the OpenTelemetry packages to be present
        # unless an operator has opted in.
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from coordination import __version__

        # ``OTEL_SERVICE_NAME`` is honoured by the SDK natively, but we
        # set it explicitly on the Resource so the default is ``coord``
        # rather than the SDK's ``unknown_service``.
        service_name = os.environ.get("OTEL_SERVICE_NAME") or "coord"
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": __version__,
            }
        )

        # The OTLP/HTTP exporter reads OTEL_EXPORTER_OTLP_ENDPOINT from
        # the environment itself; we do not pass an endpoint so the
        # standard OTel env config (and its sensible localhost default)
        # stays in force.
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()

        endpoint = (
            os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            or "OTLP default (http://localhost:4318)"
        )
        logger.info(
            "OpenTelemetry tracing enabled: service=%s exporting to %s",
            service_name,
            endpoint,
        )
    except ImportError:
        logger.warning(
            "COORD_OTEL_ENABLED is set but the OpenTelemetry SDK is not "
            "installed; tracing is disabled. Install the 'otel' extra "
            "(pip install coord-mcp-server[otel]) to enable it."
        )
    except Exception:
        # Any other failure (bad endpoint, instrumentation error) must
        # not break startup -- coord runs untraced.
        logger.warning(
            "OpenTelemetry tracing setup failed; continuing without "
            "tracing.",
            exc_info=True,
        )
