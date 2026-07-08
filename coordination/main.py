from __future__ import annotations

import asyncio
import hashlib
import hmac
import html as html_mod
import json
import logging
import os
import re
import secrets
import shlex
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from coordination import __version__
from coordination import metrics
from coordination import oidc
from coordination.repo_id import InvalidRepoId, normalize_repo_id
from coordination.cli_shared import parse_duration
from coordination.config import get_settings
from coordination.dashboard import render_dashboard
from coordination.db import _LOCK_SKIPPED, acquire_instance_lock
from coordination.deps import get_service
from coordination.tokens import generate_raw_token, sha256_token
from coordination.logging import (
    ACCESS_LOGGER_NAME,
    configure_logging,
    configure_uvicorn_logging,
    request_id_var,
)
from coordination.otel import setup_tracing
from coordination.ownership import parse_ownership_yaml
from coordination.service import LspUnavailable, RateLimitExceeded
from coordination.schemas import (
    ClaimRefactorRequest,
    CreateClaimsRequest,
    ExtendClaimRequest,
    FileRequestRequest,
    PromoteHotspotRequest,
    ReleaseClaimsRequest,
    RespondToRequestRequest,
)

logger = logging.getLogger(__name__)
access_logger = logging.getLogger(ACCESS_LOGGER_NAME)

# Leader-lease cadence for the background loops. Leadership is renewed by a
# dedicated heartbeat task (see ``lifespan``) on this fixed short interval,
# decoupled from the work loops' own cadence: the loops tick as slowly as an
# hour (auto-demote), so a TTL sized off work cadence either risks expiring
# between renewals or stretches to hours and stalls failover for that long
# after a leader crash. The TTL is three heartbeats plus slack so one missed
# renewal (GC pause, brief DB blip) does not drop leadership, while a crashed
# or SIGKILLed leader is replaced in about a minute.
LEADER_HEARTBEAT_INTERVAL_SEC = 20.0
LEADER_LEASE_TTL_SEC = LEADER_HEARTBEAT_INTERVAL_SEC * 3 + 5
LEADER_LEASE_NAME = "coord-background-loops"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    # v0.29.4: a deployment with COORD_REQUIRE_PER_ENGINEER_TOKEN=true
    # and no shared token is legal (per-engineer-only mode) -- the
    # engineer_tokens table is the credential store, so the process
    # must boot without COORD_AUTH_TOKEN. The hard refusal stays for
    # the configuration where nothing could ever authenticate.
    if (
        not settings.auth_token
        and not settings.require_per_engineer_token
        and not settings.allow_insecure_no_auth
    ):
        raise RuntimeError(
            "Set COORD_AUTH_TOKEN, enable per-engineer-only auth with "
            "COORD_REQUIRE_PER_ENGINEER_TOKEN=true, or explicitly allow "
            "insecure local mode with COORD_ALLOW_INSECURE_NO_AUTH=true."
        )

    # Take an advisory lock on <db>.lock before opening the database.
    # Held for the process lifetime; stash the fd on app state so it is
    # not garbage-collected mid-run. fcntl auto-releases the flock when
    # the fd is closed or the process exits.
    # The flock instance lock detects a second SQLite writer on one file; it
    # is meaningless across pods sharing a Postgres (design Section 7). In PG
    # mode bypass it (return the sentinel) -- multiple replicas are expected.
    if settings.database_url and (
        settings.database_url.startswith("postgresql://")
        or settings.database_url.startswith("postgres://")
    ):
        app.state.instance_lock_fd = _LOCK_SKIPPED
    else:
        app.state.instance_lock_fd = acquire_instance_lock(settings.database_path)

    await get_service().db.init()
    metrics.set_build_info(__version__)

    async def _shutdown_lsp_pool() -> None:
        # v0.31: language servers are child processes of this one; reap
        # them on every teardown path so a restart never strands pylsp
        # or gopls children. shutdown_all never raises and is a no-op
        # when nothing was spawned (the default, lsp_enabled=false).
        if settings.lsp_enabled:
            from coordination.lsp import get_lsp_pool

            await get_lsp_pool(settings).shutdown_all()

    if os.environ.get("COORD_DISABLE_BACKGROUND_CLEANUP", "").lower() in {"1", "true", "yes"}:
        try:
            yield
        finally:
            await _shutdown_lsp_pool()
        return

    # Single-leader election for the multi-replica background loops
    # (design Section 6). All the loops below mutate shared DB state
    # (and webhook delivery POSTs externally), so on Postgres three
    # replicas running them unguarded would expire/auto-demote in
    # triplicate and -- worst -- deliver every webhook 3x. The lease lets
    # exactly one replica (the leader) run the per-DB work. On SQLite
    # there is a single writer process, so the lease is unconditionally
    # True and every loop runs exactly as it always has. ``leader_id`` is
    # minted once per process so the lease is stable across renew ticks.
    #
    # Renewal runs in a dedicated heartbeat task (LEADER_HEARTBEAT_
    # INTERVAL_SEC cadence, TTL ~3 heartbeats -- see the module-level
    # constants) rather than inside the work loops, so failover after a
    # crashed leader is bounded by roughly a minute regardless of how
    # slowly the work loops tick. The work loops read the cached
    # leadership flag, which the heartbeat keeps fresh.
    leader_id = uuid4().hex
    leader_state = {"is_leader": False}

    async def _renew_leader_lease() -> bool:
        try:
            return await get_service().db.acquire_leader_lease(
                lease_name=LEADER_LEASE_NAME,
                holder_id=leader_id,
                ttl_sec=LEADER_LEASE_TTL_SEC,
            )
        except Exception:  # pragma: no cover - lease failures must not kill the loop
            logger.exception(
                "leader lease renewal failed; treating this replica as non-leader"
            )
            return False

    async def _is_background_leader() -> bool:
        return leader_state["is_leader"]

    async def lease_heartbeat_loop() -> None:
        """Renew (or contest) the background-loop leader lease on a fixed
        short cadence. The initial acquire below runs before the work
        loops start, so their first tick sees real leadership state
        instead of a cold False; this loop therefore sleeps first."""
        while True:
            await asyncio.sleep(LEADER_HEARTBEAT_INTERVAL_SEC)
            leader_state["is_leader"] = await _renew_leader_lease()

    leader_state["is_leader"] = await _renew_leader_lease()

    async def cleanup_loop() -> None:
        while True:
            # Claim expiry mutates the DB -> leader only (design Section 6).
            leader = await _is_background_leader()
            if leader:
                try:
                    # Service-layer sweep: closes TTL/idle-expired claims
                    # AND drains the FIFO queue behind each of them, so a
                    # waiter queued behind a claim whose (possibly
                    # request_release-shortened) TTL fires here is granted
                    # instead of burning its whole wait_seconds.
                    await get_service().expire_stale_claims()
                except Exception:  # pragma: no cover - background cleanup failures are logged
                    logger.exception("Failed to expire stale claims")
                # Reap queue rows by their own expires_at so a waiter
                # stranded by a process restart / crash (its in-memory
                # event lost) converges to 'expired' instead of holding a
                # 'waiting' slot against COORD_MAX_QUEUED_PER_ENGINEER and
                # the per-repo depth cap forever.
                try:
                    await get_service().db.expire_stale_queue_entries()
                except Exception:  # pragma: no cover - background cleanup failures are logged
                    logger.exception("Failed to expire stale queue entries")
            # v0.31 wave 2: rename auto-follow sweep piggybacks on the
            # cleanup cadence rather than running its own task -- one
            # background heartbeat, two cheap jobs. Gated on
            # lsp_enabled so the default-off posture stays a true
            # no-op; the sweep itself is bounded (max 20 claims per
            # pass) and never raises past this guard.
            if settings.lsp_enabled:
                # The sweep mutates the DB -> leader only.
                if leader:
                    try:
                        await get_service().rename_sweep()
                    except Exception:  # pragma: no cover - background failures are logged
                        logger.exception("Failed to run rename auto-follow sweep")
                # Reap language servers idle past
                # lsp_idle_shutdown_sec -- this loop is the only
                # production caller, so without it the reaper would be
                # dead code and every spawned server would live for
                # the process lifetime. Per-process: every replica owns
                # the language servers IT spawned, so this runs on every
                # replica regardless of leadership.
                try:
                    from coordination.lsp import get_lsp_pool

                    await get_lsp_pool(settings).shutdown_idle()
                except Exception:  # pragma: no cover - background failures are logged
                    logger.exception("Failed to reap idle LSP servers")
            await asyncio.sleep(settings.cleanup_interval_sec)

    async def auto_demote_loop() -> None:
        """v0.23 background sweep: every
        ``settings.auto_demote_interval_sec`` seconds, ask the service
        to demote coord-managed ``shared_files`` entries whose rolling
        409 count has dropped below ``auto_promote_threshold``.
        Disabled when the interval is 0 or threshold is 0 (the service
        layer short-circuits the latter)."""
        while True:
            # Auto-demote mutates ownership_config -> leader only.
            if await _is_background_leader():
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
        when ``COORD_WEBHOOK_URL`` or ``COORD_GITHUB_TOKEN`` is
        configured -- either transport writes outbox rows and this loop
        is the only drain -- so deployments that use neither pay no
        scheduler overhead."""
        while True:
            # Webhook delivery POSTs externally and marks rows delivered;
            # leader only so the outbox is drained once, not once per
            # replica (design Section 6 -- at-least-once-once delivery).
            if await _is_background_leader():
                try:
                    await get_service().deliver_pending_webhooks()
                except Exception:  # pragma: no cover - background failures are logged
                    logger.exception("webhook_delivery_loop: tick failed")
            await asyncio.sleep(settings.webhook_delivery_interval_sec)

    tasks: list[asyncio.Task] = [
        asyncio.create_task(lease_heartbeat_loop()),
        asyncio.create_task(cleanup_loop()),
    ]
    if settings.auto_demote_interval_sec > 0:
        tasks.append(asyncio.create_task(auto_demote_loop()))
    # fire_webhook enqueues kind='github' rows gated solely on
    # COORD_GITHUB_TOKEN (a documented-valid config with no webhook_url),
    # and this loop is the only drain of the outbox -- so start it when
    # EITHER transport is configured, or github PR-comment rows would sit
    # 'pending' forever without ever being attempted.
    if settings.webhook_url or settings.github_token.strip():
        tasks.append(asyncio.create_task(webhook_delivery_loop()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        # Hand leadership back on graceful shutdown so the next replica
        # takes over immediately (rolling deploys) instead of waiting out
        # the lease TTL. Best-effort: a crash skips this, and the short
        # heartbeat-derived TTL bounds the stall in that case.
        try:
            await get_service().db.release_leader_lease(
                lease_name=LEADER_LEASE_NAME, holder_id=leader_id
            )
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.debug(
                "leader lease release on shutdown failed", exc_info=True
            )
        # v0.44: close the shared SQLite writer connection (no-op when the
        # writer queue is off / on the Postgres backend).
        try:
            await get_service().db.aclose()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.debug("db.aclose on shutdown failed", exc_info=True)
        await _shutdown_lsp_pool()


app = FastAPI(title="Multi-Agent Coordination", version=__version__, lifespan=lifespan)

# Optional OpenTelemetry tracing. No-op unless COORD_OTEL_ENABLED=true;
# fails open so a tracing misconfiguration never breaks startup.
setup_tracing(app, get_settings())


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


def _unscoped_token_warning(engineer: str | None) -> str:
    """Compose the v0.43 soft-deprecation notice for an unscoped
    per-engineer token. One ASCII line (it rides in an HTTP header): what
    is wrong, why it matters, and the exact command to switch. The MCP
    wrapper surfaces it to the agent as ``coord_notice`` and ``coord
    status`` prints it, so the same message reaches humans and agents."""
    # Engineer ids are stored verbatim from ``coord tokens create``, so
    # sanitize before interpolating into an HTTP header value. Restrict to
    # visible ASCII (0x20-0x7E): that drops CR/LF/tab/controls (header
    # injection / invalid header) AND non-ASCII (Starlette encodes header
    # values as latin-1, so a Unicode id would raise UnicodeEncodeError).
    # Falls back to a placeholder if nothing usable survives, and is capped
    # so an oversized id cannot bloat the header past receiver size limits.
    sanitized = "".join(
        ch for ch in (engineer or "") if 0x20 <= ord(ch) <= 0x7E
    ).strip()[:64]
    # Whether sanitization/truncation changed the id: if so the previewed
    # command would mint under a different identity, so we warn the operator
    # to copy the exact id rather than trust the preview.
    altered = bool(engineer) and sanitized != engineer
    who = sanitized or "<engineer>"
    # The message embeds a copy/pasteable `coord tokens create <who>` command,
    # so shell-quote an id that is not a plain, safe token -- otherwise a shell
    # metacharacter (`;` `` ` `` `$` `|` space ...) could turn a pasted command
    # into something unsafe. Common ids (alnum plus / - _ . @) stay unquoted so
    # the docs/tests read naturally.
    # A leading '-' makes argparse read the id as an option rather than the
    # positional ``engineer``, so put --repo first and use ``--`` to force the
    # id to be parsed as an argument. Detected before shell-quoting (quoting
    # doesn't change how argparse sees the token once the shell strips quotes).
    starts_dash = who.startswith("-")
    if not re.fullmatch(r"[A-Za-z0-9._/@-]+", who):
        who = shlex.quote(who)
    cmd = (
        f"coord tokens create --repo <owner/name> -- {who}"
        if starts_dash
        else f"coord tokens create {who} --repo <owner/name>"
    )
    caveat = (
        " (the id above was sanitized/truncated for this header -- copy the exact "
        "engineer id from `coord tokens list`.)"
        if altered
        else ""
    )
    return (
        "Your coord token is not bound to a repo. On a shared multi-repo coord "
        "service an unscoped per-engineer token sees and can affect EVERY "
        "repo's claims, which is deprecated. Ask an operator for a repo-scoped "
        f"token and switch: `{cmd}`, "
        "then set it in .coordination/local.env. See the 'Repo-scoped tokens' "
        f"section of AGENTS.md / docs/deployment.md. Honored for now.{caveat}"
    )


@app.middleware("http")
async def _count_http_requests(request: Request, call_next):
    """Increment ``http_requests_total`` after each response. Uses the
    matched route template (e.g. ``/claims/{claim_id}``) for the ``path``
    label so cardinality stays bounded; requests that did not match a
    route (404s) collapse to the constant ``<unmatched>`` label. Using
    the raw URL path there would let an unauthenticated scanner mint one
    permanent series per probed path (series live for the process
    lifetime), growing memory and the /metrics scrape body without
    bound."""
    try:
        response = await call_next(request)
    except Exception:
        # An unhandled route exception unwinds through this middleware
        # before Starlette's outermost ServerErrorMiddleware renders the
        # 500, so count it here -- otherwise crash 500s (exactly the
        # requests error-rate dashboards care about) vanish from the
        # metric and an exception storm reads as a 0% error rate.
        route = request.scope.get("route")
        path_label = getattr(route, "path", None) or "<unmatched>"
        metrics.http_requests_total.inc(
            method=request.method,
            path=path_label,
            status="500",
        )
        raise
    # #30 slice 2/3: advertise the repo a scoped token was pinned to, so an
    # operator who dropped a token into the wrong repo's local.env can see
    # why results are empty instead of a silent void. Unscoped tokens (the
    # common case) get no header.
    _scope = getattr(request.state, "token_repo", None)
    if _scope is not None:
        response.headers["X-Coord-Repo-Scope"] = _scope
    elif (
        get_settings().warn_unscoped_token
        and getattr(request.state, "auth_kind", None) == "per_engineer"
    ):
        # v0.43: soft-deprecate unscoped per-engineer tokens -- honor the
        # request but nudge toward a repo-bound token. The shared operator
        # token (auth_kind != per_engineer) is exempt; when
        # require_scoped_token is set the unscoped token 401s before here so
        # this never double-signals.
        response.headers["X-Coord-Token-Warning"] = _unscoped_token_warning(
            getattr(request.state, "engineer", None)
        )
    # Identity-binding warn mode (_bind_mutation_engineer): surface the
    # mismatch the handler recorded so the offending client sees it on
    # the very response it caused, not just in server logs.
    _identity_warning = getattr(
        request.state, "engineer_identity_warning", None
    )
    if _identity_warning:
        response.headers["X-Coord-Identity-Warning"] = _identity_warning
    route = request.scope.get("route")
    path_label = getattr(route, "path", None) or "<unmatched>"
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
    * Skipped when the request carries no authenticated identity
      (anonymous health checks, /metrics scrapes, failed auth). The
      engineer is taken from the authenticated token, not from an
      unauthenticated client-supplied value.
    * Counting errors are swallowed so a transient DB issue never
      poisons the underlying response -- the header just goes missing
      for that one request.
    """
    response = await call_next(request)
    settings = get_settings()
    if not settings.backpressure_header:
        return response
    # v0.42: attribute queue depth from the AUTHENTICATED identity, never
    # from an unauthenticated client-supplied ``engineer``. Otherwise
    # anyone could read any engineer's queue depth by naming them
    # (``?engineer=victim``) with no credential. require_auth has run by
    # now (it is a route dependency, so request.state is populated on the
    # same request object), so:
    #   * per-engineer token -> only its own depth (self-attributed);
    #   * shared/operator token -> may attribute any engineer it names
    #     (operator-wide visibility is the point of the shared token);
    #   * unauthenticated / no-auth / failed auth -> no header at all.
    auth_kind = getattr(request.state, "auth_kind", None)
    if auth_kind == "per_engineer":
        engineer = getattr(request.state, "engineer", None)
    elif auth_kind == "shared":
        engineer = _engineer_from_request(request)
    else:
        engineer = None
    if not engineer:
        return response
    try:
        depth = await get_service().count_queued_for(
            engineer, repo=_token_repo(request)
        )
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
    try:
        response = await call_next(request)
    except Exception:
        # An unhandled route exception propagates through here before
        # Starlette's ServerErrorMiddleware renders the 500, so emit the
        # access-log line now or crash 500s never get one. The minted
        # X-Request-ID response header never materialized on this path
        # (and the contextvar was already reset by the inner middleware),
        # so fall back to the inbound header when the client sent one.
        duration_ms = (time.monotonic() - start) * 1000.0
        route = request.scope.get("route")
        path_label = getattr(route, "path", None) or request.url.path
        access_logger.info(
            "http_request",
            extra={
                "event": "http_request",
                "method": request.method,
                "path": path_label,
                "status": 500,
                "duration_ms": round(duration_ms, 2),
                "request_id": request.headers.get("x-request-id", ""),
            },
        )
        raise
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


