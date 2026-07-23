# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

# --- Builder stage: install pinned deps into a venv, then add the app ---
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Create an isolated venv that we copy into the runtime image.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install pinned runtime dependencies first for better layer caching.
# requirements-otel.txt bundles the OPTIONAL OpenTelemetry tracing stack so
# COORD_OTEL_ENABLED can be toggled at runtime without rebuilding; drop that
# -r for a slimmer, tracing-less image. The default production image remains
# SQLite-only and deliberately does NOT install requirements-postgres.txt;
# PostgreSQL operators build an explicit variant with that optional driver.
COPY requirements.txt requirements-otel.txt /build/
RUN pip install --upgrade pip && \
    pip install -r /build/requirements.txt \
        -r /build/requirements-otel.txt

# Install the app itself with no deps - they are already pinned above.
COPY pyproject.toml README.md /build/
COPY coordination /build/coordination
RUN pip install --no-deps .

# --- Runtime stage: clean slim base, non-root user, app + venv copied in ---
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    COORD_DATABASE_PATH=/data/coordination.db \
    COORD_HOST=0.0.0.0 \
    COORD_PORT=8080

# Install git so COORD_REPO_ROOT can use `git ls-files` for accurate overlap
# detection. Keep the layer small by suppressing recommended packages and
# discarding the apt cache.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and the data directory it will own.
RUN groupadd --system --gid 1000 coord \
    && useradd --system --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin coord \
    && mkdir -p /data /app \
    && chown -R coord:coord /data /app

WORKDIR /app

# Copy the pre-built virtualenv from the builder stage.
COPY --from=builder --chown=coord:coord /opt/venv /opt/venv

USER coord

VOLUME ["/data"]
EXPOSE 8080

# Probe /readyz so DB init failures surface as an unhealthy container,
# not just "process is alive".
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3); sys.exit(0 if r.status == 200 else 1)"

CMD ["coord-api"]
