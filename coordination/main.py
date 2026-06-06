from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
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

    async def auto_demote_loop() -> None:
        """v0.23 background sweep: every
        ``settings.auto_demote_interval_sec`` seconds, ask the service
        to demote coord-managed ``shared_files`` entries whose rolling
        409 count has dropped below ``auto_promote_threshold``.
        Disabled when the interval is 0 or threshold is 0 (the service
        layer short-circuits the latter)."""
        while True:
            try:
                await get_service()._maybe_auto_demote()
            except Exception:  # pragma: no cover - background failures are logged
                logger.exception("Failed to run auto-demote sweep")
            await asyncio.sleep(settings.auto_demote_interval_sec)

    async def webhook_delivery_loop() -> None:
        """v0.27 background delivery: every
        ``settings.webhook_delivery_interval_sec`` seconds, drain the
        webhook outbox. Exceptions are logged and swallowed so a single
        bad tick never tears the loop down. The loop is only started
        when ``COORD_WEBHOOK_URL`` is configured so deployments that
        don't use webhooks pay no scheduler overhead."""
        while True:
            try:
                await get_service().deliver_pending_webhooks()
            except Exception:  # pragma: no cover - background failures are logged
                logger.exception("webhook_delivery_loop: tick failed")
            await asyncio.sleep(settings.webhook_delivery_interval_sec)

    task = asyncio.create_task(cleanup_loop())
    tasks: list[asyncio.Task] = [task]
    if settings.auto_demote_interval_sec > 0:
        tasks.append(asyncio.create_task(auto_demote_loop()))
    if settings.webhook_url:
        tasks.append(asyncio.create_task(webhook_delivery_loop()))
    yield
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
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


def _engineer_from_request(request: Request) -> str | None:
    """Extract the caller's engineer identity from request signals so the
    v0.28 backpressure middleware can attribute queue depth correctly.

    Tries (in order):

    1. ``X-Coord-Engineer`` request header -- the explicit declaration
       the coord-mcp wrapper will send once it is updated.
    2. ``engineer`` query string param -- already used by
       ``/conflicts``, ``/claims``, ``/requests``, etc.

    The JSON-body fallback (``engineer`` field on POST /claims and
    POST /requests) is intentionally deferred: ASGI middlewares run
    before the route, and consuming ``request.body()`` here would
    detach the bytes from the downstream Pydantic parser. Header +
    query covers the vast majority of real call paths today; the
    wrapper can ship the explicit ``X-Coord-Engineer`` header in a
    follow-up so body parsing is never needed.
    """
    explicit = request.headers.get("x-coord-engineer")
    if explicit:
        stripped = explicit.strip()
        if stripped:
            return stripped
    qp = request.query_params.get("engineer")
    if qp:
        stripped = qp.strip()
        if stripped:
            return stripped
    return None