@dataclass(frozen=True)
class AuthOutcome:
    """Result of one pass through the bearer-auth pipeline.

    v0.29.4 collapses the per-engineer -> shared -> require-flag
    decision tree (previously triplicated across ``require_auth``,
    ``GET /dashboard`` and ``POST /dashboard/login``) into
    ``_authenticate_bearer``; this is its return type. ``ok`` is the
    only field a caller must branch on -- the rest carry either the
    authenticated identity (``auth_kind``/``engineer``/``token_id``)
    or the HTTP shape of the failure (``status_code``/``detail``).
    """

    ok: bool
    auth_kind: str | None = None  # "per_engineer" | "shared" | None
    engineer: str | None = None
    token_id: str | None = None
    # v0.42 (#30 slice 2/3): the repo this token is bound to, or None for an
    # unscoped (operator / shared) token. When set, the server forces repo
    # scope from auth rather than trusting a client-supplied ``repo``.
    token_repo: str | None = None
    # v0.29.5: the session token's own expiry (ISO ``...Z`` or None for
    # never), populated on the per-engineer ok path. The dashboard
    # token-create endpoint uses it to cap self-service token expiry.
    token_expires_at: str | None = None
    status_code: int = 200  # 401 or 500 when not ok
    detail: str | None = None


def _source_ip_from_request(request: Request) -> str | None:
    """Best-effort client IP for token activity records.

    Priority order mirrors what the proxy chain actually guarantees:
    ``CF-Connecting-IP`` (Cloudflare stamps the real client address
    at the edge and cloudflared/Traefik pass it through untouched),
    then the first hop of ``X-Forwarded-For`` (the multi-proxy
    convention -- later hops are the proxies themselves), then the
    raw socket peer. Every signal here is spoofable by a direct
    caller, so the value is operator-facing audit metadata only and
    must never feed an auth decision."""
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client:
        return request.client.host
    return None


