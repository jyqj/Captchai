"""Focused regression tests for the Phase-0 (Wave A) hardening changes.

Covers the behaviours that were introduced together and are easy to regress:

* payload-bound idempotency (same key + same payload coalesces, a different
  payload is rejected, keys are scoped per clientKey),
* ``getTaskResult`` ownership in keyless / multi-tenant mode,
* loopback-only ``/admin/*`` access when no key is configured,
* loud config validation naming the offending env var,
* the ``SessionPool`` exactly-once permit guard under checkout cancellation,
* vision parse failures surfacing as ``ERROR_CAPTCHA_UNSOLVABLE`` rather than a
  confident empty-grid answer,
* the in-process rate limiter's YesCaptcha error envelope.

No network and no browser: model / browser layers are faked, and coroutines are
driven with ``asyncio.run`` (this project has no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

import src.services.task_manager as tm  # noqa: E402
from src.assets.session_pool import SessionPool  # noqa: E402
from src.models.task import (  # noqa: E402
    CreateTaskRequest,
    GetTaskResultRequest,
    TaskObject,
)
from src.parsing.vision import (  # noqa: E402
    VisionParseError,
    VisionRequest,
    VisionRouter,
)

_TASK_TYPE = "RecaptchaV3TaskProxyless"


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _routes():
    """Current ``src.api.routes`` module.

    ``tests/test_api.py`` reloads this module, so resolve it at call time
    instead of binding the module / ``router`` / ``task_manager`` attributes at
    collection time.
    """
    return importlib.import_module("src.api.routes")


def _manager_config(**overrides):
    """Config fields ``TaskManager.configure`` reads, with a roomy queue."""
    base = dict(
        browser_concurrency=2,
        browser_proxyless_concurrency=2,
        browser_proxied_concurrency=2,
        browser_pool_proxy_concurrency=2,
        vision_concurrency=2,
        queue_max_size=20,
        solve_timeout=5,
        redis_url=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _InstaSolver:
    """Minimal browser-category solver: succeeds immediately."""

    async def solve(self, params):
        return {"gRecaptchaResponse": "tok"}


def _install_fresh_manager(monkeypatch, routes):
    """Point the routes module at a fresh TaskManager + keyless config.

    The global manager is shared process state; using a per-test instance keeps
    these tests independent of test ordering and of other modules' fixtures.
    """
    mgr = tm.TaskManager()
    mgr.configure(_manager_config())
    mgr.register_solver(_TASK_TYPE, _InstaSolver(), tm.TaskCategory.BROWSER)
    monkeypatch.setattr(routes, "task_manager", mgr)
    # Keyless local mode: no CLIENT_KEY, no ADMIN_KEY.
    monkeypatch.setattr(
        routes, "config", SimpleNamespace(client_key=None, admin_key=None)
    )
    return mgr


def _v3_request(client_key: str, *, website_key: str = "site-key", key: str | None = None):
    return CreateTaskRequest(
        clientKey=client_key,
        idempotencyKey=key,
        # Pydantic validates/coerces the dict into TaskObject at runtime.
        task=cast(
            TaskObject,
            {
                "type": _TASK_TYPE,
                "websiteURL": "https://example.com",
                "websiteKey": website_key,
            },
        ),
    )


# ---------------------------------------------------------------------------
# a. Idempotency: payload fingerprint + per-client scoping
# ---------------------------------------------------------------------------


def test_idempotency_payload_binding_and_client_scoping(monkeypatch) -> None:
    routes = _routes()

    async def run() -> None:
        mgr = _install_fresh_manager(monkeypatch, routes)
        try:
            first = await routes.create_task(_v3_request("alice", key="idem-1"))
            assert first.errorId == 0 and first.taskId

            # Same clientKey + same payload: coalesce onto the first task.
            retry = await routes.create_task(_v3_request("alice", key="idem-1"))
            assert retry.errorId == 0
            assert retry.taskId == first.taskId

            # Same key, DIFFERENT payload: must not replay the first task's
            # result — the API maps IdempotencyConflict to a client error.
            conflict = await routes.create_task(
                _v3_request("alice", website_key="other-site", key="idem-1")
            )
            assert conflict.errorId == 1
            # Dedicated conflict code: the key already identifies a DIFFERENT
            # task, so the caller must replay the original payload or use a new
            # key rather than receiving another request's result.
            assert conflict.errorCode == "ERROR_DUPLICATE_TASK_ID"
            assert conflict.taskId is None

            # Different clientKey reusing the same raw key: the route scopes
            # the key by caller, so bob gets his own task and does not collide
            # with (or observe) alice's task id.
            other_client = await routes.create_task(_v3_request("bob", key="idem-1"))
            assert other_client.errorId == 0
            assert other_client.taskId
            assert other_client.taskId != first.taskId
        finally:
            await mgr.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# b. getTaskResult ownership in keyless mode
# ---------------------------------------------------------------------------


def test_get_task_result_rejects_non_owner_in_keyless_mode(monkeypatch) -> None:
    routes = _routes()

    async def run() -> None:
        mgr = _install_fresh_manager(monkeypatch, routes)
        try:
            created = await routes.create_task(_v3_request("alice"))
            assert created.errorId == 0 and created.taskId
            task_id = created.taskId

            # Let the (instant) solver finish so a leak would reveal a solution.
            task = None
            for _ in range(100):
                task = mgr.get_task(task_id)
                if task is not None and task.status != tm.TaskStatus.PROCESSING:
                    break
                await asyncio.sleep(0.01)
            assert task is not None
            assert task.status == tm.TaskStatus.READY

            # Caller B knows the id but does not own the task: the route answers
            # exactly as if the task did not exist (no existence / solution leak).
            stolen = await routes.get_task_result(
                GetTaskResultRequest(clientKey="bob", taskId=task_id)
            )
            assert stolen.errorId == 1
            assert stolen.errorCode == "ERROR_NO_SUCH_CAPCHA_ID"
            assert stolen.solution is None

            # The owner still gets the result.
            owned = await routes.get_task_result(
                GetTaskResultRequest(clientKey="alice", taskId=task_id)
            )
            assert owned.errorId == 0
            assert owned.status == "ready"
            assert owned.solution is not None
            assert owned.solution.gRecaptchaResponse == "tok"
        finally:
            await mgr.shutdown()

    asyncio.run(run())


def test_get_task_result_ownership_survives_store_across_workers(monkeypatch) -> None:
    """Ownership must be enforced from the shared store, not process memory.

    Simulates two workers over one persistent store. Worker A admits and
    solves the task (its in-memory map is authoritative); worker B has an empty
    map and resolves the task only through ``aget_task`` -> store. The caller
    identity therefore has to be persisted at admission — as a non-reversible
    digest, since the raw ``_clientKey`` is stripped by ``redact_params`` —
    otherwise worker B cannot tell the rightful owner from an id-guessing
    stranger (and, failing open, would hand either caller the solution).
    """
    from src.orchestration.store import InMemoryTaskStore

    routes = _routes()

    async def run() -> None:
        store = InMemoryTaskStore(ttl_seconds=60)
        worker_a = tm.TaskManager()
        worker_a.configure(_manager_config(), store=store)
        worker_a.register_solver(
            _TASK_TYPE, _InstaSolver(), tm.TaskCategory.BROWSER
        )
        worker_b = tm.TaskManager()
        worker_b.configure(_manager_config(), store=store)
        # Worker B has no solver and no local tasks: it is a separate process
        # that only shares the store.

        monkeypatch.setattr(
            routes, "config", SimpleNamespace(client_key=None, admin_key=None)
        )
        try:
            # Worker A admits + solves; the record is mirrored into the store.
            monkeypatch.setattr(routes, "task_manager", worker_a)
            created = await routes.create_task(_v3_request("alice"))
            assert created.errorId == 0 and created.taskId
            task_id = created.taskId

            # Wait for the runner's final mirror, not just the in-memory status.
            record = None
            for _ in range(200):
                record = await store.get(task_id)
                if record is not None and record.get("status") == "ready":
                    break
                await asyncio.sleep(0.01)
            assert record is not None
            assert record.get("status") == "ready"
            # The store must not have persisted the raw credential, only the
            # ownership digest.
            assert "_clientKey" not in (record.get("params") or {})

            # Worker B sees the task only via the store.
            monkeypatch.setattr(routes, "task_manager", worker_b)
            assert worker_b.get_task(task_id) is None
            remote = await worker_b.aget_task(task_id)
            assert remote is not None and remote.status == tm.TaskStatus.READY

            # A stranger who knows the id gets the same answer as for a
            # nonexistent id; the owner still gets the solution.
            stolen = await routes.get_task_result(
                GetTaskResultRequest(clientKey="bob", taskId=task_id)
            )
            assert stolen.errorId == 1
            assert stolen.errorCode == "ERROR_NO_SUCH_CAPCHA_ID"
            assert stolen.solution is None

            owned = await routes.get_task_result(
                GetTaskResultRequest(clientKey="alice", taskId=task_id)
            )
            assert owned.errorId == 0
            assert owned.status == "ready"
            assert owned.solution is not None
            assert owned.solution.gRecaptchaResponse == "tok"
        finally:
            # Both managers share one store; shutting worker A down flushes its
            # pending writes and closes it once.
            await worker_a.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# c. /admin/* loopback-only when no key is configured
# ---------------------------------------------------------------------------


def test_admin_metrics_is_loopback_only_when_keyless(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    routes = _routes()
    monkeypatch.setattr(
        routes, "config", SimpleNamespace(client_key=None, admin_key=None)
    )
    app = FastAPI()
    app.include_router(routes.router)

    # Non-loopback client: denied with the YesCaptcha-style envelope.
    # The minimal local fastapi stub omits Starlette's client= kwarg.
    remote = TestClient(app, client=("203.0.113.7", 43210))  # type: ignore[reportCallIssue]
    denied = remote.get("/admin/metrics")
    assert denied.status_code == 403
    body = denied.json()
    assert body["errorId"] == 1
    assert body["errorCode"] == "ERROR_KEY_DOES_NOT_EXIST"

    # Loopback client: allowed (services may be uninitialised in tests, so only
    # the envelope is asserted).
    local = TestClient(app, client=("127.0.0.1", 43210))  # type: ignore[reportCallIssue]
    allowed = local.get("/admin/metrics")
    assert allowed.status_code == 200
    assert allowed.json()["errorId"] == 0


# ---------------------------------------------------------------------------
# d. Config validation names the malformed variable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_name", "bad_value"),
    [
        ("SERVER_PORT", "abc"),
        ("ENTERPRISE_REQUIRE_RESIDENTIAL", "ture"),
        ("BUDGET_GLOBAL_CAP_USD", "oops"),
        ("BROWSER_CONCURRENCY", "0"),
    ],
)
def test_load_config_rejects_malformed_env_naming_variable(
    monkeypatch, env_name: str, bad_value: str
) -> None:
    config_mod = importlib.import_module("src.core.config")
    monkeypatch.setenv(env_name, bad_value)
    with pytest.raises(ValueError, match=env_name):
        config_mod.load_config()


# ---------------------------------------------------------------------------
# e. SessionPool: cancellation releases the permit exactly once
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal Playwright-context stand-in for the session pool."""

    def __init__(self) -> None:
        self.pages: list = []
        self.browser = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_session_pool_cancelled_checkout_releases_permit_exactly_once() -> None:
    async def run() -> None:
        entered_factory = asyncio.Event()
        unblock_factory = asyncio.Event()
        created: list = []

        async def factory(fingerprint, proxy):
            entered_factory.set()
            await unblock_factory.wait()
            context = _FakeContext()
            created.append(context)
            return context, fingerprint.user_agent

        pool = SessionPool(factory, size=1, max_solves=8)
        checkout = asyncio.create_task(pool.checkout(key="proxyless"))
        await entered_factory.wait()

        # Permit acquired, factory in flight (no session registered yet).
        assert pool._slots._value == 0
        assert pool._creating == 1

        checkout.cancel()
        with pytest.raises(asyncio.CancelledError):
            await checkout

        # Cancelled before handoff: the permit is returned EXACTLY once — value
        # 1 (0 would mean leaked, 2 would mean double-released) — and no
        # half-built session / bookkeeping is left behind.
        assert pool._slots._value == 1
        assert pool._creating == 0
        assert pool._permit_holders == set()
        assert pool._in_use == {}
        assert pool.snapshot() == []
        assert created == []

        await pool.close_all()

    asyncio.run(run())


