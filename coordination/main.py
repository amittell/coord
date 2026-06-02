from __future__ import annotations

import asyncio
import hmac
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
from coordination.schemas import (
    CreateClaimsRequest,
    ExtendClaimRequest,
    FileRequestRequest,
    PromoteHotspotRequest,
    ReleaseClaimsRequest,
    RespondToRequestRequest,
)

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
                await get_service().db.expire_stale_claims(
                    idle_timeout_sec=settings.idle_timeout_sec
                )
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
    if not hmac.compare_digest(token, settings.auth_token):
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
    session_id: str | None = None,
    _: None = Depends(require_auth),
) -> dict:
    rows = await get_service().list_claims(
        active_only=active,
        engineer=engineer,
        module_substring=module,
        session_id=session_id,
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


@app.get("/metrics/hotspots")
async def hotspots_metric(
    days: int = Query(default=30, ge=1, le=90),
    min_attempts: int = Query(default=5, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    repo: str | None = Query(default=None),
    _: None = Depends(require_auth),
) -> dict:
    """Top files by blocked claim attempts (v0.20).

    Operators look at this list to decide which files belong in a
    ``shared_file`` rule or which areas need to be split into modules.
    """
    rows = await get_service().db.hotspot_files(
        days=days, min_attempts=min_attempts, limit=limit, repo=repo,
    )
    return {
        "hotspots": rows,
        "days": days,
        "min_attempts": min_attempts,
        "count": len(rows),
    }


@app.post("/metrics/hotspots/promote")
async def promote_hotspot(
    body: PromoteHotspotRequest,
    _: None = Depends(require_auth),
) -> dict:
    """v0.21 soft auto-promote.

    Write ``body.pattern`` into the active ownership YAML, either as a
    top-level ``shared_files`` rule (action='shared_file') or as an
    informational ``suggested_splits`` entry (action='split').
    Idempotent: re-promoting a pattern that's already present is a
    no-op and returns the unchanged YAML.

    The dashboard renders an "apply" link per qualifying hotspot row
    pointing at this endpoint; the operator is in the loop.
    """
    try:
        patched = await get_service().promote_hotspot(
            action=body.action,
            pattern=body.pattern,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "action": body.action,
        "pattern": body.pattern,
        "repo": body.repo,
        "patched_yaml": patched,
    }


@app.get("/metrics/auto-resolutions")
async def auto_resolutions_metric(
    days: int = Query(default=30, ge=1, le=90),
    repo: str | None = Query(default=None),
    _: None = Depends(require_auth),
) -> dict:
    """Daily auto-coexist / auto-narrow counts per repo (v0.18).

    The series is consumed by the dashboard heatmap and is also
    available standalone for external monitoring. Empty days are
    omitted -- callers that want a dense grid fill the gaps.
    """
    rows = await get_service().db.daily_auto_resolutions(days=days, repo=repo)
    return {"series": rows, "days": days, "count": len(rows)}


@app.get("/conflicts")
async def conflicts(
    pattern: list[str] | None = Query(default=None),
    engineer: str = Query(...),
    repo: str | None = Query(default=None),
    session_id: list[str] | None = Query(default=None),
    _: None = Depends(require_auth),
) -> dict:
    if not pattern:
        raise HTTPException(status_code=400, detail="Provide one or more pattern= query params")
    try:
        # FastAPI parses repeated `session_id=` query params into a
        # list. The pre-push hook forwards every live session_id from
        # .coordination/sessions.live so the agent's own subagent
        # claims under different engineer names don't false-positive
        # on its own push.
        result = await get_service().check_conflicts(
            patterns=pattern,
            engineer=engineer,
            repo=repo,
            session_ids=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/sessions/{session_id}/release")
async def release_session(
    session_id: str,
    _: None = Depends(require_auth),
) -> dict:
    """Release every active claim that was created with the given
    session_id. Intended for end-of-work cleanup so a single call from
    coord-mcp tears down everything the agent and its subagents
    produced, regardless of engineer name."""
    n = await get_service().db.release_for_session(session_id)
    return {"released": n}


@app.get("/sessions/{session_id}/pending_requests")
async def pending_requests(
    session_id: str,
    _: None = Depends(require_auth),
) -> dict:
    """Return the merged inbox the holder polls. Includes first-class
    release requests (``kind='request'``) and the read-only auto-conflict
    log (``kind='auto-conflict'``). An active holder polls this between
    operations so they can approve / deny pending release requests and
    see who has been blocked on their scope. Coord-mcp exposes this as
    a `pending_requests` tool."""
    rows = await get_service().pending_requests(session_id)
    return {"pending": rows, "count": len(rows)}


@app.post("/requests")
async def file_request(
    body: FileRequestRequest,
    _: None = Depends(require_auth),
) -> JSONResponse:
    """File a release request against an active claim. Filing shortens
    the holder's claim TTL to ``COORD_REQUEST_TTL_SHORT_SEC`` (default
    300s) and creates an audit-logged ``filed`` event. With
    ``wait_seconds > 0`` (default 60) the server holds the connection
    open until the holder responds, the shortened TTL fires, or the
    timeout elapses -- whichever comes first.
    """
    svc = get_service()
    try:
        request = await svc.file_request(
            claim_id=body.claim_id,
            requester=body.requester,
            requester_session_id=body.session_id,
            reason=body.reason,
            urgency=body.urgency,
            requested_scope=body.requested_scope,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if body.wait_seconds > 0:
        final = await svc.wait_for_decision(
            request["id"], timeout_seconds=body.wait_seconds
        )
        if final is not None:
            request = final

    return JSONResponse(status_code=200, content=request)


@app.post("/requests/{request_id}/respond")
async def respond_to_request(
    request_id: str,
    body: RespondToRequestRequest,
    _: None = Depends(require_auth),
) -> JSONResponse:
    """The holder responds to an open request.

    - ``approved`` releases the claim immediately.
    - ``denied`` restores the claim's original TTL.
    - ``narrowed`` (v0.11+) closes the original claim and opens a new
      one under ``narrowed_pattern`` (must be a subset of the holder's
      current pattern; rejected with 400 otherwise).
    - ``coexist`` (v0.11+) grants the requester a sibling claim on
      ``coexist_pattern``. Both holder and requester end up with active
      claims, mutually self-excluded via ``claims.coexists_with``.

    All transitions are audit-logged."""
    if body.decision not in ("approved", "denied", "narrowed", "coexist"):
        raise HTTPException(
            status_code=400,
            detail=(
                "decision must be one of 'approved', 'denied', "
                "'narrowed', 'coexist'"
            ),
        )
    if body.decision == "narrowed" and not body.narrowed_pattern:
        raise HTTPException(
            status_code=400,
            detail="decision='narrowed' requires a non-empty 'narrowed_pattern'",
        )
    if body.decision == "coexist" and not body.coexist_pattern:
        raise HTTPException(
            status_code=400,
            detail="decision='coexist' requires a non-empty 'coexist_pattern'",
        )
    try:
        result = await get_service().respond_to_request(
            request_id=request_id,
            decision=body.decision,
            actor_engineer=body.engineer,
            actor_session_id=body.session_id,
            note=body.note,
            narrowed_pattern=body.narrowed_pattern,
            coexist_pattern=body.coexist_pattern,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="request not found")
    return JSONResponse(status_code=200, content=result)


@app.get("/requests")
async def list_requests(
    requester: str | None = None,
    claim_id: str | None = None,
    decision: str | None = None,
    _: None = Depends(require_auth),
) -> dict:
    """List requests filtered by requester, claim, or decision state.
    Used by both the requester (``my_requests``) and the operator
    dashboard."""
    rows = await get_service().list_requests(
        requester_engineer=requester,
        claim_id=claim_id,
        decision=decision,
    )
    return {"requests": rows, "count": len(rows)}


@app.get("/requests/{request_id}")
async def get_request(
    request_id: str,
    _: None = Depends(require_auth),
) -> JSONResponse:
    row = await get_service().get_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    return JSONResponse(status_code=200, content=row)


@app.get("/requests/{request_id}/events")
async def get_request_events(
    request_id: str,
    _: None = Depends(require_auth),
) -> dict:
    """Return the immutable audit-event timeline for a request,
    oldest first. Each row carries the actor, timestamp, and a JSON
    detail blob with the per-event-type specifics."""
    rows = await get_service().list_request_events(request_id)
    return {"events": rows, "count": len(rows)}


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