async def _authenticate_bearer(
    request: Request, token: str | None
) -> AuthOutcome:
    """Single source of truth for bearer authentication (v0.29.4).

    Per-engineer tokens (the recommended path) live in the
    ``engineer_tokens`` table as sha256 hashes. The shared
    ``COORD_AUTH_TOKEN`` is still accepted by default for
    back-compat -- existing clients keep working untouched on
    upgrade -- and is fully rejected once the operator flips
    ``COORD_REQUIRE_PER_ENGINEER_TOKEN=true``.

    ``resolve_engineer_token`` (rather than the valid-only
    ``lookup_engineer_token``) tells us WHY a known token stopped
    authenticating, so the 401 carries an actionable hint -- expired
    vs rotated-past-grace -- instead of a bare "invalid". Token ids
    are deliberately never echoed into 401 details.

    Counts ``auth_failures_total`` on every 401 it produces; the
    misconfiguration 500 is not an auth failure and is not counted.
    """
    settings = get_settings()

    # Configuration short-circuits. Same shape as the pre-v0.29.4
    # require_auth preamble with one deliberate widening: a deployment
    # that sets COORD_REQUIRE_PER_ENGINEER_TOKEN=true and never
    # configures a shared COORD_AUTH_TOKEN is now legal
    # (per-engineer-only mode) -- per-engineer tokens authenticate
    # below and everything else 401s with the migration hint. The 500
    # stays reserved for the configuration where nothing could ever
    # authenticate.
    if not settings.auth_token:
        if settings.allow_insecure_no_auth:
            return AuthOutcome(ok=True)
        if not settings.require_per_engineer_token:
            return AuthOutcome(
                ok=False,
                status_code=500,
                detail=(
                    "Server misconfigured: set COORD_AUTH_TOKEN, enable "
                    "per-engineer-only auth with "
                    "COORD_REQUIRE_PER_ENGINEER_TOKEN=true, or explicitly "
                    "allow insecure local mode with "
                    "COORD_ALLOW_INSECURE_NO_AUTH=true"
                ),
            )

    if not token:
        metrics.auth_failures_total.inc()
        return AuthOutcome(
            ok=False, status_code=401, detail="Missing bearer token"
        )

    # Try per-engineer first: a leaked per-engineer token surfaces
    # in ``coord tokens list`` and can be revoked individually,
    # which is the whole point of having them.
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    service = get_service()
    resolved = await service.db.resolve_engineer_token(token_hash)
    if resolved is not None:
        if resolved["status"] == "ok":
            # #30 slice 2/3 hardening: a deployment can require every
            # per-engineer token to be repo-scoped. An unscoped one is
            # rejected with an actionable hint; the shared token stays the
            # operator escape hatch (handled on its own path below).
            if (
                settings.require_scoped_token
                and resolved.get("repo") is None
            ):
                metrics.auth_failures_total.inc()
                return AuthOutcome(
                    ok=False,
                    status_code=401,
                    detail=(
                        "This deployment requires a repo-scoped token "
                        "(COORD_REQUIRE_SCOPED_TOKEN). Ask an operator for a "
                        "token bound to your repo: coord tokens create "
                        "<engineer> --repo <id>."
                    ),
                )
            # Best-effort activity capture. The auth path must never
            # 401 because the update failed (e.g. transient lock
            # contention), so a broad except is correct here.
            try:
                await service.db.touch_engineer_token(
                    token_hash,
                    source_ip=_source_ip_from_request(request),
                    user_agent=request.headers.get("user-agent"),
                )
            except Exception:  # noqa: BLE001 - intentional swallow
                pass
            return AuthOutcome(
                ok=True,
                auth_kind="per_engineer",
                engineer=resolved["engineer"],
                token_id=resolved["id"],
                token_expires_at=resolved.get("expires_at"),
                token_repo=resolved.get("repo"),
            )
        metrics.auth_failures_total.inc()
        if resolved["status"] == "expired":
            return AuthOutcome(
                ok=False,
                status_code=401,
                detail=(
                    f"Per-engineer token expired {resolved['expires_at']}. "
                    "Ask an operator for a replacement: coord tokens "
                    "rotate (before expiry) or coord tokens create."
                ),
            )
        # Only remaining resolve status: "rotation_grace_elapsed".
        return AuthOutcome(
            ok=False,
            status_code=401,
            detail=(
                "Per-engineer token was rotated and its grace window "
                f"closed {resolved['rotation_grace_until']}. Switch to "
                "the replacement token, or ask an operator to mint a "
                "new one: coord tokens create."
            ),
        )

    # Unknown or revoked token (indistinguishable on purpose: both
    # mean "not a credential").
    if settings.require_per_engineer_token:
        metrics.auth_failures_total.inc()
        return AuthOutcome(
            ok=False,
            status_code=401,
            detail=(
                "Per-engineer token required "
                "(COORD_REQUIRE_PER_ENGINEER_TOKEN=true). "
                "Run token creation on the coord server/service, e.g. "
                "'kubectl -n coord exec deploy/coord -- coord tokens create "
                "\"<engineer>\" --repo <owner/name>', then paste the new "
                "token into .coordination/local.env. In remote-mode repos, "
                "do not run 'coord tokens create' from the application "
                "checkout; that creates a local SQLite token the remote "
                "service will not know."
            ),
        )

    # Legacy shared-token fallback.
    if settings.auth_token and hmac.compare_digest(token, settings.auth_token):
        return AuthOutcome(ok=True, auth_kind="shared")

    metrics.auth_failures_total.inc()
    return AuthOutcome(ok=False, status_code=401, detail="Invalid bearer token")


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """v0.29: two-tier bearer auth, delegated to
    ``_authenticate_bearer`` (the single pipeline shared with the
    dashboard routes since v0.29.4).

    The dashboard cookie also feeds this path: when the browser
    has a ``coord_session`` cookie set by ``/dashboard/login``, we
    treat its value as a bearer token, which means the same
    per-engineer / shared-token lookup applies to browser and
    headless clients uniformly. That keeps the security model
    flat -- there is no separate "session token" type to audit.
    """
    outcome = await _authenticate_bearer(
        request, _extract_bearer(authorization, request)
    )
    if not outcome.ok:
        raise HTTPException(
            status_code=outcome.status_code, detail=outcome.detail
        )
    # #30 slice 2/3: default the repo-scope state on every request so handlers
    # can read request.state.token_repo unconditionally. An unscoped or shared
    # token leaves it None (operator: sees all repos).
    request.state.token_repo = None
    request.state.token_scoped = False
    if outcome.auth_kind == "per_engineer":
        request.state.engineer = outcome.engineer
        request.state.auth_kind = "per_engineer"
        request.state.token_id = outcome.token_id
        request.state.token_repo = outcome.token_repo
        request.state.token_scoped = outcome.token_repo is not None
    elif outcome.auth_kind == "shared":
        request.state.auth_kind = "shared"


# ---------------------------------------------------------------------------
# Repo-scope enforcement (#30 slice 2/3). A scoped token's repo comes from
# auth (request.state.token_repo), never from the client, so these helpers are
# the boundary that makes visibility a server-side authorization decision.
# ---------------------------------------------------------------------------


def _token_repo(request: Request) -> str | None:
    """The repo this request's token is bound to, or None when unscoped."""
    return getattr(request.state, "token_repo", None)


def _normalized_request_repo(value: str | None) -> str | None:
    """Validate/normalize a client-supplied repo id (#61), mapping a malformed
    value to a 400 so every request ingress fails fast with the same rule."""
    try:
        return normalize_repo_id(value)
    except InvalidRepoId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _effective_read_repo(
    request: Request, client_repo: str | None, *, all_repos: bool = False
) -> str | None:
    """Resolve the repo filter for a READ, enforcing token scope.

    Unscoped (operator) token: the client's ``repo`` / ``all_repos`` are
    honored verbatim. Scoped token: the filter is forced to the token's repo;
    an explicit request for a different repo (or all repos) is a 403 rather
    than a silent lie, while an absent ``repo`` is silently scoped so a stale
    client that sends nothing is still enforced.
    """
    client_repo = _normalized_request_repo(client_repo)
    token_repo = _token_repo(request)
    if token_repo is None:
        return client_repo
    if all_repos or (client_repo is not None and client_repo != token_repo):
        want = "all repos" if all_repos else f"repo '{client_repo}'"
        raise HTTPException(
            status_code=403,
            detail=(
                f"This token is scoped to repo '{token_repo}'; it cannot "
                f"access {want}."
            ),
        )
    if client_repo is None:
        # #61: trace the silent-scope override so an operator can see why a
        # scoped token got filtered/empty results instead of a silent void.
        logger.debug(
            "repo-scope: read with absent repo silently scoped to token repo %r",
            token_repo,
        )
    return token_repo


def _enforce_write_repo(request: Request, body_repo: str | None) -> str | None:
    """Resolve the repo a WRITE tags, enforcing token scope. Scoped token:
    default to the token's repo when the body omits it, 403 on mismatch.
    Unscoped token: the body value is honored verbatim."""
    body_repo = _normalized_request_repo(body_repo)
    token_repo = _token_repo(request)
    if token_repo is None:
        return body_repo
    if body_repo is not None and body_repo != token_repo:
        raise HTTPException(
            status_code=403,
            detail=(
                f"This token is scoped to repo '{token_repo}'; it cannot "
                f"write to repo '{body_repo}'."
            ),
        )
    if body_repo is None:
        # #61: trace the default-to-token-repo override on a scoped write.
        logger.debug(
            "repo-scope: write with absent repo defaulted to token repo %r",
            token_repo,
        )
    return token_repo


def _require_operator(request: Request) -> None:
    """Reject a repo-scoped token from an operator-only (global) endpoint."""
    token_repo = _token_repo(request)
    if token_repo is not None:
        raise HTTPException(
            status_code=403,
            detail=(
                "This endpoint mutates global state and is operator-only; a "
                f"token scoped to repo '{token_repo}' cannot use it."
            ),
        )


def _scope_403(token_repo: str, kind: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=(
            f"This token is scoped to repo '{token_repo}'; that {kind} "
            "belongs to another repo."
        ),
    )


async def _require_claim_in_scope(request: Request, claim_id: str) -> None:
    """403 when a scoped token references a claim in another repo. A missing
    claim falls through so the handler's own 404 / no-op logic runs (no
    existence 403 for an id the token could not have created)."""
    token_repo = _token_repo(request)
    if token_repo is None:
        return
    exists, repo = await get_service().db.claim_repo(claim_id)
    if exists and repo != token_repo:
        raise _scope_403(token_repo, "claim")


async def _require_request_in_scope(request: Request, request_id: str) -> None:
    token_repo = _token_repo(request)
    if token_repo is None:
        return
    exists, repo = await get_service().db.request_repo(request_id)
    if exists and repo != token_repo:
        raise _scope_403(token_repo, "request")


async def _require_queue_in_scope(request: Request, queue_id: str) -> None:
    token_repo = _token_repo(request)
    if token_repo is None:
        return
    exists, repo = await get_service().db.queue_entry_repo(queue_id)
    if exists and repo != token_repo:
        raise _scope_403(token_repo, "queue entry")


def _ascii_header_value(value: str, cap: int = 256) -> str:
    """Restrict to visible ASCII (0x20-0x7E) and cap the length so a
    client-supplied engineer id cannot inject header control bytes
    (CR/LF), break Starlette's latin-1 header encoding, or bloat the
    header past receiver size limits. Same rule as the unscoped-token
    warning header."""
    return "".join(ch for ch in value if 0x20 <= ord(ch) <= 0x7E)[:cap]


