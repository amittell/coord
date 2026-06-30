"""Logging primitives for the coordination service.

Two concerns live together here because they share a context variable:

1. Per-request IDs. Every inbound HTTP request gets a stable
   ``X-Request-ID``, either the value the client supplied or a
   freshly generated one. The middleware in :mod:`coordination.main`
   sets :data:`request_id_var` for the duration of the request so any
   log record emitted during that request can pick it up.
2. Structured (JSON) logs. :class:`JsonFormatter` serialises each
   :class:`logging.LogRecord` to a one-line JSON object, including the
   request ID when one is bound.

JSON output is the default: :func:`configure_logging` installs the JSON
formatter unless ``COORD_LOG_JSON`` is explicitly set to a falsy value
(``0``/``false``/``no``/``off``), in which case it falls back to a plain
human-readable formatter -- convenient for local development. Structured
JSON is the right default for the deployments coord runs in (a log
aggregator like Loki ingests the container stream).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import UTC, datetime

# Per-request identifier, set by the FastAPI middleware and read by
# :class:`JsonFormatter`. Empty string means "no request in flight".
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "coord_request_id", default=""
)

# Dedicated logger name for per-request access logs. Lives under the
# ``coordination`` namespace so it inherits handler and level
# configuration, but can be targeted independently by log aggregators
# (e.g. drop or route just the access stream).
ACCESS_LOGGER_NAME = "coordination.access"


# LogRecord has a known set of built-in attributes. Anything else the
# caller attached via ``logger.info(..., extra={...})`` is considered
# user-defined and should be pulled into the JSON output.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Format a :class:`LogRecord` as a single-line JSON object.

    Always emits ``ts`` (ISO-8601 UTC with ``Z`` suffix), ``level``,
    ``logger`` and ``msg``. Includes ``request_id`` only when the
    context variable is non-empty so scrapers do not have to carry
    around a null field for every non-request log line. Exception info,
    if present on the record, is embedded under ``exc`` as a rendered
    multi-line string.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # Surface any caller-supplied extras without overwriting the
        # base keys above.
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value

        return json.dumps(payload, separators=(",", ":"))


def _env_flag(name: str, *, default: bool) -> bool:
    """Read a boolean env flag, falling back to ``default``.

    Truthy: ``1``/``true``/``yes``/``on``. Falsy: ``0``/``false``/``no``/
    ``off``. Unset or unrecognised returns ``default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return default


def configure_logging() -> None:
    """Configure the ``coordination`` logger namespace.

    Reads ``COORD_LOG_LEVEL`` (default ``INFO``) for the level. Logs are
    structured one-line JSON by default (best for log aggregators such as
    Loki); set ``COORD_LOG_JSON=false`` to fall back to a plain
    human-readable formatter. This only configures the ``coordination``
    logger so uvicorn's own access logs keep their existing formatting.
    """
    level_name = os.environ.get("COORD_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO

    handler = logging.StreamHandler(stream=sys.stderr)
    if _env_flag("COORD_LOG_JSON", default=True):
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )

    logger = logging.getLogger("coordination")
    logger.setLevel(level)
    # Replace existing handlers so repeated calls (e.g. tests, reload)
    # do not stack duplicate handlers.
    logger.handlers = [handler]
    logger.propagate = False
