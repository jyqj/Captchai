"""YesCaptcha / AntiCaptcha compatible HTTP routes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..core.config import config
from ..core.task_types import ValidationKind, names_for_validation
from ..models.task import (
    CreateTaskRequest,
    CreateTaskResponse,
    GetBalanceRequest,
    GetBalanceResponse,
    GetTaskResultRequest,
    GetTaskResultResponse,
    ReportTaskRequest,
    ReportTaskResponse,
    SolutionObject,
)
from ..core.services import get_services
from ..orchestration.store import IdempotencyConflict
from ..services.outcome import record_real_outcome
from ..services.task_manager import QueueFull, TaskStatus, task_manager

log = logging.getLogger(__name__)

router = APIRouter()

# Required-field validation buckets derive from the single ``core.task_types``
# registry so a newly registered task type can't silently skip validation by
# being absent from a hand-maintained set here.
_BROWSER_TASK_TYPES = names_for_validation(ValidationKind.BROWSER)
_IMAGE_TASK_TYPES = names_for_validation(ValidationKind.IMAGE)
_CLASSIFICATION_TASK_TYPES = names_for_validation(ValidationKind.CLASSIFICATION)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
# One-shot guard so the legacy admin-auth deprecation is logged only once.
_ADMIN_QUERY_DEPRECATION_LOGGED = False
# One-shot guard so a pre-upgrade task record without an owner marker is
# reported once, not on every poll.
_LEGACY_OWNERLESS_TASK_LOGGED = False
# Task-params key carrying the non-reversible owner digest. Deliberately not
# ``_``-prefixed: ``redact_params`` drops internal keys, and this marker must
# SURVIVE persistence for tasks read back from the store.
_CLIENT_KEY_HASH_PARAM = "clientKeyHash"


def _client_key_hash(client_key: str | None) -> str:
    """Return the non-reversible owner marker for ``client_key``.

    Persisted params cannot carry the raw key (``redact_params`` drops
    ``_clientKey`` and every credential field), so a task read back from the
    store proves ownership with this digest instead. ``surrogatepass`` keeps
    a lone-surrogate key (possible in JSON input) from raising here.
    """
    return hashlib.sha256(
        (client_key or "").encode("utf-8", "surrogatepass")
    ).hexdigest()


def _allow_legacy_ownerless_task(task_id: str) -> None:
    """Warn once that a task record predates the owner marker."""
    global _LEGACY_OWNERLESS_TASK_LOGGED
    if _LEGACY_OWNERLESS_TASK_LOGGED:
        return
    _LEGACY_OWNERLESS_TASK_LOGGED = True
    log.warning(
        "Task %s has no ownership marker (pre-upgrade record); allowing access "
        "without an ownership check. Records created from now on carry one.",
        task_id,
    )


def _constant_time_eq(left: str | None, right: str | None) -> bool:
    """Constant-time string comparison.

    Compares UTF-8 bytes so a non-ASCII client key can't make
    ``hmac.compare_digest`` raise (it only accepts ASCII ``str``).
    """
    if left is None or right is None:
        return False
    return hmac.compare_digest(
        str(left).encode("utf-8", "surrogatepass"),
        str(right).encode("utf-8", "surrogatepass"),
    )


def _check_client_key(client_key: str) -> CreateTaskResponse | None:
    """Return an error response if the client key is invalid, else None."""
    if config.client_key and not _constant_time_eq(client_key, config.client_key):
        return CreateTaskResponse(
            errorId=1,
            errorCode="ERROR_KEY_DOES_NOT_EXIST",
            errorDescription="Invalid clientKey",
        )
    return None


def _admin_key() -> str | None:
    """Return the configured admin bearer key, if any.

    ``admin_key`` lives on ``core.config.Config``; ``getattr`` keeps this
    module importable against config objects that predate the field.
    """
    return getattr(config, "admin_key", None) or None


def _is_loopback(request: Request) -> bool:
    """True when the request originates from a loopback address."""
    client = request.client
    host = getattr(client, "host", None) if client is not None else None
    if not host:
        return False
    host = host.strip("[]")
    if host.startswith("::ffff:"):  # IPv4-mapped IPv6 form of 127.0.0.1
        host = host[len("::ffff:") :]
    return host in _LOOPBACK_HOSTS


def _admin_error(status_code: int, description: str) -> JSONResponse:
    """Admin auth failure in the YesCaptcha envelope shape used by this file."""
    return JSONResponse(
        status_code=status_code,
        content={
            "errorId": 1,
            "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
            "errorDescription": description,
        },
    )


def _check_admin(request: Request, client_key: str) -> JSONResponse | None:
    """Authorize an ``/admin/*`` request.

    With ``ADMIN_KEY`` configured, only ``Authorization: Bearer <key>`` is
    accepted (the legacy query-param path is not consulted). Without it the
    legacy ``clientKey`` query param still works when ``CLIENT_KEY`` is set;
    in fully keyless local mode admin data is served to loopback clients only.

    Reverse-proxy caveat: the loopback fallback trusts ``request.client``, the
    ASGI peer address. Behind a same-host reverse proxy every internet request
    appears to originate from 127.0.0.1, so the guard is only sound when the
    app is bound directly to loopback with no proxy in front. ``X-Forwarded-For``
    is deliberately not trusted for this decision. Set ``ADMIN_KEY`` (or at
    least ``CLIENT_KEY``) for any proxied deployment.
    """
    admin_key = _admin_key()
    if admin_key is not None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not _constant_time_eq(
            token.strip(), admin_key
        ):
            return _admin_error(403, "Invalid or missing admin key")
        return None

    global _ADMIN_QUERY_DEPRECATION_LOGGED
    if not _ADMIN_QUERY_DEPRECATION_LOGGED:
        _ADMIN_QUERY_DEPRECATION_LOGGED = True
        log.warning(
            "Deprecated admin auth: query-param clientKey / keyless loopback is "
            "deprecated; set ADMIN_KEY and send 'Authorization: Bearer <key>'."
        )

    if config.client_key:
        # Defense in depth: once a caller key is configured it is required on
        # /admin from EVERY peer, loopback included — a local process (or a
        # same-host proxy, see the docstring) must not ride the loopback trust
        # path just because its source address looks like 127.0.0.1.
        if not _constant_time_eq(client_key, config.client_key):
            return _admin_error(200, "Invalid clientKey")
        return None

    # Keyless local mode: exact loopback forms only (``localhost`` is not
    # accepted, and forwarded headers are not consulted).
    if not _is_loopback(request):
        return _admin_error(
            403, "Admin endpoints are loopback-only when no key is configured"
        )
    return None


@router.post("/createTask", response_model=CreateTaskResponse)
async def create_task(request: CreateTaskRequest) -> CreateTaskResponse:
    err = _check_client_key(request.clientKey)
    if err:
        return err

    supported = task_manager.supported_types()
    if request.task.type not in supported:
        return CreateTaskResponse(
            errorId=1,
            errorCode="ERROR_TASK_NOT_SUPPORTED",
            errorDescription=f"Task type '{request.task.type}' is not supported. "
            f"Supported: {supported}",
        )

    # Validate required fields for browser-based tasks
    if request.task.type in _BROWSER_TASK_TYPES:
        if not request.task.websiteURL or not request.task.websiteKey:
            return CreateTaskResponse(
                errorId=1,
                errorCode="ERROR_TASK_PROPERTY_EMPTY",
                errorDescription="websiteURL and websiteKey are required",
            )

    # Validate required fields for ImageToText tasks
    if request.task.type in _IMAGE_TASK_TYPES:
        if not request.task.body:
            return CreateTaskResponse(
                errorId=1,
                errorCode="ERROR_TASK_PROPERTY_EMPTY",
                errorDescription="body (base64 image) is required",
            )

    # Validate required fields for classification tasks
    if request.task.type in _CLASSIFICATION_TASK_TYPES:
        has_image = (
            request.task.image
            or request.task.images
            or request.task.body
            or request.task.queries
        )
        if not has_image:
            return CreateTaskResponse(
                errorId=1,
                errorCode="ERROR_TASK_PROPERTY_EMPTY",
                errorDescription="image data is required for classification tasks",
            )

    params = request.task.model_dump(exclude_none=True)
    # Thread the client key through so the solver can attribute consumption to it
    # in the ledger. Prefixed with '_' to distinguish from YesCaptcha fields.
    params["_clientKey"] = request.clientKey
    # Ownership marker that survives persistence: the raw ``_clientKey`` is
    # stripped from the stored params by ``redact_params``, so a task read back
    # from the store (``aget_task``) needs a non-reversible digest to prove
    # which caller owns it.
    params[_CLIENT_KEY_HASH_PARAM] = _client_key_hash(request.clientKey)

    # Scope the idempotency key per client so two tenants reusing the same key
    # don't coalesce onto each other's task, and fingerprint the payload so a
    # retry with the same key but different parameters is distinguishable.
    # ``payload_fingerprint`` is an optional kwarg on the newer
    # ``TaskManager.acreate_task``; fall back for an older manager.
    scoped_idempotency_key: str | None = None
    payload_fingerprint: str | None = None
    if request.idempotencyKey:
        # Hash the principal: concatenating a raw clientKey that contains ':'
        # would make the scope ambiguous ("a:b" + "c" vs "a" + "b:c"), letting
        # two tenants collide on one idempotency slot.
        principal = _client_key_hash(request.clientKey)[:16]
        scoped_idempotency_key = f"{principal}:{request.idempotencyKey}"
        payload_fingerprint = hashlib.sha256(
            json.dumps(params, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    try:
        if scoped_idempotency_key is not None:
            task_id = await task_manager.acreate_task(
                request.task.type,
                params,
                idempotency_key=scoped_idempotency_key,
                payload_fingerprint=payload_fingerprint,
            )
        else:
            task_id = await task_manager.acreate_task(request.task.type, params)
    except IdempotencyConflict:
        # Same key, different payload: replaying the old task would return a
        # result for a different request, so answer with a dedicated client
        # error instead of coalescing (ERROR_DUPLICATE_TASK_ID is the
        # YesCaptcha-family code for "this key already identifies another
        # task"). The caller should reuse the original payload or a new key.
        return CreateTaskResponse(
            errorId=1,
            errorCode="ERROR_DUPLICATE_TASK_ID",
            errorDescription=(
                "idempotencyKey was already used with a different payload; "
                "replay the original request or use a new idempotencyKey"
            ),
        )
    except TypeError:
        if scoped_idempotency_key is None:
            raise
        log.warning(
            "task_manager.acreate_task does not accept payload_fingerprint yet; "
            "retrying with the scoped idempotency key only"
        )
        task_id = await task_manager.acreate_task(
            request.task.type, params, idempotency_key=scoped_idempotency_key
        )
    except QueueFull as exc:
        log.warning("Rejecting task (queue full): %s", exc)
        return CreateTaskResponse(
            errorId=1,
            errorCode="ERROR_NO_SLOT_AVAILABLE",
            errorDescription=str(exc),
        )

    log.info("Created task %s (type=%s)", task_id, request.task.type)
    return CreateTaskResponse(errorId=0, taskId=task_id)


@router.post("/getTaskResult", response_model=GetTaskResultResponse)
async def get_task_result(
    request: GetTaskResultRequest,
) -> GetTaskResultResponse:
    if config.client_key and not _constant_time_eq(
        request.clientKey, config.client_key
    ):
        return GetTaskResultResponse(
            errorId=1,
            errorCode="ERROR_KEY_DOES_NOT_EXIST",
            errorDescription="Invalid clientKey",
        )

    task = await task_manager.aget_task(request.taskId)
    if task is None:
        return GetTaskResultResponse(
            errorId=1,
            errorCode="ERROR_NO_SUCH_CAPCHA_ID",
            errorDescription="Task not found",
        )

    # Multi-tenant ownership: in keyless mode any string is an accepted
    # clientKey, so knowing a task id must not grant access to another
    # caller's solution. Verify the owner in constant time and answer with the
    # same "no such task" error a missing id produces (a distinct mismatch
    # code would leak existence). Single-tenant mode is already gated by the
    # config-key check above.
    #
    # Tasks created from now on carry ``clientKeyHash`` (persisted, so it also
    # works for records read back from the store); a live in-memory task from
    # an older process carries ``_clientKey``; a stored record from before the
    # marker existed carries neither and is legacy — allowed, logged once.
    if not config.client_key:
        params = task.params or {}
        stored_hash = params.get(_CLIENT_KEY_HASH_PARAM)
        if isinstance(stored_hash, str):
            authorized = _constant_time_eq(
                _client_key_hash(request.clientKey), stored_hash
            )
        else:
            owner = params.get("_clientKey")
            if isinstance(owner, str):
                authorized = _constant_time_eq(request.clientKey, owner)
            else:
                _allow_legacy_ownerless_task(request.taskId)
                authorized = True
        if not authorized:
            return GetTaskResultResponse(
                errorId=1,
                errorCode="ERROR_NO_SUCH_CAPCHA_ID",
                errorDescription="Task not found",
            )

    if task.status == TaskStatus.PROCESSING:
        return GetTaskResultResponse(errorId=0, status="processing")

    if task.status == TaskStatus.READY:
        try:
            solution = SolutionObject(**(task.solution or {}))
        except (TypeError, ValidationError) as exc:
            log.error(
                "Task %s produced an invalid solution: %s", request.taskId, exc
            )
            return GetTaskResultResponse(
                errorId=1,
                errorCode="ERROR_CAPTCHA_UNSOLVABLE",
                errorDescription="Solver returned an invalid solution",
            )
        return GetTaskResultResponse(errorId=0, status="ready", solution=solution)

    return GetTaskResultResponse(
        errorId=1,
        errorCode=task.error_code or "ERROR_CAPTCHA_UNSOLVABLE",
        errorDescription=task.error_description,
    )


@router.post("/getBalance", response_model=GetBalanceResponse)
async def get_balance(request: GetBalanceRequest) -> GetBalanceResponse:
    if config.client_key and not _constant_time_eq(
        request.clientKey, config.client_key
    ):
        return GetBalanceResponse(errorId=1, balance=0)
    # Real balance semantics: starting credit minus this client's ledger spend.
    services = get_services()
    balance = config.account_balance_usd
    if services is not None:
        spent = await services.ledger.total_cost_usd(request.clientKey)
        balance = max(0.0, config.account_balance_usd - spent)
    return GetBalanceResponse(errorId=0, balance=balance)


def _authorized(client_key: str) -> bool:
    return not config.client_key or _constant_time_eq(client_key, config.client_key)


@router.get("/admin/metrics", response_model=None)
async def admin_metrics(
    request: Request, clientKey: str = ""
) -> dict[str, object] | JSONResponse:
    """Per-sitekey / per-model consumption summary from the cost ledger."""
    denied = _check_admin(request, clientKey)
    if denied is not None:
        return denied
    services = get_services()
    if services is None:
        return {"errorId": 0, "summary": {}, "note": "services not initialised"}
    return {"errorId": 0, "summary": await services.ledger.summary()}


def _redact_server(server: str) -> str:
    """Return ``scheme://host:port`` with any ``user:pass@`` userinfo dropped.

    Both pool backends already keep credentials out of ``server`` (Playwright
    wants them separate), but an admin payload must not depend on every backend
    honouring that, so the routes layer normalises it defensively.
    """
    if "@" not in server and "://" not in server:
        return server
    candidate = server if "://" in server else f"//{server}"
    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
        if not host:
            return server.rpartition("@")[2]
        if ":" in host and not host.startswith("["):  # bare IPv6
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        scheme = f"{parts.scheme}://" if parts.scheme else ""
        return f"{scheme}{host}{port}"
    except ValueError:  # malformed port / bracket: strip userinfo best-effort
        return server.rpartition("@")[2]


def _redact_proxy(entry: object) -> dict[str, object]:
    """Strip credential + sticky-session fields from one admin proxy row."""
    if isinstance(entry, dict):
        row: dict[str, object] = dict(entry)
    else:
        row = {"value": entry}
    for secret in (
        "username",
        "password",
        "proxyLogin",
        "proxyPassword",
        "sticky_session_id",
    ):
        row.pop(secret, None)
    server = row.get("server")
    if isinstance(server, str) and server:
        row["server"] = _redact_server(server)
    return row


@router.get("/admin/proxies", response_model=None)
async def admin_proxies(
    request: Request, clientKey: str = ""
) -> dict[str, object] | JSONResponse:
    """Proxy-pool health / consumption snapshot (credentials redacted)."""
    denied = _check_admin(request, clientKey)
    if denied is not None:
        return denied
    services = get_services()
    if services is None:
        return {"errorId": 0, "proxies": [], "sessions": []}
    pool = getattr(services, "proxy_pool", None)
    # ``Any`` because a pool backend's snapshot shape is duck-typed here (and
    # the call is offloaded to a worker thread below).
    snapshot: Any = getattr(pool, "snapshot", None) if pool is not None else None
    proxies: list[dict[str, object]] = []
    if callable(snapshot):
        try:
            # The pool's snapshot performs blocking Redis I/O in some backends;
            # run it in a worker thread so an admin request can't stall the
            # event loop (and every in-flight solve with it).
            rows: Any = await asyncio.to_thread(snapshot)
            proxies = [_redact_proxy(row) for row in rows]
        except Exception:  # noqa: BLE001 - admin snapshot is best-effort
            log.warning("Proxy pool snapshot failed", exc_info=True)
            proxies = []
    sessions = (
        services.session_pool.snapshot() if services.session_pool is not None else []
    )
    return {
        "errorId": 0,
        "proxies": proxies,
        "sessions": sessions,
    }


def _degraded_subsystems(request: Request) -> list[str]:
    """Subsystems the lifespan recorded on ``app.state.degraded``.

    Empty before startup / when nothing degraded. Guarded because routes must
    stay importable and callable against an app whose lifespan never ran
    (e.g. a bare ``TestClient`` or a pre-lifespan probe).
    """
    state = getattr(request.app, "state", None)
    degraded = getattr(state, "degraded", None) if state is not None else None
    if not degraded:
        return []
    if isinstance(degraded, str):
        return [degraded]
    try:
        return [str(name) for name in degraded]
    except TypeError:
        return [str(degraded)]


def _browser_ready(manager: object | None) -> bool:
    """Guarded ``manager.is_ready()``; False for missing/partial managers."""
    if manager is None:
        return False
    probe = getattr(manager, "is_ready", None)
    if probe is None:
        return False
    try:
        return bool(probe() if callable(probe) else probe)
    except Exception:  # noqa: BLE001 - health probes must never raise
        log.warning("Browser readiness probe raised", exc_info=True)
        return False


def _model_health(services: object | None) -> dict[str, object]:
    """Best-effort ``services.model_pool.health_snapshot()`` (never raises)."""
    pool = getattr(services, "model_pool", None) if services is not None else None
    snapshot = getattr(pool, "health_snapshot", None) if pool is not None else None
    if not callable(snapshot):
        return {}
    try:
        result = snapshot()
    except Exception:  # noqa: BLE001 - health probes must never raise
        log.warning("Model health snapshot raised", exc_info=True)
        return {}
    return result if isinstance(result, dict) else {}


@router.get("/api/v1/health")
async def health(request: Request) -> dict[str, object]:
    # Surface the actual (post-fallback) browser runtime alongside the
    # requested one so a silent degrade to stock Chromium is observable.
    runtime_requested = config.browser_runtime
    runtime_actual = config.browser_runtime
    effective_runtime: object = None
    driver: object = None
    services = get_services()
    manager = getattr(services, "browser_manager", None) if services else None
    if manager is not None:
        runtime_requested = getattr(manager, "requested_runtime", runtime_requested)
        runtime_actual = getattr(manager, "runtime", runtime_actual)
        effective_runtime = getattr(manager, "effective_runtime", None)
        driver = getattr(manager, "driver", None)
    degraded = _degraded_subsystems(request)
    # Additive fields only: existing clients keep ``browser_runtime`` /
    # ``browser_runtime_requested``; operators get the post-fallback
    # effective runtime + driver package, a liveness flag, the degraded set
    # from the lifespan, and per-model-backend circuit health.
    return {
        "status": "ok",
        "supported_task_types": task_manager.supported_types(),
        "browser_headless": config.browser_headless,
        "browser_runtime": runtime_actual,
        "browser_runtime_requested": runtime_requested,
        "browser_runtime_effective": effective_runtime,
        "browser_runtime_degraded": runtime_actual != runtime_requested,
        "browser_driver": driver,
        "browser_ready": _browser_ready(manager),
        "degraded": bool(degraded),
        "degraded_subsystems": degraded,
        "model_backends": _model_health(services),
        "cloud_model": config.cloud_model,
        "local_model": config.local_model,
    }


# ── readiness ───────────────────────────────────────────────

# A readiness probe must answer quickly even when Redis is unreachable (a
# blackholed host would otherwise hang the probe past the orchestrator's
# timeout, which looks identical to a dead process).
_REDIS_PING_TIMEOUT_SECONDS = 1.5


async def _redis_ping(services: object | None) -> bool:
    """Ping the configured Redis once, reusing a live backend client if any.

    Cheap by design: the ledger / proxy-pool / accounting backends already hold
    an async client when Redis is configured, so the common path is one PING on
    an existing connection. When only a fallback (in-memory) backend exists the
    configured URL is pinged on a short-lived client. Never raises.
    """
    client = None
    created = False
    for owner_name in ("ledger", "proxy_pool", "accounting"):
        owner = getattr(services, owner_name, None) if services is not None else None
        if owner is None:
            continue
        client = getattr(owner, "_redis", None)
        if client is not None:
            break
    if client is None:
        redis_url = getattr(config, "redis_url", None)
        if not redis_url:
            return False
        try:
            import redis.asyncio as aioredis  # noqa: WPS433 (optional dep)
        except ImportError:
            log.warning("REDIS_URL is set but the 'redis' package is not installed")
            return False
        try:
            client = aioredis.from_url(str(redis_url), decode_responses=True)
        except Exception:  # noqa: BLE001 - malformed URL -> not ready
            return False
        created = True
    try:
        result = client.ping()
        if hasattr(result, "__await__"):  # redis.asyncio; fakes may be sync
            result = await asyncio.wait_for(
                result, timeout=_REDIS_PING_TIMEOUT_SECONDS
            )
        return bool(result)
    except Exception:  # noqa: BLE001 - any ping failure means not ready
        return False
    finally:
        if created:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


@router.get("/ready", response_model=None)
async def ready(request: Request) -> JSONResponse:
    """Kubernetes-style readiness probe (cheap; no admin/pool scans).

    Ready (200) when every *required* subsystem is up:
      * browser — only when at least one browser task type is registered, so
        a vision-only deployment passes without a browser;
      * Redis — only when ``REDIS_URL`` is configured (one PING);
      * otherwise liveness alone is sufficient.
    Not ready answers 503 with the per-check state. ``app.state.degraded`` is
    reported for visibility but does not fail the probe: a degraded optional
    plane (e.g. in-memory ledger fallback) still serves traffic.
    """
    services = get_services()
    checks: dict[str, object] = {}
    is_ready = True

    supported = set(task_manager.supported_types())
    browser_required = bool(supported & _BROWSER_TASK_TYPES)
    manager = getattr(services, "browser_manager", None) if services else None
    if browser_required:
        browser_ok = _browser_ready(manager)
        checks["browser"] = "ok" if browser_ok else "unavailable"
        is_ready = is_ready and browser_ok
    else:
        checks["browser"] = "disabled"

    if getattr(config, "redis_url", None):
        redis_ok = await _redis_ping(services)
        checks["redis"] = "ok" if redis_ok else "unavailable"
        is_ready = is_ready and redis_ok
    else:
        checks["redis"] = "not_configured"

    degraded = _degraded_subsystems(request)
    if degraded:
        checks["degraded"] = degraded

    body: dict[str, object] = {
        "status": "ready" if is_ready else "not_ready",
        "ready": is_ready,
        "checks": checks,
    }
    return JSONResponse(status_code=200 if is_ready else 503, content=body)


# ── reportCorrect / reportIncorrect (WP6) ───────────────────


async def _report_outcome(
    request: ReportTaskRequest, *, correct: bool
) -> ReportTaskResponse:
    """Shared handler for /reportCorrect and /reportIncorrect.

    Feeds the real (token-actually-accepted) outcome back into the proxy-pool
    sitekey stats (the *real-outcome* bucket, separate from the token-obtained
    bucket the solver writes), the success accounting, and the session
    reputation. All downstream calls are non-fatal: a missing proxy / session
    / record returns a clean error or ``errorId=0``, never a 500.

    Security — task ownership: a report may only mutate the shared
    proxy-pool / accounting / session state for a task the caller owns.
    After the record is fetched, ``rec.client_key`` is checked against
    ``request.clientKey``; a mismatch returns
    ``ERROR_NO_SUCH_CAPCHA_ID`` (NOT a mismatch code, so task existence
    isn't leaked to non-owners). When ``rec.client_key is None``
    (anonymous / legacy ledger record) the report is allowed — there's no
    owner to check against (backward compat).

    Concurrency — atomic claim: a single atomic claim via
    ``ledger.try_claim_reported(taskId)`` runs BEFORE any side effect and
    replaces the old read-check-then-write race (pre-check ``rec.reported``
    → side effects → post-mark). Exactly one concurrent report for a given
    task wins the claim and runs the side effects; every other report
    (concurrent or sequential retry) sees the claim already taken and
    returns ``errorId=0`` without touching proxy-pool / accounting /
    session state. The in-memory ledger claims under its lock; the Redis
    ledger claims via ``SET {prefix}:reported:{task_id} 1 NX EX <ttl>``.
    """
    if not _authorized(request.clientKey):
        return ReportTaskResponse(
            errorId=1,
            errorCode="ERROR_KEY_DOES_NOT_EXIST",
            errorDescription="Invalid clientKey",
        )

    services = get_services()
    if services is None:
        return ReportTaskResponse(
            errorId=1,
            errorCode="ERROR_NO_SUCH_CAPCHA_ID",
            errorDescription="No solve record for this task",
        )

    rec = await services.ledger.get_by_task_id(request.taskId)
    if rec is None:
        return ReportTaskResponse(
            errorId=1,
            errorCode="ERROR_NO_SUCH_CAPCHA_ID",
            errorDescription="No solve record for this task",
        )

    # Security: only the task's owner may report on it. A mismatch returns
    # the same "no such task" error as a missing record so the endpoint
    # doesn't leak task existence to non-owners. Anonymous / legacy records
    # (client_key is None) have no owner to check against and are allowed.
    if rec.client_key is not None and not _constant_time_eq(
        rec.client_key, request.clientKey
    ):
        return ReportTaskResponse(
            errorId=1,
            errorCode="ERROR_NO_SUCH_CAPCHA_ID",
            errorDescription="No solve record for this task",
        )

    # Atomic idempotency claim: flips reported False→True atomically BEFORE
    # any side effect. Exactly one caller per task wins (returns True);
    # everyone else — concurrent racers or sequential retries — loses
    # (returns False) and short-circuits with errorId=0 without re-running
    # the downstream proxy-pool / accounting / session-pool calls.
    claimed = await services.ledger.try_claim_reported(request.taskId)
    if not claimed:
        return ReportTaskResponse(errorId=0)

    # Fan the real outcome out to proxy health, the per-sitekey real bucket,
    # success accounting and session reputation. The stored ``SolveRecord``
    # supplies the egress/session identity; the shared module owns the fan-out
    # (the solver's inline siteverify path calls the same function), so this
    # route stays a thin auth + claim + delegate adapter.
    await record_real_outcome(
        services, rec, rec.sitekey, success=correct, model=rec.model
    )

    # No post-side-effect mark: the atomic claim above already set
    # reported=True (in-memory under the lock, or via the Redis SET-NX
    # key). A retry will see the claim taken and short-circuit.
    return ReportTaskResponse(errorId=0)


@router.post("/reportCorrect", response_model=ReportTaskResponse)
async def report_correct(request: ReportTaskRequest) -> ReportTaskResponse:
    """Caller reports the task's token was accepted downstream."""
    return await _report_outcome(request, correct=True)


@router.post("/reportIncorrect", response_model=ReportTaskResponse)
async def report_incorrect(request: ReportTaskRequest) -> ReportTaskResponse:
    """Caller reports the task's token was rejected downstream."""
    return await _report_outcome(request, correct=False)


# ── framework error envelopes ───────────────────────────────


def register_error_handlers(app: FastAPI) -> None:
    """Install YesCaptcha-style JSON envelopes for framework-level errors.

    YesCaptcha clients expect HTTP 200 with
    ``{errorId, errorCode, errorDescription}``; FastAPI's default 422
    (validation) and 500 (unhandled) shapes break those clients. Only
    ``RequestValidationError`` and bare ``Exception`` are intercepted —
    ``HTTPException`` keeps FastAPI's own handling so 404/405 semantics (and
    any explicitly raised status) are preserved.

    Called from ``src/main.py`` at app construction time.
    """

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        description = "Invalid request payload"
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            message = str(first.get("msg", "invalid value"))
            description = f"{location}: {message}" if location else message
        log.warning(
            "Rejected invalid request to %s: %s", request.url.path, description
        )
        return JSONResponse(
            status_code=200,
            content={
                "errorId": 1,
                "errorCode": "ERROR_BAD_REQUEST",
                "errorDescription": description,
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        log.error(
            "Unhandled error while serving %s: %s",
            request.url.path,
            exc,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=200,
            content={
                "errorId": 1,
                "errorCode": "ERROR_INTERNAL",
                "errorDescription": "Internal server error",
            },
        )
