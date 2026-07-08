"""Prometheus-style metrics registry.

Emits the Prometheus text exposition format (a.k.a. OpenMetrics text) from
scratch, without adding a runtime dependency on the official client. The
surface area is intentionally small: counters and gauges with optional
label sets, a process-global ``Registry``, and a ``render`` method that
produces the ``text/plain; version=0.0.4`` body served at ``/metrics``.

Tests reset the module-level singletons between runs via
:func:`_reset_for_tests` to keep counter values isolated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus/OpenMetrics exposition spec.

    Backslashes, double quotes, and newlines must be escaped. Other
    characters (including UTF-8) pass through unchanged.
    """
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


class _Metric:
    """Base class for counters and gauges. Stores samples keyed by the
    tuple of label values (empty tuple for an unlabeled metric)."""

    type_name: str = "untyped"

    def __init__(
        self,
        name: str,
        help_text: str,
        labels: tuple[str, ...] = (),
        registry: Registry | None = None,
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.labels = tuple(labels)
        self.values: dict[tuple[str, ...], float] = {}
        if registry is None:
            registry = _default_registry
        registry.register(self)

    def _key(self, label_values: dict[str, str]) -> tuple[str, ...]:
        if set(label_values) != set(self.labels):
            raise ValueError(
                f"metric {self.name!r} expects labels {self.labels}, "
                f"got {tuple(label_values)}"
            )
        return tuple(label_values[name] for name in self.labels)

    def _reset(self) -> None:
        self.values.clear()

    def render(self) -> str:
        lines: list[str] = []
        lines.append(f"# HELP {self.name} {self.help_text}")
        lines.append(f"# TYPE {self.name} {self.type_name}")
        if not self.values:
            if not self.labels:
                lines.append(f"{self.name} 0.0")
            return "\n".join(lines) + "\n"
        for key, value in self.values.items():
            if self.labels:
                label_bits = ",".join(
                    f'{name}="{_escape_label_value(val)}"'
                    for name, val in zip(self.labels, key, strict=True)
                )
                lines.append(f"{self.name}{{{label_bits}}} {float(value)}")
            else:
                lines.append(f"{self.name} {float(value)}")
        return "\n".join(lines) + "\n"


class Counter(_Metric):
    """A monotonically increasing counter."""

    type_name = "counter"

    def inc(self, amount: float = 1.0, **label_values: str) -> None:
        if amount < 0:
            raise ValueError("Counter.inc amount must be non-negative")
        key = self._key(label_values)
        self.values[key] = self.values.get(key, 0.0) + float(amount)


class Gauge(_Metric):
    """A gauge that can be set to an arbitrary value."""

    type_name = "gauge"

    def set(self, value: float, **label_values: str) -> None:
        key = self._key(label_values)
        self.values[key] = float(value)

    def inc(self, amount: float = 1.0, **label_values: str) -> None:
        key = self._key(label_values)
        self.values[key] = self.values.get(key, 0.0) + float(amount)


class Registry:
    """Collection of metrics. Renders them in registration order."""

    def __init__(self) -> None:
        self._metrics: list[_Metric] = []
        self._names: set[str] = set()

    def register(self, metric: _Metric) -> None:
        """Register a metric, enforcing name uniqueness.

        Duplicate names would render duplicate ``# HELP``/``# TYPE``
        blocks for one family, which Prometheus rejects -- and a
        rejected exposition drops the ENTIRE scrape, not just the
        duplicate. Raise at registration time (import time for the
        module-level singletons) so the mistake fails fast instead of
        silently breaking monitoring at scrape time. This matches the
        official client's ``Duplicated timeseries in CollectorRegistry``
        behavior.
        """
        if metric.name in self._names:
            raise ValueError(
                f"duplicate metric name {metric.name!r} already registered"
            )
        self._names.add(metric.name)
        self._metrics.append(metric)

    def render(self) -> str:
        return "".join(m.render() for m in self._metrics)

    def _reset_for_tests(self) -> None:
        for m in self._metrics:
            m._reset()

    def __iter__(self) -> Iterable[_Metric]:
        return iter(self._metrics)


_default_registry = Registry()
registry = _default_registry


# --- Module-level singletons used across the coordination package --------

claims_created_total = Counter(
    "claims_created_total",
    "Total claims successfully created, by ownership severity.",
    labels=("severity",),
)

claims_conflicts_total = Counter(
    "claims_conflicts_total",
    "Total claim-creation attempts that collided with an existing active claim.",
)

claims_released_total = Counter(
    "claims_released_total",
    "Total claims released (explicitly or via batch release).",
)

auth_failures_total = Counter(
    "auth_failures_total",
    "Total inbound requests rejected for missing or invalid bearer token.",
)

# v0.44 scale: SQLite writer-queue observability. writes_total gives write
# throughput; writer_wait_seconds_total is cumulative time spent waiting for
# the single-writer lock -- if it climbs fast relative to wall time the writer
# is the bottleneck (consider lightening create_claims, or Postgres).
sqlite_writes_total = Counter(
    "sqlite_writes_total",
    "Write commits through the funnelled SQLite writer-queue path (a commit "
    "may span multiple statements, e.g. a claim-grant unit-of-work). Stays 0 "
    "when COORD_SQLITE_WRITER_QUEUE is off and excludes the few legacy write "
    "paths that still open their own connections.",
)
sqlite_writer_wait_seconds_total = Counter(
    "sqlite_writer_wait_seconds_total",
    "Cumulative seconds spent waiting to acquire the SQLite writer lock. Only "
    "meaningful with COORD_SQLITE_WRITER_QUEUE on -- 0 with the queue off "
    "means unmeasured, not uncontended.",
)

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled, by method, path template, and status code.",
    labels=("method", "path", "status"),
)

build_info = Gauge(
    "build_info",
    "Constant gauge carrying the running coordination service version in a label.",
    labels=("version",),
)


def set_build_info(version: str) -> None:
    """Set the ``build_info`` gauge to 1.0 for the current version.
    Called once from the FastAPI lifespan at service start, and once at
    import time so the gauge is populated even before lifespan runs
    (e.g. under a bare ASGI test transport that short-circuits lifespan,
    or right after :func:`_reset_for_tests` in test fixtures)."""
    build_info.set(1.0, version=version)


def _reset_for_tests() -> None:
    """Zero out all registered metric samples, then repopulate the
    constant ``build_info`` gauge so it remains present across test
    runs. Test-only."""
    _default_registry._reset_for_tests()
    # Late import to avoid a circular import at module load.
    from coordination import __version__

    set_build_info(__version__)


# Populate build_info at import time so the gauge is always present
# when /metrics is scraped, even before the FastAPI lifespan runs.
from coordination import __version__ as _pkg_version  # noqa: E402

set_build_info(_pkg_version)
