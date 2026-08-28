"""State-machine regressions for session creation and prewarming."""

from __future__ import annotations

import asyncio

import pytest

from src.assets.session_pool import SessionPool


class _Context:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_cancel_between_context_build_and_pool_publication_reclaims_asset() -> None:
    """A built context cannot become orphaned while waiting for the pool lock."""

    async def run() -> None:
        factory_entered = asyncio.Event()
        allow_return = asyncio.Event()
        contexts: list[_Context] = []

        async def factory(fingerprint, proxy):
            factory_entered.set()
            await allow_return.wait()
            context = _Context()
            contexts.append(context)
            return context, fingerprint.user_agent

        pool = SessionPool(factory, size=1, max_solves=8)
        checkout = asyncio.create_task(pool.checkout(key="proxyless"))
        await factory_entered.wait()

        # Let the factory return while publication is deliberately blocked on
        # the condition lock, then cancel precisely in that ownership gap.
        await pool._condition.acquire()
        try:
            allow_return.set()
            await asyncio.sleep(0)
            checkout.cancel()
        finally:
            pool._condition.release()

        with pytest.raises(asyncio.CancelledError):
            await checkout

        assert contexts and contexts[0].closed
        assert pool.stats()["live"] == 0
        assert pool.stats()["creating"] == 0
        await pool.close_all()

    asyncio.run(run())


def test_prewarm_does_not_consume_session_solve_budget() -> None:
    """Prewarming is asset preparation, not a completed captcha solve."""

    async def run() -> None:
        calls = 0

        async def factory(fingerprint, proxy):
            nonlocal calls
            calls += 1
            return _Context(), fingerprint.user_agent

        pool = SessionPool(factory, size=1, max_solves=1)
        assert await pool.prewarm() == 1
        assert pool.snapshot()[0]["solves"] == 0

        session = await pool.checkout(key="proxyless")
        assert calls == 1  # the prewarmed context was reused
        await pool.release(session, success=True)

        # The first real solve, not prewarm, exhausts max_solves=1.
        assert session.context.closed
        assert pool.stats()["live"] == 0
        await pool.close_all()

    asyncio.run(run())


def test_cancelled_prewarm_reclaims_completed_child_leases() -> None:
    """Partial startup cancellation must not leave successful checkouts in use."""

    async def run() -> None:
        first_ready = asyncio.Event()
        block_second = asyncio.Event()
        calls = 0

        async def factory(fingerprint, proxy):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_ready.set()
                return _Context(), fingerprint.user_agent
            await block_second.wait()
            return _Context(), fingerprint.user_agent

        pool = SessionPool(factory, size=2, max_solves=8)
        prewarm = asyncio.create_task(pool.prewarm())
        await first_ready.wait()
        for _ in range(3):
            await asyncio.sleep(0)

        prewarm.cancel()
        with pytest.raises(asyncio.CancelledError):
            await prewarm

        stats = pool.stats()
        assert stats["creating"] == 0
        assert stats["in_use"] == 0
        assert stats["idle"] == 1
        assert pool.snapshot()[0]["solves"] == 0
        await pool.close_all()

    asyncio.run(run())
