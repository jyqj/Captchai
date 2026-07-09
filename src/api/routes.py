"""YesCaptcha / AntiCaptcha compatible HTTP routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter

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
from ..services.task_manager import QueueFull, TaskStatus, task_manager

log = logging.getLogger(__name__)

router = APIRouter()

# Required-field validation buckets derive from the single ``core.task_types``
# registry so a newly registered task type can't silently skip validation by
# being absent from a hand-maintained set here.
_BROWSER_TASK_TYPES = names_for_validation(ValidationKind.BROWSER)
_IMAGE_TASK_TYPES = names_for_validation(ValidationKind.IMAGE)
_CLASSIFICATION_TASK_TYPES = names_for_validation(ValidationKind.CLASSIFICATION)


def _check_client_key(client_key: str) -> CreateTaskResponse | None:
    """Return an error response if the client key is invalid, else None."""
    if config.client_key and client_key != config.client_key:
        return CreateTaskResponse(
            errorId=1,
            errorCode="ERROR_KEY_DOES_NOT_EXIST",
            errorDescription="Invalid clientKey",
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
    try:
        task_id = await task_manager.acreate_task(
            request.task.type, params, idempotency_key=request.idempotencyKey
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
    if config.client_key and request.clientKey != config.client_key:
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

    if task.status == TaskStatus.PROCESSING:
        return GetTaskResultResponse(errorId=0, status="processing")

    if task.status == TaskStatus.READY:
        return GetTaskResultResponse(
            errorId=0,
            status="ready",
            solution=SolutionObject(**(task.solution or {})),
        )

    return GetTaskResultResponse(
        errorId=1,
        errorCode=task.error_code or "ERROR_CAPTCHA_UNSOLVABLE",
        errorDescription=task.error_description,
    )


@router.post("/getBalance", response_model=GetBalanceResponse)
async def get_balance(request: GetBalanceRequest) -> GetBalanceResponse:
    if config.client_key and request.clientKey != config.client_key:
        return GetBalanceResponse(errorId=1, balance=0)
    # Real balance semantics: starting credit minus this client's ledger spend.
    services = get_services()
    balance = config.account_balance_usd
    if services is not None:
        spent = await services.ledger.total_cost_usd(request.clientKey)
        balance = max(0.0, config.account_balance_usd - spent)
    return GetBalanceResponse(errorId=0, balance=balance)


def _authorized(client_key: str) -> bool:
    return not config.client_key or client_key == config.client_key


@router.get("/admin/metrics")
async def admin_metrics(clientKey: str = "") -> dict[str, object]:
    """Per-sitekey / per-model consumption summary from the cost ledger."""
    if not _authorized(clientKey):
        return {"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST"}
    services = get_services()
    if services is None:
        return {"errorId": 0, "summary": {}, "note": "services not initialised"}
    return {"errorId": 0, "summary": await services.ledger.summary()}


@router.get("/admin/proxies")
async def admin_proxies(clientKey: str = "") -> dict[str, object]:
    """Proxy-pool health / consumption snapshot."""
    if not _authorized(clientKey):
        return {"errorId": 1, "errorCode": "ERROR_KEY_DOES_NOT_EXIST"}
    services = get_services()
    if services is None:
        return {"errorId": 0, "proxies": [], "sessions": []}
    sessions = (
        services.session_pool.snapshot() if services.session_pool is not None else []
    )
    return {
        "errorId": 0,
        "proxies": services.proxy_pool.snapshot(),
        "sessions": sessions,
    }


@router.get("/api/v1/health")
async def health() -> dict[str, object]:
    # Surface the actual (post-fallback) browser runtime alongside the
    # requested one so a silent degrade to stock Chromium is observable.
    runtime_requested = config.browser_runtime
    runtime_actual = config.browser_runtime
    services = get_services()
    manager = getattr(services, "browser_manager", None) if services else None
    if manager is not None:
        runtime_requested = getattr(manager, "requested_runtime", runtime_requested)
        runtime_actual = getattr(manager, "runtime", runtime_actual)
    return {
        "status": "ok",
        "supported_task_types": task_manager.supported_types(),
        "browser_headless": config.browser_headless,
        "browser_runtime": runtime_actual,
        "browser_runtime_requested": runtime_requested,
        "browser_runtime_degraded": runtime_actual != runtime_requested,
        "cloud_model": config.cloud_model,
        "local_model": config.local_model,
    }


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
    if rec.client_key is not None and rec.client_key != request.clientKey:
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

    # Proxy-pool per-sitekey real-outcome ranking — only when both ids are
    # present. Writes into the *real* bucket (``report_sitekey_real``) so the
    # solver's token-obtained bucket (``report_sitekey``) stays undiluted and
    # checkout ranking can prefer the real signal when present.
    if rec.proxy_id and rec.sitekey:
        try:
            await services.proxy_pool.report_sitekey_real(
                rec.proxy_id, rec.sitekey, success=correct
            )
        except Exception:  # noqa: BLE001 - non-fatal: report must not 500
            log.debug(
                "proxy_pool.report_sitekey_real failed for task %s",
                request.taskId,
                exc_info=True,
            )

    # Real outcome also drives proxy *health*, not just per-sitekey ranking:
    # a pool proxy whose tokens are rejected downstream (correct=False) accrues
    # a consecutive-fail streak and cools down, even though it "obtained" a
    # token during the solve. Without this, a proxy that reliably mints tokens
    # that Stripe/hCaptcha-enterprise then reject would keep a 100% health
    # score and be reselected indefinitely. Only pool proxies (rec.proxy_id
    # set) are governed here; caller-supplied task proxies are the caller's
    # responsibility.
    if rec.proxy_id:
        try:
            await services.proxy_pool.report(rec.proxy_id, success=correct)
        except Exception:  # noqa: BLE001 - non-fatal: report must not 500
            log.debug(
                "proxy_pool.report (real outcome) failed for task %s",
                request.taskId,
                exc_info=True,
            )

    # Real-outcome accounting (always recorded, even with an empty sitekey).
    try:
        await services.accounting.record_real_outcome(
            rec.sitekey,
            success=correct,
            proxy_kind=rec.proxy_kind,
            model=rec.model,
        )
    except Exception:  # noqa: BLE001 - non-fatal
        log.debug(
            "accounting.record_real_outcome failed for task %s",
            request.taskId,
            exc_info=True,
        )

    # Session reputation nudge — no-op if the session was already retired.
    if rec.session_id and services.session_pool is not None:
        try:
            await services.session_pool.report_outcome(
                rec.session_id, success=correct
            )
        except Exception:  # noqa: BLE001 - non-fatal
            log.debug(
                "session_pool.report_outcome failed for task %s",
                request.taskId,
                exc_info=True,
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