def _bind_mutation_engineer(
    request: Request, engineer: str | None, *, operation: str
) -> str | None:
    """Bind the ``engineer`` named on a mutating request to the
    authenticated identity.

    Shared/operator tokens and no-auth deployments are exempt: acting on
    other engineers' claims is what the operator escape hatch is for.
    For per-engineer tokens the behaviour follows
    ``COORD_ENFORCE_ENGINEER_IDENTITY``:

    - ``warn`` (default): a mismatching or omitted engineer is honored
      unchanged -- live fleets share one token across several agent
      identities, so hard enforcement must be opt-in -- but the mismatch
      is logged and echoed back in an ``X-Coord-Identity-Warning``
      response header so operators can find offending clients before
      flipping to enforce.
    - ``enforce``: an omitted engineer is defaulted to the token's own
      identity, so destructive DB updates always carry an ownership
      predicate, and a mismatching value is rejected with 403.

    Returns the effective engineer the handler should pass downstream.
    """
    if getattr(request.state, "auth_kind", None) != "per_engineer":
        return engineer
    token_engineer = getattr(request.state, "engineer", None)
    if not token_engineer or engineer == token_engineer:
        return engineer
    mode = (get_settings().enforce_engineer_identity or "").strip().lower()
    if mode == "enforce":
        if engineer is None:
            return token_engineer
        raise HTTPException(
            status_code=403,
            detail=(
                f"This token authenticates engineer '{token_engineer}'; "
                f"it cannot {operation} as engineer '{engineer}' "
                "(COORD_ENFORCE_ENGINEER_IDENTITY=enforce)."
            ),
        )
    named = engineer if engineer is not None else "<omitted>"
    logger.warning(
        "engineer identity mismatch on %s: token engineer %r, "
        "request engineer %r (COORD_ENFORCE_ENGINEER_IDENTITY=warn)",
        operation,
        token_engineer,
        engineer,
    )
    request.state.engineer_identity_warning = _ascii_header_value(
        f"Request named engineer '{named}' but the token authenticates "
        f"'{token_engineer}'. Honored for now "
        "(COORD_ENFORCE_ENGINEER_IDENTITY=warn); with enforce a mismatch "
        "is rejected and an omitted engineer is scoped to the token's own."
    )
    return engineer


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


# v0.29.5 CSRF protection for the dashboard's state-changing forms
# (logout, token create/revoke). Double-submit cookie pattern: the
# server mints a random value into ``coord_csrf`` and every form
# embeds the same value as a hidden ``csrf_token`` field; a cross-site
# attacker can make the browser send the cookie but cannot read it to
# forge the matching field. The cookie is deliberately session-scoped
# (no max_age) -- it dies with the browser session, and login rotates
# it anyway.
CSRF_COOKIE = "coord_csrf"


def _issue_csrf_token() -> str:
    return secrets.token_hex(32)


def _csrf_cookie_kwargs(request: Request) -> dict[str, Any]:
    """Cookie attributes for ``coord_csrf``, mirroring the session
    cookie (HttpOnly, SameSite=Lax, Secure behind TLS/proxies, path=/)
    except for ``max_age``: the CSRF cookie is session-scoped."""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": _request_uses_https(request),
        "path": "/",
    }


def _valid_csrf_shape(value: str | None) -> bool:
    """True for exactly 64 lowercase hex chars -- the only shape
    ``_issue_csrf_token`` ever mints. Anything else is treated as
    absent so a corrupt or attacker-planted cookie gets re-minted."""
    if not value or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _validate_dashboard_csrf(request: Request, form_value: str | None) -> bool:
    """Double-submit check: both the ``coord_csrf`` cookie and the
    form's ``csrf_token`` field must be present, well-shaped, and
    equal (constant-time compare)."""
    cookie = request.cookies.get(CSRF_COOKIE)
    if not _valid_csrf_shape(cookie) or not _valid_csrf_shape(form_value):
        return False
    return hmac.compare_digest(cookie or "", form_value or "")


def _csrf_failure_response() -> HTMLResponse:
    """403 page for a failed CSRF check. Cookies are deliberately left
    untouched: a stale tab should not be able to log anyone out."""
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>coord -- CSRF validation failed</title></head><body>"
        "<p>CSRF validation failed. Reload the dashboard and retry.</p>"
        "<p><a href=\"/dashboard\">back to dashboard</a></p>"
        "</body></html>",
        status_code=403,
    )


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
    # Deliberately unauthenticated (readiness probes rarely carry bearer
    # tokens), so the payload must not leak server internals: auth_mode
    # and version are documented probe fields (docs/deployment.md), but
    # the absolute database filesystem path is reconnaissance material on
    # an internet-fronted service and has no unauthenticated consumer.
    settings = get_settings()
    await get_service().db.init()
    return {
        "status": "ready",
        "version": __version__,
        "auth_mode": settings.auth_mode,
    }


@app.get("/meta")
async def meta() -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "name": "multi-agent-coordination",
        "version": __version__,
        "auth_mode": settings.auth_mode,
        "repo_root_configured": bool(settings.repo_root),
        "oidc_enabled": settings.oidc_enabled,
    }


def _csrf_for_request(request: Request) -> tuple[str, bool]:
    """Resolve the CSRF value the rendered page's forms must embed.
    Returns ``(value, minted)``: the existing well-shaped cookie value
    with ``minted=False``, or a fresh token with ``minted=True`` (the
    caller sets the cookie on its response)."""
    cookie = request.cookies.get(CSRF_COOKIE)
    if _valid_csrf_shape(cookie):
        return cookie or "", False
    return _issue_csrf_token(), True


def _login_page_with_csrf(
    request: Request, *, error: str | None = None
) -> HTMLResponse:
    """Login page response that also guarantees a CSRF cookie exists,
    so the very first GET already arms the dashboard's forms."""
    csrf, minted = _csrf_for_request(request)
    response = _login_page_response(error=error, csrf_token=csrf)
    if minted:
        response.set_cookie(
            CSRF_COOKIE, value=csrf, **_csrf_cookie_kwargs(request)
        )
    return response


