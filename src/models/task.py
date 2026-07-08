"""YesCaptcha / AntiCaptcha compatible API models."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── createTask ──────────────────────────────────────────────

class TaskObject(BaseModel):
    type: str
    websiteURL: str | None = None
    websiteKey: str | None = None
    pageAction: str | None = None
    minScore: float | None = None
    isInvisible: bool | None = None

    # ── Cloudflare Turnstile advanced params ──
    # Widgets configured with an action / cData / chlPageData must receive the
    # exact same values, otherwise the generated token is rejected server-side.
    action: str | None = None
    cData: str | None = None
    chlPageData: str | None = None

    # ── hCaptcha enterprise params ──
    # `rqdata` (a.k.a. enterprise "data") binds the token to a challenge nonce.
    rqdata: str | None = None
    enterprisePayload: dict[str, object] | None = None

    # ── Fingerprint / session binding ──
    # Callers may force a specific User-Agent so the token they submit downstream
    # matches the one used during solving. If omitted the solver picks a default
    # and echoes it back in solution.userAgent.
    userAgent: str | None = None

    # ── Proxy (YesCaptcha-style fields) ──
    # CF / reCAPTCHA tokens are IP-bound; solving through the same egress IP the
    # caller will submit from is required for the token to validate.
    proxyType: str | None = None
    proxyAddress: str | None = None
    proxyPort: int | None = None
    proxyLogin: str | None = None
    proxyPassword: str | None = None
    # Convenience single-string form: "http://user:pass@host:port"
    proxy: str | None = None

    # Egress intent: "auto" (default, current behavior), "proxyless" (force server
    # egress, refuse task proxy), "pool" (force server-side proxy pool, fail if
    # empty), "task" (require caller-supplied proxy, fail if none). Used by the
    # scheduler to pick the right concurrency pool and by the solver to enforce
    # the caller's egress requirement.
    egress: str | None = None

    # Image captcha / classification fields
    body: str | None = None
    image: str | None = None
    images: list[str] | None = None
    question: str | None = None
    queries: list[str] | str | None = None
    project_name: str | None = None


class CreateTaskRequest(BaseModel):
    clientKey: str
    task: TaskObject
    # Optional idempotency key: retries carrying the same key coalesce onto the
    # same task instead of launching a second solve (no double-spend of proxies /
    # model calls).
    idempotencyKey: str | None = None


class CreateTaskResponse(BaseModel):
    errorId: int = 0
    taskId: str | None = None
    errorCode: str | None = None
    errorDescription: str | None = None


# ── getTaskResult ───────────────────────────────────────────

class GetTaskResultRequest(BaseModel):
    clientKey: str
    taskId: str


class SolutionObject(BaseModel):
    gRecaptchaResponse: str | None = None
    text: str | None = None
    token: str | None = None
    objects: list[int] | None = None
    answer: bool | list[int] | None = None
    userAgent: str | None = None
    # WP3: fingerprint geo used for the solve, surfaced so callers can align
    # their submit context (Accept-Language header / TZ) with the solve
    # context. Optional — omitted when the solver didn't pin a fingerprint.
    timezoneId: str | None = None
    acceptLanguage: str | None = None


class GetTaskResultResponse(BaseModel):
    errorId: int = 0
    status: str | None = None
    solution: SolutionObject | None = None
    errorCode: str | None = None
    errorDescription: str | None = None


# ── getBalance ──────────────────────────────────────────────

class GetBalanceRequest(BaseModel):
    clientKey: str


class GetBalanceResponse(BaseModel):
    errorId: int = 0
    balance: float = 99999.0


# ── reportCorrect / reportIncorrect ─────────────────────────

class ReportTaskRequest(BaseModel):
    clientKey: str
    taskId: str


class ReportTaskResponse(BaseModel):
    errorId: int = 0
    errorCode: str | None = None
    errorDescription: str | None = None
