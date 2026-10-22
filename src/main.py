"""FastAPI application with Playwright lifecycle management."""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .api import routes as routes_module
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


# ── Rate limiting (in-process, per client IP) ───────────────────────────

# Infrastructure probes must never be throttled: a 200-with-error envelope on
# /health would take the box out of a load balancer's rotation.
_RATE_LIMIT_EXEMPT_PATHS = frozenset({"/health", "/api/v1/health", "/ready"})

# Bucket-map housekeeping: sweep idle buckets every N checks (amortised) and
# unconditionally once the map exceeds the hard size cap. Idle means "no
# request for longer than a bucket needs to refill from empty" with a wide
# margin (10 minutes) so a legitimate bursty client keeps its state.
_RATE_LIMIT_PRUNE_EVERY = 256
_RATE_LIMIT_MAX_BUCKETS = 10_000
_RATE_LIMIT_IDLE_SECONDS = 600.0


class _TokenBucketRateLimiter:
    """Thread-safe per-client token bucket (no external dependency).

    Each client IP starts with a full bucket of ``rpm`` tokens, refilled
    continuously at ``rpm / 60`` tokens per second. ``rpm <= 0`` disables the
    limiter (every check passes).
    """

    def __init__(self, rpm: int) -> None:
        self._rpm = max(0, int(rpm))
        self._capacity = float(self._rpm)
        self._refill_per_second = self._rpm / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self._checks_since_prune = 0

    @property
    def enabled(self) -> bool:
        return self._rpm > 0

    def check(self, client: str) -> tuple[bool, float]:
        """Consume one token for ``client``; returns ``(allowed, retry_after_s)``."""
        if not self.enabled:
            return True, 0.0
        now = time.monotonic()
        with self._lock:
            tokens, updated = self._buckets.get(client, (self._capacity, now))
            tokens = min(
                self._capacity,
                tokens + (now - updated) * self._refill_per_second,
            )
            if tokens >= 1.0:
                self._buckets[client] = (tokens - 1.0, now)
                self._prune_stale(now)
                return True, 0.0
            self._buckets[client] = (tokens, now)
            wait = (
                (1.0 - tokens) / self._refill_per_second
                if self._refill_per_second
                else 60.0
            )
            self._prune_stale(now)
            return False, wait

    def _prune_stale(self, now: float) -> None:
        """Amortised sweep of idle buckets (caller holds ``self._lock``).

        Pruning only on denied requests lets a stream of one-off clients that
        never trip the limit grow the map without bound. Sweeping every
        ``_RATE_LIMIT_PRUNE_EVERY`` checks on BOTH the allow and deny paths
        (plus a hard size trigger) bounds memory under allow-path churn at
        O(1) amortised cost per request.
        """
        self._checks_since_prune += 1
        if (
            self._checks_since_prune < _RATE_LIMIT_PRUNE_EVERY
            and len(self._buckets) <= _RATE_LIMIT_MAX_BUCKETS
        ):
            return
        self._checks_since_prune = 0
        cutoff = now - _RATE_LIMIT_IDLE_SECONDS
        self._buckets = {
            client: state
            for client, state in self._buckets.items()
            if state[1] >= cutoff
        }


def _rate_limit_rpm() -> int:
    """Requests/minute budget per client IP (0 disables the limiter).

    ``Config.rate_limit_rpm`` (env ``RATE_LIMIT_RPM``) is read through
    ``getattr`` because ``core/config.py`` is owned by a separate change: the
    env var is honoured directly until that field lands. The default mirrors
    the intended config default (60).
    """
    value: Any = getattr(config, "rate_limit_rpm", None)
    if value is None:
        value = os.environ.get("RATE_LIMIT_RPM", "60")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        log.warning("Invalid RATE_LIMIT_RPM %r; defaulting to 60", value)
        return 60


def _rate_limited_response(retry_after: float) -> JSONResponse:
    """YesCaptcha-style error envelope: HTTP 200 with ``errorId=1``.

    The API contract (matching ``CreateTaskResponse`` in ``models/task.py``)
    signals errors in the JSON body, not via the transport status, so callers
    see a normal 200 and back off on ``ERROR_RATE_LIMITED``.
    """
    return JSONResponse(
        status_code=200,
        content={
            "errorId": 1,
            "errorCode": "ERROR_RATE_LIMITED",
            "errorDescription": (
                "Rate limit exceeded; retry after {:.1f}s".format(max(0.0, retry_after))
            ),
        },
        headers={"Retry-After": str(max(1, int(retry_after) + 1))},
    )


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """Lightweight in-process token bucket keyed by client IP.

    Process-local by design: no Redis / no new dependency. With multiple
    workers each enforces its own share of the budget, which is sufficient as
    a first-line abuse guard.
    """

    def __init__(self, app: Any, *, rpm: int = 60) -> None:
        super().__init__(app)
        self._limiter = _TokenBucketRateLimiter(rpm)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in _RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = self._limiter.check(client_ip)
        if not allowed:
            log.warning(
                "Rate limit exceeded: client=%s path=%s", client_ip, request.url.path
            )
            return _rate_limited_response(retry_after)
        return await call_next(request)