async def _render_dashboard_for(
    outcome: AuthOutcome,
    csrf: str,
    *,
    token_error: str | None = None,
    token_success: str | None = None,
) -> str:
    """Map an auth outcome onto render_dashboard's viewer context.
    Per-engineer sessions see their own token panel; shared-token
    sessions are operators and see everyone's; the insecure no-auth
    mode (ok with auth_kind None) gets no token panel at all."""
    return await render_dashboard(
        viewer_engineer=outcome.engineer,
        is_operator=outcome.auth_kind == "shared",
        viewer_repo=outcome.token_repo,
        csrf_token=csrf,
        token_error=token_error,
        token_success=token_success,
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """v0.29: gracefully render the login form for unauthenticated
    browsers instead of returning the raw JSON 401 the rest of the
    API uses. The auth path is exactly the one require_auth uses
    (``_authenticate_bearer`` since v0.29.4); a failure surfaces as
    the login form, not a 401 -- and never a 500, so per-engineer-only
    deployments (no shared token) still get a usable login page.

    v0.29.5: also seeds the ``coord_csrf`` cookie and threads the
    viewer's identity into the renderer so the engineer-tokens panel
    can scope itself (own tokens vs operator view).
    """
    token = _extract_bearer(authorization, request)
    outcome = await _authenticate_bearer(request, token)
    if outcome.ok:
        csrf, minted = _csrf_for_request(request)
        response = HTMLResponse(await _render_dashboard_for(outcome, csrf))
        if minted:
            response.set_cookie(
                CSRF_COOKIE, value=csrf, **_csrf_cookie_kwargs(request)
            )
        return response

    if not token:
        return _login_page_with_csrf(request)

    # Stale or wrong token: render login form with an error banner so
    # the user can paste a fresh token without poking at curl. Surface
    # the pipeline's specific hint (expired vs rotated vs invalid) --
    # the cookie path is where an expired token is most likely to be
    # discovered, and the same detail already ships in API 401s.
    return _login_page_with_csrf(
        request,
        error=outcome.detail or "Invalid or expired token. Paste a fresh one.",
    )


@app.get("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login_form(request: Request) -> Response:
    """Always serves the login page (even if the user is already
    authenticated) -- this is the bookmark target for ``log out then
    log back in as someone else``. Logging in a fresh time
    overwrites the existing cookie."""
    return _login_page_with_csrf(request)


@app.post("/dashboard/login", response_class=HTMLResponse)
async def dashboard_login_submit(request: Request) -> Response:
    """Validate the submitted token and set the session cookie.

    The form submits ``application/x-www-form-urlencoded`` with one
    field, ``token``. Validation flows through the same
    ``_authenticate_bearer`` pipeline as ``require_auth`` so the
    semantics stay identical between header-bearer and cookie-bearer
    paths. On success we set ``coord_session`` and 303 to the
    dashboard; on failure we re-render the login form with the
    outcome's detail as the error banner -- an expired or
    rotated-past-grace token shows its specific replacement hint
    instead of a generic "invalid".

    v0.29.5: deliberately exempt from the CSRF check the other
    dashboard POSTs enforce -- operators script this endpoint with
    curl (no cookie jar, no prior GET), and SameSite=Lax on the
    session cookie already blocks the cross-site POST that CSRF
    would otherwise enable. Instead there is a soft Origin guard:
    a browser sends ``Origin`` on cross-site form posts, so a
    present-but-mismatched Origin is rejected; an absent Origin
    (curl) passes."""
    settings = get_settings()
    origin = request.headers.get("origin")
    if origin:
        origin_host = urlsplit(origin).netloc.strip().lower()
        request_host = request.headers.get("host", "").strip().lower()
        if not origin_host or origin_host != request_host:
            return _login_page_response(
                error="Cross-site login submission rejected."
            )
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

    outcome = await _authenticate_bearer(request, token)
    if not outcome.ok:
        return _login_page_response(error=outcome.detail or "Invalid token.")

    response = Response(status_code=303)
    response.headers["Location"] = "/dashboard"
    # Cookie security:
    # * httponly=True  -- no JS in the dashboard can exfiltrate it
    # * samesite=lax   -- SameSite=Strict would log the user out on
    #                     every external link; Lax lets top-level
    #                     navigation carry the cookie while still
    #                     blocking cross-site POSTs (the actual CSRF
    #                     surface). Matches GitHub/GitLab dashboards.
    # * secure         -- ``_request_uses_https`` honours
    #                     ``X-Forwarded-Proto`` so requests fronted by
    #                     Cloudflare or Traefik (which terminate TLS
    #                     and tunnel HTTP to the origin) still set the
    #                     Secure flag. Plain dev over
    #                     ``http://127.0.0.1`` stays usable because no
    #                     proxy injects the header.
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        value=token,
        max_age=settings.dashboard_session_lifetime_sec,
        httponly=True,
        samesite="lax",
        secure=_request_uses_https(request),
        path="/",
    )
    # v0.29.5: rotate the CSRF cookie on every successful login so a
    # form rendered for the previous session (a stale tab) can never
    # submit into the new one.
    response.set_cookie(
        CSRF_COOKIE,
        value=_issue_csrf_token(),
        **_csrf_cookie_kwargs(request),
    )
    return response


def _request_uses_https(request: Request) -> bool:
    """Decide whether the request reached us over TLS.

    ``request.url.scheme`` only sees the immediate transport, so a
    deployment fronted by Cloudflare or Traefik (which terminate
    TLS at the edge and tunnel HTTP to the origin) would always
    read as ``http`` even when the client connected over HTTPS.
    This helper checks four signals in priority order so the
    Secure cookie flag still gets set behind real-world proxy
    chains:

    1. ``request.url.scheme == "https"`` -- direct TLS to the
       origin (uvicorn with ``--ssl-certfile``).
    2. ``COORD_DASHBOARD_COOKIE_FORCE_SECURE=true`` -- operator
       escape hatch for proxy stacks that strip the headers below.
    3. ``X-Forwarded-Proto: https`` -- the standard header used by
       most reverse proxies. Traefik silently rewrites this header
       when the source IP is not in its trusted-IPs list, so it
       often comes through as ``http`` even when the real client
       transport was HTTPS.
    4. ``CF-Visitor: {"scheme":"https"}`` -- Cloudflare adds this
       at the edge and ``cloudflared`` preserves it into the
       tunnel. Traefik does NOT rewrite arbitrary headers, so
       this signal survives the cloudflared -> Traefik hop intact.
       The Cloudflare docs guarantee its presence on all proxied
       requests.

    Only the first hop of each multi-value header is checked, per
    the X-Forwarded-Proto convention.
    """
    if request.url.scheme == "https":
        return True

    if get_settings().dashboard_cookie_force_secure:
        return True

    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        first = forwarded.split(",")[0].strip().lower()
        if first == "https":
            return True

    cf_visitor = request.headers.get("cf-visitor", "")
    if cf_visitor:
        try:
            parsed = json.loads(cf_visitor)
        except (ValueError, TypeError):
            parsed = {}
        if isinstance(parsed, dict) and parsed.get("scheme") == "https":
            return True

    return False


@app.post("/dashboard/logout", response_class=HTMLResponse)
async def dashboard_logout(request: Request) -> Response:
    """Clear the session cookies and bounce back to the login form.

    Always returns 303 so a browser refresh after logout doesn't
    re-submit a POST. The cookies are cleared regardless of whether
    they were set, so a user who lost their session can hit
    /dashboard/logout to force-clean cookies before the next login.

    v0.29.5: requires a valid ``csrf_token`` form field -- a
    cross-site page must not be able to log the operator out (logout
    CSRF is a real annoyance attack). On failure the cookies stay
    untouched.
    """
    form = await request.form()
    csrf_value = form.get("csrf_token")
    if not isinstance(csrf_value, str) or not _validate_dashboard_csrf(
        request, csrf_value
    ):
        return _csrf_failure_response()
    response = Response(status_code=303)
    response.headers["Location"] = "/dashboard/login"
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
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
    .sso-sep {{
      margin: 1.25rem 0 0.75rem 0;
      text-align: center;
      color: var(--muted);
      font-size: 0.8rem;
      text-transform: lowercase;
    }}
    a.sso {{
      display: block;
      box-sizing: border-box;
      width: 100%;
      padding: 0.5rem 1rem;
      text-align: center;
      font-weight: 600;
      color: var(--accent);
      border: 1px solid var(--accent);
      border-radius: 6px;
      text-decoration: none;
    }}
    a.sso:hover {{ filter: brightness(1.1); }}
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
      {csrf_html}<button type="submit">log in</button>
    </form>
    {sso_html}<p class="help">
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


def _login_page_response(
    *, error: str | None = None, csrf_token: str | None = None
) -> HTMLResponse:
    """Render the login form. Status is always 200 so browsers do
    not display a default-style error page; the auth failure path
    surfaces via the inline error banner instead.

    ``csrf_token`` (v0.29.5) embeds the hidden ``csrf_token`` field.
    The login POST itself does not enforce CSRF (see
    ``dashboard_login_submit``), so the field is informational here;
    POST-failure re-renders may omit it."""
    error_html = ""
    if error:
        # Escape '<', '>', '&' so an attacker-controlled error string
        # (none today, but the helper is here so the next refactor
        # cannot regress) cannot inject markup.
        safe = (
            error.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        error_html = f'<p class="error">{safe}</p>'
    csrf_html = ""
    if csrf_token:
        csrf_html = (
            '<input type="hidden" name="csrf_token" '
            f'value="{html_mod.escape(csrf_token)}">\n      '
        )
    # v0.29.6: when SSO is configured the page offers it as an
    # alternative below the token form -- the form stays primary so
    # headless/per-engineer-token users keep their muscle memory.
    sso_html = ""
    if get_settings().oidc_enabled:
        sso_html = (
            '<div class="sso-sep">or</div>\n    '
            '<a class="sso" href="/auth/oidc/login">Sign in with SSO</a>\n    '
        )
    return HTMLResponse(
        _LOGIN_HTML.format(
            error_html=error_html, csrf_html=csrf_html, sso_html=sso_html
        )
    )


# ---------------------------------------------------------------------------
# v0.29.6 OIDC SSO login (generic authorization-code + PKCE)
# ---------------------------------------------------------------------------


# Transient cookie that carries the signed login state (state, nonce,
# PKCE verifier) across the redirect to the IdP and back. Ten minutes
# is plenty for a human to finish the IdP's login screen; the blob is
# HMAC-signed (keyed off the client secret) so it verifies on any
# replica and cannot be tampered with by the browser.
OIDC_LOGIN_STATE_COOKIE = "coord_oidc"
OIDC_LOGIN_STATE_MAX_AGE_SEC = 600


def _oidc_http_client() -> httpx.AsyncClient:
    """Factory for the outbound client used to talk to the IdP.

    Module-level and trivially small on purpose: tests monkeypatch
    this to return an ``AsyncClient`` backed by ``MockTransport`` so
    the real routes exercise the full protocol against an in-process
    fake IdP. Callers use ``async with`` so every request-handling
    closes its connections."""
    return httpx.AsyncClient(timeout=10.0)


def _oidc_error_page(
    title: str, message: str, status_code: int
) -> HTMLResponse:
    """Small HTML error page in the style of the other dashboard
    error pages (CSRF failure, token-management forbidden). Both the
    title and message are escaped, so IdP-supplied strings (the
    ``error`` query param) can be surfaced verbatim-but-safe."""
    safe_title = html_mod.escape(title)
    safe_message = html_mod.escape(message)
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>coord -- {safe_title}</title></head><body>"
        f"<h1>{safe_title}</h1>"
        f"<p>{safe_message}</p>"
        "<p><a href=\"/dashboard/login\">back to login</a></p>"
        "</body></html>",
        status_code=status_code,
    )


def _oidc_error_clearing_state(
    title: str, message: str, status_code: int
) -> HTMLResponse:
    """Error page that also drops the coord_oidc cookie -- used when
    the login state itself is the problem (missing, tampered,
    expired), so the user's retry starts from a clean slate."""
    response = _oidc_error_page(title, message, status_code)
    response.delete_cookie(OIDC_LOGIN_STATE_COOKIE, path="/")
    return response


@app.get("/auth/oidc/login")
async def oidc_login(request: Request) -> Response:
    """Start the OIDC authorization-code flow.

    Mints fresh state/nonce/PKCE material, signs it into the
    ``coord_oidc`` cookie, and 302s the browser to the IdP's
    authorize endpoint. Returns the API-style JSON 404 when SSO is
    not configured (the route effectively does not exist), the
    public-issuer policy refusal as a 403 HTML page, and discovery
    failures as a 502 HTML page."""
    settings = get_settings()
    if not settings.oidc_enabled:
        # detail is byte-identical to FastAPI's default for a route
        # that does not exist, so a probe cannot distinguish "coord
        # without SSO configured" from "no such endpoint".
        raise HTTPException(status_code=404, detail="Not Found")

    policy_error = oidc.principal_policy_error(settings)
    if policy_error:
        return _oidc_error_page(
            "SSO configuration refused", policy_error, 403
        )

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = oidc.make_pkce()

    try:
        async with _oidc_http_client() as client:
            metadata = await oidc.fetch_metadata(client, settings.oidc_issuer)
    except oidc.OIDCProtocolError as exc:
        return _oidc_error_page("IdP discovery failed", str(exc), 502)

    authorize_url = oidc.build_authorize_url(
        metadata,
        client_id=settings.oidc_client_id,
        redirect_uri=settings.oidc_redirect_uri,
        scopes=settings.oidc_scopes_list,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
    )

    response = Response(status_code=302)
    response.headers["Location"] = authorize_url
    response.set_cookie(
        OIDC_LOGIN_STATE_COOKIE,
        value=oidc.sign_login_state(
            {"state": state, "nonce": nonce, "verifier": verifier},
            secret=settings.oidc_client_secret,
        ),
        max_age=OIDC_LOGIN_STATE_MAX_AGE_SEC,
        httponly=True,
        samesite="lax",
        secure=_request_uses_https(request),
        path="/",
    )
    return response


@app.get("/auth/oidc/callback")
async def oidc_callback(request: Request) -> Response:
    """Finish the OIDC flow: verify the login state, redeem the code,
    validate the ID token, map the claim to an engineer, and mint a
    real per-engineer token bound to the session cookie.

    Every failure mode maps onto the oidc module's error taxonomy:
    state/cookie problems are 403 (the request is not a continuation
    of a login this server started), IdP/network problems are 502,
    token-validation problems are 401, and claim-policy problems are
    403. The raw session token is never logged and never appears in
    any rendered page -- it travels only inside the Set-Cookie header.
    """
    settings = get_settings()
    if not settings.oidc_enabled:
        # Same indistinguishable-404 posture as /auth/oidc/login.
        raise HTTPException(status_code=404, detail="Not Found")

    cookie = request.cookies.get(OIDC_LOGIN_STATE_COOKIE)
    login_state = (
        oidc.verify_login_state(cookie, secret=settings.oidc_client_secret)
        if cookie
        else None
    )
    if login_state is None:
        return _oidc_error_clearing_state(
            "SSO sign-in failed",
            "The sign-in attempt is missing, expired, or invalid. "
            "Start again from the login page.",
            403,
        )

    # IdP-reported denial (user clicked cancel, consent refused, ...).
    # The error code is attacker-influenced query input; the error
    # page escapes it.
    idp_error = request.query_params.get("error")
    if idp_error:
        return _oidc_error_clearing_state(
            "SSO sign-in failed",
            f"The identity provider reported: {idp_error}",
            403,
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return _oidc_error_clearing_state(
            "SSO sign-in failed",
            "The identity provider's response is missing the "
            "authorization code or state.",
            403,
        )
    if not hmac.compare_digest(state, login_state["state"]):
        return _oidc_error_clearing_state(
            "SSO sign-in failed",
            "State mismatch: this response does not belong to the "
            "sign-in attempt this browser started.",
            403,
        )

    try:
        async with _oidc_http_client() as client:
            metadata = await oidc.fetch_metadata(
                client, settings.oidc_issuer
            )
            token_payload = await oidc.exchange_code(
                client,
                token_endpoint=metadata["token_endpoint"],
                client_id=settings.oidc_client_id,
                client_secret=settings.oidc_client_secret,
                code=code,
                redirect_uri=settings.oidc_redirect_uri,
                code_verifier=login_state["verifier"],
            )
            claims = await oidc.validate_id_token(
                client,
                id_token=token_payload["id_token"],
                issuer=settings.oidc_issuer,
                client_id=settings.oidc_client_id,
                nonce=login_state["nonce"],
                jwks_uri=metadata["jwks_uri"],
            )
    except oidc.OIDCProtocolError as exc:
        return _oidc_error_clearing_state(
            "SSO sign-in failed", str(exc), 502
        )
    except oidc.OIDCValidationError as exc:
        return _oidc_error_clearing_state(
            "SSO sign-in failed", str(exc), 401
        )

    try:
        engineer = oidc.map_claim_to_engineer(
            claims,
            claim_name=settings.oidc_engineer_claim,
            allowed=settings.oidc_allowed_principal_set,
            prefix=settings.oidc_engineer_prefix,
            allow_any=settings.oidc_allow_any_principal,
            issuer=settings.oidc_issuer,
        )
    except oidc.OIDCClaimError as exc:
        return _oidc_error_clearing_state(
            "SSO sign-in refused", str(exc), 403
        )

    # #30 slice 2/3: optionally bind the SSO-minted token to a repo from a
    # configured claim, so SSO dashboard sessions are repo-scoped instead of
    # operator-wide. Configured-but-missing is a refusal, never a silent grant
    # of all-repo access.
    oidc_repo: str | None = None
    if settings.oidc_repo_claim:
        raw_repo = claims.get(settings.oidc_repo_claim)
        if not isinstance(raw_repo, str) or not raw_repo.strip():
            return _oidc_error_clearing_state(
                "SSO sign-in refused",
                (
                    f"the configured OIDC repo claim "
                    f"'{settings.oidc_repo_claim}' is missing or empty; this "
                    "deployment scopes SSO sessions to a repo."
                ),
                403,
            )
        try:
            oidc_repo = normalize_repo_id(raw_repo)
        except InvalidRepoId as exc:
            # #61: a malformed repo claim value is a refusal, not a 500 when
            # the token mint later rejects it.
            return _oidc_error_clearing_state(
                "SSO sign-in refused",
                (
                    f"the OIDC repo claim '{settings.oidc_repo_claim}' is not a "
                    f"valid repo id: {exc}"
                ),
                403,
            )

    # A deployment that requires repo-scoped tokens but does not bind SSO
    # sessions to a repo would mint an unscoped session token that the
    # bearer path (_authenticate_bearer) rejects on the very next
    # request -- a dead session. Refuse the login up front with an
    # actionable message instead of handing out a credential that cannot
    # be used.
    if settings.require_scoped_token and oidc_repo is None:
        return _oidc_error_clearing_state(
            "SSO configuration error",
            "COORD_REQUIRE_SCOPED_TOKEN is set but this SSO login is not "
            "bound to a repo. Configure COORD_OIDC_REPO_CLAIM so the "
            "ID token carries the repo, or the minted session would be "
            "rejected immediately.",
            500,
        )

    # SSO sessions mint a REAL per-engineer token whose lifetime is
    # exactly the dashboard session lifetime, so the credential dies
    # with the cookie instead of accumulating as an immortal row. A
    # deployment that disabled cookie sessions (lifetime <= 0) cannot
    # host SSO logins at all -- there would be nothing to bind the
    # minted token to and a max_age=0 cookie would never persist --
    # so that combination is a configuration error, not a silent
    # fallback.
    if settings.dashboard_session_lifetime_sec <= 0:
        return _oidc_error_clearing_state(
            "SSO configuration error",
            "COORD_DASHBOARD_SESSION_LIFETIME_SEC must be positive for "
            "OIDC SSO logins (the session cookie is the credential).",
            500,
        )
    expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.dashboard_session_lifetime_sec
    )
    raw = generate_raw_token()
    await get_service().db.create_engineer_token(
        engineer,
        sha256_token(raw),
        description="oidc sso login",
        expires_at=expires_at,
        repo=oidc_repo,
    )

    response = Response(status_code=303)
    response.headers["Location"] = "/dashboard"
    # Same cookie posture as the form login: HttpOnly, SameSite=Lax,
    # Secure behind TLS/proxies (see dashboard_login_submit for the
    # full rationale), lifetime matching the minted token's expiry.
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        value=raw,
        max_age=settings.dashboard_session_lifetime_sec,
        httponly=True,
        samesite="lax",
        secure=_request_uses_https(request),
        path="/",
    )
    # Rotate the CSRF cookie exactly like the form login does, so a
    # form rendered for a previous session cannot submit into the
    # fresh SSO session.
    response.set_cookie(
        CSRF_COOKIE,
        value=_issue_csrf_token(),
        **_csrf_cookie_kwargs(request),
    )
    # The login state is single-use: success consumes it.
    response.delete_cookie(OIDC_LOGIN_STATE_COOKIE, path="/")
    return response


