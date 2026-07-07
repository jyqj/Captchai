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
