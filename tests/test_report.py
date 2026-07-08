"""Tests for WP6 — reportCorrect / reportIncorrect feedback interface.

Covers the real-outcome feedback loop: callers report whether a token was
actually accepted downstream, and that signal feeds back into proxy-pool
sitekey stats, success accounting, and session reputation.

Endpoint tests use ``httpx.AsyncClient`` with ``ASGITransport`` so the route
handler and the test-side service verification share one event loop (the
asyncio locks inside the ledger / accounting / session pool bind to the
loop on first use, so crossing loops would raise). Unit tests for the
ledger / accounting / session pool follow the repo's existing
``asyncio.run(...)`` pattern.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    _ = sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from src.assets.fingerprint import generate_fingerprint  # noqa: E402
from src.assets.proxy_pool import ProxyAsset, ProxyPool  # noqa: E402
from src.assets.session_pool import BrowserSession, SessionPool  # noqa: E402
from src.consumption.accounting import SuccessAccounting  # noqa: E402
from src.consumption.ledger import CostLedger, SolveRecord  # noqa: E402
from src.core.services import set_services  # noqa: E402
from src.models.task import ReportTaskRequest, ReportTaskResponse  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _FakeCtx:
    """Stand-in for a Playwright BrowserContext (close is a no-op)."""

    async def close(self) -> None:
        pass


def _fake_factory():
    async def factory(fingerprint, proxy):  # noqa: ARG001
        return _FakeCtx(), fingerprint.user_agent

    return factory


def _build_app(*, client_key: str | None = None):
    """Reload config + routes with fresh env vars and return a FastAPI app.

    Reloading ``config`` is necessary because ``Config`` is a frozen dataclass
    built from env vars at import time — the only way to change
    ``client_key`` for the auth-failure test is to set ``CLIENT_KEY`` and
    reload. Routes are reloaded afterwards so they pick up the new config
    object (they capture ``config`` at import time via ``from ..core.config
    import config``).
    """
    os.environ.pop("CLIENT_KEY", None)
    os.environ.setdefault("CAPTCHA_BASE_URL", "https://example.com/v1")
    os.environ.setdefault("CAPTCHA_API_KEY", "test-key")
    os.environ.setdefault("CAPTCHA_MODEL", "gpt-5.4")
    os.environ.setdefault("CAPTCHA_MULTIMODAL_MODEL", "qwen3.5-2b")
    os.environ.setdefault("BROWSER_HEADLESS", "true")
    if client_key is not None:
        os.environ["CLIENT_KEY"] = client_key

    config_mod = importlib.import_module("src.core.config")
    importlib.reload(config_mod)

    routes_mod = importlib.import_module("src.api.routes")
    importlib.reload(routes_mod)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(routes_mod.router)
    return app


def _make_services():
    """Build a minimal services namespace for the report endpoints."""
    ledger = CostLedger()
    accounting = SuccessAccounting()
    proxy_pool = ProxyPool()
    session_pool = SessionPool(_fake_factory(), size=2, max_solves=8)
    services = SimpleNamespace(
        ledger=ledger,
        accounting=accounting,
        proxy_pool=proxy_pool,
        session_pool=session_pool,
    )
    return services


def _make_record(task_id: str, **overrides) -> SolveRecord:
    defaults = dict(
        task_id=task_id,
        sitekey="sk-1",
        task_type="RecaptchaV2TaskProxyless",
        proxy_id="px-1",
        proxy_kind="datacenter",
        session_id="sess-1",
        model="gpt-4o",
        outcome="ready",
        client_key="client-1",
    )
    defaults.update(overrides)
    return SolveRecord(**defaults)


def _make_idle_session(session_id: str, *, reputation: float = 1.0) -> BrowserSession:
    session = BrowserSession(
        id=session_id,
        context=_FakeCtx(),
        fingerprint=generate_fingerprint(),
        proxy=None,
        user_agent="UA",
        created_at=time.monotonic(),
        warm=True,
    )
    session.reputation = reputation
    return session


# --------------------------------------------------------------------------- #
# Endpoint: /reportCorrect happy path
# --------------------------------------------------------------------------- #


def test_report_correct_happy_path() -> None:
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        proxy = ProxyAsset(id="px-1", server="http://px:1")
        services.proxy_pool.add(proxy)
        session = _make_idle_session("sess-1", reputation=0.5)
        services.session_pool._idle["proxyless"] = [session]
        await services.ledger.record(_make_record("task-1"))

        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-1"},
                )
            assert resp.status_code == 200
            body = resp.json()
            assert body["errorId"] == 0
            assert body["errorCode"] is None

            # proxy_pool.report_sitekey_real called with success=True — writes
            # into the *real* bucket, NOT the token-obtained bucket.
            assert proxy.real_sitekey_stats["sk-1"][0] == 1  # success count
            assert proxy.real_sitekey_stats["sk-1"][1] == 0  # fail count
            # Token-obtained bucket untouched by the report endpoint.
            assert proxy.sitekey_stats == {}

            # accounting.record_real_outcome called with success=True
            rate = await services.accounting.real_success_rate(
                "sk-1", proxy_kind="datacenter", model="gpt-4o"
            )
            assert rate == 1.0

            # session_pool.report_outcome called with success=True
            assert abs(session.reputation - 0.55) < 1e-9  # 0.5 + 0.05
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_incorrect_happy_path() -> None:
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        proxy = ProxyAsset(id="px-1", server="http://px:1")
        services.proxy_pool.add(proxy)
        session = _make_idle_session("sess-1", reputation=0.8)
        services.session_pool._idle["proxyless"] = [session]
        await services.ledger.record(_make_record("task-2"))

        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/reportIncorrect",
                    json={"clientKey": "client-1", "taskId": "task-2"},
                )
            assert resp.status_code == 200
            body = resp.json()
            assert body["errorId"] == 0

            # proxy_pool.report_sitekey_real called with success=False — writes
            # into the *real* bucket, NOT the token-obtained bucket.
            assert proxy.real_sitekey_stats["sk-1"][0] == 0  # success count
            assert proxy.real_sitekey_stats["sk-1"][1] == 1  # fail count
            # Token-obtained bucket untouched by the report endpoint.
            assert proxy.sitekey_stats == {}

            # accounting.record_real_outcome called with success=False
            rate = await services.accounting.real_success_rate(
                "sk-1", proxy_kind="datacenter", model="gpt-4o"
            )
            assert rate == 0.0

            # session_pool.report_outcome called with success=False
            assert abs(session.reputation - 0.4) < 1e-9  # 0.8 - 0.4
        finally:
            set_services(None)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Endpoint: error cases
# --------------------------------------------------------------------------- #


def test_report_missing_task_returns_no_such_capcha() -> None:
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "any", "taskId": "nonexistent"},
                )
            body = resp.json()
            assert body["errorId"] == 1
            assert body["errorCode"] == "ERROR_NO_SUCH_CAPCHA_ID"
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_bad_client_key_returns_key_does_not_exist() -> None:
    async def run() -> None:
        app = _build_app(client_key="secret")
        services = _make_services()
        # Record owned by "secret" so the second (auth-passing) request's
        # clientKey matches the record's owner and the ownership check passes.
        await services.ledger.record(_make_record("task-3", client_key="secret"))
        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Wrong clientKey -> auth failure.
                resp = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "wrong", "taskId": "task-3"},
                )
                body = resp.json()
                assert body["errorId"] == 1
                assert body["errorCode"] == "ERROR_KEY_DOES_NOT_EXIST"

                # Correct clientKey -> success.
                resp2 = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "secret", "taskId": "task-3"},
                )
                body2 = resp2.json()
                assert body2["errorId"] == 0
        finally:
            set_services(None)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Endpoint: task ownership (security finding 1)
# --------------------------------------------------------------------------- #


def test_report_wrong_client_key_returns_no_such_capcha() -> None:
    """Security: a report whose clientKey doesn't match the record's owner is
    rejected with ERROR_NO_SUCH_CAPCHA_ID (NOT a mismatch code, so task
    existence isn't leaked to non-owners). No downstream side effects run,
    and the claim is NOT taken — a subsequent owner's report still succeeds.
    """
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        proxy = ProxyAsset(id="px-1", server="http://px:1")
        services.proxy_pool.add(proxy)
        session = _make_idle_session("sess-1", reputation=0.5)
        services.session_pool._idle["proxyless"] = [session]
        # Record owned by client-1.
        await services.ledger.record(
            _make_record("task-owner", client_key="client-1")
        )

        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Wrong owner → ERROR_NO_SUCH_CAPCHA_ID (not a mismatch code).
                resp = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "other", "taskId": "task-owner"},
                )
                body = resp.json()
                assert body["errorId"] == 1
                assert body["errorCode"] == "ERROR_NO_SUCH_CAPCHA_ID"

                # Side effects did NOT run.
                assert proxy.real_sitekey_stats == {}
                rate = await services.accounting.real_success_rate(
                    "sk-1", proxy_kind="datacenter", model="gpt-4o"
                )
                assert rate == 1.0  # optimistic default, never recorded
                assert abs(session.reputation - 0.5) < 1e-9  # unchanged

                # Claim was NOT taken: a subsequent owner's report still
                # succeeds and runs side effects.
                resp2 = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-owner"},
                )
                assert resp2.json()["errorId"] == 0
                assert proxy.real_sitekey_stats["sk-1"][0] == 1
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_matching_client_key_runs_side_effects() -> None:
    """Security: a report whose clientKey matches the record's owner succeeds
    with errorId=0 and the downstream side effects run exactly once."""
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        proxy = ProxyAsset(id="px-1", server="http://px:1")
        services.proxy_pool.add(proxy)
        session = _make_idle_session("sess-1", reputation=0.5)
        services.session_pool._idle["proxyless"] = [session]
        await services.ledger.record(
            _make_record("task-match", client_key="client-1")
        )

        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-match"},
                )
                assert resp.json()["errorId"] == 0
                # Side effects ran.
                assert proxy.real_sitekey_stats["sk-1"][0] == 1
                rate = await services.accounting.real_success_rate(
                    "sk-1", proxy_kind="datacenter", model="gpt-4o"
                )
                assert rate == 1.0
                assert abs(session.reputation - 0.55) < 1e-9
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_anonymous_record_allows_any_client_key() -> None:
    """Backward compat: a record with client_key=None (anonymous / legacy)
    has no owner to check against, so any clientKey is accepted and the
    report runs side effects with errorId=0."""
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        proxy = ProxyAsset(id="px-1", server="http://px:1")
        services.proxy_pool.add(proxy)
        session = _make_idle_session("sess-1", reputation=0.5)
        services.session_pool._idle["proxyless"] = [session]
        # Anonymous / legacy record (no owner).
        await services.ledger.record(
            _make_record("task-anon", client_key=None)
        )

        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "anybody", "taskId": "task-anon"},
                )
                assert resp.json()["errorId"] == 0
                # Side effects ran.
                assert proxy.real_sitekey_stats["sk-1"][0] == 1
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_with_no_proxy_or_session_still_succeeds() -> None:
    """A record with no proxy_id / session_id → no proxy/session calls, errorId=0."""
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        # Record with no proxy_id and no session_id.
        await services.ledger.record(
            _make_record("task-4", proxy_id=None, session_id=None)
        )
        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-4"},
                )
            body = resp.json()
            assert body["errorId"] == 0

            # No proxy was added to the pool, so no sitekey stats anywhere.
            for p in services.proxy_pool._proxies.values():
                assert p.sitekey_stats == {}
                assert p.real_sitekey_stats == {}

            # Accounting still recorded the real outcome (sitekey is present).
            rate = await services.accounting.real_success_rate(
                "sk-1", proxy_kind="datacenter", model="gpt-4o"
            )
            assert rate == 1.0

            # No session in the pool, so report_outcome was a no-op.
            snap = services.session_pool.snapshot()
            assert snap == []
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_services_not_initialised_returns_no_such_capcha() -> None:
    """When services is None (not started), report returns a clean error."""
    async def run() -> None:
        app = _build_app()
        set_services(None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/reportCorrect",
                json={"clientKey": "any", "taskId": "task-5"},
            )
            body = resp.json()
            assert body["errorId"] == 1
            assert body["errorCode"] == "ERROR_NO_SUCH_CAPCHA_ID"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Endpoint: idempotent /reportCorrect (client retries must not double-count)
# --------------------------------------------------------------------------- #


def test_report_correct_idempotent_on_retry() -> None:
    """A second /reportCorrect for the same taskId returns errorId=0 but does
    NOT re-call report_sitekey_real / record_real_outcome / report_outcome.

    Uses the real downstream services (ProxyPool / SuccessAccounting /
    SessionPool) and asserts the call counts via the observable state: the
    real-sitekey counter, the accounting real_attempts, and the session
    reputation are each nudged exactly once across two identical reports.
    """
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        proxy = ProxyAsset(id="px-1", server="http://px:1")
        services.proxy_pool.add(proxy)
        session = _make_idle_session("sess-1", reputation=0.5)
        services.session_pool._idle["proxyless"] = [session]
        await services.ledger.record(_make_record("task-retry"))

        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # First report — downstream calls fire.
                resp1 = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-retry"},
                )
                assert resp1.json()["errorId"] == 0

                # Snapshot the post-first-report state.
                real_success_after_1 = proxy.real_sitekey_stats["sk-1"][0]
                real_fail_after_1 = proxy.real_sitekey_stats["sk-1"][1]
                stats_after_1 = await services.accounting.stats("sk-1")
                real_attempts_after_1 = stats_after_1["real_attempts"]
                reputation_after_1 = session.reputation
                # Sanity: the first report actually did something.
                assert real_success_after_1 == 1
                assert real_attempts_after_1 == 1
                assert abs(reputation_after_1 - 0.55) < 1e-9

                # Second report — idempotent short-circuit, no downstream calls.
                resp2 = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-retry"},
                )
                assert resp2.json()["errorId"] == 0

                # No double-count: real-sitekey counter unchanged.
                assert proxy.real_sitekey_stats["sk-1"][0] == real_success_after_1
                assert proxy.real_sitekey_stats["sk-1"][1] == real_fail_after_1
                # No double-count: accounting real_attempts unchanged.
                stats_after_2 = await services.accounting.stats("sk-1")
                assert stats_after_2["real_attempts"] == real_attempts_after_1
                # No double-count: session reputation nudged only once.
                assert session.reputation == reputation_after_1

                # The ledger record is now flagged reported=True.
                rec = await services.ledger.get_by_task_id("task-retry")
                assert rec is not None
                assert rec.reported is True
        finally:
            set_services(None)

    asyncio.run(run())


def test_report_incorrect_then_correct_is_not_idempotent() -> None:
    """A reportCorrect following a reportIncorrect for the same task is NOT
    a retry — they carry different outcomes. The idempotency guard keys only
    on ``reported``, so the second (different-outcome) report is also
    short-circuited. This documents the v1 contract: one report per task,
    first outcome wins. A future iteration could key on (task_id, outcome)
    if clients are allowed to correct a wrong initial report.
    """
    async def run() -> None:
        app = _build_app()
        services = _make_services()
        proxy = ProxyAsset(id="px-1", server="http://px:1")
        services.proxy_pool.add(proxy)
        session = _make_idle_session("sess-1", reputation=0.5)
        services.session_pool._idle["proxyless"] = [session]
        await services.ledger.record(_make_record("task-flip"))

        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # First report: incorrect (success=False).
                resp1 = await client.post(
                    "/reportIncorrect",
                    json={"clientKey": "client-1", "taskId": "task-flip"},
                )
                assert resp1.json()["errorId"] == 0
                assert proxy.real_sitekey_stats["sk-1"] == [0, 1]

                # Second report: correct — idempotent guard fires (first
                # outcome wins), so the real bucket stays at [0, 1] and not
                # [1, 1].
                resp2 = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-flip"},
                )
                assert resp2.json()["errorId"] == 0
                assert proxy.real_sitekey_stats["sk-1"] == [0, 1]
        finally:
            set_services(None)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Endpoint: atomic claim under concurrency (security finding 2)
# --------------------------------------------------------------------------- #


class _FakeProxyPool:
    """Counts ``report_sitekey_real`` calls so the concurrency test can
    assert exactly one set of side effects ran across N concurrent reports."""

    def __init__(self) -> None:
        self.report_sitekey_real_calls = 0

    async def report_sitekey_real(
        self, proxy_id: str, sitekey: str, success: bool  # noqa: ARG002
    ) -> None:
        self.report_sitekey_real_calls += 1


class _FakeAccounting:
    """Counts ``record_real_outcome`` calls for the concurrency test."""

    def __init__(self) -> None:
        self.record_real_outcome_calls = 0

    async def record_real_outcome(
        self,
        sitekey: str,
        success: bool,
        proxy_kind: str | None = None,
        model: str | None = None,
    ) -> None:  # noqa: ARG002
        self.record_real_outcome_calls += 1


class _FakeSessionPool:
    """Counts ``report_outcome`` calls for the concurrency test."""

    def __init__(self) -> None:
        self.report_outcome_calls = 0

    async def report_outcome(
        self, session_id: str, success: bool  # noqa: ARG002
    ) -> None:
        self.report_outcome_calls += 1


def test_report_concurrent_only_one_runs_side_effects() -> None:
    """Security: two concurrent /reportCorrect for the same taskId — exactly
    one wins the atomic claim and runs the downstream side effects, the
    other short-circuits with errorId=0 and NO side effects. Asserted via
    call counters on fake proxy / accounting / session pools.
    """
    async def run() -> None:
        app = _build_app()
        ledger = CostLedger()
        await ledger.record(
            _make_record("task-concurrent", client_key="client-1")
        )
        proxy_pool = _FakeProxyPool()
        accounting = _FakeAccounting()
        session_pool = _FakeSessionPool()
        services = SimpleNamespace(
            ledger=ledger,
            accounting=accounting,
            proxy_pool=proxy_pool,
            session_pool=session_pool,
        )
        set_services(services)  # type: ignore[arg-type]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Two concurrent reports for the same task — gather them so
                # they race against each other on the claim.
                resp1, resp2 = await asyncio.gather(
                    client.post(
                        "/reportCorrect",
                        json={"clientKey": "client-1", "taskId": "task-concurrent"},
                    ),
                    client.post(
                        "/reportCorrect",
                        json={"clientKey": "client-1", "taskId": "task-concurrent"},
                    ),
                )
                # Both return errorId=0 (the loser is idempotent, not an error).
                assert resp1.json()["errorId"] == 0
                assert resp2.json()["errorId"] == 0

                # Exactly one set of side effects ran.
                assert proxy_pool.report_sitekey_real_calls == 1
                assert accounting.record_real_outcome_calls == 1
                assert session_pool.report_outcome_calls == 1

                # A sequential third report also short-circuits — no extra
                # side effects.
                resp3 = await client.post(
                    "/reportCorrect",
                    json={"clientKey": "client-1", "taskId": "task-concurrent"},
                )
                assert resp3.json()["errorId"] == 0
                assert proxy_pool.report_sitekey_real_calls == 1
                assert accounting.record_real_outcome_calls == 1
                assert session_pool.report_outcome_calls == 1
        finally:
            set_services(None)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# CostLedger.get_by_task_id
# --------------------------------------------------------------------------- #


def test_ledger_get_by_task_id_returns_latest() -> None:
    async def run() -> None:
        ledger = CostLedger()
        # Three records sharing task_id="t1" with explicit created_at.
        rec1 = SolveRecord(
            task_id="t1", sitekey="sk", task_type="X", outcome="failed",
            created_at=100.0,
        )
        rec2 = SolveRecord(
            task_id="t1", sitekey="sk", task_type="X", outcome="ready",
            created_at=200.0,
        )
        rec3 = SolveRecord(
            task_id="t1", sitekey="sk", task_type="X", outcome="timeout",
            created_at=150.0,
        )
        await ledger.record(rec1)
        await ledger.record(rec2)
        await ledger.record(rec3)

        found = await ledger.get_by_task_id("t1")
        assert found is not None
        # Latest by created_at (200.0) is rec2 (outcome="ready").
        assert found.outcome == "ready"
        assert found.created_at == 200.0

    asyncio.run(run())


def test_ledger_get_by_task_id_missing_returns_none() -> None:
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(
            SolveRecord(task_id="t1", sitekey="sk", task_type="X")
        )
        assert await ledger.get_by_task_id("nonexistent") is None

    asyncio.run(run())


def test_ledger_get_by_task_id_empty_ledger_returns_none() -> None:
    async def run() -> None:
        ledger = CostLedger()
        assert await ledger.get_by_task_id("anything") is None

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# CostLedger.try_claim_reported (atomic idempotency claim for /reportCorrect
# // /reportIncorrect)
# --------------------------------------------------------------------------- #


def test_ledger_try_claim_reported_first_call_wins_second_loses() -> None:
    """try_claim_reported atomically flips reported False→True under the
    lock: the first call for a task_id returns True, a second call (the
    retry / concurrent racer) returns False. A subsequent get_by_task_id
    reflects the flipped flag."""
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(
            SolveRecord(
                task_id="t-claim", sitekey="sk", task_type="X", outcome="ready",
                created_at=100.0,
            )
        )
        # Fresh record defaults to reported=False.
        rec = await ledger.get_by_task_id("t-claim")
        assert rec is not None
        assert rec.reported is False

        # First claim wins — flips reported False→True atomically.
        ok = await ledger.try_claim_reported("t-claim")
        assert ok is True

        rec = await ledger.get_by_task_id("t-claim")
        assert rec is not None
        assert rec.reported is True

        # Second claim loses — already reported.
        ok2 = await ledger.try_claim_reported("t-claim")
        assert ok2 is False

    asyncio.run(run())


def test_ledger_try_claim_reported_targets_latest_record_for_task() -> None:
    """When multiple records share a task id, try_claim_reported claims only
    the most recent (same latest-by-created_at logic as get_by_task_id)."""
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(
            SolveRecord(
                task_id="t-multi", sitekey="sk", task_type="X", outcome="failed",
                created_at=100.0,
            )
        )
        await ledger.record(
            SolveRecord(
                task_id="t-multi", sitekey="sk", task_type="X", outcome="ready",
                created_at=200.0,
            )
        )
        ok = await ledger.try_claim_reported("t-multi")
        assert ok is True
        records = await ledger.records()
        # Only the latest (created_at=200.0) is flagged.
        flagged = [r for r in records if r.task_id == "t-multi" and r.reported]
        assert len(flagged) == 1
        assert flagged[0].created_at == 200.0

    asyncio.run(run())


def test_ledger_try_claim_reported_missing_task_returns_false() -> None:
    async def run() -> None:
        ledger = CostLedger()
        assert await ledger.try_claim_reported("nonexistent") is False

    asyncio.run(run())


def test_ledger_try_claim_reported_concurrent_only_one_wins() -> None:
    """N concurrent try_claim_reported calls for the same task_id: exactly
    one returns True (the claim winner), the rest return False. The atomic
    in-memory lock serialises the callers so the flag flip is race-free."""
    async def run() -> None:
        ledger = CostLedger()
        await ledger.record(
            SolveRecord(
                task_id="t-race", sitekey="sk", task_type="X", outcome="ready",
                created_at=100.0,
            )
        )
        n = 32
        results = await asyncio.gather(
            *[ledger.try_claim_reported("t-race") for _ in range(n)]
        )
        assert sum(1 for r in results if r is True) == 1
        assert sum(1 for r in results if r is False) == n - 1

    asyncio.run(run())


def test_ledger_reported_default_false_on_old_serialized_record() -> None:
    """Backward compat: a record dict missing the 'reported' key deserializes
    to reported=False (old serialized records pre-date the field)."""
    from src.consumption.ledger import _deserialize_record

    rec = _deserialize_record(
        {"task_id": "t-old", "sitekey": "sk", "task_type": "X"}
    )
    assert rec.reported is False


# --------------------------------------------------------------------------- #
# SessionPool.report_outcome
# --------------------------------------------------------------------------- #


def test_session_report_outcome_unknown_id_returns_false() -> None:
    async def run() -> None:
        pool = SessionPool(_fake_factory(), size=2, max_solves=8)
        assert await pool.report_outcome("nonexistent", success=True) is False
        assert await pool.report_outcome("nonexistent", success=False) is False

    asyncio.run(run())


def test_session_report_outcome_idle_session_success_nudges_reputation() -> None:
    async def run() -> None:
        pool = SessionPool(_fake_factory(), size=2, max_solves=8)
        session = _make_idle_session("sess-1", reputation=0.5)
        pool._idle["proxyless"] = [session]

        result = await pool.report_outcome("sess-1", success=True)
        assert result is True
        assert abs(session.reputation - 0.55) < 1e-9  # 0.5 + 0.05

    asyncio.run(run())


def test_session_report_outcome_idle_session_failure_drops_reputation() -> None:
    async def run() -> None:
        pool = SessionPool(_fake_factory(), size=2, max_solves=8)
        session = _make_idle_session("sess-2", reputation=0.8)
        pool._idle["proxyless"] = [session]

        result = await pool.report_outcome("sess-2", success=False)
        assert result is True
        assert abs(session.reputation - 0.4) < 1e-9  # 0.8 - 0.4

    asyncio.run(run())


def test_session_report_outcome_clamps_to_zero() -> None:
    async def run() -> None:
        pool = SessionPool(_fake_factory(), size=2, max_solves=8)
        session = _make_idle_session("sess-3", reputation=0.1)
        pool._idle["proxyless"] = [session]

        await pool.report_outcome("sess-3", success=False)
        assert session.reputation == 0.0  # 0.1 - 0.4 clamped to 0.0

    asyncio.run(run())


def test_session_report_outcome_clamps_to_one() -> None:
    async def run() -> None:
        pool = SessionPool(_fake_factory(), size=2, max_solves=8)
        session = _make_idle_session("sess-4", reputation=1.0)
        pool._idle["proxyless"] = [session]

        await pool.report_outcome("sess-4", success=True)
        assert session.reputation == 1.0  # 1.0 + 0.05 clamped to 1.0

    asyncio.run(run())


def test_session_report_outcome_finds_in_use_session() -> None:
    async def run() -> None:
        pool = SessionPool(_fake_factory(), size=2, max_solves=8)
        session = _make_idle_session("sess-5", reputation=0.7)
        # Simulate an in-use session (checked out, not yet released).
        pool._in_use[session.id] = session

        result = await pool.report_outcome("sess-5", success=False)
        assert result is True
        assert abs(session.reputation - 0.3) < 1e-9  # 0.7 - 0.4

    asyncio.run(run())


def test_session_report_outcome_does_not_retire_session() -> None:
    """report_outcome adjusts reputation but does not close/retire the session."""
    async def run() -> None:
        pool = SessionPool(_fake_factory(), size=2, max_solves=8)
        session = _make_idle_session("sess-6", reputation=0.3)
        pool._idle["proxyless"] = [session]

        # A failure would normally retire via release(), but report_outcome
        # only nudges reputation — the session stays in the idle pool.
        await pool.report_outcome("sess-6", success=False)
        assert session.reputation < 0.3  # reputation dropped below threshold
        # But the session is still in the idle pool (not retired).
        assert session in pool._idle["proxyless"]
        assert session.context is not None  # not closed

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# SuccessAccounting real-outcome tracking
# --------------------------------------------------------------------------- #


def test_accounting_real_outcome_optimistic_default() -> None:
    async def run() -> None:
        acc = SuccessAccounting()
        assert await acc.real_success_rate("never-seen") == 1.0
        stats = await acc.stats("never-seen")
        assert stats["real_attempts"] == 0
        assert stats["real_successes"] == 0
        assert stats["real_rate"] == 1.0

    asyncio.run(run())


def test_accounting_record_real_outcome_and_rate() -> None:
    async def run() -> None:
        acc = SuccessAccounting()
        await acc.record_real_outcome(
            "sk", success=True, proxy_kind="residential", model="gpt-4o"
        )
        await acc.record_real_outcome(
            "sk", success=False, proxy_kind="residential", model="gpt-4o"
        )
        await acc.record_real_outcome(
            "sk", success=True, proxy_kind="residential", model="gpt-4o"
        )

        rate = await acc.real_success_rate(
            "sk", proxy_kind="residential", model="gpt-4o"
        )
        assert abs(rate - 2 / 3) < 1e-9

        stats = await acc.stats("sk")
        assert stats["real_attempts"] == 3
        assert stats["real_successes"] == 2
        assert abs(stats["real_rate"] - 2 / 3) < 1e-9

    asyncio.run(run())


def test_accounting_real_outcome_dimensions_independent() -> None:
    async def run() -> None:
        acc = SuccessAccounting()
        await acc.record_real_outcome(
            "sk", success=False, proxy_kind="residential", model="gpt-4o"
        )
        await acc.record_real_outcome(
            "sk", success=True, proxy_kind="datacenter", model="gpt-4o"
        )

        rate_res = await acc.real_success_rate(
            "sk", proxy_kind="residential", model="gpt-4o"
        )
        rate_dc = await acc.real_success_rate(
            "sk", proxy_kind="datacenter", model="gpt-4o"
        )
        assert rate_res == 0.0
        assert rate_dc == 1.0
        # Bare-sitekey bucket has no real data -> optimistic
        assert await acc.real_success_rate("sk") == 1.0
        # But stats() aggregates across all real buckets for the sitekey.
        stats = await acc.stats("sk")
        assert stats["real_attempts"] == 2
        assert stats["real_successes"] == 1

    asyncio.run(run())


def test_accounting_real_outcome_independent_from_token_outcomes() -> None:
    """Token-obtained and real-outcome buckets are tracked separately."""
    async def run() -> None:
        acc = SuccessAccounting()
        # Token obtained: 1 success.
        await acc.record("sk", "ready")
        # Real outcome: 1 failure (token was rejected downstream).
        await acc.record_real_outcome("sk", success=False)

        assert await acc.success_rate("sk") == 1.0
        assert await acc.real_success_rate("sk") == 0.0

        stats = await acc.stats("sk")
        assert stats["attempts"] == 1
        assert stats["successes"] == 1
        assert stats["rate"] == 1.0
        assert stats["real_attempts"] == 1
        assert stats["real_successes"] == 0
        assert stats["real_rate"] == 0.0

    asyncio.run(run())


def test_accounting_stats_includes_real_fields() -> None:
    """stats() returns real_attempts / real_successes / real_rate alongside
    the existing token-obtained fields."""
    async def run() -> None:
        acc = SuccessAccounting()
        await acc.record("sk", "ready")
        await acc.record("sk", "failed")
        await acc.record_real_outcome("sk", success=True)
        await acc.record_real_outcome("sk", success=True)
        await acc.record_real_outcome("sk", success=False)

        stats = await acc.stats("sk")
        # Token-obtained fields (unchanged behavior).
        assert stats["attempts"] == 2
        assert stats["successes"] == 1
        assert stats["rate"] == 0.5
        # Real-outcome fields (new).
        assert stats["real_attempts"] == 3
        assert stats["real_successes"] == 2
        assert abs(stats["real_rate"] - 2 / 3) < 1e-9

    asyncio.run(run())


def test_accounting_real_outcome_respects_window() -> None:
    async def run() -> None:
        acc = SuccessAccounting(window=4)
        for _ in range(4):
            await acc.record_real_outcome("sk", success=False)
        assert await acc.real_success_rate("sk") == 0.0
        for _ in range(4):
            await acc.record_real_outcome("sk", success=True)
        assert await acc.real_success_rate("sk") == 1.0
        stats = await acc.stats("sk")
        assert stats["real_attempts"] == 4  # bounded by window

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# ReportTaskRequest / ReportTaskResponse models
# --------------------------------------------------------------------------- #


def test_report_task_request_model_fields() -> None:
    req = ReportTaskRequest(clientKey="key", taskId="t1")
    assert req.clientKey == "key"
    assert req.taskId == "t1"


def test_report_task_response_defaults() -> None:
    resp = ReportTaskResponse()
    assert resp.errorId == 0
    assert resp.errorCode is None
    assert resp.errorDescription is None


def test_report_task_response_with_error() -> None:
    resp = ReportTaskResponse(
        errorId=1,
        errorCode="ERROR_NO_SUCH_CAPCHA_ID",
        errorDescription="No solve record for this task",
    )
    assert resp.errorId == 1
    assert resp.errorCode == "ERROR_NO_SUCH_CAPCHA_ID"