# ---------------------------------------------------------------------------
# v0.29.5 in-dashboard token management
# ---------------------------------------------------------------------------


def _form_str(form: Any, name: str) -> str:
    """Pull a text field out of a parsed form. ``form.get`` can return
    UploadFile when a multipart field is bound to a file input; only
    strings are credentials/identifiers here, so anything else reads
    as empty."""
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


async def _authenticate_token_management(
    request: Request, authorization: str | None
) -> AuthOutcome | Response:
    """Shared gate for the token create/revoke endpoints. Returns the
    AuthOutcome for an authenticated per-engineer or shared session,
    or a ready-to-send error Response otherwise. The insecure no-auth
    mode is deliberately locked out: with no identity and no
    credential there is nothing to bind a minted token to and nothing
    stopping anyone on the network from revoking everything."""
    outcome = await _authenticate_bearer(
        request, _extract_bearer(authorization, request)
    )
    if not outcome.ok:
        raise HTTPException(
            status_code=outcome.status_code, detail=outcome.detail
        )
    if outcome.auth_kind not in ("per_engineer", "shared"):
        return HTMLResponse(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>coord -- forbidden</title></head><body>"
            "<p>Token management requires an authenticated session. "
            "The insecure no-auth mode cannot manage tokens.</p>"
            "</body></html>",
            status_code=403,
        )
    return outcome


async def _dashboard_with_token_error(
    request: Request, outcome: AuthOutcome, message: str
) -> HTMLResponse:
    """Re-render the dashboard with the token panel's error banner.
    Only reached after the CSRF check passed, so the cookie is known
    to be present and well-shaped -- no minting needed."""
    csrf, _ = _csrf_for_request(request)
    return HTMLResponse(
        await _render_dashboard_for(outcome, csrf, token_error=message)
    )


