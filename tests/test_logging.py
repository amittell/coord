from __future__ import annotations

import json
import logging

import pytest

from coordination import logging as coord_logging


def _make_record(msg: str = "hello", logger_name: str = "coordination.test") -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name,
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_json_formatter_emits_valid_json() -> None:
    formatter = coord_logging.JsonFormatter()
    rec = _make_record()
    out = formatter.format(rec)
    parsed = json.loads(out)
    for key in ("ts", "level", "logger", "msg"):
        assert key in parsed, f"missing {key}"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "coordination.test"
    assert parsed["msg"] == "hello"


def test_json_formatter_includes_request_id_when_set() -> None:
    formatter = coord_logging.JsonFormatter()
    token = coord_logging.request_id_var.set("rid-9999")
    try:
        out = formatter.format(_make_record())
    finally:
        coord_logging.request_id_var.reset(token)
    parsed = json.loads(out)
    assert parsed.get("request_id") == "rid-9999"


def test_json_formatter_omits_request_id_when_unset() -> None:
    formatter = coord_logging.JsonFormatter()
    # Ensure empty / default context
    token = coord_logging.request_id_var.set("")
    try:
        out = formatter.format(_make_record())
    finally:
        coord_logging.request_id_var.reset(token)
    parsed = json.loads(out)
    assert "request_id" not in parsed


def test_configure_logging_uses_env_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_LOG_LEVEL", "WARNING")
    monkeypatch.delenv("COORD_LOG_JSON", raising=False)
    coord_logging.configure_logging()
    assert logging.getLogger("coordination").level == logging.WARNING


def test_configure_logging_json_mode_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_LOG_JSON", "true")
    monkeypatch.setenv("COORD_LOG_LEVEL", "INFO")
    coord_logging.configure_logging()
    handlers = logging.getLogger("coordination").handlers
    assert handlers, "configure_logging should install at least one handler"
    assert any(isinstance(h.formatter, coord_logging.JsonFormatter) for h in handlers)


def test_configure_logging_defaults_to_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COORD_LOG_JSON", raising=False)
    monkeypatch.setenv("COORD_LOG_LEVEL", "INFO")
    coord_logging.configure_logging()
    handlers = logging.getLogger("coordination").handlers
    assert handlers
    assert not any(isinstance(h.formatter, coord_logging.JsonFormatter) for h in handlers)


def test_access_log_record_has_event_field() -> None:
    """An access-log record passed through JsonFormatter must round-trip
    the structured extras used by the middleware (method, path, status,
    duration_ms, request_id, event=http_request)."""
    formatter = coord_logging.JsonFormatter()
    record = logging.LogRecord(
        name="coordination.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="http_request",
        args=(),
        exc_info=None,
    )
    record.event = "http_request"
    record.method = "GET"
    record.path = "/health"
    record.status = 200
    record.duration_ms = 1.23
    record.request_id = "rid-abc"

    out = formatter.format(record)
    parsed = json.loads(out)
    assert parsed["event"] == "http_request"
    assert parsed["method"] == "GET"
    assert parsed["path"] == "/health"
    assert parsed["status"] == 200
    assert parsed["duration_ms"] == 1.23
    assert parsed["request_id"] == "rid-abc"
    assert parsed["logger"] == "coordination.access"
