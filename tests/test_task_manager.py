"""Unit tests for the runtime plane: admission, idempotency, categories, cancel."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.services.task_manager as tm  # noqa: E402

# NOTE: reference classes via the live module (``tm.X``) rather than binding them
# at import time. ``test_api.py`` reloads this module, which would otherwise leave
# our imported ``tm.QueueFull`` pointing at a stale class that ``except`` won't catch.


def _cfg(**over):
    base = dict(
        browser_concurrency=1,
        vision_concurrency=2,
        queue_max_size=1,
        solve_timeout=5,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_backpressure_rejects_when_full() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(_cfg(queue_max_size=1, browser_concurrency=1))

        release = asyncio.Event()

        class Slow:
            async def solve(self, params):
                await release.wait()
                return {"token": "x"}

        mgr.register_solver("T", Slow(), tm.TaskCategory.BROWSER)
        first = mgr.create_task("T", {})
        assert first
        # Second admission should be rejected while the first is in-flight.
        try:
            mgr.create_task("T", {})
            assert False, "expected tm.QueueFull"
        except tm.QueueFull:
            pass
        release.set()
        # let the first finish
        await asyncio.sleep(0.05)
        assert mgr.get_task(first).status == tm.TaskStatus.READY

    asyncio.run(run())


def test_idempotency_coalesces() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(_cfg(queue_max_size=10))
        release = asyncio.Event()

        class Slow:
            async def solve(self, params):
                await release.wait()
                return {"token": "x"}

        mgr.register_solver("T", Slow(), tm.TaskCategory.BROWSER)
        a = mgr.create_task("T", {}, idempotency_key="k1")
        b = mgr.create_task("T", {}, idempotency_key="k1")
        assert a == b  # same key -> same task, no double spawn
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_split_pools_vision_not_blocked_by_browser() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(_cfg(browser_concurrency=1, vision_concurrency=2, queue_max_size=10))
        release_browser = asyncio.Event()

        class Browser:
            async def solve(self, params):
                await release_browser.wait()
                return {"token": "b"}

        class Vision:
            async def solve(self, params):
                return {"text": "v"}

        mgr.register_solver("B", Browser(), tm.TaskCategory.BROWSER)
        mgr.register_solver("V", Vision(), tm.TaskCategory.VISION)

        mgr.create_task("B", {})  # occupies the single browser slot
        v = mgr.create_task("V", {})  # must still run on the vision pool
        await asyncio.sleep(0.05)
        assert mgr.get_task(v).status == tm.TaskStatus.READY
        release_browser.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_task_proxy_not_blocked_by_proxyless_browser_pool() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(
            _cfg(
                browser_concurrency=1,
                browser_proxyless_concurrency=1,
                browser_proxied_concurrency=1,
                queue_max_size=10,
            )
        )
        release_proxyless = asyncio.Event()

        class Browser:
            async def solve(self, params):
                if params.get("_proxyKind") == "proxyless":
                    await release_proxyless.wait()
                    return {"token": "proxyless"}
                return {"token": params.get("_proxyKind")}

        mgr.register_solver("B", Browser(), tm.TaskCategory.BROWSER)
        mgr.create_task("B", {})  # occupies the proxyless browser slot
        proxied = mgr.create_task(
            "B",
            {"proxyType": "http", "proxyAddress": "1.2.3.4", "proxyPort": 8080},
        )
        await asyncio.sleep(0.05)
        task = mgr.get_task(proxied)
        assert task.status == tm.TaskStatus.READY
        assert task.solution == {"token": "task_proxy"}
        release_proxyless.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_timeout_marks_failed() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(_cfg(solve_timeout=0, queue_max_size=10))

        class Hang:
            async def solve(self, params):
                await asyncio.sleep(1)
                return {"token": "x"}

        mgr.register_solver("T", Hang(), tm.TaskCategory.BROWSER)
        t = mgr.create_task("T", {})
        await asyncio.sleep(0.1)
        task = mgr.get_task(t)
        assert task.status == tm.TaskStatus.FAILED
        assert task.error_code == "ERROR_CAPTCHA_TIMEOUT"

    asyncio.run(run())


def test_task_id_injected_into_params() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(_cfg(queue_max_size=10))
        seen = {}

        class Cap:
            async def solve(self, params):
                seen.update(params)
                return {"token": "x"}

        mgr.register_solver("T", Cap(), tm.TaskCategory.BROWSER)
        t = mgr.create_task("T", {})
        await asyncio.sleep(0.05)
        assert seen.get("_taskId") == t

    asyncio.run(run())


def test_pool_proxy_semaphore_separate() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(
            _cfg(
                browser_concurrency=1,
                browser_proxyless_concurrency=1,
                browser_proxied_concurrency=1,
                browser_pool_proxy_concurrency=1,
                queue_max_size=10,
            )
        )
        release_pool = asyncio.Event()

        class Browser:
            async def solve(self, params):
                if params.get("egress") == "pool":
                    await release_pool.wait()
                return {"token": "x"}

        mgr.register_solver("B", Browser(), tm.TaskCategory.BROWSER)
        # Occupies the pool_proxy slot.
        pool_id = mgr.create_task("B", {"egress": "pool"})
        # A proxyless task must still complete — it draws from a separate
        # semaphore, so the pool_proxy task can't block it.
        proxyless_id = mgr.create_task("B", {"egress": "proxyless"})
        await asyncio.sleep(0.05)
        assert mgr.get_task(pool_id).status == tm.TaskStatus.PROCESSING
        assert mgr.get_task(proxyless_id).status == tm.TaskStatus.READY
        release_pool.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_egress_task_uses_proxied_sem() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(
            _cfg(
                browser_concurrency=1,
                browser_proxyless_concurrency=1,
                browser_proxied_concurrency=1,
                browser_pool_proxy_concurrency=1,
                queue_max_size=10,
            )
        )
        release_proxyless = asyncio.Event()

        class Browser:
            async def solve(self, params):
                if params.get("egress") == "proxyless":
                    await release_proxyless.wait()
                return {"token": "x"}

        mgr.register_solver("B", Browser(), tm.TaskCategory.BROWSER)
        # Fill the proxyless slot.
        proxyless_id = mgr.create_task("B", {"egress": "proxyless"})
        # egress="task" with no caller-supplied proxy must NOT use the proxyless
        # semaphore — it should land on the proxied sem and run concurrently.
        task_id = mgr.create_task("B", {"egress": "task"})
        await asyncio.sleep(0.05)
        assert mgr.get_task(proxyless_id).status == tm.TaskStatus.PROCESSING
        assert mgr.get_task(task_id).status == tm.TaskStatus.READY
        release_proxyless.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_egress_auto_preserves_legacy_behavior() -> None:
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(
            _cfg(
                browser_concurrency=1,
                browser_proxyless_concurrency=1,
                browser_proxied_concurrency=1,
                browser_pool_proxy_concurrency=1,
                queue_max_size=10,
            )
        )
        release_proxied = asyncio.Event()

        class Browser:
            async def solve(self, params):
                if params.get("_proxyKind") == "task_proxy":
                    await release_proxied.wait()
                return {"token": params.get("_proxyKind")}

        mgr.register_solver("B", Browser(), tm.TaskCategory.BROWSER)
        # No egress field + caller proxy → proxied sem (legacy behaviour).
        proxied_id = mgr.create_task(
            "B",
            {"proxyType": "http", "proxyAddress": "1.2.3.4", "proxyPort": 8080},
        )
        # No egress, no proxy → proxyless sem; should complete while proxied held.
        proxyless_id = mgr.create_task("B", {})
        await asyncio.sleep(0.05)
        assert mgr.get_task(proxied_id).status == tm.TaskStatus.PROCESSING
        assert mgr.get_task(proxyless_id).status == tm.TaskStatus.READY
        release_proxied.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_auto_peeks_proxy_pool_for_routing() -> None:
    """auto + no task proxy: peek the pool → pool_proxy_sem when proxies exist."""
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(
            _cfg(
                browser_concurrency=1,
                browser_proxyless_concurrency=1,
                browser_proxied_concurrency=1,
                browser_pool_proxy_concurrency=1,
                queue_max_size=10,
            ),
            proxy_pool=SimpleNamespace(has_available=lambda: True),
        )
        release_pool = asyncio.Event()

        class Browser:
            async def solve(self, params):
                # The auto-routed task lands on the pool_proxy sem; hold it.
                if params.get("egress") is None and not params.get("proxyType"):
                    await release_pool.wait()
                return {"token": "x"}

        mgr.register_solver("B", Browser(), tm.TaskCategory.BROWSER)
        # auto + no proxy → peeked pool available → pool_proxy sem.
        pool_id = mgr.create_task("B", {})
        # A proxyless task draws from a separate sem → completes immediately.
        proxyless_id = mgr.create_task("B", {"egress": "proxyless"})
        await asyncio.sleep(0.05)
        assert mgr.get_task(pool_id).status == tm.TaskStatus.PROCESSING
        assert mgr.get_task(proxyless_id).status == tm.TaskStatus.READY
        release_pool.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_auto_falls_to_proxyless_when_pool_empty() -> None:
    """auto + no task proxy + empty pool → proxyless sem (no pool_proxy contention)."""
    async def run() -> None:
        mgr = tm.TaskManager()
        mgr.configure(
            _cfg(
                browser_concurrency=1,
                browser_proxyless_concurrency=1,
                browser_proxied_concurrency=1,
                browser_pool_proxy_concurrency=1,
                queue_max_size=10,
            ),
            proxy_pool=SimpleNamespace(has_available=lambda: False),
        )
        release_proxyless = asyncio.Event()

        class Browser:
            async def solve(self, params):
                if params.get("egress") is None and not params.get("proxyType"):
                    await release_proxyless.wait()
                return {"token": "x"}

        mgr.register_solver("B", Browser(), tm.TaskCategory.BROWSER)
        # auto + no proxy + empty pool → proxyless sem.
        proxyless_id = mgr.create_task("B", {})
        # An explicit pool task draws from pool_proxy sem → completes immediately.
        pool_id = mgr.create_task("B", {"egress": "pool"})
        await asyncio.sleep(0.05)
        assert mgr.get_task(proxyless_id).status == tm.TaskStatus.PROCESSING
        assert mgr.get_task(pool_id).status == tm.TaskStatus.READY
        release_proxyless.set()
        await asyncio.sleep(0.05)

    asyncio.run(run())


def test_redis_store_wired_when_redis_url_set() -> None:
    import pytest

    pytest.importorskip("redis.asyncio")
    from src.orchestration.store import RedisTaskStore

    async def run() -> None:
        cfg = _cfg(redis_url="redis://localhost:6379/0")
        mgr = tm.TaskManager()
        mgr.configure(cfg)
        # configure() must build a RedisTaskStore when redis_url is set, and
        # construction must not require a live Redis server (the client connects
        # lazily on first command).
        assert isinstance(mgr._store, RedisTaskStore)
        await mgr._store.close()

    asyncio.run(run())
