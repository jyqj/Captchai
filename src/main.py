"""FastAPI application with Playwright lifecycle management."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .api.routes import router
from .core.config import config
from .core.services import SolverServices, set_services
from .core.task_types import Provider, types_for_provider
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

# Type lists derive from the single registry in ``core.task_types`` so solver
# registration here, request validation in ``api/routes.py``, and the test
# roster can't drift apart.
_RECAPTCHA_V3_TYPES = types_for_provider(Provider.RECAPTCHA_V3)
_RECAPTCHA_V2_TYPES = types_for_provider(Provider.RECAPTCHA_V2)
_HCAPTCHA_TYPES = types_for_provider(Provider.HCAPTCHA)
_TURNSTILE_TYPES = types_for_provider(Provider.TURNSTILE)
_CLASSIFICATION_TYPES = types_for_provider(Provider.CLASSIFICATION)
_IMAGE_TEXT_TYPES = types_for_provider(Provider.IMAGE_TO_TEXT)


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
    # requests can't starve the browser solvers. Both share the model-call seam
    # (via ``services``) so their spend is budget-gated and reaches the ledger.
    recognizer = CaptchaRecognizer(config, services=services)
    for task_type in _IMAGE_TEXT_TYPES:
        task_manager.register_solver(task_type, recognizer, TaskCategory.VISION)
    log.info("Registered image captcha recognizer for types: %s", _IMAGE_TEXT_TYPES)

    classifier = ClassificationSolver(config, services=services)
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
    title="CaptchAI Service",
    version="3.0.0",
    description="YesCaptcha-compatible captcha solving service for flow2api.",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "service": "captchai",
        "version": "3.0.0",
        "endpoints": {
            "createTask": "/createTask",
            "getTaskResult": "/getTaskResult",
            "getBalance": "/getBalance",
            "health": "/api/v1/health",
        },
        "supported_task_types": task_manager.supported_types(),
    }
