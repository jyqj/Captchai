"""FastAPI application with Playwright lifecycle management."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .api.routes import router
from .core.config import config
from .core.services import SolverServices, set_services
from .services.browser import BrowserManager
from .services.classification import ClassificationSolver
from .services.hcaptcha import HCaptchaSolver
from .services.recognition import CaptchaRecognizer
from .services.recaptcha_v2 import RecaptchaV2Solver
from .services.recaptcha_v3 import RecaptchaV3Solver
from .services.task_manager import TaskCategory, task_manager
from .services.turnstile import TurnstileSolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

_RECAPTCHA_V3_TYPES = [
    "RecaptchaV3TaskProxyless",
    "RecaptchaV3TaskProxylessM1",
    "RecaptchaV3TaskProxylessM1S7",
    "RecaptchaV3TaskProxylessM1S9",
    "RecaptchaV3EnterpriseTask",
    "RecaptchaV3EnterpriseTaskM1",
]

_RECAPTCHA_V2_TYPES = [
    "NoCaptchaTaskProxyless",
    "RecaptchaV2TaskProxyless",
    "RecaptchaV2EnterpriseTaskProxyless",
]

_HCAPTCHA_TYPES = [
    "HCaptchaTaskProxyless",
]

_TURNSTILE_TYPES = [
    "TurnstileTaskProxyless",
    "TurnstileTaskProxylessM1",
]

_CLASSIFICATION_TYPES = [
    "HCaptchaClassification",
    "ReCaptchaV2Classification",
    "FunCaptchaClassification",
    "AwsClassification",
]

_IMAGE_TEXT_TYPES = [
    "ImageToTextTask",
    "ImageToTextTaskMuggle",
    "ImageToTextTaskM1",
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── startup ──
    # One Chromium process shared by every browser-based solver. Each solve
    # still gets an isolated context (with its own proxy/UA), but we avoid
    # launching four separate browsers.
    browser = BrowserManager(config)
    await browser.start()

    # Shared asset / consumption / vision layers, injected into the solvers.
    services = SolverServices(config)
    services.attach_browser(browser)
    await services.prewarm_sessions()
    set_services(services)

    # Configure the task manager after services exist so the scheduler can peek
    # the proxy pool for best-effort pool-proxy routing on egress="auto" tasks.
    task_manager.configure(config, proxy_pool=services.proxy_pool)

    v3_solver = RecaptchaV3Solver(config, manager=browser, services=services)
    for task_type in _RECAPTCHA_V3_TYPES:
        task_manager.register_solver(task_type, v3_solver, TaskCategory.BROWSER)
    log.info("Registered reCAPTCHA v3 solver for types: %s", _RECAPTCHA_V3_TYPES)

    v2_solver = RecaptchaV2Solver(config, manager=browser, services=services)
    for task_type in _RECAPTCHA_V2_TYPES:
        task_manager.register_solver(task_type, v2_solver, TaskCategory.BROWSER)
    log.info("Registered reCAPTCHA v2 solver for types: %s", _RECAPTCHA_V2_TYPES)

    hcaptcha_solver = HCaptchaSolver(config, manager=browser, services=services)
    for task_type in _HCAPTCHA_TYPES:
        task_manager.register_solver(task_type, hcaptcha_solver, TaskCategory.BROWSER)
    log.info("Registered hCaptcha solver for types: %s", _HCAPTCHA_TYPES)

    turnstile_solver = TurnstileSolver(config, manager=browser, services=services)
    for task_type in _TURNSTILE_TYPES:
        task_manager.register_solver(task_type, turnstile_solver, TaskCategory.BROWSER)
    log.info("Registered Turnstile solver for types: %s", _TURNSTILE_TYPES)

    # Pure-vision tasks draw from a separate concurrency pool so a burst of image
    # requests can't starve the browser solvers.
    recognizer = CaptchaRecognizer(config)
    for task_type in _IMAGE_TEXT_TYPES:
        task_manager.register_solver(task_type, recognizer, TaskCategory.VISION)
    log.info("Registered image captcha recognizer for types: %s", _IMAGE_TEXT_TYPES)

    classifier = ClassificationSolver(config)
    for task_type in _CLASSIFICATION_TYPES:
        task_manager.register_solver(task_type, classifier, TaskCategory.VISION)
    log.info("Registered classification solver for types: %s", _CLASSIFICATION_TYPES)

    yield
    # ── shutdown ──
    await task_manager.shutdown()
    await services.close()
    set_services(None)
    await browser.stop()


app = FastAPI(
    title="Captcha Solver Service",
    version="3.0.0",
    description="YesCaptcha-compatible captcha solving service for flow2api.",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "service": "captcha-solver",
        "version": "3.0.0",
        "endpoints": {
            "createTask": "/createTask",
            "getTaskResult": "/getTaskResult",
            "getBalance": "/getBalance",
            "health": "/api/v1/health",
        },
        "supported_task_types": task_manager.supported_types(),
    }