# ── Lifespan ────────────────────────────────────────────────────────────


def _is_loopback_host(host: str | None) -> bool:
    """True when ``host`` is a loopback bind address (or the name localhost)."""
    if not host:
        return False
    value = host.strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _register_solver(
    name: str,
    solver_types: list[str],
    factory: Callable[[], Any],
    category: TaskCategory,
    registered: list[str],
) -> None:
    """Instantiate one solver and register its task types; degrade on failure.

    One broken solver must not take the whole service down: its task types are
    skipped (``createTask`` then answers ``ERROR_TASK_NOT_SUPPORTED``) while
    every other provider keeps serving.
    """
    try:
        solver = factory()
    except Exception:  # noqa: BLE001 - one bad solver is not fatal
        log.exception(
            "Failed to initialise %s solver; task types %s are DEGRADED",
            name,
            solver_types,
        )
        return
    for task_type in solver_types:
        task_manager.register_solver(task_type, solver, category)
    registered.extend(solver_types)
    log.info("Registered %s solver for types: %s", name, solver_types)


def _install_routes_error_handlers(app: FastAPI) -> None:
    """Wire exception handlers exported by ``api.routes``, when present.

    ``api/routes.py`` owns the handlers (validation / unhandled errors become
    YesCaptcha-style JSON envelopes); the registration hook is looked up
    defensively so this module stays importable before that wiring lands.
    """
    for hook_name in (
        "register_exception_handlers",
        "register_error_handlers",
        "install_exception_handlers",
    ):
        hook = getattr(routes_module, hook_name, None)
        if callable(hook):
            try:
                hook(app)
            except Exception:  # noqa: BLE001 - handler wiring is not fatal
                log.exception("Failed to install routes error handlers via %s", hook_name)
            return


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    browser: BrowserManager | None = None
    services: SolverServices | None = None
    browser_ready = False
    degraded: list[str] = []
    registered: list[str] = []

    try:
        # ── startup (partial failures degrade; only a total loss is fatal) ──
        # Fail closed: with no CLIENT_KEY and ALLOW_ANONYMOUS unset the API
        # accepts every caller anonymously, which is only tolerable on a
        # loopback bind. A publicly reachable keyless bind is a misconfig, so
        # refuse to start rather than silently serving an open service.
        if (
            config.client_key is None
            and not getattr(config, "allow_anonymous", False)
            and not _is_loopback_host(config.server_host)
        ):
            raise RuntimeError(
                "Refusing to start an unauthenticated public service: CLIENT_KEY "
                "is unset, ALLOW_ANONYMOUS is false, and SERVER_HOST="
                f"{config.server_host!r} is not a loopback address. Set CLIENT_KEY, "
                "or set ALLOW_ANONYMOUS=true to explicitly accept anonymous "
                "callers (ADMIN_KEY alone does not authenticate API callers)."
            )

        if config.client_key is None:
            log.warning(
                "AUTH DISABLED: running in local anonymous mode; "
                "/admin/* restricted to loopback"
            )

        # Readiness surface: per-model-backend health (circuit state/counters).
        # Filled in once the model pool exists; an empty dict means the model
        # plane did not start, so a health route never has to special-case a
        # missing attribute.
        app.state.model_pool_health = {}

        # One Chromium process shared by every browser-based solver. Each solve
        # still gets an isolated context (with its own proxy/UA), but we avoid
        # launching four separate browsers.
        try:
            browser = BrowserManager(config)
            await browser.start()
            browser_ready = True
        except Exception:  # noqa: BLE001 - vision tasks can still serve
            degraded.append("browser")
            # Keep the (possibly partially started) manager so shutdown can
            # release the Playwright driver that DID start.
            log.exception(
                "Browser startup failed; browser task types are DEGRADED. "
                "Vision-only task types remain available."
            )

        # Shared asset / consumption / vision layers, injected into the solvers.
        # ``SolverServices`` degrades individual planes internally (in-memory
        # fallbacks) and records failures in ``.degraded``.
        try:
            services = SolverServices(config)
        except Exception:  # noqa: BLE001 - never leak; run without shared planes
            log.exception(
                "Shared services initialisation failed; running without metering / "
                "proxy pool / warm sessions (DEGRADED)"
            )
            services = None
            degraded.append("services")
        if services is not None:
            try:
                services.attach_browser(browser)
                await services.prewarm_sessions()
            except Exception:  # noqa: BLE001 - warm-up is best-effort
                log.exception(
                    "Service warm-up failed; continuing with the subsystems that "
                    "started (DEGRADED)"
                )
                degraded.append("services")
            set_services(services)
            # Best-effort model-plane health for /health and admin routes. The
            # snapshot method is looked up defensively (a partially built or
            # alternate services object may not expose one).
            health_snapshot = getattr(
                getattr(services, "model_pool", None), "health_snapshot", None
            )
            if callable(health_snapshot):
                try:
                    app.state.model_pool_health = health_snapshot()
                except Exception:  # noqa: BLE001 - readiness is not fatal
                    log.exception("Failed to build the model-pool health snapshot")
            degraded.extend(sorted(services.degraded))
            if services.degraded:
                log.warning(
                    "Running with degraded subsystems: %s",
                    ", ".join(sorted(services.degraded)),
                )

        # Configure the task manager after services exist so the scheduler can
        # peek the proxy pool for best-effort pool-proxy routing on egress="auto"
        # tasks. Both pool backends answer ``has_available()`` from an in-process
        # snapshot (the Redis backend reconciles it in a background task), so the
        # enqueue path performs no blocking redis I/O on the event loop.
        try:
            task_manager.configure(
                config,
                proxy_pool=services.proxy_pool if services is not None else None,
            )
        except Exception:  # noqa: BLE001 - in-process defaults still work
            log.exception(
                "Task-manager configuration failed; using in-process defaults "
                "(task persistence DEGRADED)"
            )
            degraded.append("task_store")

        if browser_ready:
            _register_solver(
                "reCAPTCHA v3",
                _RECAPTCHA_V3_TYPES,
                lambda: RecaptchaV3Solver(config, manager=browser, services=services),
                TaskCategory.BROWSER,
                registered,
            )
            _register_solver(
                "reCAPTCHA v2",
                _RECAPTCHA_V2_TYPES,
                lambda: RecaptchaV2Solver(config, manager=browser, services=services),
                TaskCategory.BROWSER,
                registered,
            )
            _register_solver(
                "hCaptcha",
                _HCAPTCHA_TYPES,
                lambda: HCaptchaSolver(config, manager=browser, services=services),
                TaskCategory.BROWSER,
                registered,
            )
            _register_solver(
                "Turnstile",
                _TURNSTILE_TYPES,
                lambda: TurnstileSolver(config, manager=browser, services=services),
                TaskCategory.BROWSER,
                registered,
            )
        else:
            log.warning(
                "Browser solvers not registered (browser DEGRADED): %s",
                _RECAPTCHA_V3_TYPES
                + _RECAPTCHA_V2_TYPES
                + _HCAPTCHA_TYPES
                + _TURNSTILE_TYPES,
            )

        # Pure-vision tasks draw from a separate concurrency pool so a burst of
        # image requests can't starve the browser solvers. Both share the
        # model-call seam (via ``services``) so their spend is budget-gated and
        # reaches the ledger; without wired services they build their own pool.
        _register_solver(
            "image captcha recognizer",
            _IMAGE_TEXT_TYPES,
            lambda: CaptchaRecognizer(config, services=services),
            TaskCategory.VISION,
            registered,
        )
        _register_solver(
            "classification",
            _CLASSIFICATION_TYPES,
            lambda: ClassificationSolver(config, services=services),
            TaskCategory.VISION,
            registered,
        )

        if not registered:
            # No browser path and no vision path: there is nothing this
            # process could ever solve, so this one IS fatal.
            raise RuntimeError(
                "No solver could be initialised (browser and model planes both "
                "failed); refusing to start a service that can solve nothing"
            )

        # Operator-visible degradation, readable from routes without reaching
        # into module globals (cross-file follow-up: surface this from /health).
        app.state.degraded = sorted(set(degraded))
        app.state.solver_ready = True
        yield
    finally:
        # ── shutdown ──
        # Close in reverse startup order; each step is guarded so one failing
        # subsystem can't leak the rest.
        try:
            await task_manager.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("Task manager shutdown failed")
        if services is not None:
            try:
                await services.close()
            except Exception:  # noqa: BLE001
                log.exception("Service shutdown failed")
        set_services(None)
        if browser is not None:
            try:
                await browser.stop()
            except Exception:  # noqa: BLE001
                log.exception("Browser shutdown failed")


app = FastAPI(
    title="CaptchAI Service",
    version="3.0.0",
    description="YesCaptcha-compatible captcha solving service for flow2api.",
    lifespan=lifespan,
)

# Per-client-IP token bucket; 0 disables. Exempts health probes.
app.add_middleware(_RateLimitMiddleware, rpm=_rate_limit_rpm())

app.include_router(router)

# Exception handlers exported by ``api/routes`` (validation / unhandled errors
# answered with YesCaptcha-style JSON envelopes).
_install_routes_error_handlers(app)


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
