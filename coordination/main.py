from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from coordination import __version__
from coordination import metrics
from coordination.config import get_settings
from coordination.dashboard import render_dashboard
from coordination.db import acquire_instance_lock
from coordination.deps import get_service
from coordination.logging import ACCESS_LOGGER_NAME, configure_logging, request_id_var
from coordination.ownership import parse_ownership_yaml
from coordination.schemas import CreateClaimsRequest, ExtendClaimRequest, ReleaseClaimsRequest

logger = logging.getLogger(__name__)
access_logger = logging.getLogger(ACCESS_LOGGER_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    if not settings.auth_token and not settings.allow_insecure_no_auth:
        raise RuntimeError(
            "Set COORD_AUTH_TOKEN, or explicitly allow insecure local mode with "
            "COORD_ALLOW_INSECURE_NO_AUTH=true."
        )

    # Take an advisory lock on <db>.lock before opening the database.
    # Held for the process lifetime; stash the fd on app state so it is
    # not garbage-collected mid-run. fcntl auto-releases the flock when
    # the fd is closed or the process exits.
    app.state.instance_lock_fd = acquire_instance_lock(settings.database_path)

    await get_service().db.init()
    metrics.set_build_info(__version__)

    if os.environ.get("COORD_DISABLE_BACKGROUND_CLEANUP", "").lower() in {"1", "true", "yes"}:
        yield
        return

    async def cleanup_loop() -> None:
        while True:
            try:
                await get_service().db.expire_stale_claims()
            except Exception:  # pragma: no cover - background cleanup failures are logged
                logger.exception("Failed to expire stale claims")
            await asyncio.sleep(settings.cleanup_interval_sec)

    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Multi-Agent Coordination", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    """Bind a stable per-request ID for the duration of the request.

    Honours an inbound ``X-Request-ID`` header if the client supplied
    one (useful for tracing a call across multiple services); otherwise
    mints a 16-character hex id. The id is exposed via
    :data:`coordination.logging.request_id_var` so log records emitted
    during the request can pick it up, and echoed back on the response
    so callers can correlate errors with server-side logs."""
    rid = request.headers.get("x-request-id") or uuid4().hex[:16]
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def _count_http_requests(request: Request, call_next):
    """Increment ``http_requests_total`` after each response. Uses the
    matched route template (e.g. ``/claims/{claim_id}``) for the ``path``
    label so cardinality stays bounded; falls back to the raw URL path
    if routing did not attach a matched route (404s, /metrics itself)."""
    response = await call_next(request)
    route = request.scope.get("route")
    path_label = getattr(route, "path", None) or request.url.path
    metrics.http_requests_total.inc(
        method=request.method,
        path=path_label,
        status=str(response.status_code),
    )
    return response


@app.middleware("http")
async def _access_log_middleware(request: Request, call_next):
    """Emit one structured access-log record per HTTP request.

    Registered last so it wraps the other middlewares; this means
    ``call_next`` unwinds through the request_id middleware (which
    stamps ``X-Request-ID`` on the response) before we emit the log.
    Reading the request id off the response header rather than the
    contextvar keeps the middleware order-independent: the contextvar
    is already ``reset`` by the time we log, but the header value is
    still on the response.

    Uses the matched route template for ``path`` (e.g.
    ``/claims/{claim_id}``) so downstream log aggregators see bounded
    cardinality; falls back to the raw URL path when routing did not
    attach a matched route (404s, static endpoints like ``/metrics``)."""
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000.0
    route = request.scope.get("route")
    path_label = getattr(route, "path", None) or request.url.path
    access_logger.info(
        "http_request",
        extra={
            "event": "http_request",
            "method": request.method,
            "path": path_label,
            "status": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "request_id": response.headers.get("X-Request-ID", ""),
        },
    )
    return response


def require_auth(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.auth_token:
        if settings.allow_insecure_no_auth:
            return
        raise HTTPException(
            status_code=500,
            detail=(
                "Server misconfigured: set COORD_AUTH_TOKEN or "
                "COORD_ALLOW_INSECURE_NO_AUTH=true"
            ),
        )
    if not authorization or not authorization.startswith("Bearer "):
        metrics.auth_failures_total.inc()
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.auth_token:
        metrics.auth_failures_total.inc()
        raise HTTPException(status_code=401, detail="Invalid bearer token")


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "ok"


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus scrape endpoint. Intentionally unauthenticated: this is
    the convention for Prometheus scrapers which rarely support bearer
    tokens. Operators who need to restrict access should front the
    service with a reverse proxy that gates ``/metrics`` separately."""
    body = metrics.registry.render()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    settings = get_settings()
    await get_service().db.init()
    return {
        "status": "ready",
        "version": __version__,
        "auth_mode": settings.auth_mode,
        "database_path": str(settings.database_path),
    }


@app.get("/meta")
async def meta() -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "name": "multi-agent-coordination",
        "version": __version__,
        "auth_mode": settings.auth_mode,
        "repo_root_configured": bool(settings.repo_root),
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(_: None = Depends(require_auth)) -> str:
    return await render_dashboard()


@app.post("/claims")
async def create_claims(body: CreateClaimsRequest, _: None = Depends(require_auth)) -> JSONResponse:
    result = await get_service().create_claims(body)
    payload = jsonable_encoder(result)
    if result.conflicts:
        return JSONResponse(status_code=409, content=payload)
    if not result.claim_ids and result.warnings:
        return JSONResponse(status_code=400, content=payload)
    return JSONResponse(status_code=200, content=payload)


@app.get("/claims")
async def list_claims(
    active: bool = Query(default=True, alias="active_only"),
    engineer: str | None = None,
    module: str | None = None,
    repo: str | None = None,
    _: None = Depends(require_auth),
) -> dict:
    rows = await get_service().list_claims(
        active_only=active,
        engineer=engineer,
        module_substring=module,
    )
    if repo is not None:
        rows = [r for r in rows if r.get("repo") == repo]
    return {"claims": rows, "count": len(rows)}


@app.get("/repos")
async def list_repos(_: None = Depends(require_auth)) -> dict:
    """Per-repo activity summary: one row for each distinct repo that has
    ever submitted a claim with a non-null repo identifier."""
    rows = await get_service().db.list_repos()
    return {"repos": rows, "count": len(rows)}


@app.get("/conflicts")
async def conflicts(
    pattern: list[str] | None = Query(default=None),
    engineer: str = Query(...),
    _: None = Depends(require_auth),
) -> dict:
    if not pattern:
        raise HTTPException(status_code=400, detail="Provide one or more pattern= query params")
    try:
        result = await get_service().check_conflicts(patterns=pattern, engineer=engineer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.model_dump()


@app.delete("/claims/{claim_id}")
async def delete_claim(
    claim_id: str,
    engineer: str | None = Query(default=None),
    _: None = Depends(require_auth),
) -> dict:
    released = await get_service().release_claims([claim_id], engineer)
    return {"released": released}


@app.post("/claims/release")
async def release_claims(body: ReleaseClaimsRequest, _: None = Depends(require_auth)) -> dict:
    released = await get_service().release_claims(body.claim_ids, body.engineer)
    return {"released": released}


@app.post("/claims/{claim_id}/extend")
async def extend_claim(
    claim_id: str,
    body: ExtendClaimRequest,
    _: None = Depends(require_auth),
) -> dict:
    ok = await get_service().extend_claim(claim_id, body.engineer, body.ttl_hours)
    if not ok:
        raise HTTPException(status_code=404, detail="Claim not found or not owned")
    return {"ok": True}


@app.post("/config/ownership")
async def set_ownership(request: Request, _: None = Depends(require_auth)) -> dict:
    raw = await request.body()
    text = raw.decode("utf-8")
    try:
        rules = parse_ownership_yaml(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await get_service().set_ownership_yaml(text)
    return {"ok": True, "rules": len(rules)}


@app.get("/config/ownership")
async def get_ownership(_: None = Depends(require_auth)) -> Response:
    text = await get_service().get_ownership_yaml()
    if text is None:
        return PlainTextResponse("", status_code=204)
    return PlainTextResponse(text, media_type="text/yaml")


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "coordination.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    run()