def _iso_z_or_never(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@app.post("/dashboard/tokens/create", response_class=HTMLResponse)
async def dashboard_tokens_create(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Mint a per-engineer token from the dashboard's create form.

    Form fields: ``engineer``, ``description``, ``expires_in``,
    ``csrf_token``. Per-engineer sessions can only mint for
    themselves (the submitted engineer field is ignored) and, when
    their own session token expires, only tokens that expire no later
    -- self-service must never escalate lifetime. Shared-token
    (operator) sessions mint for anyone with no expiry cap.

    On success the raw token is returned in a one-time HTML page with
    ``Cache-Control: no-store``; it is never logged and cannot be
    re-rendered -- only its sha256 hash is stored.
    """
    gate = await _authenticate_token_management(request, authorization)
    if isinstance(gate, Response):
        return gate
    outcome = gate

    form = await request.form()
    csrf_value = form.get("csrf_token")
    if not isinstance(csrf_value, str) or not _validate_dashboard_csrf(
        request, csrf_value
    ):
        return _csrf_failure_response()

    description = _form_str(form, "description") or None
    expires_in = _form_str(form, "expires_in")

    if outcome.auth_kind == "per_engineer":
        # Self-service: the session's identity wins, always. A forged
        # engineer field must not mint credentials for someone else.
        engineer = outcome.engineer or ""
    else:
        engineer = _form_str(form, "engineer")
        if not engineer:
            return await _dashboard_with_token_error(
                request, outcome, "Engineer is required to mint a token."
            )

    expires_at: datetime | None = None
    if expires_in:
        # Parse before touching the database so a typo'd duration
        # never leaves a half-configured token behind.
        try:
            expires_at = datetime.now(UTC) + parse_duration(expires_in)
        except ValueError as exc:
            return await _dashboard_with_token_error(
                request, outcome, str(exc)
            )

    # Self-service expiry policy: a per-engineer session whose own
    # token expires can only mint tokens that expire at or before
    # that point. Otherwise an engineer with a 7-day token could mint
    # themselves a never-expiring one and defeat the operator's
    # rotation policy. Operator (shared) sessions are uncapped.
    if (
        outcome.auth_kind == "per_engineer"
        and outcome.token_expires_at is not None
    ):
        try:
            cap = datetime.fromisoformat(
                outcome.token_expires_at.replace("Z", "+00:00")
            )
        except ValueError:
            return await _dashboard_with_token_error(
                request,
                outcome,
                "Your session token's expiry could not be read; "
                "ask an operator to mint the token.",
            )
        if expires_at is None:
            return await _dashboard_with_token_error(
                request,
                outcome,
                "Your session token expires "
                f"{outcome.token_expires_at}; new tokens must set "
                "expires_in and expire no later than that.",
            )
        if expires_at > cap:
            return await _dashboard_with_token_error(
                request,
                outcome,
                f"expires_in reaches past {outcome.token_expires_at}, "
                "your own token's expiry. Self-service tokens cannot "
                "outlive the session token; pick a shorter duration.",
            )

    # #30 slice 2/3: a repo-scoped session may only mint tokens bound to its
    # own repo -- it must never be able to mint an unscoped (operator) token
    # or one for another repo, which would be a privilege escalation. An
    # operator (unscoped / shared) session has token_repo=None and keeps
    # minting unscoped tokens as before.
    new_token_repo = outcome.token_repo
    raw = generate_raw_token()
    token_id = await get_service().db.create_engineer_token(
        engineer,
        sha256_token(raw),
        description=description,
        expires_at=expires_at,
        repo=new_token_repo,
    )

    # One-time reveal page. The raw value exists only in this
    # response body: not logged, not stored, not re-renderable.
    # no-store keeps it out of browser/proxy caches.
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>coord -- token created</title></head><body>"
        "<h1>token created</h1>"
        "<p><strong>This token is shown exactly once.</strong> "
        "coord stores only its sha256 hash; if lost, revoke and "
        "reissue.</p>"
        f"<p><code>{html_mod.escape(raw)}</code></p>"
        f"<p>engineer: {html_mod.escape(engineer)}<br>"
        f"token id: {html_mod.escape(token_id)}<br>"
        f"repo scope: {html_mod.escape(new_token_repo or 'all repos (unscoped)')}<br>"
        f"expires: {html_mod.escape(_iso_z_or_never(expires_at))}</p>"
        "<p><a href=\"/dashboard\">back to dashboard</a></p>"
        "</body></html>"
    )
    return HTMLResponse(
        page,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/dashboard/tokens/revoke", response_class=HTMLResponse)
async def dashboard_tokens_revoke(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Revoke a token from the dashboard's inline revoke buttons.

    Form fields: ``token_id``, ``csrf_token``. Per-engineer sessions
    can only revoke their own tokens (the UPDATE is engineer-scoped
    in the database, so the check is atomic); a repo-scoped session is
    further confined to its own repo, so it cannot revoke the same
    engineer's token in another repo or an unscoped operator token. A
    foreign live token answers 403 with no mutation. Operators revoke
    anyone's. Missing or already-revoked ids are idempotent successes
    -- 303 back to the dashboard either way (PRG)."""
    gate = await _authenticate_token_management(request, authorization)
    if isinstance(gate, Response):
        return gate
    outcome = gate

    form = await request.form()
    csrf_value = form.get("csrf_token")
    if not isinstance(csrf_value, str) or not _validate_dashboard_csrf(
        request, csrf_value
    ):
        return _csrf_failure_response()

    token_id = _form_str(form, "token_id")
    db = get_service().db
    if outcome.auth_kind == "per_engineer":
        # A repo-scoped session may only revoke tokens in its own repo;
        # the repo predicate is folded into the atomic UPDATE so a
        # same-engineer token in another repo (or an unscoped operator
        # token) is never touched.
        revoked = await db.revoke_engineer_token(
            token_id,
            engineer=outcome.engineer,
            repo=outcome.token_repo,
        )
        if not revoked:
            # Distinguish "not yours" (403, no mutation) from "gone or
            # already revoked" (idempotent 303). The scoped UPDATE
            # already guaranteed no foreign row was touched.
            row = await db.get_engineer_token_by_id(token_id)
            if row is not None:
                if row.get("engineer") != outcome.engineer:
                    return HTMLResponse(
                        "<!doctype html><html lang=\"en\"><head>"
                        "<meta charset=\"utf-8\">"
                        "<title>coord -- forbidden</title></head><body>"
                        "<p>That token belongs to another engineer. Ask an "
                        "operator to revoke it.</p>"
                        "<p><a href=\"/dashboard\">back to dashboard</a></p>"
                        "</body></html>",
                        status_code=403,
                    )
                if (
                    outcome.token_repo is not None
                    and row.get("repo") != outcome.token_repo
                ):
                    return HTMLResponse(
                        "<!doctype html><html lang=\"en\"><head>"
                        "<meta charset=\"utf-8\">"
                        "<title>coord -- forbidden</title></head><body>"
                        "<p>That token belongs to another repo. Ask an "
                        "operator to revoke it.</p>"
                        "<p><a href=\"/dashboard\">back to dashboard</a></p>"
                        "</body></html>",
                        status_code=403,
                    )
    else:
        await db.revoke_engineer_token(token_id)

    response = Response(status_code=303)
    response.headers["Location"] = "/dashboard"
    return response


@app.post("/claims")
async def create_claims(request: Request, body: CreateClaimsRequest, _: None = Depends(require_auth)) -> JSONResponse:
    body.repo = _enforce_write_repo(request, body.repo)
    # v0.30: rate-limit raises map to 429 before (and independent of)
    # the warnings->400 mapping below -- a quota breach is not a
    # validation problem and must carry its own Retry-After signal.
    # This is the only endpoint that can see RateLimitExceeded: the
    # release paths reach create_claims only via _drain_queue_for,
    # which swallows the exception per-waiter, and the v0.11
    # narrowed/coexist decisions create claims in the DB layer without
    # passing through create_claims at all.
    # Auto-promote writes global ownership YAML, so only an unscoped
    # (operator) token may trigger it. A repo-scoped token creating a
    # conflicting claim must not rewrite deployment-wide config.
    try:
        result = await get_service().create_claims(
            body, auto_promote_allowed=_token_repo(request) is None
        )
    except RateLimitExceeded as exc:
        return JSONResponse(
            status_code=429,
            content={
                "detail": exc.detail,
                "scope": exc.scope,
                "retry_after": exc.retry_after_sec,
            },
            headers={"Retry-After": str(exc.retry_after_sec)},
        )
    payload = jsonable_encoder(result)
    if result.conflicts:
        return JSONResponse(status_code=409, content=payload)
    if not result.claim_ids and result.warnings:
        return JSONResponse(status_code=400, content=payload)
    return JSONResponse(status_code=200, content=payload)


@app.post("/claims/refactor")
async def claim_refactor(
    request: Request, body: ClaimRefactorRequest, _: None = Depends(require_auth)
) -> JSONResponse:
    """v0.31 wave 2: reserve a symbol's definition plus every callsite
    the language server can see, as one normal claims batch. Response
    shapes are byte-compatible with POST /claims (200 / 400 / 409 /
    429); the only new shape is 503 when no language server can answer,
    because refactor claims are meaningless without references."""
    body.repo = _enforce_write_repo(request, body.repo)
    try:
        result = await get_service().create_refactor_claims(
            body, auto_promote_allowed=_token_repo(request) is None
        )
    except LspUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    f"{exc}. LSP integration is disabled or unavailable "
                    "(COORD_LSP_ENABLED); refactor claims need a live "
                    "language server."
                )
            },
        )
    except RateLimitExceeded as exc:
        # Same 429 contract as POST /claims: the generated batch goes
        # through the normal create_claims pipeline, so the same quota
        # raises can surface here.
        return JSONResponse(
            status_code=429,
            content={
                "detail": exc.detail,
                "scope": exc.scope,
                "retry_after": exc.retry_after_sec,
            },
            headers={"Retry-After": str(exc.retry_after_sec)},
        )
    payload = jsonable_encoder(result)
    if result.conflicts:
        return JSONResponse(status_code=409, content=payload)
    if not result.claim_ids and result.warnings:
        return JSONResponse(status_code=400, content=payload)
    return JSONResponse(status_code=200, content=payload)


@app.get("/claims")
async def list_claims(
    request: Request,
    active: bool = Query(default=True, alias="active_only"),
    engineer: str | None = None,
    module: str | None = None,
    repo: str | None = None,
    all_repos: bool = Query(default=False),
    session_id: str | None = None,
    _: None = Depends(require_auth),
) -> dict:
    repo = _effective_read_repo(request, repo, all_repos=all_repos)
    rows = await get_service().list_claims(
        active_only=active,
        engineer=engineer,
        module_substring=module,
        session_id=session_id,
        repo=repo,
    )
    if repo is not None:
        rows = [r for r in rows if r.get("repo") == repo]
    return {"claims": rows, "count": len(rows)}


@app.get("/repos")
async def list_repos(request: Request, _: None = Depends(require_auth)) -> dict:
    """Per-repo activity summary: one row for each distinct repo that has
    ever submitted a claim with a non-null repo identifier."""
    rows = await get_service().db.list_repos()
    token_repo = _token_repo(request)
    if token_repo is not None:
        rows = [r for r in rows if r.get("repo") == token_repo]
    return {"repos": rows, "count": len(rows)}


