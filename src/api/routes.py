"""YesCaptcha / AntiCaptcha compatible HTTP routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from ..core.config import config
from ..models.task import (
    CreateTaskRequest,
    CreateTaskResponse,
    GetBalanceRequest,
    GetBalanceResponse,
    GetTaskResultRequest,
    GetTaskResultResponse,
    SolutionObject,
)
from ..core.services import get_services
from ..services.task_manager import QueueFull, TaskStatus, task_manager

log = logging.getLogger(__name__)

router = APIRouter()

_BROWSER_TASK_TYPES = {
    "RecaptchaV3TaskProxyless",
    "RecaptchaV3TaskProxylessM1",
    "RecaptchaV3TaskProxylessM1S7",
    "RecaptchaV3TaskProxylessM1S9",
    "RecaptchaV3EnterpriseTask",
    "RecaptchaV3EnterpriseTaskM1",
    "NoCaptchaTaskProxyless",
    "RecaptchaV2TaskProxyless",
    "RecaptchaV2EnterpriseTaskProxyless",
    "HCaptchaTaskProxyless",
    "TurnstileTaskProxyless",
    "TurnstileTaskProxylessM1",
}

_IMAGE_TASK_TYPES = {
    "ImageToTextTask",
    "ImageToTextTaskMuggle",
    "ImageToTextTaskM1",
}

_CLASSIFICATION_TASK_TYPES = {
    "HCaptchaClassification",
    "ReCaptchaV2Classification",
    "FunCaptchaClassification",
    "AwsClassification",
}


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
        task_id = task_manager.create_task(
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
    return {
        "status": "ok",
        "supported_task_types": task_manager.supported_types(),
        "browser_headless": config.browser_headless,
        "cloud_model": config.cloud_model,
        "local_model": config.local_model,
    }