@app.middleware("http")
async def _backpressure_middleware(request: Request, call_next):
    """v0.28: stamp ``X-Coord-Queue-Depth`` on responses when the caller's
    engineer can be identified, so clients self-regulate without a
    follow-up ``GET /requests?queued=true``.

    Best-effort by design:

    * Skipped entirely when ``settings.backpressure_header`` is False
      (operators on receivers that strip unknown headers).
    * Skipped when no engineer signal is present (anonymous health
      checks, /metrics scrapes, etc.).
    * Counting errors are swallowed so a transient DB issue never
      poisons the underlying response -- the header just goes missing
      for that one request.
    """
    response = await call_next(request)
    settings = get_settings()
    if not settings.backpressure_header:
        return response
    engineer = _engineer_from_request(request)
    if not engineer:
        return response
    try:
        depth = await get_service().count_queued_for(engineer)
        response.headers["X-Coord-Queue-Depth"] = str(depth)
    except Exception:  # pragma: no cover - best-effort header
        logger.debug(
            "backpressure_middleware: queue depth lookup failed",
            exc_info=True,
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


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """v0.29: two-tier bearer auth.

    Per-engineer tokens (the new, recommended path) live in the
    ``engineer_tokens`` table as sha256 hashes. The shared
    ``COORD_AUTH_TOKEN`` is still accepted by default for
    back-compat -- existing clients keep working untouched on
    upgrade -- and is fully rejected once the operator flips
    ``COORD_REQUIRE_PER_ENGINEER_TOKEN=true``.

    The dashboard cookie also feeds this path: when the browser
    has a ``coord_session`` cookie set by ``/dashboard/login``, we
    treat its value as a bearer token, which means the same
    per-engineer / shared-token lookup applies to browser and
    headless clients uniformly. That keeps the security model
    flat -- there is no separate "session token" type to audit.
    """
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

    token = _extract_bearer(authorization, request)
    if not token:
        metrics.auth_failures_total.inc()
        raise HTTPException(status_code=401, detail="Missing bearer token")

    # Try per-engineer first: a leaked per-engineer token surfaces
    # in ``coord tokens list`` and can be revoked individually,
    # which is the whole point of having them.
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    service = get_service()
    engineer_row = await service.db.lookup_engineer_token(token_hash)
    if engineer_row is not None:
        # Best-effort touch. The auth path must never 401 because
        # the update failed (e.g. transient lock contention), so a
        # broad except is correct here.
        try:
            await service.db.touch_engineer_token(token_hash)
        except Exception:  # noqa: BLE001 - intentional swallow
            pass
        request.state.engineer = engineer_row["engineer"]
        request.state.auth_kind = "per_engineer"
        return

    if settings.require_per_engineer_token:
        metrics.auth_failures_total.inc()
        raise HTTPException(
            status_code=401,
            detail=(
                "Per-engineer token required "
                "(COORD_REQUIRE_PER_ENGINEER_TOKEN=true). "
                "Run 'coord tokens create <engineer>' on the server "
                "and paste the new token into .coordination/local.env."
            ),
        )

    # Legacy shared-token fallback.
    if hmac.compare_digest(token, settings.auth_token):
        request.state.auth_kind = "shared"
        return

    metrics.auth_failures_total.inc()
    raise HTTPException(status_code=401, detail="Invalid bearer token")


# Cookie set by ``/dashboard/login``. HTTP-only so JS in any
# extension/widget can't read it; SameSite=Lax so a cross-site GET
# doesn't accidentally carry it; Secure because the login page
# refuses to set it over plaintext (the operator who bypasses the
# secure check is opting in).
DASHBOARD_SESSION_COOKIE = "coord_session"


def _extract_bearer(
    authorization: str | None, request: Request
) -> str | None:
    """Resolve the bearer token from either the ``Authorization``
    header (the standard headless path) or the ``coord_session``
    cookie set by the dashboard login (the browser path). The
    explicit header always wins so an operator can override a
    stale cookie by retrying with curl.
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            return token
    cookie = request.cookies.get(DASHBOARD_SESSION_COOKIE)
    if cookie:
        return cookie.strip()
    return None


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
async def dashboard(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """v0.29: gracefully render the login form for unauthenticated
    browsers instead of returning the raw JSON 401 the rest of the
    API uses. The auth path is exactly the same require_auth uses
    (per-engineer token, then shared, then COORD_REQUIRE flag); a
    failure surfaces as the login form, not a 401.
    """
    settings = get_settings()
    token = _extract_bearer(authorization, request)
    if not token:
        return _login_page_response()

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    service = get_service()
    if await service.db.lookup_engineer_token(token_hash) is not None:
        # Best-effort touch; never let it gate the page render.
        try:
            await service.db.touch_engineer_token(token_hash)
        except Exception:  # noqa: BLE001
            pass
        return HTMLResponse(await render_dashboard())
    if not settings.require_per_engineer_token and settings.auth_token:
        if hmac.compare_digest(token, settings.auth_token):
            return HTMLResponse(await render_dashboard())

    # Stale or wrong token: render login form with an error banner so
    # the user can paste a fresh token without poking at curl.
    metrics.auth_failures_total.inc()
    return _login_page_response(error="Invalid or expired token. Paste a fresh one.")


@app.get("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login_form(request: Request) -> Response:
    """Always serves the login page (even if the user is already
    authenticated) -- this is the bookmark target for ``log out then
    log back in as someone else``. Logging in a fresh time
    overwrites the existing cookie."""
    return _login_page_response()


@app.post("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login_submit(request: Request) -> Response:
    """Validate the submitted token and set the session cookie.

    The form submits ``application/x-www-form-urlencoded`` with one
    field, ``token``. Validation flows through the same per-engineer
    -> shared -> COORD_REQUIRE pipeline as ``require_auth`` so the
    semantics stay identical between header-bearer and cookie-bearer
    paths. On success we set ``coord_session`` and 303 to the
    dashboard; on failure we re-render the login form with an
    error banner."""
    settings = get_settings()
    form = await request.form()
    raw = form.get("token") or ""
    # form.get can return UploadFile when a multipart field is bound to
    # a file input. The login form posts urlencoded text, so we only
    # accept strings -- silently rejecting an upload prevents a curl
    # user from uploading a binary blob that pretends to be a token.
    if not isinstance(raw, str):
        return _login_page_response(error="Invalid token submission.")
    token = raw.strip()
    if not token:
        return _login_page_response(error="Token is required.")

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    service = get_service()
    valid = False
    if await service.db.lookup_engineer_token(token_hash) is not None:
        valid = True
    elif (
        not settings.require_per_engineer_token
        and settings.auth_token
        and hmac.compare_digest(token, settings.auth_token)
    ):
        valid = True

    if not valid:
        metrics.auth_failures_total.inc()
        return _login_page_response(error="Invalid token.")

    response = Response(status_code=303)
    response.headers["Location"] = "/dashboard"
    # Cookie security:
    # * httponly=True  -- no JS in the dashboard can exfiltrate it
    # * samesite=lax   -- SameSite=Strict would log the user out on
    #                     every external link; Lax lets top-level
    #                     navigation carry the cookie while still
    #                     blocking cross-site POSTs (the actual CSRF
    #                     surface). Matches GitHub/GitLab dashboards.
    # * secure         -- mirror the request's scheme. Production is
    #                     served over https through Cloudflare, so
    #                     this resolves to True there; local dev over
    #                     http://127.0.0.1 stays usable because we
    #                     do not force Secure on plaintext requests.
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        value=token,
        max_age=settings.dashboard_session_lifetime_sec,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@app.post("/dashboard/logout", response_class=HTMLResponse)
async def dashboard_logout(request: Request) -> Response:
    """Clear the session cookie and bounce back to the login form.

    Always returns 303 so a browser refresh after logout doesn't
    re-submit a POST. The cookie is cleared regardless of whether
    one was set, so a user who lost their session can hit
    /dashboard/logout to force-clean cookies before the next login.
    """
    response = Response(status_code=303)
    response.headers["Location"] = "/dashboard/login"
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    return response


_LOGIN_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>coord -- log in</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #0f1115;
      --fg: #e6e7eb;
      --muted: #9aa0a6;
      --border: #2a2f37;
      --accent: #6ea8ff;
      --error: #ff7676;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #fafbfc;
        --fg: #1a1d22;
        --muted: #586069;
        --border: #d0d7de;
        --accent: #0969da;
        --error: #cf222e;
      }}
    }}
    html, body {{
      margin: 0; padding: 0;
      background: var(--bg);
      color: var(--fg);
      font: 14px/1.5 system-ui, -apple-system, sans-serif;
      min-height: 100vh;
    }}
    main {{
      max-width: 28rem;
      margin: 5rem auto;
      padding: 2rem;
      border: 1px solid var(--border);
      border-radius: 8px;
    }}
    h1 {{
      margin: 0 0 0.25rem 0;
      font-size: 1.25rem;
      font-weight: 600;
    }}
    p.subtitle {{
      margin: 0 0 1.5rem 0;
      color: var(--muted);
      font-size: 0.9rem;
    }}
    label {{
      display: block;
      margin-bottom: 0.5rem;
      font-weight: 500;
    }}
    input[type=password] {{
      box-sizing: border-box;
      width: 100%;
      padding: 0.5rem 0.75rem;
      font: inherit;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      background: var(--bg);
      color: var(--fg);
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    input[type=password]:focus {{
      outline: 2px solid var(--accent);
      outline-offset: -1px;
    }}
    button {{
      margin-top: 1rem;
      padding: 0.5rem 1rem;
      font: inherit;
      font-weight: 600;
      background: var(--accent);
      color: white;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
    }}
    button:hover {{ filter: brightness(1.1); }}
    .error {{
      margin: 0 0 1rem 0;
      padding: 0.5rem 0.75rem;
      border: 1px solid var(--error);
      border-radius: 6px;
      color: var(--error);
      font-size: 0.85rem;
    }}
    .help {{
      margin-top: 1rem;
      color: var(--muted);
      font-size: 0.8rem;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 0.85em;
      background: rgba(127, 127, 127, 0.15);
      padding: 0.05rem 0.3rem;
      border-radius: 4px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>coord</h1>
    <p class="subtitle">paste your bearer token to continue</p>
    {error_html}
    <form method="POST" action="/dashboard/login" autocomplete="off">
      <label for="token">bearer token</label>
      <input id="token" type="password" name="token" required autofocus
             placeholder="coordt_..." spellcheck="false">
      <button type="submit">log in</button>
    </form>
    <p class="help">
      Generate a per-engineer token on the server with
      <code>coord tokens create &lt;engineer&gt;</code>.
      The legacy shared <code>COORD_AUTH_TOKEN</code> still works
      unless the operator has set
      <code>COORD_REQUIRE_PER_ENGINEER_TOKEN=true</code>.
    </p>
  </main>
</body>
</html>
"""


def _login_page_response(*, error: str | None = None) -> HTMLResponse:
    """Render the login form. Status is always 200 so browsers do
    not display a default-style error page; the auth failure path
    surfaces via the inline error banner instead."""
    error_html = ""
    if error:
        # Escape '<', '>', '&' so an attacker-controlled error string
        # (none today, but the helper is here so the next refactor
        # cannot regress) cannot inject markup.
        safe = (
            error.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        error_html = f'<p class="error">{safe}</p>'
    return HTMLResponse(_LOGIN_HTML.format(error_html=error_html))


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
    requester: str | None = Query(default=None),
    claim_id: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    queued: bool = Query(default=False),
    state: str | None = Query(default=None),
    _: None = Depends(require_auth),
) -> dict:
    """List requests filtered by requester, claim, or decision state.
    Used by both the requester (``my_requests``) and the operator
    dashboard.

    v0.22: ``queued=true`` switches the response to the live FIFO queue
    rows (``claim_queue``), joined with the blocking holder's engineer
    and pattern so the requester can see ``who am I waiting on?`` in a
    single round-trip. ``state`` further narrows the queue filter
    (defaults to ``waiting``; pass an empty string is not supported --
    omit the param to keep the default). ``requester`` continues to
    apply, filtering queue rows by ``requester_engineer``.
    """
    if queued:
        rows = await get_service().db.list_queued_with_holder(
            engineer=requester,
            state=state or "waiting",
        )
        out: list[dict] = []
        for r in rows:
            symbols_field: list[str] | None = None
            raw_symbols = r.get("symbols")
            if raw_symbols:
                try:
                    parsed = json.loads(raw_symbols)
                    if isinstance(parsed, list):
                        symbols_field = [str(s) for s in parsed]
                except (TypeError, ValueError):
                    symbols_field = None
            out.append(
                {
                    "kind": "queued",
                    "queue_id": r["id"],
                    "blocking_claim_id": r["blocking_claim_id"],
                    "blocking_engineer": r.get("blocking_engineer"),
                    "blocking_pattern": r.get("blocking_pattern"),
                    "requester_engineer": r["requester_engineer"],
                    "requester_pattern": r["pattern"],
                    "claim_type": r["claim_type"],
                    "symbols": symbols_field,
                    "position": int(r["position"]),
                    "state": r["state"],
                    "enqueued_at": r["enqueued_at"],
                    "expires_at": r["expires_at"],
                    "granted_claim_id": r.get("granted_claim_id"),
                }
            )
        return {"requests": out, "count": len(out), "queued": True}
    rows_legacy = await get_service().list_requests(
        requester_engineer=requester,
        claim_id=claim_id,
        decision=decision,
    )
    return {"requests": rows_legacy, "count": len(rows_legacy)}


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


@app.delete("/requests/{queue_id}")
async def cancel_request(
    queue_id: str,
    engineer: str | None = Query(default=None),
    _: None = Depends(require_auth),
) -> dict:
    """v0.26: cancel a queued claim_files request before its
    wait_seconds timeout fires. When ``engineer`` is supplied the
    cancellation is scoped to that engineer (prevents cross-engineer
    interference). Returns {ok, cancelled} -- cancelled=True when a
    waiting/in_progress row was actually transitioned to cancelled,
    False when the row was already terminal or unknown.
    """
    cancelled = await get_service().cancel_queue_request(
        queue_id, engineer=engineer
    )
    return {"ok": True, "cancelled": cancelled, "queue_id": queue_id}


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