@app.get("/metrics/hotspots")
async def hotspots_metric(
    days: int = Query(default=30, ge=1, le=90),
    min_attempts: int = Query(default=5, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    repo: str | None = Query(default=None),
    all_repos: bool = Query(default=False),
    request: Request = None,  # type: ignore[assignment]
    _: None = Depends(require_auth),
) -> dict:
    """Top files by blocked claim attempts (v0.20).

    Operators look at this list to decide which files belong in a
    ``shared_file`` rule or which areas need to be split into modules.
    """
    repo = _effective_read_repo(request, repo, all_repos=all_repos)
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
    request: Request,
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
    _require_operator(request)
    try:
        patched = await get_service().promote_hotspot(
            action=body.action,
            pattern=body.pattern,
            note=body.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # No ``repo`` in the response: the ownership YAML write is
    # deployment-global (which is why the route is operator-only), so
    # echoing a client-supplied repo would misrepresent the promotion as
    # repo-scoped. Clients still sending the retired field are ignored.
    return {
        "ok": True,
        "action": body.action,
        "pattern": body.pattern,
        "patched_yaml": patched,
    }


@app.get("/metrics/auto-resolutions")
async def auto_resolutions_metric(
    days: int = Query(default=30, ge=1, le=90),
    repo: str | None = Query(default=None),
    all_repos: bool = Query(default=False),
    request: Request = None,  # type: ignore[assignment]
    _: None = Depends(require_auth),
) -> dict:
    """Daily auto-coexist / auto-narrow counts per repo (v0.18).

    The series is consumed by the dashboard heatmap and is also
    available standalone for external monitoring. Empty days are
    omitted -- callers that want a dense grid fill the gaps.
    """
    repo = _effective_read_repo(request, repo, all_repos=all_repos)
    rows = await get_service().db.daily_auto_resolutions(days=days, repo=repo)
    return {"series": rows, "days": days, "count": len(rows)}


@app.get("/conflicts")
async def conflicts(
    pattern: list[str] | None = Query(default=None),
    engineer: str = Query(...),
    repo: str | None = Query(default=None),
    all_repos: bool = Query(default=False),
    session_id: list[str] | None = Query(default=None),
    branch: str | None = Query(default=None),
    request: Request = None,  # type: ignore[assignment]
    _: None = Depends(require_auth),
) -> dict:
    repo = _effective_read_repo(request, repo, all_repos=all_repos)
    if not pattern:
        raise HTTPException(status_code=400, detail="Provide one or more pattern= query params")
    try:
        # FastAPI parses repeated `session_id=` query params into a
        # list. The pre-push hook forwards every live session_id from
        # .coordination/sessions.live so the agent's own subagent
        # claims under different engineer names don't false-positive
        # on its own push.
        #
        # ``branch`` (v0.34) is the pushing branch the hook resolved.
        # check_conflicts uses it to build the push_bounced GitHub
        # event so a bounced push can comment on the open PR; it is a
        # no-op when COORD_GITHUB_TOKEN is unset and never affects the
        # conflict response shape.
        result = await get_service().check_conflicts(
            patterns=pattern,
            engineer=engineer,
            repo=repo,
            all_repos=all_repos,
            session_ids=session_id,
            pushing_branch=branch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result.model_dump()


@app.post("/sessions/{session_id}/release")
async def release_session(
    session_id: str,
    request: Request,
    _: None = Depends(require_auth),
) -> dict:
    """Release every active claim that was created with the given
    session_id. Intended for end-of-work cleanup so a single call from
    coord-mcp tears down everything the agent and its subagents
    produced, regardless of engineer name.

    A repo-scoped token releases only that repo's claims within the
    session (#30 slice 2/3), so it cannot tear down another repo's work
    that happens to share a session id.

    Routed through the service layer (not db.release_for_session
    directly) so every released claim drains its FIFO queue -- waiters
    queued behind this session's claims are granted here exactly like
    an explicit release would grant them."""
    n = await get_service().release_session(
        session_id, repo=_token_repo(request)
    )
    return {"released": n}


@app.get("/sessions/{session_id}/pending_requests")
async def pending_requests(
    session_id: str,
    request: Request,
    _: None = Depends(require_auth),
) -> dict:
    """Return the merged inbox the holder polls. Includes first-class
    release requests (``kind='request'``) and the read-only auto-conflict
    log (``kind='auto-conflict'``). An active holder polls this between
    operations so they can approve / deny pending release requests and
    see who has been blocked on their scope. Coord-mcp exposes this as
    a `pending_requests` tool."""
    # Row-level repo scoping: a scoped token gets only its own repo's
    # rows even when the session id spans repos. Filtering per row (not
    # per session) also means out-of-scope requests never fire a
    # ``notified`` audit event.
    rows = await get_service().pending_requests(
        session_id, repo=_token_repo(request)
    )
    return {"pending": rows, "count": len(rows)}


@app.post("/requests")
async def file_request(
    request: Request,
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
    await _require_claim_in_scope(request, body.claim_id)
    svc = get_service()
    try:
        filed = await svc.file_request(
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
            filed["id"], timeout_seconds=body.wait_seconds
        )
        if final is not None:
            filed = final

    return JSONResponse(status_code=200, content=filed)


@app.post("/requests/{request_id}/respond")
async def respond_to_request(
    request_id: str,
    body: RespondToRequestRequest,
    request: Request,
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
    await _require_request_in_scope(request, request_id)
    named_engineer = body.engineer
    if (
        named_engineer is None
        and getattr(request.state, "auth_kind", None) == "per_engineer"
    ):
        # An omitted actor is not an impersonation attempt: the
        # authenticated identity is acting. Defaulting it here (in both
        # warn and enforce modes) keeps the standard MCP holder flow --
        # which sends no ``engineer`` on respond -- working under the
        # holder-authorization check below, and records a real identity
        # in ``decided_by_engineer`` instead of NULL.
        named_engineer = getattr(request.state, "engineer", None)
    actor_engineer = _bind_mutation_engineer(
        request, named_engineer, operation="respond to a release request"
    )
    # Holder authorization: only the target claim's holder may decide a
    # request against it -- otherwise a requester could file a request
    # and immediately self-approve it, releasing the holder's claim.
    # Enforced at the API layer for per-engineer tokens; the shared
    # operator token (and no-auth deployments) stays exempt so dashboard
    # and operator flows keep working. The check compares the NAMED
    # actor: with COORD_ENFORCE_ENGINEER_IDENTITY=enforce the actor is
    # already bound to the token identity above, which closes the loop
    # against a requester lying about who they are.
    if getattr(request.state, "auth_kind", None) == "per_engineer":
        exists, holder = await get_service().db.request_claim_holder(
            request_id
        )
        if exists and holder is not None and actor_engineer != holder:
            named_actor = (
                f"engineer '{actor_engineer}'"
                if actor_engineer is not None
                else "no engineer"
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Only the claim holder '{holder}' may respond to "
                    f"this request; the response named {named_actor}. "
                    "Operator (shared) tokens are exempt."
                ),
            )
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
    if (
        body.decision == "coexist"
        and not body.coexist_pattern
        and not body.coexist_symbols
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "decision='coexist' requires a non-empty 'coexist_pattern' "
                "or 'coexist_symbols'"
            ),
        )
    try:
        result = await get_service().respond_to_request(
            request_id=request_id,
            decision=body.decision,
            actor_engineer=actor_engineer,
            actor_session_id=body.session_id,
            note=body.note,
            narrowed_pattern=body.narrowed_pattern,
            coexist_pattern=body.coexist_pattern,
            coexist_symbols=body.coexist_symbols,
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
    request: Request = None,  # type: ignore[assignment]
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
    token_repo = _token_repo(request)
    if queued:
        # The repo filter is pushed into the query (claim_queue.repo is
        # the requester's repo at enqueue time) so the LIMIT window is
        # per-repo: on a shared multi-repo service with more live queue
        # rows than the limit, filtering after the LIMIT would let other
        # repos' rows crowd a scoped token's own entries out entirely.
        rows = await get_service().db.list_queued_with_holder(
            engineer=requester,
            state=state or "waiting",
            repo=token_repo,
        )
        if token_repo is not None:
            # Belt-and-braces re-check of the SQL-side scope filter.
            rows = [r for r in rows if r.get("repo") == token_repo]
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
    if token_repo is not None:
        # holder_repo is the target claim's repo (joined in list_requests).
        rows_legacy = [
            r for r in rows_legacy if r.get("holder_repo") == token_repo
        ]
    return {"requests": rows_legacy, "count": len(rows_legacy)}


@app.get("/requests/{request_id}")
async def get_request(
    request_id: str,
    request: Request,
    _: None = Depends(require_auth),
) -> JSONResponse:
    await _require_request_in_scope(request, request_id)
    row = await get_service().get_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="request not found")
    return JSONResponse(status_code=200, content=row)


@app.get("/requests/{request_id}/events")
async def get_request_events(
    request_id: str,
    request: Request,
    _: None = Depends(require_auth),
) -> dict:
    """Return the immutable audit-event timeline for a request,
    oldest first. Each row carries the actor, timestamp, and a JSON
    detail blob with the per-event-type specifics."""
    await _require_request_in_scope(request, request_id)
    rows = await get_service().list_request_events(request_id)
    return {"events": rows, "count": len(rows)}


@app.delete("/requests/{queue_id}")
async def cancel_request(
    queue_id: str,
    request: Request,
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
    await _require_queue_in_scope(request, queue_id)
    engineer = _bind_mutation_engineer(
        request, engineer, operation="cancel a queued request"
    )
    cancelled = await get_service().cancel_queue_request(
        queue_id, engineer=engineer
    )
    return {"ok": True, "cancelled": cancelled, "queue_id": queue_id}


@app.delete("/claims/{claim_id}")
async def delete_claim(
    claim_id: str,
    request: Request,
    engineer: str | None = Query(default=None),
    _: None = Depends(require_auth),
) -> dict:
    await _require_claim_in_scope(request, claim_id)
    engineer = _bind_mutation_engineer(
        request, engineer, operation="release a claim"
    )
    released = await get_service().release_claims([claim_id], engineer)
    return {"released": released}


@app.post("/claims/release")
async def release_claims(body: ReleaseClaimsRequest, request: Request, _: None = Depends(require_auth)) -> dict:
    for _cid in body.claim_ids:
        await _require_claim_in_scope(request, _cid)
    engineer = _bind_mutation_engineer(
        request, body.engineer, operation="release claims"
    )
    released = await get_service().release_claims(body.claim_ids, engineer)
    return {"released": released}


@app.post("/claims/{claim_id}/extend")
async def extend_claim(
    claim_id: str,
    body: ExtendClaimRequest,
    request: Request,
    _: None = Depends(require_auth),
) -> dict:
    await _require_claim_in_scope(request, claim_id)
    ok = await get_service().extend_claim(claim_id, body.engineer, body.ttl_hours)
    if not ok:
        raise HTTPException(status_code=404, detail="Claim not found or not owned")
    return {"ok": True}


# Ownership YAML files are a few KB; anything past this is a mistake or
# abuse, and reading it fully into memory before parsing would let one
# oversized upload balloon the process.
OWNERSHIP_MAX_BODY_BYTES = 1 << 20  # 1 MiB


@app.post("/config/ownership")
async def set_ownership(request: Request, _: None = Depends(require_auth)) -> dict:
    _require_operator(request)
    # Bound the body read: check the declared Content-Length first for a
    # fast 413 without consuming the stream, then enforce the same cap
    # while streaming so a chunked body with no declared length cannot
    # buffer unbounded either.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_len = int(declared)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length header"
            ) from exc
        if declared_len > OWNERSHIP_MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Ownership YAML body exceeds the "
                    f"{OWNERSHIP_MAX_BODY_BYTES}-byte limit"
                ),
            )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > OWNERSHIP_MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Ownership YAML body exceeds the "
                    f"{OWNERSHIP_MAX_BODY_BYTES}-byte limit"
                ),
            )
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Ownership YAML must be valid UTF-8"
        ) from exc
    try:
        rules = parse_ownership_yaml(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await get_service().set_ownership_yaml(text)
    return {"ok": True, "rules": len(rules)}


@app.get("/config/ownership")
async def get_ownership(
    request: Request, _: None = Depends(require_auth)
) -> Response:
    # Ownership YAML is deployment-wide (not repo-tagged) config: it can
    # disclose other repos' shared_files / split rules. The POST sibling is
    # already operator-only, so the read is too (v0.42) -- a repo-scoped
    # token cannot enumerate the whole deployment's ownership policy.
    _require_operator(request)
    text = await get_service().get_ownership_yaml()
    if text is None:
        return PlainTextResponse("", status_code=204)
    return PlainTextResponse(text, media_type="text/yaml")


def run() -> None:
    import uvicorn

    settings = get_settings()
    # coord owns logging: route uvicorn's loggers through coord's formatter
    # and pass log_config=None so uvicorn does not reinstall its own. Disable
    # uvicorn's access log -- the access-log middleware already emits a richer
    # structured line (method/path/status/duration_ms/request_id), so leaving
    # uvicorn's on would just duplicate it.
    configure_uvicorn_logging()
    uvicorn.run(
        "coordination.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    run()
