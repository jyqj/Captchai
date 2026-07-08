"""Tests for shared browser-solver proxy categorisation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.assets.proxy_pool import ProxyAsset, ProxyPool  # noqa: E402
from src.services.browser_solver import BaseBrowserSolver, ProxyKind  # noqa: E402


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeManager:
    def __init__(self) -> None:
        self.calls = []
        self.contexts = []

    async def new_context(self, params):
        self.calls.append(dict(params))
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx, params.get("userAgent") or "UA"


def _config():
    return SimpleNamespace(
        human_mouse_enabled=False,
        human_mouse_jitter_ms=0,
    )


def test_task_proxy_is_explicit_category() -> None:
    async def run() -> None:
        manager = FakeManager()
        solver = BaseBrowserSolver(_config(), manager=manager, services=None)
        params = {
            "websiteKey": "site",
            "proxyType": "http",
            "proxyAddress": "1.2.3.4",
            "proxyPort": 8080,
        }

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.TASK_PROXY
        assert params["_proxyKind"] == "task_proxy"
        assert manager.calls[0]["proxyAddress"] == "1.2.3.4"
        await solver._release_context(ctx, True, params)
        assert ctx.context.closed is True

    asyncio.run(run())


def test_pool_proxy_is_final_category_when_inventory_selected() -> None:
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.POOL_PROXY
        assert ctx.proxy_id == "pool-1"
        assert params["_proxyKind"] == "pool_proxy"
        assert manager.calls[0]["_proxy_override"] == {"server": "http://pool:8080"}
        await solver._release_context(ctx, True, params)
        assert pool.snapshot()[0]["success_count"] == 1

    asyncio.run(run())


def test_no_proxy_and_no_pool_stays_proxyless() -> None:
    async def run() -> None:
        manager = FakeManager()
        services = SimpleNamespace(session_pool=None)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.PROXYLESS
        assert params["_proxyKind"] == "proxyless"
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_egress_task_without_proxy_raises() -> None:
    async def run() -> None:
        manager = FakeManager()
        solver = BaseBrowserSolver(_config(), manager=manager, services=None)
        params = {"websiteKey": "site", "egress": "task"}

        try:
            await solver._acquire_context(params)
        except RuntimeError as exc:
            assert "egress=task" in str(exc)
        else:
            raise AssertionError("egress=task without proxy should raise RuntimeError")

    asyncio.run(run())


def test_egress_pool_with_empty_pool_raises() -> None:
    async def run() -> None:
        manager = FakeManager()
        pool = ProxyPool()  # empty: checkout returns None
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site", "egress": "pool"}

        try:
            await solver._acquire_context(params)
        except RuntimeError as exc:
            assert "egress=pool" in str(exc)
        else:
            raise AssertionError("egress=pool with empty pool should raise RuntimeError")

    asyncio.run(run())


def test_egress_proxyless_skips_task_proxy() -> None:
    async def run() -> None:
        manager = FakeManager()
        services = SimpleNamespace(session_pool=None)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {
            "websiteKey": "site",
            "egress": "proxyless",
            "proxyType": "http",
            "proxyAddress": "1.2.3.4",
            "proxyPort": 8080,
        }

        ctx = await solver._acquire_context(params)

        assert ctx.proxy_kind == ProxyKind.PROXYLESS
        assert params["_proxyKind"] == "proxyless"
        await solver._release_context(ctx, True, params)

    asyncio.run(run())


def test_egress_auto_legacy_behavior() -> None:
    async def run() -> None:
        # Subcase 1: no egress, has task proxy → TASK_PROXY.
        manager = FakeManager()
        solver = BaseBrowserSolver(_config(), manager=manager, services=None)
        params = {
            "websiteKey": "site",
            "proxyType": "http",
            "proxyAddress": "1.2.3.4",
            "proxyPort": 8080,
        }

        ctx = await solver._acquire_context(params)
        assert ctx.proxy_kind == ProxyKind.TASK_PROXY
        assert params["_proxyKind"] == "task_proxy"
        await solver._release_context(ctx, True, params)

        # Subcase 2: no egress, no proxy, no services → PROXYLESS (server egress).
        manager2 = FakeManager()
        solver2 = BaseBrowserSolver(_config(), manager=manager2, services=None)
        params2 = {"websiteKey": "site"}

        ctx2 = await solver2._acquire_context(params2)
        assert ctx2.proxy_kind == ProxyKind.PROXYLESS
        assert params2["_proxyKind"] == "proxyless"
        await solver2._release_context(ctx2, True, params2)

    asyncio.run(run())


def test_release_context_stashes_proxy_bytes() -> None:
    """Fresh-context release reads _omc_bytes_used into params for the recorder."""
    async def run() -> None:
        from src.assets.proxy_pool import ProxyPool

        manager = FakeManager()
        pool = ProxyPool()
        pool.add(ProxyAsset(id="pool-1", server="http://pool:8080"))
        services = SimpleNamespace(session_pool=None, proxy_pool=pool)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        ctx = await solver._acquire_context(params)
        # Simulate the response listener accumulating bytes on the context.
        ctx.context._omc_bytes_used = 42_613  # type: ignore[attr-defined]
        await solver._release_context(ctx, solved=True, params=params)

        assert params["_proxy_bytes"] == 42_613
        # Proxy pool report reflects the byte count too.
        snap = pool.snapshot()[0]
        assert snap["bytes_used"] == 42_613

    asyncio.run(run())


def test_release_context_session_skips_proxy_bytes() -> None:
    """Warm-session release must NOT stash proxy_bytes (context outlives the solve)."""
    async def run() -> None:
        from src.assets.session_pool import BrowserSession

        class FakeSession:
            id = "sess-1"

            def __init__(self) -> None:
                self.context = FakeContext()

        manager = FakeManager()

        class FakeSessionPool:
            async def release(self, session, *, success, burned=False) -> None:
                return None

        services = SimpleNamespace(session_pool=FakeSessionPool(), proxy_pool=None)
        solver = BaseBrowserSolver(_config(), manager=manager, services=services)
        params = {"websiteKey": "site"}

        from src.services.browser_solver import SolveContext

        session = FakeSession()
        ctx = SolveContext(
            context=session.context,
            user_agent="UA",
            proxy_kind=ProxyKind.PROXYLESS,
            session=session,
            session_id="sess-1",
        )
        await solver._release_context(ctx, solved=True, params=params)
        # Session release path must not set _proxy_bytes.
        assert "_proxy_bytes" not in params

    asyncio.run(run())
