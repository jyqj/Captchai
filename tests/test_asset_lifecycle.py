"""Regression tests for pooled-asset handoff, identity, and shutdown."""

from __future__ import annotations

import asyncio

import pytest

from src.assets.inventory import inventory_proxy_id
from src.assets.model_pool import ModelClient
from src.assets.proxy_pool import proxy_from_params
from src.assets.session_pool import SessionPool
from src.consumption.token_verify import HttpTokenVerifier
from src.core.services import SolverServices


class _Context:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _factory():
    state = {"calls": 0, "contexts": []}

    async def build(fingerprint, proxy):
        state["calls"] += 1
        context = _Context()
        state["contexts"].append(context)
        return context, fingerprint.user_agent

    return build, state


def test_full_session_pool_hands_released_asset_to_waiter() -> None:
    """Regression: a full pool must not wait forever on a live-slot semaphore."""

    async def run() -> None:
        factory, state = _factory()
        pool = SessionPool(factory, size=1, max_solves=8)
        first = await pool.checkout(key="proxyless")

        waiter = asyncio.create_task(pool.checkout(key="proxyless"))
        await asyncio.sleep(0)
        assert not waiter.done()
        assert pool.stats()["waiters"] == 1

        await pool.release(first, success=True)
        second = await asyncio.wait_for(waiter, timeout=0.25)
        assert second.id == first.id
        assert state["calls"] == 1
        await pool.close_all()

    asyncio.run(run())


def test_cancelled_context_build_returns_reserved_capacity() -> None:
    async def run() -> None:
        started = asyncio.Event()
        calls = 0

        async def factory(fingerprint, proxy):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await asyncio.Future()
            return _Context(), fingerprint.user_agent

        pool = SessionPool(factory, size=1, max_solves=8)
        creating = asyncio.create_task(pool.checkout(key="proxyless"))
        await started.wait()
        creating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creating

        assert pool.stats()["live"] == 0
        session = await asyncio.wait_for(
            pool.checkout(key="proxyless"), timeout=0.25
        )
        assert calls == 2
        await pool.release(session, success=True)
        await pool.close_all()

    asyncio.run(run())


def test_close_all_wakes_waiters_and_prevents_asset_resurrection() -> None:
    async def run() -> None:
        factory, state = _factory()
        pool = SessionPool(factory, size=1, max_solves=8)
        await pool.checkout(key="proxyless")
        waiter = asyncio.create_task(pool.checkout(key="proxyless"))
        await asyncio.sleep(0)

        await pool.close_all()
        with pytest.raises(RuntimeError, match="closed"):
            await waiter
        assert all(context.closed for context in state["contexts"])
        assert pool.stats() == {
            "capacity": 1,
            "live": 0,
            "idle": 0,
            "in_use": 0,
            "creating": 0,
            "closing": 0,
            "waiters": 0,
            "closed": True,
        }

    asyncio.run(run())


def test_duplicate_release_is_idempotent() -> None:
    async def run() -> None:
        factory, _ = _factory()
        pool = SessionPool(factory, size=1, max_solves=8)
        session = await pool.checkout(key="proxyless")
        await pool.release(session, success=True)
        await pool.release(session, success=False)
        assert session.solves == 1
        assert pool.stats()["idle"] == 1
        await pool.close_all()

    asyncio.run(run())


def test_retirement_releases_capacity_only_after_context_closes() -> None:
    """A slow browser close must not let the pool transiently exceed its cap."""

    async def run() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        calls = 0

        class SlowContext(_Context):
            async def close(self) -> None:
                close_started.set()
                await allow_close.wait()
                await super().close()

        async def factory(fingerprint, proxy):
            nonlocal calls
            calls += 1
            context = SlowContext() if calls == 1 else _Context()
            return context, fingerprint.user_agent

        pool = SessionPool(factory, size=1, max_solves=1)
        first = await pool.checkout(key="proxyless")
        retiring = asyncio.create_task(pool.release(first, success=True))
        await close_started.wait()

        assert pool.stats()["live"] == 1
        assert pool.stats()["closing"] == 1
        waiter = asyncio.create_task(pool.checkout(key="proxyless"))
        await asyncio.sleep(0)
        assert not waiter.done()
        assert calls == 1

        allow_close.set()
        await retiring
        second = await asyncio.wait_for(waiter, timeout=0.25)
        assert calls == 2
        await pool.close_all()
        assert second.context.closed

    asyncio.run(run())