def test_session_pool_cancel_while_waiting_does_not_release_unheld_permit() -> None:
    async def run() -> None:
        async def factory(fingerprint, proxy):
            return _FakeContext(), fingerprint.user_agent

        pool = SessionPool(factory, size=1, max_solves=8)
        held = await pool.checkout(key="proxyless")
        assert pool._slots._value == 0  # the only permit is in use

        # This checkout parks on ``acquire`` and never receives a permit.
        waiter = asyncio.create_task(pool.checkout(key="proxyless"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # A checkout cancelled before acquisition must not release a permit it
        # never held; the in-use permit is still exactly the one held above.
        assert pool._slots._value == 0

        await pool.release(held, success=True)
        assert pool._slots._value == 1

        await pool.close_all()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# f. Vision parse failure -> ERROR_CAPTCHA_UNSOLVABLE, never an empty grid
# ---------------------------------------------------------------------------


class _ScriptedCompletions:
    def __init__(self, content: str, recorder: list) -> None:
        self._content = content
        self._recorder = recorder

    async def create(self, **kwargs):
        self._recorder.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )


class _ScriptedClient:
    def __init__(self, base_url: str, content: str, recorder: list) -> None:
        self.base_url = base_url
        self.chat = SimpleNamespace(
            completions=_ScriptedCompletions(content, recorder)
        )


def _scripted_pool(config, content: str):
    from src.assets.model_pool import ModelPool

    recorder: list = []

    def factory(base_url: str, api_key: str):
        return _ScriptedClient(base_url, content, recorder)

    return ModelPool(config, client_factory=factory), recorder


def _vision_config():
    return SimpleNamespace(
        cloud_base_url="http://cloud",
        cloud_api_key="cloud-key",
        cloud_model="gpt-cloud",
        local_base_url="http://local",
        local_api_key="local-key",
        local_model="qwen-local",
        vision_cloud_enabled=False,
        vision_vote_samples=1,
        vision_confidence_threshold=0.6,
        vision_tier2_detail="high",
        vision_stitch_grid=False,
        vision_inline_escalate=False,
        vision_trust_self_confidence=False,
        model_connection_fallback=False,
        captcha_timeout=5,
    )


def test_unparseable_vision_response_fails_instead_of_empty_grid() -> None:
    async def run() -> None:
        config = _vision_config()
        # No brackets / JSON / indices list: unparseable for a grid shape.
        pool, recorder = _scripted_pool(config, "I cannot read that image.")
        router = VisionRouter(pool, config)
        req = VisionRequest(
            prompt="select all buses",
            images=[b"\x89PNGfake"],
            task_tier=1,
            shape="grid_select",
        )

        # The router must raise, not return a confident empty-grid answer.
        with pytest.raises(VisionParseError):
            await router.classify(req)
        assert recorder, "the model must actually have been called"

        # End-to-end through the task manager: the failure is classified as
        # ERROR_CAPTCHA_UNSOLVABLE (the API's retryable solve-failure code).
        mgr = tm.TaskManager()
        mgr.configure(_manager_config())

        class _RouterSolver:
            async def solve(self, params):
                result = await router.classify(req)
                return {"objects": result.indices}

        mgr.register_solver("VisionGrid", _RouterSolver(), tm.TaskCategory.VISION)
        try:
            task_id = mgr.create_task("VisionGrid", {})
            task = None
            for _ in range(100):
                task = mgr.get_task(task_id)
                if task is not None and task.status != tm.TaskStatus.PROCESSING:
                    break
                await asyncio.sleep(0.01)
            assert task is not None
            assert task.status == tm.TaskStatus.FAILED
            assert task.error_code == "ERROR_CAPTCHA_UNSOLVABLE"
            assert task.solution is None
        finally:
            await mgr.shutdown()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# g. Rate limiting returns the rate-limit error envelope
# ---------------------------------------------------------------------------


def test_rate_limit_middleware_returns_error_envelope() -> None:
    from fastapi.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from src.main import _RateLimitMiddleware

    async def ok(request):
        return JSONResponse({"errorId": 0})

    app = Starlette(
        routes=[
            Route("/createTask", ok, methods=["POST"]),
            Route("/api/v1/health", ok, methods=["GET"]),
        ]
    )
    app.add_middleware(_RateLimitMiddleware, rpm=2)
    client = TestClient(app)

    # Bucket capacity 2: the first two calls pass, the third is throttled.
    assert client.post("/createTask").json()["errorId"] == 0
    assert client.post("/createTask").json()["errorId"] == 0
    throttled = client.post("/createTask")
    body = throttled.json()
    assert throttled.status_code == 200  # YesCaptcha envelope, not HTTP 429
    assert body["errorId"] == 1
    assert body["errorCode"] == "ERROR_RATE_LIMITED"
    # The minimal local fastapi stub omits Response.headers (real httpx.Response).
    assert "Retry-After" in throttled.headers  # type: ignore[reportAttributeAccessIssue]

    # Health probes are exempt even with the bucket empty.
    for _ in range(5):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["errorId"] == 0


def test_rate_limit_rpm_resolution_and_app_wiring(monkeypatch) -> None:
    main_mod = importlib.import_module("src.main")

    # Config field wins when present (forward-compatible with Config gaining
    # ``rate_limit_rpm``); otherwise the RATE_LIMIT_RPM env is honoured.
    monkeypatch.setattr(main_mod, "config", SimpleNamespace(rate_limit_rpm=7))
    assert main_mod._rate_limit_rpm() == 7
    monkeypatch.setattr(main_mod, "config", SimpleNamespace())
    monkeypatch.setenv("RATE_LIMIT_RPM", "13")
    assert main_mod._rate_limit_rpm() == 13

    # The app constructed in src.main actually installs the limiter.
    assert any(
        getattr(middleware, "cls", None) is main_mod._RateLimitMiddleware
        for middleware in main_mod.app.user_middleware
    )