def test_cancelled_retirement_finishes_cleanup_and_restores_capacity() -> None:
    """Cancellation cannot strand a closing context or its capacity slot."""

    async def run() -> None:
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        calls = 0

        class SlowContext(_Context):
            async def close(self) -> None:
                close_started.set()
                await allow_close.wait()
                await super().close()

        async def factory(fingerprint, proxy):
            nonlocal calls
            calls += 1
            context = SlowContext() if calls == 1 else _Context()
            return context, fingerprint.user_agent

        pool = SessionPool(factory, size=1, max_solves=1)
        first = await pool.checkout(key="proxyless")
        retiring = asyncio.create_task(pool.release(first, success=True))
        await close_started.wait()
        retiring.cancel()
        await asyncio.sleep(0)
        assert not retiring.done()
        assert pool.stats()["live"] == 1
        assert pool.stats()["closing"] == 1

        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await retiring
        assert first.context.closed
        assert pool.stats()["live"] == 0
        assert pool.stats()["closing"] == 0

        replacement = await asyncio.wait_for(
            pool.checkout(key="proxyless"), timeout=0.25
        )
        await pool.close_all()
        assert replacement.context.closed

    asyncio.run(run())


def test_configured_proxy_identity_is_stable_and_secret_safe() -> None:
    first = proxy_from_params(
        {"proxy": "http://user:pass@gateway.example:8080|kind=residential"}
    )
    second = proxy_from_params(
        {"proxy": "http://user:pass@gateway.example:8080|kind=residential"}
    )
    changed = proxy_from_params(
        {"proxy": "http://user:other@gateway.example:8080|kind=residential"}
    )
    assert first is not None and second is not None and changed is not None
    assert first.id != second.id  # task-proxy parser remains ephemeral
    assert inventory_proxy_id(first) == inventory_proxy_id(second)
    assert inventory_proxy_id(first) != inventory_proxy_id(changed)
    assert "user" not in inventory_proxy_id(first)
    assert "pass" not in inventory_proxy_id(first)


def test_proxy_inventory_reconcile_preserves_existing_state(monkeypatch) -> None:
    raw = (
        "http://u:p@gateway.example:8080|kind=residential,"
        "http://u:p@gateway.example:8080|kind=residential,"
        "http://u2:p2@gateway.example:8080|kind=mobile"
    )
    monkeypatch.setenv("PROXY_POOL", raw)

    existing = proxy_from_params(
        {"proxy": "http://u:p@gateway.example:8080|kind=residential"}
    )
    assert existing is not None
    existing_id = inventory_proxy_id(existing)

    class Pool:
        def __init__(self) -> None:
            self.added = []

        def snapshot(self):
            return [{"id": existing_id, "success_count": 17}]

        def add(self, asset):
            self.added.append(asset)

    services = SolverServices.__new__(SolverServices)
    services.proxy_pool = Pool()
    services._load_proxy_inventory()

    assert len(services.proxy_pool.added) == 1
    assert services.proxy_pool.added[0].kind == "mobile"
    assert services.proxy_pool.added[0].id != existing_id


def test_model_client_close_releases_underlying_http_pool() -> None:
    class RawClient:
        def __init__(self) -> None:
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    raw = RawClient()
    client = ModelClient(
        name="local",
        model="m",
        base_url="http://model/v1",
        api_key="k",
        client_factory=lambda _url, _key: raw,
    )
    assert client._raw() is raw
    asyncio.run(client.close())
    asyncio.run(client.close())
    assert raw.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        client._raw()


def test_token_verifier_reuses_and_closes_shared_http_client() -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"success": True}

    class Client:
        def __init__(self) -> None:
            self.posts = 0
            self.closed = 0

        async def post(self, endpoint, data):
            self.posts += 1
            return Response()

        async def aclose(self) -> None:
            self.closed += 1

    async def run() -> None:
        raw = Client()
        factories = 0

        def factory(timeout):
            nonlocal factories
            factories += 1
            return raw

        verifier = HttpTokenVerifier(
            {"sk": "secret"},
            endpoints={"test": "https://verify.example"},
            client_factory=factory,
        )
        assert await verifier.verify("a", provider="test", sitekey="sk") is True
        assert await verifier.verify("b", provider="test", sitekey="sk") is True
        assert factories == 1
        assert raw.posts == 2
        await verifier.close()
        await verifier.close()
        assert raw.closed == 1

    asyncio.run(run())


def test_services_close_drains_all_resources_after_one_failure() -> None:
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def close(self) -> None:
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("boom")

    services = SolverServices.__new__(SolverServices)
    services.session_pool = Resource("session")
    services.token_verifier = Resource("verifier", fail=True)
    services.model_pool = Resource("models")
    services.accounting = Resource("accounting")
    services.proxy_pool = Resource("proxies")
    services.ledger = Resource("ledger")

    asyncio.run(services.close())
    assert closed == [
        "session",
        "verifier",
        "models",
        "accounting",
        "proxies",
        "ledger",
    ]
